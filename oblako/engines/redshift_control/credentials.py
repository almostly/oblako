"""GetClusterCredentials: temporary database credentials, as Redshift issues them.

Redshift answers ``GetClusterCredentials`` with a login ``IAM:<user>`` (``IAMA:``
with ``AutoCreate``) and a password valid until ``Expiration``; the session acts
as ``<user>``. awswrangler's ``connect_temp`` and the redshift-connector's IAM
login use it. moto returns a password no engine knows, so the control proxy
answers this call itself, on the cluster's real engine:

* the login role gets a fresh password and ``VALID UNTIL`` the expiration;
* it is a member of ``<user>`` and switches to it at login (a per-role ``role``
  setting), so the session has ``<user>``'s privileges;
* ``AutoCreate`` creates ``<user>`` when it doesn't exist; ``DbGroups`` are granted
  to the login role.
"""

from __future__ import annotations

import datetime as dt
import secrets

from oblako import ports

from . import clusters


class CredentialsError(Exception):
    """A GetClusterCredentials error, with its Redshift error code."""

    def __init__(self, code: str, message: str):
        """Keep the Redshift error code with the message."""
        super().__init__(message)
        self.code = code


def _admin(cluster_id: str) -> dict:
    """Return connection settings for a cluster's admin: its own engine, or the shared one."""
    record = clusters.get(cluster_id)
    if record is not None:
        return {
            "host": "localhost",
            "port": record["port"],
            "user": record["user"],
            "password": record["password"],
            "dbname": record["database"],
        }
    from oblako.services.redshift import RedshiftService

    shared = RedshiftService()
    return {
        "host": "localhost",
        "port": ports.REDSHIFT_PG,
        "user": shared.user,
        "password": shared.password,
        "dbname": shared.database,
    }


def issue(form: dict[str, str]) -> dict:
    """Issue temporary credentials for ``form`` (a GetClusterCredentials call)."""
    import psycopg
    from psycopg import sql

    user = form.get("DbUser")
    cluster_id = form.get("ClusterIdentifier")
    if not user or not cluster_id:
        raise CredentialsError(
            "InvalidParameterValue", "DbUser and ClusterIdentifier are required."
        )
    duration = int(form.get("DurationSeconds") or 900)
    if not 900 <= duration <= 3600:
        raise CredentialsError(
            "InvalidParameterValue",
            "DurationSeconds must be between 900 and 3600 seconds.",
        )
    auto_create = (form.get("AutoCreate") or "false").lower() == "true"
    groups = [v for k, v in sorted(form.items()) if k.startswith("DbGroups.member.")]
    login = ("IAMA:" if auto_create else "IAM:") + user
    password = secrets.token_urlsafe(32)
    expiration = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) + dt.timedelta(
        seconds=duration
    )
    admin = _admin(cluster_id)
    with psycopg.connect(
        **admin, autocommit=True, sslmode="prefer", connect_timeout=10
    ) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (user,)
        ).fetchone()
        if not exists and auto_create:
            conn.execute(
                sql.SQL("CREATE USER {} PASSWORD NULL").format(sql.Identifier(user))
            )
            exists = True
        valid = expiration.strftime("%Y-%m-%d %H:%M:%S+00")
        role = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (login,)
        ).fetchone()
        verb = sql.SQL("ALTER" if role else "CREATE")
        # Redshift issues credentials without checking the user; for a user that
        # doesn't exist (and no AutoCreate) the login then fails, as it does here
        can_login = sql.SQL("LOGIN" if exists else "NOLOGIN")
        conn.execute(
            sql.SQL("{} ROLE {} {} PASSWORD {} VALID UNTIL {}").format(
                verb,
                sql.Identifier(login),
                can_login,
                sql.Literal(password),
                sql.Literal(valid),
            )
        )
        if exists:
            conn.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(user), sql.Identifier(login)
                )
            )
            conn.execute(
                sql.SQL("ALTER ROLE {} SET role TO {}").format(
                    sql.Identifier(login), sql.Identifier(user)
                )
            )
        for group in groups:
            if conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (group,)
            ).fetchone():
                conn.execute(
                    sql.SQL("GRANT {} TO {}").format(
                        sql.Identifier(group), sql.Identifier(login)
                    )
                )
    return {
        "DbUser": login,
        "DbPassword": password,
        "Expiration": expiration.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ---------------------------------------------------------------------------
# GetClusterCredentialsWithIAM and Redshift Serverless GetCredentials
# ---------------------------------------------------------------------------
# Both map the calling IAM identity 1:1 to a database user, which Redshift
# creates on first use: IAM:<user name> for an IAM user, IAMR:<role name> for an
# assumed role. The password is temporary (VALID UNTIL the expiration).
def iam_db_user(caller_arn: str) -> str:
    """Return the database user an IAM identity maps to."""
    resource = caller_arn.split(":", 5)[-1]
    if resource.startswith("assumed-role/"):
        return "IAMR:" + resource.split("/")[1]
    if resource.startswith("role/"):
        return "IAMR:" + resource.rsplit("/", 1)[-1]
    return "IAM:" + resource.rsplit("/", 1)[-1]


def caller_arn(access_key_id: str) -> str:
    """Ask oblako's STS (moto) who an access key belongs to."""
    import boto3

    sts = boto3.client(
        "sts",
        endpoint_url=f"http://localhost:{ports.MOTO}",
        region_name="us-east-1",
        aws_access_key_id=access_key_id,
        aws_secret_access_key="oblako",  # moto doesn't check signatures
    )
    return sts.get_caller_identity()["Arn"]


def issue_for_identity(
    caller: str, cluster_id: str | None, duration: int
) -> tuple[str, str, dt.datetime]:
    """Create or refresh the identity's database user; return user, password, expiry."""
    import psycopg
    from psycopg import sql

    if not 900 <= duration <= 3600:
        raise CredentialsError(
            "InvalidParameterValue",
            "DurationSeconds must be between 900 and 3600 seconds.",
        )
    user = iam_db_user(caller)
    password = secrets.token_urlsafe(32)
    expiration = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) + dt.timedelta(
        seconds=duration
    )
    admin = _admin(cluster_id) if cluster_id else _admin("")
    with psycopg.connect(
        **admin, autocommit=True, sslmode="prefer", connect_timeout=10
    ) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (user,)
        ).fetchone()
        conn.execute(
            sql.SQL("{} ROLE {} LOGIN PASSWORD {} VALID UNTIL {}").format(
                sql.SQL("ALTER" if exists else "CREATE"),
                sql.Identifier(user),
                sql.Literal(password),
                sql.Literal(expiration.strftime("%Y-%m-%d %H:%M:%S+00")),
            )
        )
    return user, password, expiration
