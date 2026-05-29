"""Example 10: Amazon RDS + Aurora (local).

Control plane via boto3 'rds' (instances & clusters, through moto); data plane
via a direct psycopg2 connection to the real Postgres engine. Real SQL,
simulated topology.

Prerequisites:
    make up        # starts the rds (postgres) engine + moto
"""

from oblako.services import RdsService


def ignore_exists(fn):
    """Run fn, ignoring 'already exists' faults so the example is re-runnable."""
    try:
        fn()
    except Exception as e:
        if "AlreadyExists" not in type(e).__name__:
            raise


svc = RdsService()
rds = svc.get_client()  # boto3.client("rds")

# --- RDS: a standalone instance ---
ignore_exists(lambda: rds.create_db_instance(
    DBInstanceIdentifier="app-db", Engine="postgres", DBInstanceClass="db.t3.micro",
    MasterUsername="oblako", MasterUserPassword="Oblako123", AllocatedStorage=20, DBName="oblako",
))
inst = rds.describe_db_instances(DBInstanceIdentifier="app-db")["DBInstances"][0]
print("RDS instance:")
print(f"id:       {inst['DBInstanceIdentifier']}")
print(f"engine:   {inst['Engine']} ({inst['DBInstanceClass']})")
print(f"endpoint: {inst['Endpoint']['Address']}:{inst['Endpoint']['Port']}")

# --- Aurora: a cluster with a writer instance ---
ignore_exists(lambda: rds.create_db_cluster(
    DBClusterIdentifier="analytics", Engine="aurora-postgresql",
    MasterUsername="oblako", MasterUserPassword="Oblako123", DatabaseName="oblako",
))
ignore_exists(lambda: rds.create_db_instance(
    DBInstanceIdentifier="analytics-1", DBClusterIdentifier="analytics",
    Engine="aurora-postgresql", DBInstanceClass="db.r6g.large",
))
cluster = rds.describe_db_clusters(DBClusterIdentifier="analytics")["DBClusters"][0]
print("\nAurora cluster:")
print(f"id:      {cluster['DBClusterIdentifier']}")
print(f"writer:  {cluster['Endpoint']}")
print(f"reader:  {cluster['ReaderEndpoint']}")
print(f"members: {[m['DBInstanceIdentifier'] for m in cluster.get('DBClusterMembers', [])]}")

# --- Data plane: real SQL against the engine ---
conn = svc.connect()
conn.autocommit = True
cur = conn.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS widgets (id INT PRIMARY KEY, name TEXT)")
cur.executemany(
    "INSERT INTO widgets VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
    [(1, "sprocket"), (2, "gizmo")],
)
cur.execute("SELECT id, name FROM widgets ORDER BY id")
print("\nQuery results (real SQL on the engine):")
for row in cur.fetchall():
    print(f"{row[0]}: {row[1]}")
cur.close()
conn.close()
