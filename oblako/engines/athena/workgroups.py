"""Athena workgroups: ``primary`` plus any created, kept in a JSON file.

A workgroup carries the result configuration queries fall back to (or are held
to, with ``EnforceWorkGroupConfiguration``). Locally ``primary`` comes with an
output location, ``s3://oblako-athena-results/``, so queries need none of their
own; on AWS you'd set one on the workgroup the same way.
"""

from __future__ import annotations

import copy
import datetime
import json
import os
import threading
from pathlib import Path

DEFAULT_OUTPUT = "s3://oblako-athena-results/"
_ENGINE = {
    "SelectedEngineVersion": "AUTO",
    "EffectiveEngineVersion": "Athena engine version 3",
}


def _default_path() -> Path:
    return Path(
        os.environ.get(
            "OBLAKO_ATHENA_WORKGROUPS",
            str(Path.home() / ".oblako" / "athena" / "workgroups.json"),
        )
    )


def _now() -> float:
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


class WorkGroupError(Exception):
    """An InvalidRequestException about a workgroup."""


class WorkGroups:
    """The workgroups, persisted so they survive an engine restart."""

    def __init__(self, path: Path | None = None):
        """Load the workgroups (``primary`` always exists)."""
        self._path = path or _default_path()
        self._lock = threading.Lock()
        try:
            self._groups: dict[str, dict] = json.loads(self._path.read_text())
        except (OSError, ValueError):
            self._groups = {}
        self._groups.setdefault(
            "primary",
            {
                "Name": "primary",
                "State": "ENABLED",
                "Description": "",
                "CreationTime": _now(),
                "Configuration": {
                    "ResultConfiguration": {"OutputLocation": DEFAULT_OUTPUT},
                    "EnforceWorkGroupConfiguration": False,
                    "PublishCloudWatchMetricsEnabled": False,
                    "RequesterPaysEnabled": False,
                    "EngineVersion": dict(_ENGINE),
                },
            },
        )

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._groups, indent=1))

    def get(self, name: str) -> dict:
        """Return a workgroup, or raise if there's none by that name."""
        with self._lock:
            group = self._groups.get(name)
            if group is None:
                raise WorkGroupError(f"WorkGroup {name} is not found.")
            return copy.deepcopy(group)

    def summaries(self) -> list[dict]:
        """Return the workgroup summaries ListWorkGroups gives."""
        with self._lock:
            return [
                {
                    "Name": g["Name"],
                    "State": g["State"],
                    "Description": g.get("Description", ""),
                    "CreationTime": g["CreationTime"],
                    "EngineVersion": g["Configuration"].get("EngineVersion", _ENGINE),
                }
                for g in sorted(self._groups.values(), key=lambda g: g["Name"])
            ]

    def create(self, req: dict) -> None:
        """Create a workgroup from a CreateWorkGroup request."""
        name = req["Name"]
        configuration = {
            "EnforceWorkGroupConfiguration": True,
            "PublishCloudWatchMetricsEnabled": True,
            "RequesterPaysEnabled": False,
            "EngineVersion": dict(_ENGINE),
            **(req.get("Configuration") or {}),
        }
        with self._lock:
            if name in self._groups:
                raise WorkGroupError(f"WorkGroup {name} is already created")
            self._groups[name] = {
                "Name": name,
                "State": "ENABLED",
                "Description": req.get("Description", ""),
                "CreationTime": _now(),
                "Configuration": configuration,
            }
            self._save()

    def update(self, req: dict) -> None:
        """Apply an UpdateWorkGroup request."""
        name = req["WorkGroup"]
        with self._lock:
            group = self._groups.get(name)
            if group is None:
                raise WorkGroupError(f"WorkGroup {name} is not found.")
            if "Description" in req:
                group["Description"] = req["Description"]
            if "State" in req:
                group["State"] = req["State"]
            updates = dict(req.get("ConfigurationUpdates") or {})
            result_updates = updates.pop("ResultConfigurationUpdates", None) or {}
            config = group["Configuration"]
            result = config.setdefault("ResultConfiguration", {})
            for key, value in result_updates.items():
                if key.startswith("Remove"):
                    if value:
                        result.pop(key.removeprefix("Remove"), None)
                else:
                    result[key] = value
            config.update(updates)
            self._save()

    def delete(self, name: str) -> None:
        """Delete a workgroup (never ``primary``)."""
        with self._lock:
            if name == "primary":
                raise WorkGroupError("Cannot delete the primary workgroup")
            if self._groups.pop(name, None) is None:
                raise WorkGroupError(f"WorkGroup {name} is not found.")
            self._save()

    def output_location(self, name: str, requested: str | None) -> str:
        """Resolve where a query's results go, as Athena does."""
        group = self.get(name)
        if group["State"] != "ENABLED":
            raise WorkGroupError(f"WorkGroup {name} is disabled.")
        config = group["Configuration"]
        own = (config.get("ResultConfiguration") or {}).get("OutputLocation")
        location = (
            own if config.get("EnforceWorkGroupConfiguration") else requested or own
        )
        if not location:
            raise WorkGroupError(
                "No output location provided. An output location is required "
                "either through the Workgroup result configuration setting or "
                "as an API input."
            )
        return location
