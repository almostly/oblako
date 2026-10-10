"""AppConfig agent — Python port of the AWS AppConfig caching agent.

Encodes the same logic as Amazon's Go agent (the Lambda extension / sidecar):
fetch raw config via the *management* API (so ``_variants`` + rules are always
present), cache it in-process (survives Lambda warm starts), and back it up to
disk. ``evaluate()`` then resolves feature-flag variants against a request
context using the bundled rule evaluator — identical behavior to the agent that
runs against real AWS, so application code is unchanged.

Point it at oblako via ``endpoint_url`` (or AWS_ENDPOINT_URL_APPCONFIG); leave it
unset to run against real AWS.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

from .rule_evaluator import evaluate_config

LOG = logging.getLogger(__name__)

# Module-level caches — survive Lambda warm starts, like the agent process.
_CACHE: dict[str, tuple[bytes, float]] = {}
_ID_CACHE: dict[str, str] = {}

_DEFAULT_BACKUP_DIR = Path(os.environ.get("APPCONFIG_BACKUP_DIR", "/tmp/appconfig"))
_DEFAULT_POLL_INTERVAL = int(os.environ.get("APPCONFIG_POLL_INTERVAL", "45"))


def _config_key(application: str, environment: str, profile: str) -> str:
    """Return the cache key for an application/environment/profile triple."""
    return f"{application}/{environment}/{profile}"


def _write_backup(key: str, value: bytes, backup_dir: Path) -> None:
    """Write config bytes to the disk backup, logging (not raising) on failure."""
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / f"{key.replace('/', '__')}.json").write_bytes(value)
    except OSError as exc:
        LOG.warning("unable to write backup for '%s': %s", key, exc)


def _read_backup(key: str, backup_dir: Path) -> bytes | None:
    """Return the disk backup for ``key``, or None if missing or unreadable."""
    path = backup_dir / f"{key.replace('/', '__')}.json"
    if path.exists():
        try:
            return path.read_bytes()
        except OSError as exc:
            LOG.warning("unable to load backup for '%s': %s", key, exc)
    return None


class AppConfigClient:
    """Python equivalent of the AWS AppConfig agent (drop-in for the sidecar)."""

    def __init__(
        self,
        region: str | None = None,
        endpoint_url: str | None = None,
        poll_interval: int = _DEFAULT_POLL_INTERVAL,
        backup_dir: Path | None = None,
        boto_session: boto3.Session | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
    ) -> None:
        """Wire a boto3 ``appconfig`` (management) client; optionally at oblako."""
        session = boto_session or boto3.Session()
        self._client = session.client(
            "appconfig",
            region_name=region or os.environ.get("AWS_REGION", "us-east-1"),
            endpoint_url=endpoint_url or os.environ.get("AWS_ENDPOINT_URL_APPCONFIG"),
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )
        self._poll_interval = poll_interval
        self._backup_dir = backup_dir or _DEFAULT_BACKUP_DIR

    def _resolve_id(self, list_method: str, name: str, **kwargs: Any) -> str | None:
        """Return the Id of the item named (or id'd) ``name`` from a List call, cached."""
        cache_key = f"{list_method}/{name}/" + ",".join(
            f"{k}={v}" for k, v in sorted(kwargs.items())
        )
        if cache_key in _ID_CACHE:
            return _ID_CACHE[cache_key]
        try:
            for item in getattr(self._client, list_method)(**kwargs).get("Items", []):
                if item.get("Name") == name or item.get("Id") == name:
                    _ID_CACHE[cache_key] = item["Id"]
                    return item["Id"]
        except ClientError as exc:
            LOG.error("AppConfig %s failed for '%s': %s", list_method, name, exc)
        return None

    def _fetch(self, application: str, profile: str) -> bytes:
        """Fetch raw config bytes via the management API — always includes _variants."""
        app_id = self._resolve_id("list_applications", application)
        if not app_id:
            raise RuntimeError(f"AppConfig application '{application}' not found")
        profile_id = self._resolve_id(
            "list_configuration_profiles", profile, ApplicationId=app_id
        )
        if not profile_id:
            raise RuntimeError(
                f"AppConfig profile '{profile}' not found in '{application}'"
            )
        versions = self._client.list_hosted_configuration_versions(
            ApplicationId=app_id, ConfigurationProfileId=profile_id, MaxResults=1
        )
        if not versions.get("Items"):
            raise RuntimeError(f"No versions for AppConfig profile '{profile}'")
        resp = self._client.get_hosted_configuration_version(
            ApplicationId=app_id,
            ConfigurationProfileId=profile_id,
            VersionNumber=versions["Items"][0]["VersionNumber"],
        )
        return resp["Content"].read()

    def get_configuration(
        self, application: str, environment: str, profile: str, as_json: bool = True
    ) -> Any:
        """Return the raw configuration (cache when fresh, disk backup on failure)."""
        key = _config_key(application, environment, profile)
        cached = _CACHE.get(key)
        if cached and time.monotonic() - cached[1] < self._poll_interval:
            return self._decode(cached[0], as_json)
        try:
            value = self._fetch(application, profile)
        except (ClientError, RuntimeError) as exc:
            LOG.warning("fetch failed for '%s', trying backup: %s", key, exc)
            backup = _read_backup(key, self._backup_dir)
            if backup is not None:
                return self._decode(backup, as_json)
            raise
        _CACHE[key] = (value, time.monotonic())
        _write_backup(key, value, self._backup_dir)
        return self._decode(value, as_json)

    def evaluate(
        self,
        application: str,
        environment: str,
        profile: str,
        context: dict[str, Any] | None = None,
        flag_name: str | None = None,
    ) -> dict[str, Any]:
        """Resolve feature-flag variants against ``context`` (the agent's job).

        Fetches the raw config, then runs the rule evaluator over its ``values``
        map. Returns all flags, or just ``flag_name`` if given.
        """
        raw = self.get_configuration(application, environment, profile)
        values = raw.get("values", raw) if isinstance(raw, dict) else {}
        result = evaluate_config(values, context or {})
        return result.get(flag_name, {}) if flag_name else result

    def get_variant(
        self,
        application: str,
        environment: str,
        profile: str,
        context: dict[str, Any],
        flag_name: str,
    ) -> str | None:
        """Return just the resolved variant name for one flag (or None)."""
        flag = self.evaluate(application, environment, profile, context, flag_name)
        return flag.get("_variant") if isinstance(flag, dict) else None

    @staticmethod
    def _decode(value: bytes, as_json: bool) -> Any:
        """Return the bytes parsed as JSON, or unchanged when ``as_json`` is False."""
        return json.loads(value) if as_json else value
