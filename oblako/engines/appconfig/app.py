"""restJson1 server for AWS AppConfig (control plane) + AppConfigData (data plane).

Speaks the exact wire protocol botocore expects, so unmodified
``boto3.client("appconfig")`` and ``boto3.client("appconfigdata")`` hit oblako:

    appconfig:      POST /applications, /applications/{id}/configurationprofiles,
                    .../hostedconfigurationversions, /deploymentstrategies, …
    appconfigdata:  POST /configurationsessions, GET /configuration

Both services share one port (their paths don't collide). The data plane returns
RAW configuration content, exactly like AWS — feature-flag rule evaluation is the
job of the bundled AppConfigClient agent (with request context).
"""

from __future__ import annotations

import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .engine import AppConfigError, AppConfigStore

_store = AppConfigStore()


def _err(exc: AppConfigError) -> JSONResponse:
    """Return an AppConfigError as an AWS error response (400 or 404)."""
    status = 400 if exc.code == "BadRequestException" else 404
    return JSONResponse(
        {"Message": str(exc)},
        status_code=status,
        headers={"X-Amzn-Errortype": exc.code},
    )


async def _json_body(request: Request) -> dict:
    """Return the request body parsed as JSON ({} when empty)."""
    raw = await request.body()
    return json.loads(raw) if raw else {}


# appconfig — control plane
async def applications(request: Request):
    """Handle ListApplications (GET) / CreateApplication (POST)."""
    if request.method == "POST":
        body = await _json_body(request)
        return JSONResponse(
            _store.create_application(body["Name"], body.get("Description", ""))
        )
    return JSONResponse({"Items": _store.list_applications()})


async def application(request: Request):
    """Handle GetApplication."""
    try:
        return JSONResponse(_store.get_application(request.path_params["app_id"]))
    except AppConfigError as e:
        return _err(e)


async def environments(request: Request):
    """Handle ListEnvironments (GET) / CreateEnvironment (POST)."""
    app_id = request.path_params["app_id"]
    try:
        if request.method == "POST":
            body = await _json_body(request)
            return JSONResponse(
                _store.create_environment(
                    app_id, body["Name"], body.get("Description", "")
                )
            )
        return JSONResponse({"Items": _store.list_environments(app_id)})
    except AppConfigError as e:
        return _err(e)


async def configuration_profiles(request: Request):
    """Handle ListConfigurationProfiles (GET) / CreateConfigurationProfile (POST)."""
    app_id = request.path_params["app_id"]
    try:
        if request.method == "POST":
            body = await _json_body(request)
            return JSONResponse(
                _store.create_configuration_profile(
                    app_id,
                    body["Name"],
                    body.get("LocationUri", "hosted"),
                    body.get("Type"),
                    body.get("Description", ""),
                )
            )
        return JSONResponse({"Items": _store.list_configuration_profiles(app_id)})
    except AppConfigError as e:
        return _err(e)


async def configuration_profile(request: Request):
    """Handle GetConfigurationProfile."""
    try:
        return JSONResponse(
            _store.get_configuration_profile(
                request.path_params["app_id"], request.path_params["profile_id"]
            )
        )
    except AppConfigError as e:
        return _err(e)


def _version_headers(v: dict) -> dict:
    """Return the response headers that describe a hosted configuration version."""
    return {
        "Application-Id": v["ApplicationId"],
        "Configuration-Profile-Id": v["ConfigurationProfileId"],
        "Version-Number": str(v["VersionNumber"]),
        "Content-Type": v.get("ContentType", "application/json"),
    }


async def hosted_versions(request: Request):
    """Handle ListHostedConfigurationVersions (GET) / CreateHostedConfigurationVersion (POST)."""
    app_id = request.path_params["app_id"]
    profile_id = request.path_params["profile_id"]
    try:
        if request.method == "POST":
            content = await request.body()  # Content is the httpPayload
            v = _store.create_hosted_configuration_version(
                app_id,
                profile_id,
                content,
                request.headers.get("Content-Type", "application/json"),
                request.headers.get("Description", ""),
            )
            return Response(v["Content"], headers=_version_headers(v))
        return JSONResponse(
            {"Items": _store.list_hosted_configuration_versions(app_id, profile_id)}
        )
    except AppConfigError as e:
        return _err(e)


async def hosted_version(request: Request):
    """Handle GetHostedConfigurationVersion (raw content in the body)."""
    try:
        v = _store.get_hosted_configuration_version(
            request.path_params["app_id"],
            request.path_params["profile_id"],
            int(request.path_params["version"]),
        )
        return Response(v["Content"], headers=_version_headers(v))
    except AppConfigError as e:
        return _err(e)


async def deployment_strategies(request: Request):
    """Handle ListDeploymentStrategies (GET) / CreateDeploymentStrategy (POST)."""
    if request.method == "POST":
        body = await _json_body(request)
        return JSONResponse(
            _store.create_deployment_strategy(
                body["Name"],
                body.get("DeploymentDurationInMinutes", 0),
                body.get("GrowthFactor", 100.0),
                body.get("Description", ""),
            )
        )
    return JSONResponse({"Items": _store.list_deployment_strategies()})


async def deployments(request: Request):
    """Handle ListDeployments (GET) / StartDeployment (POST)."""
    app_id = request.path_params["app_id"]
    env_id = request.path_params["env_id"]
    try:
        if request.method == "POST":
            body = await _json_body(request)
            return JSONResponse(
                _store.start_deployment(
                    app_id,
                    env_id,
                    body["ConfigurationProfileId"],
                    body["ConfigurationVersion"],
                    body["DeploymentStrategyId"],
                    body.get("Description", ""),
                )
            )
        return JSONResponse({"Items": _store.list_deployments(app_id, env_id)})
    except AppConfigError as e:
        return _err(e)


# appconfigdata — data plane (raw content; the agent evaluates flags)
async def configuration_sessions(request: Request):
    """Handle StartConfigurationSession (appconfigdata)."""
    body = await _json_body(request)
    try:
        app = _store._resolve_app(body["ApplicationIdentifier"])
        env = _store._resolve_env(app["Id"], body["EnvironmentIdentifier"])
        profile = _store._resolve_profile(
            app["Id"], body["ConfigurationProfileIdentifier"]
        )
    except AppConfigError as e:
        return _err(e)
    token = _store._id() + _store._id()  # opaque session token
    _store.sessions[token] = {
        "app_id": app["Id"],
        "env_id": env["Id"],
        "profile_id": profile["Id"],
    }
    return JSONResponse({"InitialConfigurationToken": token})


async def get_latest_configuration(request: Request):
    """Handle GetLatestConfiguration (appconfigdata) — returns raw content."""
    token = request.query_params.get("configuration_token") or ""
    session = _store.sessions.get(token)
    if not session:
        return _err(AppConfigError("Token not valid", "BadRequestException"))
    try:
        v = _store.latest_version(session["app_id"], session["profile_id"])
    except AppConfigError:
        v = {"Content": b"", "ContentType": "application/json"}
    return Response(
        v["Content"],
        headers={
            "Content-Type": v.get("ContentType", "application/json"),
            "Next-Poll-Configuration-Token": token,
            "Next-Poll-Interval-In-Seconds": "60",
        },
    )


def create_app(store: AppConfigStore | None = None) -> Starlette:
    """Build the AppConfig + AppConfigData ASGI app (optionally with a given store)."""
    global _store
    if store is not None:
        _store = store
    return Starlette(
        routes=[
            Route("/applications", applications, methods=["GET", "POST"]),
            Route("/applications/{app_id}", application, methods=["GET"]),
            Route(
                "/applications/{app_id}/environments",
                environments,
                methods=["GET", "POST"],
            ),
            Route(
                "/applications/{app_id}/configurationprofiles",
                configuration_profiles,
                methods=["GET", "POST"],
            ),
            Route(
                "/applications/{app_id}/configurationprofiles/{profile_id}",
                configuration_profile,
                methods=["GET"],
            ),
            Route(
                "/applications/{app_id}/configurationprofiles/{profile_id}/hostedconfigurationversions",
                hosted_versions,
                methods=["GET", "POST"],
            ),
            Route(
                "/applications/{app_id}/configurationprofiles/{profile_id}/hostedconfigurationversions/{version}",
                hosted_version,
                methods=["GET"],
            ),
            Route(
                "/deploymentstrategies", deployment_strategies, methods=["GET", "POST"]
            ),
            Route(
                "/applications/{app_id}/environments/{env_id}/deployments",
                deployments,
                methods=["GET", "POST"],
            ),
            Route("/configurationsessions", configuration_sessions, methods=["POST"]),
            Route("/configuration", get_latest_configuration, methods=["GET"]),
        ]
    )


app = create_app()
