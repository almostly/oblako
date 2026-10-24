import redshift_connector


def connect():
    conn = redshift_connector.connect(
        host="localhost",
        port=5439,
        database="oblako",
        user="oblako",
        password="oblako",
        ssl=True,
        sslmode="verify-ca",
    )
    conn.autocommit = True
    return conn


def query(sql):
    with connect().cursor() as cur:
        cur.execute(sql)
        return cur.fetchall() if cur.description else []
