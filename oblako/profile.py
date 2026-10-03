"""An AWS profile that points every SDK and the AWS CLI at oblako.

The shared config file can carry an endpoint per service (a ``services``
section, read by botocore 1.31+ and AWS CLI 2.13+), so a profile can stand in for
the ``AWS_ENDPOINT_URL_*`` variables. ``oblako configure`` writes:

* ``[profile oblako]`` and ``[services oblako]`` in ``~/.aws/config``: the region,
  S3 path-style addressing, checksums only when required, and one endpoint per
  service, from the same table as ``oblako notebook``;
* ``[oblako]`` in ``~/.aws/credentials``: keys generated once per machine (oblako
  does not check them) and kept on later runs.

Then ``AWS_PROFILE=oblako`` reaches oblako and another profile reaches AWS, as a
kubectl context does. Other profiles in the files are left as they are; the
``AWS_CONFIG_FILE`` and ``AWS_SHARED_CREDENTIALS_FILE`` variables are honoured.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from oblako import config, ports

ENDPOINT_PREFIX = "AWS_ENDPOINT_URL_"


def config_path() -> Path:
    """Return the shared config file the SDKs read."""
    return Path(os.environ.get("AWS_CONFIG_FILE") or Path.home() / ".aws" / "config")


def credentials_path() -> Path:
    """Return the shared credentials file the SDKs read."""
    return Path(
        os.environ.get("AWS_SHARED_CREDENTIALS_FILE")
        or Path.home() / ".aws" / "credentials"
    )


def service_endpoints() -> dict[str, str]:
    """Return ``{service key: endpoint}`` for the ``services`` section.

    A service's key there is its ``AWS_ENDPOINT_URL_*`` suffix in lower case
    (``AWS_ENDPOINT_URL_REDSHIFT_DATA`` is ``redshift_data``).
    """
    from oblako.notebook import ENDPOINTS

    return {
        name[len(ENDPOINT_PREFIX) :].lower(): url
        for name, url in ENDPOINTS.items()
        if name.startswith(ENDPOINT_PREFIX)
    }


def _without_section(text: str, header: str) -> str:
    """Remove one ``[header]`` section (up to the next section) from an INI text."""
    pattern = re.compile(
        rf"^\[{re.escape(header)}\][^\n]*\n(?:(?!\[).*\n?)*", re.MULTILINE
    )
    return pattern.sub("", text)


def _replace_sections(path: Path, sections: dict[str, str]) -> None:
    text = path.read_text() if path.exists() else ""
    for header in sections:
        text = _without_section(text, header)
    text = text.rstrip("\n")
    blocks = [f"[{header}]\n{body}" for header, body in sections.items()]
    text = "\n\n".join(part for part in [text, *blocks] if part) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _existing_keys(path: Path, name: str) -> tuple[str, str] | None:
    if not path.exists():
        return None
    match = re.search(
        rf"^\[{re.escape(name)}\]\n((?:(?!\[).*\n?)*)", path.read_text(), re.MULTILINE
    )
    if match is None:
        return None
    fields = dict(re.findall(r"^\s*(\w+)\s*=\s*(\S+)", match.group(1), re.MULTILINE))
    key, secret = fields.get("aws_access_key_id"), fields.get("aws_secret_access_key")
    return (key, secret) if key and secret else None


def write_profile(name: str = "oblako") -> dict[str, str]:
    """Write the oblako profile and its credentials; return what was written where."""
    endpoints = "".join(
        f"{service} =\n    endpoint_url = {url}\n"
        for service, url in sorted(service_endpoints().items())
    )
    profile = (
        f"region = {config.region()}\n"
        # any service the services section does not list goes to moto, never
        # to AWS: a tool that calls one would otherwise leave oblako
        f"endpoint_url = http://localhost:{ports.MOTO}\n"
        f"services = {name}\n"
        "request_checksum_calculation = when_required\n"
        "response_checksum_validation = when_required\n"
        "s3 =\n"
        "    addressing_style = path\n"
    )
    _replace_sections(
        config_path(), {f"profile {name}": profile, f"services {name}": endpoints}
    )
    keys = _existing_keys(credentials_path(), name)
    if keys is None:
        keys = ("OBLAKO" + secrets.token_hex(7).upper(), secrets.token_urlsafe(30))
    _replace_sections(
        credentials_path(),
        {name: f"aws_access_key_id = {keys[0]}\naws_secret_access_key = {keys[1]}\n"},
    )
    credentials_path().chmod(0o600)  # keys: readable by you only, as the CLI makes it
    return {
        "profile": name,
        "config": str(config_path()),
        "credentials": str(credentials_path()),
        "services": str(len(service_endpoints())),
    }
