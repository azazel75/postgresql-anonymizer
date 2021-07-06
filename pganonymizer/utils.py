"""Helper methods"""

from __future__ import absolute_import

import csv
import json
import logging
import math
import re
import subprocess


import parmap
import psycopg2
import psycopg2.extras
from psycopg2.errors import BadCopyFileFormat, InvalidTextRepresentation
from six import StringIO
from tqdm import trange

from pganonymizer.constants import COPY_DB_DELIMITER, DEFAULT_CHUNK_SIZE, DEFAULT_PRIMARY_KEY
from pganonymizer.exceptions import BadDataFormat
from pganonymizer.providers import get_provider


def anonymize_tables(connection, definitions, verbose=False):
    """
    Anonymize a list of tables according to the schema definition.

    :param connection: A database connection instance.
    :param list definitions: A list of table definitions from the YAML schema.
    :param bool verbose: Display logging information and a progress bar.
    """
    for definition in definitions:
        table_name = list(definition.keys())[0]
        logging.info('Found table definition "%s"', table_name)
        table_definition = definition[table_name]
        columns = table_definition.get('fields', [])
        excludes = table_definition.get('excludes', [])
        search = table_definition.get('search')
        primary_key = table_definition.get('primary_key', DEFAULT_PRIMARY_KEY)
        total_count = get_table_count(connection, table_name)
        chunk_size = table_definition.get('chunk_size', DEFAULT_CHUNK_SIZE)
        build_and_then_import_data(connection, table_name, primary_key, columns, excludes,
                                   search, total_count, chunk_size, verbose=verbose)


def process_row(row, columns, excludes):
    if row_matches_excludes(row, excludes):
        return None
    else:
        row_column_dict = get_column_values(row, columns)
        for key, value in row_column_dict.items():
            row[key] = value
        return row


def build_and_then_import_data(connection, table, primary_key, columns,
                               excludes, search, total_count, chunk_size, verbose=False):
    """
    Select all data from a table and return it together with a list of table columns.

    :param connection: A database connection instance.
    :param str table: Name of the table to retrieve the data.
    :param str primary_key: Table primary key
    :param list columns: A list of table fields
    :param list[dict] excludes: A list of exclude definitions.
    :param str search: A SQL WHERE (search_condition) to filter and keep only the searched rows.
    :param int total_count: The amount of rows for the current table
    :param int chunk_size: Number of data rows to fetch with the cursor
    :param bool verbose: Display logging information and a progress bar.
    """
    column_names = get_column_names(columns)
    sql_columns = ', '.join(['"{}"'.format(column_name) for column_name in [primary_key] + column_names])
    sql_select = 'SELECT {columns} FROM "{table}"'.format(table=table, columns=sql_columns)
    if search:
        sql = "{select} WHERE {search_condition};".format(select=sql_select, search_condition=search)
    else:
        sql = "{select};".format(select=sql_select)
    cursor = connection.cursor(cursor_factory=psycopg2.extras.DictCursor, name='fetch_large_result')
    cursor.execute(sql)
    temp_table = 'tmp_{table}'.format(table=table)
    create_temporary_table(connection, columns, table, temp_table, primary_key)
    batches = int(math.ceil((1.0 * total_count) / (1.0 * chunk_size)))
    for i in trange(batches, desc="Processing {} batches for {}".format(batches, table), disable=not verbose):
        records = cursor.fetchmany(size=chunk_size)
        if not records:
            break
        data = parmap.map(process_row, records, columns, excludes, pm_pbar=verbose)
        import_data(connection, temp_table, filter(None, data))
    apply_anonymized_data(connection, temp_table, table, primary_key, columns)

    cursor.close()


def apply_anonymized_data(connection, temp_table, source_table, primary_key, definitions):
    logging.info('Applying changes on table {}'.format(source_table))
    cursor = connection.cursor()
    create_index_sql = 'CREATE INDEX ON "{temp_table}" ("{primary_key}")'
    cursor.execute(create_index_sql.format(temp_table=temp_table, primary_key=primary_key))

    column_names = ['"{}"'.format(list(definition.keys())[0]) for definition in definitions]
    set_columns = ', '.join(['{column} = s.{column}'.format(column=column) for column in column_names])
    sql = (
        'UPDATE "{table}" t '
        'SET {columns} '
        'FROM "{source}" s '
        'WHERE t."{primary_key}" = s."{primary_key}";'
    ).format(table=source_table, columns=set_columns, source=temp_table, primary_key=primary_key)
    cursor.execute(sql)
    cursor.close()


def row_matches_excludes(row, excludes=None):
    """
    Check whether a row matches a list of field exclusion patterns.

    :param list row: The data row
    :param list excludes: A list of field exclusion roles, e.g.:

    >>> [
    >>>     {'email': ['\\S.*@example.com', '\\S.*@foobar.com', ]}
    >>> ]

    :return: True or False
    :rtype: bool
    """
    excludes = excludes if excludes else []
    for definition in excludes:
        column = list(definition.keys())[0]
        for exclude in definition.get(column, []):
            pattern = re.compile(exclude, re.IGNORECASE)
            if row[column] is not None and pattern.match(row[column]):
                return True
    return False


def copy_from(connection, data, table):
    """
    Copy the data from a table to a temporary table.

    :param connection: A database connection instance.
    :param list data: The data of a table.
    :param str table: Name of the temporary table used for copying the data.
    :raises BadDataFormat: If the data cannot be imported due to a invalid format.
    """
    new_data = data2csv(data)
    cursor = connection.cursor()
    try:
        cursor.copy_from(new_data, table, sep=COPY_DB_DELIMITER, null='\\N')
    except (BadCopyFileFormat, InvalidTextRepresentation) as exc:
        raise BadDataFormat(exc)
    finally:
        new_data.close()
        cursor.close()


def create_temporary_table(connection, definitions, source_table, temp_table, primary_key):
    primary_key = primary_key if primary_key else DEFAULT_PRIMARY_KEY
    column_names = get_column_names(definitions)
    sql_columns = ', '.join(['"{}"'.format(column_name) for column_name in [primary_key] + column_names])
    ctas_query = """CREATE TEMP TABLE "{temp_table}" AS SELECT {columns}
                    FROM "{source_table}" WITH NO DATA"""
    cursor = connection.cursor()
    cursor.execute(ctas_query.format(temp_table=temp_table, source_table=source_table, columns=sql_columns))
    cursor.close()


def import_data(connection, table_name, data):
    """
    Import the temporary and anonymized data to a temporary table and write the changes back.
    :param connection: A database connection instance.
    :param str table_name: Name of the table to be populated with data.
    :param list data: The table data.
    """

    cursor = connection.cursor()
    copy_from(connection, data, table_name)
    cursor.close()


def get_connection(pg_args):
    """
    Return a connection to the database.

    :param pg_args:
    :return: A psycopg connection instance
    :rtype: psycopg2.connection
    """
    return psycopg2.connect(**pg_args)


def get_table_count(connection, table):
    """
    Return the number of table entries.

    :param connection: A database connection instance
    :param str table: Name of the database table
    :return: The number of table entries
    :rtype: int
    """
    sql = 'SELECT COUNT(*) FROM {table};'.format(table=table)
    cursor = connection.cursor()
    cursor.execute(sql)
    total_count = cursor.fetchone()[0]
    cursor.close()
    return total_count


def data2csv(data):
    """
    Return a string buffer, that contains delimited data.

    :param list data: A list of values
    :return: A stream that contains tab delimited csv data
    :rtype: StringIO
    """
    buf = StringIO()
    writer = csv.writer(buf, delimiter=COPY_DB_DELIMITER, lineterminator='\n', quotechar='~')
    for row in data:
        row_data = []
        for x in row:
            if x is None:
                val = '\\N'
            elif type(x) == str:
                val = escape_str_replace(x.strip())
            elif type(x) == dict:
                val = escape_str_replace(json.dumps(x))
            else:
                val = x
            row_data.append(val)
        writer.writerow(row_data)
    buf.seek(0)
    return buf


def get_column_values(row, columns):
    """
    Return a dictionary for a single data row, with altered data.

    :param psycopg2.extras.DictRow row: A data row from the current table to be altered
    :param list columns: A list of table columns with their provider rules, e.g.:

    >>> [
    >>>     {'guest_email': {'append': '@localhost', 'provider': 'md5'}}
    >>> ]

    :return: A dictionary with all fields that have to be altered and their value for a single data row, e.g.:
        {'guest_email': '12faf5a9bb6f6f067608dca3027c8fcb@localhost'}
    :rtype: dict
    """
    column_dict = {}
    for definition in columns:
        column_name = list(definition.keys())[0]
        column_definition = definition[column_name]
        provider_config = column_definition.get('provider')
        orig_value = row.get(column_name)
        if not orig_value:
            # Skip the current column if there is no value to be altered
            continue
        provider = get_provider(provider_config)
        value = provider.alter_value(orig_value)
        append = column_definition.get('append')
        if append:
            value = value + append
        column_dict[column_name] = value
    return column_dict


def truncate_tables(connection, tables):
    """
    Truncate a list of tables.

    :param connection: A database connection instance
    :param list[str] tables: A list of table names
    """
    if not tables:
        return
    cursor = connection.cursor()
    table_names = ', '.join(tables)
    logging.info('Truncating tables "%s"', table_names)
    cursor.execute('TRUNCATE TABLE {tables};'.format(tables=table_names))
    cursor.close()


def create_database_dump(filename, db_args):
    """
    Create a dump file from the current database.

    :param str filename: Path to the dumpfile that should be created
    :param dict db_args: A dictionary with database related information
    """
    arguments = '-d {dbname} -U {user} -h {host} -p {port}'.format(**db_args)
    cmd = 'pg_dump -p -Fc -Z 9 {args} -f {filename}'.format(
        args=arguments,
        filename=filename
    )
    logging.info('Creating database dump file "%s"', filename)
    subprocess.run(cmd, shell=True)


def get_column_names(definitions):
    return [list(definition.keys())[0] for definition in definitions]


def escape_str_replace(text):
    return text.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
