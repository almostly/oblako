"""Redshift service: local Amazon Redshift on oblako's own image.

``oblako/redshift`` is a modern PostgreSQL 16 that impersonates Amazon Redshift:
a small ``shared_preload`` C extension registers the Redshift-only startup
parameters Amazon's ``redshift-connector`` driver sends (``client_protocol_version``
…) as no-op GUCs and reports ``server_version = 8.0.2``, so the driver connects
*natively* (no wire shim for the handshake; a bundled proxy on 5439 makes the
engine tolerate Redshift physical DDL). It also ships the Redshift system tables,
date/time functions, catalog views, UDFs (via plpython3u), and ``SET
query_group``. Built multi-arch, so it runs on Docker and Apple ``container``
alike. See ``oblako/images/redshift``.

Three ways in:
  * ``connect()``         - psycopg2 connection straight to the engine (5439).
  * ``get_client()``      - boto3 ``redshift`` control plane (clusters, nodes)
                            served by the local moto container.
  * ``get_data_client()`` - boto3 ``redshift-data`` client whose SQL executes
                            for real against the engine (auto-starts the server).
"""

from pathlib import Path

import psycopg2

from oblako import config, ports

from .base import PortMapping, Service

# Published image oblako pulls; falls back to building oblako/images/redshift
# locally if it isn't pullable yet (see Service.build_context).
REDSHIFT_IMAGE = "public.ecr.aws/oblako/redshift-local:16"
_BUILD_CONTEXT = str((Path(__file__).parent.parent / "images" / "redshift").resolve())

# The proxy's TLS cert inside the container. A fixed self-signed cert is baked
# into the image, so it's identical across containers, `down -v`, and clones.
SSL_CERT_PATH = "/etc/oblako-redshift/server.crt"


def _redshift_connector_bundle(python_exe: str) -> str:
    """Path to redshift_connector's trusted-CA bundle in the given interpreter."""
    import subprocess

    out = subprocess.check_output(
        [
            python_exe,
            "-c",
            "import os, redshift_connector as r; "
            "print(os.path.join(os.path.dirname(r.__file__), 'files', 'redshift-ca-bundle.crt'))",
        ],
        text=True,
    )
    return out.strip()


# This machine's TLS certificate and key for the Redshift proxy, mounted into every
# Redshift container. Each machine makes its own, so no one else holds the key.
CERT_DIR = Path.home() / ".oblako" / "redshift" / "tls"
# Images up to oblako 0.1.0 baked one certificate into every container, and its key
# was published with the image. `oblako trust` removes it from bundles it added it to.
LEGACY_CERT_SHA256 = "163ec1ef92f7c3ef5fc4ff74fbed5e388a0287ad552f2447b8da4f322370dc31"


def ensure_cert(cert_dir: Path | None = None) -> Path:
    """Create this machine's certificate and key for the Redshift proxy, once.

    A server certificate for localhost and 127.0.0.1 only (CA:FALSE), so even its
    key could not vouch for another host. Kept across restarts and rebuilds, so a
    bundle that trusts it (``oblako trust``) stays valid. Its subject carries a
    random OU: OpenSSL finds a trusted self-signed certificate by its subject, and
    two trusted oblako certificates with one subject make it reject the server.
    Returns the directory.
    """
    import datetime
    import ipaddress
    import secrets

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    cert_dir = cert_dir or CERT_DIR
    crt, key_path = cert_dir / "server.crt", cert_dir / "server.key"
    if crt.exists() and key_path.exists():
        return cert_dir
    cert_dir.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "oblako"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, secrets.token_hex(6)),
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        ]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_dir


def remove_certs_from_bundle(bundle_path: str, sha256s: set[str]) -> int:
    """Remove PEM certs whose SHA-256 fingerprint is in ``sha256s``; return how many."""
    import hashlib
    import re
    import ssl

    p = Path(bundle_path)
    text = p.read_text()
    blocks = re.findall(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", text, re.S
    )
    removed = 0
    for block in blocks:
        fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(block)).hexdigest()
        if fingerprint in sha256s:
            text = text.replace(block, "")
            removed += 1
    if removed:
        p.write_text(re.sub(r"\n{3,}", "\n\n", text).strip() + "\n")
    return removed


# What `oblako trust` installs into a venv so the trust survives a
# redshift-connector reinstall (which restores the pristine bundle): a module that
# re-appends the certificates at interpreter start, imported by a .pth file.
KEEPER_MODULE = "_oblako_redshift_trust"
_KEEPER_SOURCE = """\
# Written by `oblako trust`: keeps oblako's Redshift TLS certificates in
# redshift-connector's CA bundle, which a reinstall restores to Amazon's own.
# Runs at interpreter start (see {module}.pth); `oblako trust --remove` deletes it.
CERTS = {certs!r}


def _keep():
    import importlib.util
    import os

    try:
        spec = importlib.util.find_spec("redshift_connector")  # not imported
        if spec is None or not spec.origin:
            return
        bundle = os.path.join(os.path.dirname(spec.origin), "files", "redshift-ca-bundle.crt")
        with open(bundle) as fh:
            text = fh.read()
        missing = [c for c in CERTS if c.strip() not in text]
        if missing:
            with open(bundle, "a") as fh:
                fh.write("".join("\\n" + c.strip() + "\\n" for c in missing))
    except OSError:
        pass


_keep()
"""


def _site_packages(python_exe: str) -> Path:
    """Return the venv's site-packages directory for ``python_exe``."""
    import subprocess

    out = subprocess.check_output(
        [python_exe, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        text=True,
    )
    return Path(out.strip())


def write_trust_keeper(site: Path, certs: list[str]) -> None:
    """Install the keeper (module + .pth) that re-trusts ``certs`` after a reinstall."""
    source = _KEEPER_SOURCE.format(
        module=KEEPER_MODULE, certs=[c.strip() for c in certs]
    )
    (site / f"{KEEPER_MODULE}.py").write_text(source)
    (site / f"{KEEPER_MODULE}.pth").write_text(f"import {KEEPER_MODULE}\n")


def kept_certs(site: Path) -> list[str]:
    """Return the certificates a venv's keeper holds (none without a keeper)."""
    module = site / f"{KEEPER_MODULE}.py"
    if not module.exists():
        return []
    import ast

    for node in ast.parse(module.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "CERTS" for t in node.targets
        ):
            return list(ast.literal_eval(node.value))
    return []


def remove_trust_keeper(site: Path) -> None:
    """Delete the keeper's module and .pth file."""
    for suffix in (".py", ".pth"):
        (site / f"{KEEPER_MODULE}{suffix}").unlink(missing_ok=True)


def _fingerprint(cert: str) -> str:
    import hashlib
    import ssl

    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert)).hexdigest()


def oblako_certs_in_bundle(bundle_path: str) -> set[str]:
    """Return the SHA-256 fingerprints of the oblako certificates in a CA bundle."""
    import hashlib
    import re
    import ssl

    from cryptography import x509
    from cryptography.x509.oid import NameOID

    found = set()
    text = Path(bundle_path).read_text()
    for block in re.findall(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", text, re.S
    ):
        der = ssl.PEM_cert_to_DER_cert(block)
        orgs = x509.load_der_x509_certificate(der).subject.get_attributes_for_oid(
            NameOID.ORGANIZATION_NAME
        )
        if any(o.value == "oblako" for o in orgs):
            found.add(hashlib.sha256(der).hexdigest())
    return found


def _sha256(cert: str) -> str:
    import hashlib
    import ssl

    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.strip())).hexdigest()


def append_cert_to_bundle(bundle_path: str, cert: str) -> bool:
    """Append a PEM cert to a CA bundle if not already present.

    redshift_connector has no CA-override option, but its bundle is a plain PEM
    file and ``load_verify_locations`` accepts extra certs appended to it. Returns
    True if the cert was added, False if it was already there (idempotent).
    """
    p = Path(bundle_path)
    text = p.read_text()
    cert = cert.strip()
    if cert in text:
        return False
    p.write_text(text.rstrip() + "\n" + cert + "\n")
    return True


class RedshiftService(Service):
    """Local Amazon Redshift, backed by the oblako/redshift image.

    A bundled wire proxy handles the Redshift-only SQL (physical DDL, varchar(max))
    and terminates TLS. libpq clients (psycopg2, JDBC) can use ``sslmode=require``;
    for redshift_connector (dbt, awswrangler) run ``oblako trust`` once per venv to
    trust the proxy's cert, then use ``sslmode=verify-ca``. See ``trust_cert``.
    """

    def __init__(
        self,
        host_port: int = ports.REDSHIFT_PG,
        user: str = "oblako",
        password: str = "oblako",
        database: str = "oblako",
        control_port: int = ports.REDSHIFT_CONTROL,
        data_port: int = ports.REDSHIFT_DATA,
        region: str | None = None,
    ):
        """Initialize the Redshift engine container and connection settings."""
        super().__init__(
            name="redshift",
            image=REDSHIFT_IMAGE,
            build_context=_BUILD_CONTEXT,
            # Redshift's real port (5439) both on the host and inside the
            # container (the proxy listens there; PG is internal on 5433). Using
            # 5439, not 5432, avoids colliding with a plain Postgres co-located
            # in the same network (e.g. a Metabase metadata DB on Fargate).
            ports=[PortMapping(container_port=5439, host_port=host_port)],
            environment={
                "POSTGRES_USER": user,
                "POSTGRES_PASSWORD": password,
                "POSTGRES_DB": database,
                # Redshift uses md5 auth, which redshift-connector expects; the
                # image stores passwords as md5 to match (see its Dockerfile).
                "POSTGRES_HOST_AUTH_METHOD": "md5",
                # Where the in-engine COPY/UNLOAD bridge reaches the object store:
                # S3Proxy on the host (host.docker.internal is substituted for the
                # host gateway by the backend). Unset, it falls back to real AWS.
                "OBLAKO_S3_ENDPOINT": f"http://host.docker.internal:{ports.S3}",
                # the Iceberg REST catalog Redshift's Iceberg tables register in
                "OBLAKO_ICEBERG_URL": f"http://host.docker.internal:{ports.ICEBERG}",
            },
            volumes={
                "oblako-redshift-data": {
                    "bind": "/var/lib/postgresql/data",
                    "mode": "rw",
                },
                # Redshift ML: the in-container agent trains CREATE MODELs in a
                # container on the host Docker daemon (as moto does for Lambda).
                "/var/run/docker.sock": {
                    "bind": "/var/run/docker.sock",
                    "mode": "rw",
                },
                # this machine's TLS cert and key (ensure_cert, before start)
                str(CERT_DIR): {"bind": "/etc/oblako-redshift", "mode": "ro"},
            },
            # So the in-engine COPY/UNLOAD bridge can reach S3Proxy on the host
            # via host.docker.internal (Docker needs the explicit host-gateway
            # alias on Linux; the Apple backend rewrites it to the vmnet gateway).
            extra_hosts={"host.docker.internal": "host-gateway"},
        )
        self.host_port = host_port
        self.user = user
        self.password = password
        self.database = database
        self.control_port = control_port
        self.data_port = data_port
        self.region = region or config.region()

    def connect(self):
        """Return a psycopg2 connection straight to the Redshift engine."""
        return psycopg2.connect(
            host="localhost",
            port=self.host_port,
            user=self.user,
            password=self.password,
            dbname=self.database,
        )

    def server_certs(self) -> list[str]:
        """Return every TLS cert (PEM) a Redshift proxy on this machine presents.

        oblako's containers, the single-node engine and every node of a multi-node
        cluster, mount this machine's cert (``ensure_cert``), so ``oblako trust``
        works before anything runs and covers them all. A container started by
        ``docker compose`` makes its own, so a running compose ``redshift`` or
        ``redshift-coordinator`` container's cert is added: both kinds can run on
        one machine, as in CI.
        """
        certs = [(ensure_cert() / "server.crt").read_text()]
        try:
            containers = self.client.containers.list(
                filters={"label": "com.docker.compose.service"}
            )
        except Exception:  # no Docker, or a backend without labels
            containers = []
        for container in containers:
            service = container.labels.get("com.docker.compose.service")
            if service not in ("redshift", "redshift-coordinator"):
                continue
            code, out = container.exec_run(["cat", SSL_CERT_PATH])
            if code == 0 and out.decode() not in certs:
                certs.append(out.decode())
        return certs

    def trust_cert(self, python_exe: str | None = None) -> str:
        """Trust the proxy's cert in a venv's redshift_connector CA bundle.

        redshift_connector (and thus dbt-redshift, awswrangler) verifies TLS
        against a hardcoded Amazon CA bundle with no override, so it can't verify
        a local cert out of the box. This appends the proxy's self-signed cert to
        that bundle in ``python_exe``'s environment (default: the current one), so
        ``sslmode=verify-ca`` then gives real, verified TLS locally, no
        ``ssl=False``. Idempotent. A keeper installed in the same venv re-appends
        the certificates at interpreter start, so the trust survives a
        redshift-connector reinstall (which restores the pristine bundle). The
        cert is this machine's own and cannot sign others (CA:FALSE), so trusting
        it vouches for nothing else.

        oblako certificates no proxy here presents any more (a removed compose
        container's, say) are dropped from the bundle and the keeper: OpenSSL
        finds a trusted self-signed certificate by its subject, and certificates
        older oblako versions made share one, so a stale one can make it reject
        the current server.
        """
        import sys

        python_exe = python_exe or sys.executable
        bundle = _redshift_connector_bundle(python_exe)
        legacy = remove_certs_from_bundle(bundle, {LEGACY_CERT_SHA256})
        certs = self.server_certs()
        current = {_sha256(c) for c in certs}
        stale = remove_certs_from_bundle(
            bundle, oblako_certs_in_bundle(bundle) - current
        )
        added = sum(append_cert_to_bundle(bundle, cert) for cert in certs)
        site = _site_packages(python_exe)
        write_trust_keeper(site, certs)
        note = (
            " (and removed the certificate older oblako images shared, whose key "
            "is public)"
            if legacy
            else ""
        )
        if stale:
            note += f" (and removed {stale} oblako cert(s) no proxy here presents)"
        done = (
            f"appended {added} oblako Redshift cert(s) to {bundle}"
            if added
            else f"already trusted in {bundle}"
        )
        return f"{done}{note}; kept across redshift-connector reinstalls"

    def untrust(self, python_exe: str | None = None) -> str:
        """Undo ``trust_cert``: remove the keeper and its certificates from the bundle."""
        import sys

        python_exe = python_exe or sys.executable
        site = _site_packages(python_exe)
        certs = kept_certs(site) + self.server_certs()
        bundle = _redshift_connector_bundle(python_exe)
        removed = remove_certs_from_bundle(bundle, {_fingerprint(c) for c in certs})
        remove_trust_keeper(site)
        return f"removed {removed} oblako Redshift cert(s) from {bundle} and its keeper"

    def start(self) -> None:
        """Start the engine, then the Redshift API that runs multi-node clusters."""
        from oblako.engines import host

        ensure_cert()  # mounted into the container: it must exist first
        super().start()
        if self.control_port == ports.REDSHIFT_CONTROL:
            host.start("redshift-control")

    def get_client(self):
        """boto3 ``redshift`` control-plane client (single-node and multi-node)."""
        from . import boto

        if self.control_port == ports.REDSHIFT_CONTROL:
            from oblako.engines import redshift_control

            redshift_control.start_in_thread(self.control_port)
        return boto.client(
            "redshift",
            f"http://localhost:{self.control_port}",
            region=self.region,
        )

    def start_data_server(self):
        """Start the redshift-data server in-process (idempotent). Returns its URL."""
        from oblako.engines.redshift_data import RedshiftDataExecutor, start_in_thread

        executor = RedshiftDataExecutor(
            host="localhost",
            port=self.host_port,
            user=self.user,
            password=self.password,
            database=self.database,
        )
        return start_in_thread(port=self.data_port, executor=executor)

    def get_data_client(self, autostart: bool = True):
        """boto3 ``redshift-data`` client executing real SQL against the engine."""
        from oblako.engines import redshift_data

        from . import boto

        if autostart and not redshift_data.is_running(self.data_port):
            self.start_data_server()
        return boto.client(
            "redshift-data",
            f"http://localhost:{self.data_port}",
            region=self.region,
        )

    def _health_check(self) -> bool:
        try:
            self.connect().close()
            return True
        except psycopg2.OperationalError:
            return False
