"""ASGI app speaking the AWS CloudFormation wire protocol (query/XML).

A real boto3 `cloudformation` client — and therefore `aws cloudformation deploy`
and `sam deploy` (via AWS_ENDPOINT_URL_CLOUDFORMATION) — can target this and have
their templates provisioned into oblako's real engines.
"""

from __future__ import annotations

import json
import urllib.parse
import uuid
from xml.sax.saxutils import escape

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from .engine import STATE, StackNotFound, StackStore

NS = "http://cloudformation.amazonaws.com/doc/2010-05-15/"


def _iso(moment) -> str:
    """Serialize a time as CloudFormation does, to the millisecond."""
    return moment.isoformat(timespec="milliseconds")


def _now():
    from .engine import tick

    return _iso(tick())


def _el(tag, value):
    return f"<{tag}>{escape(str(value))}</{tag}>"


def _outputs_xml(outputs):
    members = "".join(
        "<member>"
        + _el("OutputKey", o["OutputKey"])
        + _el("OutputValue", o["OutputValue"])
        + (_el("Description", o["Description"]) if "Description" in o else "")
        + "</member>"
        for o in outputs
    )
    return f"<Outputs>{members}</Outputs>"


def _parameters_xml(stack):
    """Return a stack's parameters as DescribeStacks lists them, NoEcho values masked."""
    specs = stack["template"].get("Parameters", {})
    values = {key: spec["Default"] for key, spec in specs.items() if "Default" in spec}
    values.update(stack.get("params") or {})
    members = "".join(
        "<member>"
        + _el("ParameterKey", key)
        + _el(
            "ParameterValue",
            "****" if specs.get(key, {}).get("NoEcho") else str(value),
        )
        + "</member>"
        for key, value in values.items()
    )
    return f"<Parameters>{members}</Parameters>"


def _stack_xml(stack):
    description = stack["template"].get("Description")
    updated = stack.get("LastUpdatedTime")
    return (
        "<member>"
        + _el("StackId", stack["StackId"])
        + _el("StackName", stack["StackName"])
        + (_el("Description", description) if description else "")
        + _parameters_xml(stack)
        + _el("StackStatus", stack["StackStatus"])
        + _el("CreationTime", _iso(stack["CreationTime"]))
        + (_el("LastUpdatedTime", _iso(updated)) if updated else "")
        + (
            _el("StackStatusReason", stack["StackStatusReason"])
            if stack.get("StackStatusReason")
            else ""
        )
        # fields AWS returns for every stack, which clients read unconditionally
        + _el("DisableRollback", "false")
        + _el("EnableTerminationProtection", "false")
        + "<NotificationARNs/><Tags/>"
        + "<DriftInformation>"
        + _el("StackDriftStatus", "NOT_CHECKED")
        + "</DriftInformation>"
        + _outputs_xml(stack["Outputs"])
        + "</member>"
    )


def _wrap(action, result_inner):
    return (
        f'<{action}Response xmlns="{NS}"><{action}Result>{result_inner}</{action}Result>'
        f"<ResponseMetadata><RequestId>{uuid.uuid4()}</RequestId></ResponseMetadata></{action}Response>"
    )


def _ok(action, result_inner=""):
    return Response(_wrap(action, result_inner), media_type="text/xml")


def _error(message, code="ValidationError", status=400):
    body = (
        f'<ErrorResponse xmlns="{NS}"><Error><Type>Sender</Type>'
        f"<Code>{code}</Code><Message>{escape(message)}</Message></Error>"
        f"<RequestId>{uuid.uuid4()}</RequestId></ErrorResponse>"
    )
    return Response(body, status_code=status, media_type="text/xml")


def _fetch_template_url(url):
    """Read a template that sam/aws uploaded to S3 (via S3Proxy) and passed as TemplateURL."""
    if not url:
        return ""
    from urllib.parse import unquote, urlparse

    from oblako.services import S3ProxyService

    p = urlparse(url)
    path = unquote(p.path).lstrip("/")
    host = p.netloc.split(":")[0]
    parts = host.split(".")
    if (
        len(parts) > 2
        and parts[1] in ("s3", "s3-website")
        or (len(parts) > 1 and parts[1].startswith("s3"))
    ):
        bucket, key = (
            parts[0],
            path,
        )  # virtual-host style: <bucket>.s3.<region>.amazonaws.com/<key>
    else:
        bucket, _, key = path.partition("/")  # path style: <host>/<bucket>/<key>
    return (
        S3ProxyService()
        .get_client()
        .get_object(Bucket=bucket, Key=key)["Body"]
        .read()
        .decode()
    )


def _parse_params(form):
    """Pull Parameters.member.N.ParameterKey/Value pairs into a dict."""
    keys, values = {}, {}
    for k, v in form.items():
        if k.endswith(".ParameterKey"):
            keys[k.rsplit(".", 1)[0]] = v
        elif k.endswith(".ParameterValue"):
            values[k.rsplit(".", 1)[0]] = v
    return {keys[p]: values.get(p) for p in keys}


class CfnApp:
    """ASGI handler that dispatches CloudFormation query-protocol requests to StackStore."""

    def __init__(self, store: StackStore):
        """Initialize with the given StackStore instance."""
        self.store = store

    async def handle(self, request: Request) -> Response:
        """Dispatch an incoming CloudFormation request to the matching op_ method."""
        raw = (await request.body()).decode()
        form = dict(urllib.parse.parse_qsl(raw))
        action = form.get("Action", "")
        # looked up first, so an AttributeError inside a handler is not mistaken
        # for an unsupported action
        handler = getattr(self, f"op_{action}", None)
        if handler is None:
            return _error(f"unsupported action: {action}", code="InvalidAction")
        try:
            return handler(form)
        except (StackNotFound, ValueError) as e:
            return _error(str(e))
        except Exception as e:
            return _error(str(e), code="InternalFailure", status=500)

    def op_DescribeStacks(self, form):
        """Handle DescribeStacks and return XML for the requested stack(s)."""
        name = form.get("StackName")
        stacks = [self.store.get(name)] if name else self.store.all()
        return _ok(
            "DescribeStacks",
            "<Stacks>" + "".join(_stack_xml(s) for s in stacks) + "</Stacks>",
        )

    def op_DescribeStackResources(self, form):
        """Handle DescribeStackResources and return XML for all resources in the stack."""
        resources = self.store.describe_stack_resources(form["StackName"])
        members = "".join(
            "<member>"
            + _el("StackName", form["StackName"])
            + _el("LogicalResourceId", r["LogicalResourceId"])
            + _el("PhysicalResourceId", r["PhysicalResourceId"])
            + _el("ResourceType", r["ResourceType"])
            + _el("ResourceStatus", r["ResourceStatus"])
            + _el("Timestamp", _now())
            + "</member>"
            for r in resources
        )
        return _ok(
            "DescribeStackResources", f"<StackResources>{members}</StackResources>"
        )

    def op_CreateChangeSet(self, form):
        """Handle CreateChangeSet, fetching the template from S3 if a URL was given."""
        # sam/aws upload large templates to S3 and pass TemplateURL instead of body.
        body = form.get("TemplateBody") or _fetch_template_url(
            form.get("TemplateURL", "")
        )
        out = self.store.create_change_set(
            form["StackName"],
            body,
            _parse_params(form),
            form.get("ChangeSetName", "oblako-cs"),
            form.get("ChangeSetType", "CREATE"),
        )
        return _ok(
            "CreateChangeSet", _el("Id", out["Id"]) + _el("StackId", out["StackId"])
        )

    def op_CreateStack(self, form):
        """Handle CreateStack: a change set created and executed in one call."""
        body = form.get("TemplateBody") or _fetch_template_url(
            form.get("TemplateURL", "")
        )
        out = self.store.create_change_set(
            form["StackName"], body, _parse_params(form), "oblako-create", "CREATE"
        )
        self.store.execute_change_set(form["StackName"], "oblako-create")
        return _ok("CreateStack", _el("StackId", out["StackId"]))

    def op_GetTemplate(self, form):
        """Handle GetTemplate: the template as submitted, or after transforms."""
        stack = self.store.get(form["StackName"])
        if form.get("TemplateStage") == "Processed":
            body = json.dumps(stack["template"])
        else:
            body = stack.get("template_body") or json.dumps(stack["template"])
        stages = "<member>Original</member><member>Processed</member>"
        return _ok(
            "GetTemplate",
            _el("TemplateBody", body) + f"<StagesAvailable>{stages}</StagesAvailable>",
        )

    def op_GetTemplateSummary(self, form):
        """Handle GetTemplateSummary for a template body, a template URL or a stack."""
        body = form.get("TemplateBody")
        if body is None and form.get("TemplateURL"):
            body = _fetch_template_url(form["TemplateURL"])
        summary = self.store.template_summary(body, form.get("StackName"))

        def members(items):
            return "".join(_el("member", i) for i in items)

        params = "".join(
            "<member>"
            + _el("ParameterKey", p["ParameterKey"])
            + (_el("DefaultValue", p["DefaultValue"]) if "DefaultValue" in p else "")
            + _el("ParameterType", p["ParameterType"])
            + _el("NoEcho", str(p["NoEcho"]).lower())
            + (_el("Description", p["Description"]) if "Description" in p else "")
            + "</member>"
            for p in summary["Parameters"]
        )
        return _ok(
            "GetTemplateSummary",
            f"<Parameters>{params}</Parameters>"
            + (
                _el("Description", summary["Description"])
                if summary["Description"]
                else ""
            )
            + f"<Capabilities>{members(summary['Capabilities'])}</Capabilities>"
            + f"<ResourceTypes>{members(summary['ResourceTypes'])}</ResourceTypes>"
            + _el("Version", summary["Version"])
            + "<DeclaredTransforms>"
            + members(summary["DeclaredTransforms"])
            + "</DeclaredTransforms>",
        )

    def op_DescribeChangeSet(self, form):
        """Handle DescribeChangeSet and return XML for the requested change set."""
        cs = self.store.describe_change_set(form["StackName"], form["ChangeSetName"])
        changes = "".join(
            "<member><Type>Resource</Type><ResourceChange>"
            + _el("Action", c["Action"])
            + _el("LogicalResourceId", c["LogicalResourceId"])
            + _el("ResourceType", c["ResourceType"])
            + "</ResourceChange></member>"
            for c in cs["Changes"]
        )
        inner = (
            _el("ChangeSetName", cs["ChangeSetName"])
            + _el("ChangeSetId", cs["ChangeSetId"])
            + _el("StackId", cs["StackId"])
            + _el("StackName", cs["StackName"])
            + _el("Status", cs["Status"])
            + _el("ExecutionStatus", cs["ExecutionStatus"])
            + f"<Changes>{changes}</Changes>"
        )
        return _ok("DescribeChangeSet", inner)

    def op_ExecuteChangeSet(self, form):
        """Handle ExecuteChangeSet and provision all resources in the change set."""
        self.store.execute_change_set(form["StackName"], form["ChangeSetName"])
        return _ok("ExecuteChangeSet")

    def op_DeleteStack(self, form):
        """Handle DeleteStack, destroying the stack and all its provisioned resources."""
        self.store.delete_stack(form["StackName"])
        return _ok("DeleteStack")

    def op_DescribeStackEvents(self, form):
        """Handle DescribeStackEvents and return XML for all events on the stack."""
        events = self.store.describe_stack_events(form["StackName"])
        members = "".join(
            "<member>"
            + _el("StackId", e["StackId"])
            + _el("EventId", e["EventId"])
            + _el("StackName", e["StackName"])
            + _el("LogicalResourceId", e["LogicalResourceId"])
            + _el("PhysicalResourceId", e["PhysicalResourceId"])
            + _el("ResourceType", e["ResourceType"])
            + _el("Timestamp", _iso(e["Timestamp"]))
            + _el("ResourceStatus", e["ResourceStatus"])
            + (
                _el("ResourceStatusReason", e["ResourceStatusReason"])
                if e.get("ResourceStatusReason")
                else ""
            )
            + "</member>"
            for e in reversed(events)
        )
        return _ok("DescribeStackEvents", f"<StackEvents>{members}</StackEvents>")


def create_app(store: StackStore | None = None) -> Starlette:
    """Build and return the Starlette ASGI app for the CloudFormation service."""
    handler = CfnApp(store or StackStore(STATE))

    async def health(_request):
        return PlainTextResponse("ok")

    return Starlette(
        routes=[
            Route("/", health, methods=["GET"]),
            Route("/", handler.handle, methods=["POST"]),
        ]
    )


app = create_app()
