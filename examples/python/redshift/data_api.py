"""Example 7: Amazon Redshift management API + Redshift Data API.

Two AWS APIs, both local and boto3-compatible:
  * 'redshift'      (control plane) - create/describe clusters & nodes, via moto.
  * 'redshift-data' (data plane)    - run SQL over HTTP, results executed for
                                      real against the pgredshift container.

Prerequisites:
    make up           # starts pgredshift + moto
"""

from oblako.services import RedshiftService

rs = RedshiftService()

# --- Control plane: create and describe a cluster ---------------------------
redshift = rs.get_client()  # boto3.client("redshift")
try:
    redshift.create_cluster(
        ClusterIdentifier="credit-dw",
        NodeType="ra3.xlplus",
        NumberOfNodes=2,
        MasterUsername="oblako",
        MasterUserPassword="Oblako123",
        DBName="oblako",
    )
except redshift.exceptions.ClusterAlreadyExistsFault:
    pass

cluster = redshift.describe_clusters(ClusterIdentifier="credit-dw")["Clusters"][0]
print("Cluster:")
print(f"  id:       {cluster['ClusterIdentifier']}")
print(f"  status:   {cluster['ClusterStatus']}")
print(f"  nodes:    {cluster['NumberOfNodes']} x {cluster['NodeType']}")
print(f"  endpoint: {cluster['Endpoint']['Address']}:{cluster['Endpoint']['Port']}")

# --- Data plane: run SQL via the Redshift Data API --------------------------
data = rs.get_data_client()  # boto3.client("redshift-data"); auto-starts server


def run_sql(sql, parameters=None):
    kwargs = {"ClusterIdentifier": "credit-dw", "Database": "oblako", "Sql": sql}
    if parameters:
        kwargs["Parameters"] = parameters
    stmt_id = data.execute_statement(**kwargs)["Id"]
    desc = data.describe_statement(Id=stmt_id)
    if desc["Status"] == "FAILED":
        raise RuntimeError(desc.get("Error"))
    return stmt_id, desc


run_sql("""
    CREATE TABLE IF NOT EXISTS dim_segment (
        segment TEXT PRIMARY KEY,
        floor_score INT NOT NULL
    )
""")
run_sql(
    "INSERT INTO dim_segment VALUES (:seg, :floor) ON CONFLICT (segment) DO NOTHING",
    parameters=[{"name": "seg", "value": "prime"}, {"name": "floor", "value": "700"}],
)
run_sql(
    "INSERT INTO dim_segment VALUES (:seg, :floor) ON CONFLICT (segment) DO NOTHING",
    parameters=[{"name": "seg", "value": "subprime"}, {"name": "floor", "value": "0"}],
)
print("\nInserted segment dimension rows.")

# Read them back and fetch the real result set.
stmt_id, desc = run_sql("SELECT segment, floor_score FROM dim_segment ORDER BY floor_score DESC")
result = data.get_statement_result(Id=stmt_id)
columns = [c["name"] for c in result["ColumnMetadata"]]
print(f"\nQuery returned {result['TotalNumRows']} rows ({', '.join(columns)}):")
for record in result["Records"]:
    values = [next(iter(field.values())) for field in record]
    print(f"  {values}")

# Catalog introspection through the Data API.
tables = [t["name"] for t in data.list_tables(ClusterIdentifier="credit-dw", Database="oblako")["Tables"]]
print(f"\nTables visible via Data API: {tables}")
