"""Integration tests: database users mapped 1:1 to IAM identities.

GetClusterCredentialsWithIAM (provisioned) and Redshift Serverless
GetCredentials return a database user named after the calling IAM identity
(``IAM:<user>``, or ``IAMR:<role>`` for an assumed role), created on first use
with a temporary password. Needs the Redshift engine, moto (IAM and STS) and the
Redshift API on their default ports; skips otherwise.
"""

import json
import uuid

import pytest

from oblako import ports

boto3 = pytest.importorskip("boto3")
psycopg = pytest.importorskip("psycopg")

U = uuid.uuid4().hex[:6]
ENDPOINTS = {
    "AWS_ENDPOINT_URL_REDSHIFT": f"http://localhost:{ports.REDSHIFT_CONTROL}",
    "AWS_ENDPOINT_URL_REDSHIFT_SERVERLESS": f"http://localhost:{ports.REDSHIFT_CONTROL}",
    "AWS_ENDPOINT_URL_IAM": f"http://localhost:{ports.MOTO}",
    "AWS_ENDPOINT_URL_STS": f"http://localhost:{ports.MOTO}",
}
ADMIN = dict(
    host="localhost", port=5439, user="oblako", password="oblako", dbname="oblako"
)


@pytest.fixture(scope="module")
def aws():
    mp = pytest.MonkeyPatch()
    for name, url in ENDPOINTS.items():
        mp.setenv(name, url)
    mp.setenv("AWS_ACCESS_KEY_ID", "oblako")
    mp.setenv("AWS_SECRET_ACCESS_KEY", "oblako")
    mp.setenv("AWS_DEFAULT_REGION", "us-east-1")
    mp.delenv("AWS_PROFILE", raising=False)
    try:
        boto3.client("redshift").describe_clusters()
        psycopg.connect(connect_timeout=3, **ADMIN).close()
    except Exception as e:
        mp.undo()
        pytest.skip(f"the Redshift engine or API isn't running: {e}")
    created: list[str] = []
    yield created
    with psycopg.connect(autocommit=True, **ADMIN) as conn:
        for user in created:
            conn.execute(f'DROP USER IF EXISTS "{user}"')
    mp.undo()


@pytest.fixture(scope="module")
def cluster(aws):
    api = boto3.client("redshift")
    try:
        api.create_cluster(
            ClusterIdentifier="iam-creds",
            NodeType="ra3.large",
            ClusterType="single-node",
            MasterUsername="oblako",
            MasterUserPassword="Oblako123x",
            DBName="oblako",
        )
    except api.exceptions.ClusterAlreadyExistsFault:
        pass
    return "iam-creds"


def _login(user: str, password: str, host="localhost", port=5439) -> str:
    with psycopg.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        dbname="oblako",
        sslmode="require",
    ) as conn:
        return conn.execute("SELECT current_user").fetchone()[0]


def test_with_iam_maps_the_caller_to_a_database_user(aws, cluster):
    creds = boto3.client("redshift").get_cluster_credentials_with_iam(
        ClusterIdentifier=cluster, DbName="oblako", DurationSeconds=900
    )
    aws.append(creds["DbUser"])
    assert creds["DbUser"].startswith("IAM:")
    assert _login(creds["DbUser"], creds["DbPassword"]) == creds["DbUser"]
    # asking again refreshes the password; the old one stops working
    again = boto3.client("redshift").get_cluster_credentials_with_iam(
        ClusterIdentifier=cluster, DbName="oblako"
    )
    assert again["DbUser"] == creds["DbUser"]
    with pytest.raises(psycopg.OperationalError):
        _login(creds["DbUser"], creds["DbPassword"])


def test_an_assumed_role_is_an_iamr_user(aws, cluster):
    iam = boto3.client("iam")
    role = f"etl-{U}"
    arn = iam.create_role(
        RoleName=role,
        AssumeRolePolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": "*"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            }
        ),
    )["Role"]["Arn"]
    keys = boto3.client("sts").assume_role(RoleArn=arn, RoleSessionName="run")[
        "Credentials"
    ]
    as_role = boto3.client(
        "redshift",
        aws_access_key_id=keys["AccessKeyId"],
        aws_secret_access_key=keys["SecretAccessKey"],
        aws_session_token=keys["SessionToken"],
    )
    creds = as_role.get_cluster_credentials_with_iam(ClusterIdentifier=cluster)
    aws.append(creds["DbUser"])
    assert creds["DbUser"] == f"IAMR:{role}"
    assert _login(creds["DbUser"], creds["DbPassword"]) == f"IAMR:{role}"


def test_with_iam_refuses_an_unknown_cluster(aws):
    api = boto3.client("redshift")
    with pytest.raises(api.exceptions.ClusterNotFoundFault):
        api.get_cluster_credentials_with_iam(ClusterIdentifier=f"nope-{U}")


def test_serverless_get_credentials(aws):
    api = boto3.client("redshift-serverless")
    namespace, workgroup = f"iam-ns-{U}", f"iam-wg-{U}"
    api.create_namespace(namespaceName=namespace)
    api.create_workgroup(workgroupName=workgroup, namespaceName=namespace)
    try:
        creds = api.get_credentials(workgroupName=workgroup, dbName="oblako")
        aws.append(creds["dbUser"])
        endpoint = api.get_workgroup(workgroupName=workgroup)["workgroup"]["endpoint"]
        assert creds["dbUser"].startswith("IAM:")
        assert creds["expiration"] > creds["expiration"].now(creds["expiration"].tzinfo)
        user = _login(
            creds["dbUser"], creds["dbPassword"], endpoint["address"], endpoint["port"]
        )
        assert user == creds["dbUser"]
        with pytest.raises(api.exceptions.ResourceNotFoundException):
            api.get_credentials(workgroupName=f"nope-{U}")
    finally:
        api.delete_workgroup(workgroupName=workgroup)
        api.delete_namespace(namespaceName=namespace)
