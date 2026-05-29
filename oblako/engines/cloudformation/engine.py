"""Local CloudFormation engine.

Parse a template, resolve intrinsics, and provision resources into oblako's
real engines via the provider registry.
"""

from __future__ import annotations

import datetime
import json
import threading
import uuid

import yaml

from .providers import PROVIDERS
from .transform import is_sam, transform_sam

REGION = "us-east-1"
ACCOUNT = "000000000000"


class StackNotFound(Exception):
    """Raised when a referenced stack or change set does not exist."""


# CloudFormation-flavored YAML (handles !Ref, !GetAtt, !Sub, … short tags)
class _CfnLoader(yaml.SafeLoader):
    pass


def _multi(loader, tag_suffix, node):
    tag = tag_suffix  # e.g. "Ref", "GetAtt", "Sub", "Join"
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    if tag == "Ref":
        return {"Ref": value}
    if tag == "Condition":
        return {"Condition": value}
    if tag == "GetAtt" and isinstance(value, str):
        return {"Fn::GetAtt": value.split(".", 1)}
    return {f"Fn::{tag}": value}


_CfnLoader.add_multi_constructor("!", _multi)


def parse_template(body: str) -> dict:
    """Parse a CloudFormation template body from JSON or YAML and return it as a dict."""
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return yaml.load(body, Loader=_CfnLoader)


# Intrinsic resolution
def _resolve(node, ctx):
    if isinstance(node, dict):
        if len(node) == 1:
            ((k, v),) = node.items()
            if k == "Ref":
                return _ref(v, ctx)
            if k == "Fn::GetAtt":
                if isinstance(v, list):
                    logical, attr = v[0], (v[1] if len(v) > 1 else None)
                else:
                    logical, _, attr = v.partition(".")
                    attr = attr or None
                attrs = ctx.get("attrs", {}).get(logical, {})
                if attr in attrs:
                    return attrs[attr]
                return ctx["physical"].get(logical, logical)
            if k == "Fn::Sub":
                tmpl = v[0] if isinstance(v, list) else v
                return _sub(tmpl, ctx)
            if k == "Fn::Join":
                delim, parts = v
                return delim.join(str(_resolve(p, ctx)) for p in parts)
        return {k: _resolve(val, ctx) for k, val in node.items()}
    if isinstance(node, list):
        return [_resolve(x, ctx) for x in node]
    return node


def _ref(name, ctx):
    pseudo = {
        "AWS::Region": REGION,
        "AWS::AccountId": ACCOUNT,
        "AWS::StackName": ctx["stack"],
        "AWS::Partition": "aws",
        "AWS::URLSuffix": "amazonaws.com",
        "AWS::NoValue": None,
    }
    if name in pseudo:
        return pseudo[name]
    if name in ctx["params"]:
        return ctx["params"][name]
    if name in ctx["physical"]:
        return ctx["physical"][name]
    return name


def _sub(template, ctx):
    import re

    def repl(m):
        return str(_ref(m.group(1).strip(), ctx))

    return re.sub(r"\$\{([^}]+)\}", repl, template)


def _resource_deps(resource):
    """Logical ids this resource references (for ordering)."""
    deps = set(
        resource.get("DependsOn", [])
        if isinstance(resource.get("DependsOn"), list)
        else ([resource["DependsOn"]] if "DependsOn" in resource else [])
    )

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "Ref" and isinstance(v, str):
                    deps.add(v)
                elif k == "Fn::GetAtt":
                    deps.add(v[0] if isinstance(v, list) else v.split(".")[0])
                else:
                    walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(resource.get("Properties", {}))
    return deps


def _ordered(resources):
    """Topologically order resources by intra-template references (best effort)."""
    ids = list(resources)
    deps = {rid: _resource_deps(r) & set(ids) for rid, r in resources.items()}
    ordered, seen = [], set()

    def visit(rid, stack):
        if rid in seen or rid in stack:
            return
        stack.add(rid)
        for d in deps[rid]:
            visit(d, stack)
        stack.discard(rid)
        seen.add(rid)
        ordered.append(rid)

    for rid in ids:
        visit(rid, set())
    return ordered


class StackStore:
    """In-memory store for CloudFormation stacks and their associated change sets."""

    def __init__(self):
        """Initialize the store with an empty stacks dict and a reentrant lock."""
        self._stacks: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get(self, name):
        """Return the named stack dict, raising StackNotFound if it does not exist."""
        stack = self._stacks.get(name)
        if stack is None:
            raise StackNotFound(f"Stack with id {name} does not exist")
        return stack

    def exists(self, name):
        """Return True if a stack with the given name is currently stored."""
        return name in self._stacks

    def all(self):
        """Return a list of all stored stack dicts."""
        return list(self._stacks.values())

    def describe_stack_resources(self, name):
        """Return a list of resource summary dicts for every resource in the named stack."""
        stack = self.get(name)
        status = (
            stack["StackStatus"]
            if stack["StackStatus"].endswith(("COMPLETE", "FAILED"))
            else "CREATE_COMPLETE"
        )
        return [
            {
                "LogicalResourceId": rid,
                "PhysicalResourceId": r["PhysicalId"],
                "ResourceType": r["Type"],
                "ResourceStatus": status,
            }
            for rid, r in stack["resources"].items()
        ]

    def _new_stack(self, name, template, params):
        return {
            "StackId": f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{name}/{uuid.uuid4()}",
            "StackName": name,
            "StackStatus": "REVIEW_IN_PROGRESS",
            "CreationTime": datetime.datetime.now(datetime.timezone.utc),
            "template": template,
            "params": params,
            "resources": {},
            "Outputs": [],
            "events": [],
            "change_sets": {},
        }

    def create_change_set(self, name, template_body, params, cs_name, cs_type):
        """Create a change set for the named stack, creating the stack record if needed.

        The change set is a real diff of the new template against what the stack
        last knew: brand-new logical ids are ``Add``, ids whose definition changed
        are ``Modify``, ids that disappeared are ``Remove``, and unchanged ids are
        omitted (as real CloudFormation does). For a first deploy everything is an
        ``Add``, which keeps CREATE behavior identical.
        """
        template = parse_template(template_body or "{}")
        if is_sam(template):
            template = transform_sam(template)  # expand SAM to base CFN resources
        with self._lock:
            creating = name not in self._stacks
            if creating:
                self._stacks[name] = self._new_stack(name, template, params)
                # a stack-level event so describe_stack_events is never empty
                # (sam deploy reads StackEvents[0] right after CreateChangeSet)
                self._stacks[name]["events"].append(
                    _event(
                        self._stacks[name],
                        name,
                        "AWS::CloudFormation::Stack",
                        name,
                        "REVIEW_IN_PROGRESS",
                    )
                )
            stack = self._stacks[name]
            # Diff against the previously-adopted template (empty for a new stack).
            old_resources = {} if creating else stack["template"].get("Resources", {})
            new_resources = template.get("Resources", {})
            changes = []
            for rid, r in new_resources.items():
                if rid not in old_resources:
                    action = "Add"
                elif old_resources[rid] != r:
                    action = "Modify"
                else:
                    continue  # unchanged — CloudFormation omits it from the change set
                changes.append(
                    {
                        "Action": action,
                        "LogicalResourceId": rid,
                        "ResourceType": r["Type"],
                    }
                )
            for rid, r in old_resources.items():
                if rid not in new_resources:
                    changes.append(
                        {
                            "Action": "Remove",
                            "LogicalResourceId": rid,
                            "ResourceType": r.get("Type", ""),
                        }
                    )
            # Adopt the new template + params only after diffing the old one.
            stack["template"] = template
            stack["params"] = params
            cs_id = f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:changeSet/{cs_name}/{uuid.uuid4()}"
            stack["change_sets"][cs_name] = {
                "id": cs_id,
                "changes": changes,
                "type": cs_type,
            }
            return {"Id": cs_id, "StackId": stack["StackId"]}

    def _find_cs(self, stack, ref):
        """Resolve a change set by its name or its full Id (ARN)."""
        css = stack["change_sets"]
        if ref in css:
            return ref, css[ref]
        for cs_name, cs in css.items():
            if cs["id"] == ref:
                return cs_name, cs
        raise StackNotFound(f"ChangeSet [{ref}] does not exist")

    def describe_change_set(self, name, cs_ref):
        """Return a description dict for the named change set (by name or ARN)."""
        stack = self.get(name)
        cs_name, cs = self._find_cs(stack, cs_ref)
        return {
            "ChangeSetName": cs_name,
            "ChangeSetId": cs["id"],
            "StackId": stack["StackId"],
            "StackName": name,
            "Status": "CREATE_COMPLETE",
            "ExecutionStatus": "AVAILABLE",
            "Changes": cs["changes"],
        }

    def execute_change_set(self, name, cs_ref):
        """Apply the change set, provisioning only what actually changed.

        Adds and Modifies run the create provider (a Modify first tears down the
        old physical resource — CloudFormation replacement semantics, which is the
        honest simulation given providers expose create/delete but no in-place
        update). Removes run the delete provider. Resources untouched by the change
        set keep their existing physical ids, so Refs to them still resolve.
        """
        stack = self.get(name)
        _, cs = self._find_cs(stack, cs_ref)
        template = stack["template"]
        resources = template.get("Resources", {})
        # A stack that already has provisioned resources is being updated, not
        # created — drives UPDATE_* vs CREATE_* status/events.
        is_update = cs.get("type") == "UPDATE" or bool(stack["resources"])
        verb = "UPDATE" if is_update else "CREATE"

        # Template defaults first, then overlay any parameters the client passed.
        params = {
            p: spec["Default"]
            for p, spec in template.get("Parameters", {}).items()
            if "Default" in spec
        }
        params.update(stack["params"])
        # Seed the context with already-provisioned resources so intrinsics that
        # reference unchanged resources resolve during this run.
        ctx = {"stack": name, "params": params, "physical": {}, "attrs": {}}
        for rid, res in stack["resources"].items():
            ctx["physical"][rid] = res["PhysicalId"]
            ctx["attrs"][rid] = res.get("Attributes", {})

        changes = cs["changes"]
        to_apply = [
            c["LogicalResourceId"] for c in changes if c["Action"] in ("Add", "Modify")
        ]
        to_remove = [c["LogicalResourceId"] for c in changes if c["Action"] == "Remove"]

        if is_update:
            stack["StackStatus"] = "UPDATE_IN_PROGRESS"
        try:
            # Adds + Modifies, in intra-template dependency order.
            for rid in _ordered({rid: resources[rid] for rid in to_apply}):
                r = resources[rid]
                rtype = r["Type"]
                if rtype not in PROVIDERS:
                    raise ValueError(
                        f"unsupported resource type {rtype} (oblako CFN supports {sorted(PROVIDERS)})"
                    )
                # Modify == replace: delete the old physical resource first.
                if rid in stack["resources"]:
                    old = stack["resources"][rid]
                    PROVIDERS[old["Type"]][1](old["PhysicalId"], old["Properties"])
                props = _resolve(r.get("Properties", {}), ctx)
                result = PROVIDERS[rtype][0](rid, props, ctx)
                # a provider returns a physical id, or {"PhysicalId", "Attributes"}
                if isinstance(result, dict):
                    physical, attrs = result["PhysicalId"], result.get("Attributes", {})
                else:
                    physical, attrs = result, {}
                ctx["physical"][rid] = physical
                ctx["attrs"][rid] = attrs
                stack["resources"][rid] = {
                    "Type": rtype,
                    "PhysicalId": physical,
                    "Properties": props,
                    "Attributes": attrs,
                }
                stack["events"].append(
                    _event(stack, rid, rtype, physical, f"{verb}_COMPLETE")
                )
            # Removes, in reverse of provisioning order so dependents go first.
            for rid in reversed(list(stack["resources"])):
                if rid not in to_remove:
                    continue
                old = stack["resources"].pop(rid)
                PROVIDERS[old["Type"]][1](old["PhysicalId"], old["Properties"])
                ctx["physical"].pop(rid, None)
                ctx["attrs"].pop(rid, None)
                stack["events"].append(
                    _event(
                        stack, rid, old["Type"], old["PhysicalId"], "DELETE_COMPLETE"
                    )
                )
            stack["Outputs"] = [
                {
                    "OutputKey": k,
                    "OutputValue": str(_resolve(o.get("Value"), ctx)),
                    **({"Description": o["Description"]} if "Description" in o else {}),
                }
                for k, o in template.get("Outputs", {}).items()
            ]
            stack["StackStatus"] = f"{verb}_COMPLETE"
            stack["events"].append(
                _event(
                    stack, name, "AWS::CloudFormation::Stack", name, f"{verb}_COMPLETE"
                )
            )
        except Exception as e:  # noqa: BLE001
            stack["StackStatus"] = f"{verb}_FAILED"
            stack["StackStatusReason"] = str(e)
            stack["events"].append(
                _event(
                    stack,
                    name,
                    "AWS::CloudFormation::Stack",
                    name,
                    f"{verb}_FAILED",
                    str(e),
                )
            )
            raise

    def delete_stack(self, name):
        """Delete the named stack, destroying all provisioned resources in reverse order."""
        with self._lock:
            stack = self._stacks.get(name)
            if not stack:
                return
            for rid, res in reversed(list(stack["resources"].items())):
                PROVIDERS[res["Type"]][1](res["PhysicalId"], res["Properties"])
            stack["StackStatus"] = "DELETE_COMPLETE"
            self._stacks.pop(name, None)

    def describe_stack_events(self, name):
        """Return the list of stack events recorded for the named stack."""
        return self.get(name)["events"]


def _event(stack, logical, rtype, physical, status, reason=None):
    return {
        "StackId": stack["StackId"],
        "EventId": uuid.uuid4().hex,
        "StackName": stack["StackName"],
        "LogicalResourceId": logical,
        "PhysicalResourceId": physical,
        "ResourceType": rtype,
        "Timestamp": datetime.datetime.now(datetime.timezone.utc),
        "ResourceStatus": status,
        **({"ResourceStatusReason": reason} if reason else {}),
    }
