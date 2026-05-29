"""In-memory AppConfig store — the AWS AppConfig resource model.

Holds applications, environments, configuration profiles, hosted configuration
versions, deployment strategies, and deployments, plus the appconfigdata runtime
sessions. The restJson1 server (app.py) maps boto3 requests onto these methods;
field names mirror the AWS API so boto3 parses the responses unchanged.

State is in-memory (reset on restart), like oblako's other in-process control
planes (CloudFormation, redshift-data).
"""

from __future__ import annotations

import datetime
import string
import threading

# AppConfig ids are 7-char lowercase alphanumeric (e.g. "abc1234"). Deterministic
# per-store counter keeps tests stable (no Math.random equivalent needed).
_ALPHABET = string.ascii_lowercase + string.digits


class AppConfigError(Exception):
    """Raised for not-found / bad-request conditions (mapped to AWS errors)."""

    def __init__(self, message: str, code: str = "ResourceNotFoundException"):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class AppConfigStore:
    """In-memory store for the AppConfig control plane + data sessions."""

    def __init__(self):
        """Initialize empty resource maps + a lock + an id counter."""
        self._lock = threading.RLock()
        self._counter = 0
        self.applications: dict[str, dict] = {}
        self.environments: dict[str, dict] = {}  # env_id -> env (carries ApplicationId)
        self.profiles: dict[str, dict] = {}      # profile_id -> profile (ApplicationId)
        # (app_id, profile_id) -> list of {VersionNumber, Content: bytes, ContentType}
        self.versions: dict[tuple[str, str], list[dict]] = {}
        self.deployment_strategies: dict[str, dict] = {}
        # (app_id, env_id) -> list of deployment dicts
        self.deployments: dict[tuple[str, str], list[dict]] = {}
        self.sessions: dict[str, dict] = {}  # appconfigdata session token -> session
        self._seed_predefined_strategies()

    def _id(self) -> str:
        """Return a stable 7-char id (deterministic per store, AWS-shaped)."""
        self._counter += 1
        n = self._counter
        chars = []
        for _ in range(7):
            chars.append(_ALPHABET[n % len(_ALPHABET)])
            n //= len(_ALPHABET)
        return "".join(reversed(chars))

    def _seed_predefined_strategies(self) -> None:
        """AWS ships predefined deployment strategies; mirror the common ones."""
        for sid, name, dur, growth in [
            ("AppConfig.AllAtOnce", "AppConfig.AllAtOnce", 0, 100.0),
            ("AppConfig.Linear50PercentEvery30Seconds",
             "AppConfig.Linear50PercentEvery30Seconds", 1, 50.0),
            ("AppConfig.Canary10Percent20Minutes",
             "AppConfig.Canary10Percent20Minutes", 20, 10.0),
        ]:
            self.deployment_strategies[sid] = {
                "Id": sid, "Name": name, "DeploymentDurationInMinutes": dur,
                "GrowthFactor": growth, "GrowthType": "LINEAR", "ReplicateTo": "NONE",
            }

    # Resolution helpers — AppConfig accepts an Id or a Name as an identifier.
    def _resolve_app(self, identifier: str) -> dict:
        with self._lock:
            for app in self.applications.values():
                if identifier in (app["Id"], app["Name"]):
                    return app
        raise AppConfigError(f"Application '{identifier}' not found")

    def _resolve_env(self, app_id: str, identifier: str) -> dict:
        for env in self.environments.values():
            if env["ApplicationId"] == app_id and identifier in (env["Id"], env["Name"]):
                return env
        raise AppConfigError(f"Environment '{identifier}' not found")

    def _resolve_profile(self, app_id: str, identifier: str) -> dict:
        for p in self.profiles.values():
            if p["ApplicationId"] == app_id and identifier in (p["Id"], p["Name"]):
                return p
        raise AppConfigError(f"ConfigurationProfile '{identifier}' not found")

    # Applications
    def create_application(self, name: str, description: str = "") -> dict:
        with self._lock:
            app = {"Id": self._id(), "Name": name, "Description": description}
            self.applications[app["Id"]] = app
            return app

    def list_applications(self) -> list[dict]:
        return list(self.applications.values())

    def get_application(self, app_id: str) -> dict:
        return self._resolve_app(app_id)

    # Environments
    def create_environment(self, app_id: str, name: str, description: str = "") -> dict:
        with self._lock:
            self._resolve_app(app_id)  # validate
            env = {"Id": self._id(), "Name": name, "ApplicationId": app_id,
                   "Description": description, "State": "ReadyForDeployment"}
            self.environments[env["Id"]] = env
            return env

    def list_environments(self, app_id: str) -> list[dict]:
        return [e for e in self.environments.values() if e["ApplicationId"] == app_id]

    # Configuration profiles
    def create_configuration_profile(self, app_id: str, name: str,
                                     location_uri: str = "hosted",
                                     profile_type: str | None = None,
                                     description: str = "") -> dict:
        with self._lock:
            self._resolve_app(app_id)
            profile = {
                "Id": self._id(), "Name": name, "ApplicationId": app_id,
                "LocationUri": location_uri, "Description": description,
                # AWS.AppConfig.FeatureFlags or AWS.Freeform
                "Type": profile_type or "AWS.Freeform",
            }
            self.profiles[profile["Id"]] = profile
            return profile

    def list_configuration_profiles(self, app_id: str) -> list[dict]:
        return [p for p in self.profiles.values() if p["ApplicationId"] == app_id]

    def get_configuration_profile(self, app_id: str, profile_id: str) -> dict:
        return self._resolve_profile(app_id, profile_id)

    # Hosted configuration versions
    def create_hosted_configuration_version(self, app_id: str, profile_id: str,
                                            content: bytes, content_type: str,
                                            description: str = "") -> dict:
        with self._lock:
            self._resolve_app(app_id)
            self._resolve_profile(app_id, profile_id)
            versions = self.versions.setdefault((app_id, profile_id), [])
            version_number = (versions[-1]["VersionNumber"] + 1) if versions else 1
            v = {"ApplicationId": app_id, "ConfigurationProfileId": profile_id,
                 "VersionNumber": version_number, "Content": content,
                 "ContentType": content_type, "Description": description}
            versions.append(v)
            return v

    def list_hosted_configuration_versions(self, app_id: str, profile_id: str) -> list[dict]:
        # summaries (no Content), newest first — like the AWS API
        versions = self.versions.get((app_id, profile_id), [])
        return [{k: x[k] for k in ("ApplicationId", "ConfigurationProfileId",
                                   "VersionNumber", "ContentType", "Description")}
                for x in reversed(versions)]

    def get_hosted_configuration_version(self, app_id: str, profile_id: str,
                                         version_number: int) -> dict:
        for v in self.versions.get((app_id, profile_id), []):
            if v["VersionNumber"] == version_number:
                return v
        raise AppConfigError(f"HostedConfigurationVersion {version_number} not found")

    def latest_version(self, app_id: str, profile_id: str) -> dict:
        versions = self.versions.get((app_id, profile_id), [])
        if not versions:
            raise AppConfigError("No hosted configuration versions")
        return versions[-1]

    # Deployment strategies
    def create_deployment_strategy(self, name: str, duration: int = 0,
                                   growth_factor: float = 100.0,
                                   description: str = "") -> dict:
        with self._lock:
            s = {"Id": self._id(), "Name": name,
                 "DeploymentDurationInMinutes": duration,
                 "GrowthFactor": growth_factor, "GrowthType": "LINEAR",
                 "ReplicateTo": "NONE", "Description": description}
            self.deployment_strategies[s["Id"]] = s
            return s

    def list_deployment_strategies(self) -> list[dict]:
        return list(self.deployment_strategies.values())

    # Deployments — oblako completes them immediately (the local engine has no ramp)
    def start_deployment(self, app_id: str, env_id: str, profile_id: str,
                         version: str, strategy_id: str, description: str = "") -> dict:
        with self._lock:
            self._resolve_app(app_id)
            deps = self.deployments.setdefault((app_id, env_id), [])
            number = (deps[-1]["DeploymentNumber"] + 1) if deps else 1
            d = {"ApplicationId": app_id, "EnvironmentId": env_id,
                 "DeploymentNumber": number, "ConfigurationProfileId": profile_id,
                 "ConfigurationVersion": str(version),
                 "DeploymentStrategyId": strategy_id, "Description": description,
                 "State": "COMPLETE", "PercentageComplete": 100.0,
                 "StartedAt": _now(), "CompletedAt": _now()}
            deps.append(d)
            return d

    def list_deployments(self, app_id: str, env_id: str) -> list[dict]:
        return list(reversed(self.deployments.get((app_id, env_id), [])))
