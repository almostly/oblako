import psycopg


def connect():
    # host, port, database, user and password: the PG* variables
    return psycopg.connect(autocommit=True)
