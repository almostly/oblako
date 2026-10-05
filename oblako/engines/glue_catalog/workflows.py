"""Glue triggers and workflows on the Glue engine.

A trigger starts jobs and crawlers (its ``Actions``):

* ``ON_DEMAND`` fires when started (``StartTrigger``, or as a workflow's start);
* ``SCHEDULED`` fires on its ``cron(...)`` schedule once activated;
* ``CONDITIONAL`` fires when its ``Predicate`` holds: job states (``SUCCEEDED``,
  ``FAILED``, ``STOPPED``, ``TIMEOUT``) and crawl states (``SUCCEEDED``,
  ``FAILED``, ``CANCELLED``), all of them (``AND``) or any (``ANY``).

Outside a workflow an activated conditional trigger watches every job run and
crawl. In a workflow it watches that run's jobs and crawls: ``StartWorkflowRun``
fires the workflow's start trigger (on-demand or scheduled), starts jobs with
``--WORKFLOW_NAME`` and ``--WORKFLOW_RUN_ID`` as Glue does, and follows the graph,
each conditional trigger firing once, until nothing runs and nothing more can
fire; the run is then ``COMPLETED`` with its ``Statistics``. ``GetWorkflow`` and
``GetWorkflowRun`` return the graph (``IncludeGraph``): trigger, job and crawler
nodes with the edges between them. Run properties are kept per run.

Definitions and runs are kept in ``~/.oblako/glue/workflows.json``.
"""

from __future__ import annotations

import datetime
import json
import threading
import time
import uuid
from pathlib import Path

from oblako.engines.glue_catalog import _ACTIONS, GlueError, _action, _not_found

STATE = Path.home() / ".oblako" / "glue" / "workflows.json"
_lock = threading.RLock()
_JOB_DONE = ("SUCCEEDED", "FAILED", "STOPPED", "TIMEOUT", "ERROR")
_CRAWL_DONE = ("SUCCEEDED", "FAILED", "CANCELLED")
_TRIGGER_FIELDS = (
    "WorkflowName",
    "Type",
    "Schedule",
    "Predicate",
    "Actions",
    "Description",
    "EventBatchingCondition",
)


def _load() -> dict:
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    for key in ("workflows", "triggers", "runs", "seen"):
        state.setdefault(key, {})
    return state


def _save(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, default=str))
    tmp.replace(STATE)


def _require(state: dict, kind: str, name: str) -> dict:
    item = state[kind].get(name)
    if item is None:
        label = {"workflows": "Workflow", "triggers": "Trigger"}[kind]
        raise _not_found(f"{label} with name {name} not found")
    return item


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------
@_action("AWSGlue.CreateTrigger")
def _create_trigger(body):
    name = body["Name"]
    if body.get("Type") not in ("ON_DEMAND", "SCHEDULED", "CONDITIONAL", "EVENT"):
        raise GlueError(
            "InvalidInputException",
            "Type must be ON_DEMAND, SCHEDULED, CONDITIONAL or EVENT",
        )
    if body["Type"] == "SCHEDULED" and not body.get("Schedule"):
        raise GlueError("InvalidInputException", "A SCHEDULED trigger needs a Schedule")
    if body["Type"] == "CONDITIONAL" and not (body.get("Predicate") or {}).get(
        "Conditions"
    ):
        raise GlueError(
            "InvalidInputException", "A CONDITIONAL trigger needs a Predicate"
        )
    if not body.get("Actions"):
        raise GlueError("InvalidInputException", "A trigger needs Actions")
    with _lock:
        state = _load()
        if name in state["triggers"]:
            raise GlueError("AlreadyExistsException", f"Trigger {name} already exists")
        if body.get("WorkflowName"):
            _require(state, "workflows", body["WorkflowName"])
        trigger = {k: body[k] for k in _TRIGGER_FIELDS if k in body}
        trigger.update(Name=name, Id=name, State="CREATED")
        if body.get("StartOnCreation") and body["Type"] != "ON_DEMAND":
            trigger["State"] = "ACTIVATED"
        state["triggers"][name] = trigger
        _save(state)
    return {"Name": name}


@_action("AWSGlue.GetTrigger")
def _get_trigger(body):
    return {"Trigger": _require(_load(), "triggers", body["Name"])}


@_action("AWSGlue.GetTriggers")
def _get_triggers(body):
    triggers = list(_load()["triggers"].values())
    if job := body.get("DependentJobName"):
        triggers = [
            t for t in triggers if any(a.get("JobName") == job for a in t["Actions"])
        ]
    return {"Triggers": triggers}


@_action("AWSGlue.ListTriggers")
def _list_triggers(body):
    triggers = _load()["triggers"].values()
    if job := body.get("DependentJobName"):
        triggers = [
            t for t in triggers if any(a.get("JobName") == job for a in t["Actions"])
        ]
    return {"TriggerNames": sorted(t["Name"] for t in triggers)}


@_action("AWSGlue.BatchGetTriggers")
def _batch_get_triggers(body):
    triggers = _load()["triggers"]
    names = body.get("TriggerNames") or []
    return {
        "Triggers": [triggers[n] for n in names if n in triggers],
        "TriggersNotFound": [n for n in names if n not in triggers],
    }


@_action("AWSGlue.UpdateTrigger")
def _update_trigger(body):
    with _lock:
        state = _load()
        trigger = _require(state, "triggers", body["Name"])
        update = body.get("TriggerUpdate") or {}
        trigger.update({k: v for k, v in update.items() if k in _TRIGGER_FIELDS})
        _save(state)
    return {"Trigger": trigger}


@_action("AWSGlue.DeleteTrigger")
def _delete_trigger(body):
    with _lock:
        state = _load()
        _require(state, "triggers", body["Name"])
        del state["triggers"][body["Name"]]
        _save(state)
    return {"Name": body["Name"]}


@_action("AWSGlue.StartTrigger")
def _start_trigger(body):
    name = body["Name"]
    with _lock:
        state = _load()
        trigger = _require(state, "triggers", name)
        if trigger["Type"] == "ON_DEMAND":
            pass  # fires now, stays CREATED
        else:
            trigger["State"] = "ACTIVATED"
            _save(state)
            return {"Name": name}
    if trigger.get("WorkflowName"):
        _start_workflow_run({"Name": trigger["WorkflowName"]})
    else:
        _fire(trigger, workflow=None, run_id=None)
    return {"Name": name}


@_action("AWSGlue.StopTrigger")
def _stop_trigger(body):
    with _lock:
        state = _load()
        trigger = _require(state, "triggers", body["Name"])
        trigger["State"] = "DEACTIVATED"
        _save(state)
    return {"Name": body["Name"]}


def _fire(trigger: dict, workflow: str | None, run_id: str | None) -> list[dict]:
    """Start a trigger's actions; return the nodes it started ({type, name, id})."""
    from oblako.engines.glue_catalog import crawlers

    started = []
    for action in trigger.get("Actions") or []:
        if action.get("JobName"):
            arguments = dict(action.get("Arguments") or {})
            if workflow:
                arguments.update(
                    {"--WORKFLOW_NAME": workflow, "--WORKFLOW_RUN_ID": run_id}
                )
            request = {"JobName": action["JobName"], "Arguments": arguments}
            if action.get("Timeout"):
                request["Timeout"] = action["Timeout"]
            job_run = _ACTIONS["AWSGlue.StartJobRun"](request)["JobRunId"]
            started.append({"type": "JOB", "name": action["JobName"], "id": job_run})
        elif action.get("CrawlerName"):
            crawlers.start(action["CrawlerName"])
            started.append(
                {
                    "type": "CRAWLER",
                    "name": action["CrawlerName"],
                    "id": str(time.time()),
                }
            )
    return started


def _node_state(node: dict) -> str | None:
    """Return a started job run's or crawl's finished state, or None while it runs."""
    from oblako.engines.glue_catalog import crawlers, jobs

    if node["type"] == "JOB":
        state = jobs._find_run(node["name"], node["id"])["JobRunState"]
        return state if state in _JOB_DONE else None
    crawler = crawlers._load()["crawlers"].get(node["name"]) or {}
    last = crawler.get("LastCrawl") or {}
    if (
        crawler.get("State") == "READY"
        and last.get("StartTime", 0) >= float(node["id"]) - 1
    ):
        return last.get("Status")
    return None


def _holds(predicate: dict, done: dict[tuple[str, str], str]) -> bool:
    """Whether a predicate holds over finished nodes {(type, name): state}."""
    results = []
    for cond in predicate.get("Conditions") or []:
        if cond.get("JobName"):
            got = done.get(("JOB", cond["JobName"]))
            want = cond.get("State")
        else:
            got = done.get(("CRAWLER", cond.get("CrawlerName", "")))
            want = cond.get("CrawlState")
        results.append(got is not None and got == want)
    return all(results) if predicate.get("Logical", "AND") == "AND" else any(results)


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------
@_action("AWSGlue.CreateWorkflow")
def _create_workflow(body):
    name = body["Name"]
    with _lock:
        state = _load()
        if name in state["workflows"]:
            raise GlueError("AlreadyExistsException", f"Workflow {name} already exists")
        now = time.time()
        state["workflows"][name] = {
            "Name": name,
            "Description": body.get("Description", ""),
            "DefaultRunProperties": body.get("DefaultRunProperties") or {},
            "MaxConcurrentRuns": body.get("MaxConcurrentRuns"),
            "CreatedOn": now,
            "LastModifiedOn": now,
        }
        _save(state)
    return {"Name": name}


@_action("AWSGlue.UpdateWorkflow")
def _update_workflow(body):
    with _lock:
        state = _load()
        wf = _require(state, "workflows", body["Name"])
        for key in ("Description", "DefaultRunProperties", "MaxConcurrentRuns"):
            if key in body:
                wf[key] = body[key]
        wf["LastModifiedOn"] = time.time()
        _save(state)
    return {"Name": body["Name"]}


@_action("AWSGlue.DeleteWorkflow")
def _delete_workflow(body):
    with _lock:
        state = _load()
        _require(state, "workflows", body["Name"])
        del state["workflows"][body["Name"]]
        state["runs"].pop(body["Name"], None)
        _save(state)
    return {"Name": body["Name"]}


@_action("AWSGlue.ListWorkflows")
def _list_workflows(_body):
    return {"Workflows": sorted(_load()["workflows"])}


def _graph(state: dict, workflow: str, run: dict | None) -> dict:
    """Return the workflow's graph: trigger, job and crawler nodes and their edges."""
    nodes: dict[str, dict] = {}
    edges = []

    def node(kind: str, name: str) -> str:
        uid = f"{kind.lower()}_{name}"
        if uid not in nodes:
            entry: dict = {"Type": kind, "Name": name, "UniqueId": uid}
            if kind == "TRIGGER":
                entry["TriggerDetails"] = {"Trigger": state["triggers"][name]}
            elif kind == "JOB":
                runs = [
                    n
                    for n in (run or {}).get("nodes", [])
                    if n["type"] == "JOB" and n["name"] == name
                ]
                entry["JobDetails"] = {
                    "JobRuns": [{"Id": n["id"], "JobName": name} for n in runs]
                }
            else:
                entry["CrawlerDetails"] = {"Crawls": []}
            nodes[uid] = entry
        return uid

    for trigger in state["triggers"].values():
        if trigger.get("WorkflowName") != workflow:
            continue
        t = node("TRIGGER", trigger["Name"])
        for action in trigger.get("Actions") or []:
            kind, name = (
                ("JOB", action["JobName"])
                if action.get("JobName")
                else ("CRAWLER", action["CrawlerName"])
            )
            edges.append({"SourceId": t, "DestinationId": node(kind, name)})
        for cond in (trigger.get("Predicate") or {}).get("Conditions") or []:
            kind, name = (
                ("JOB", cond["JobName"])
                if cond.get("JobName")
                else ("CRAWLER", cond["CrawlerName"])
            )
            edges.append({"SourceId": node(kind, name), "DestinationId": t})
    return {"Nodes": list(nodes.values()), "Edges": edges}


def _public_run(state: dict, workflow: str, run: dict, graph: bool) -> dict:
    out = {k: v for k, v in run.items() if k not in ("nodes", "fired")}
    if graph:
        out["Graph"] = _graph(state, workflow, run)
    return out


@_action("AWSGlue.GetWorkflow")
def _get_workflow(body):
    state = _load()
    wf = dict(_require(state, "workflows", body["Name"]))
    runs = state["runs"].get(body["Name"]) or []
    if runs:
        wf["LastRun"] = _public_run(state, body["Name"], runs[0], False)
    if body.get("IncludeGraph"):
        wf["Graph"] = _graph(state, body["Name"], None)
    return {"Workflow": wf}


@_action("AWSGlue.BatchGetWorkflows")
def _batch_get_workflows(body):
    names = body.get("Names") or []
    state = _load()
    found = [n for n in names if n in state["workflows"]]
    return {
        "Workflows": [
            _get_workflow({"Name": n, "IncludeGraph": body.get("IncludeGraph")})[
                "Workflow"
            ]
            for n in found
        ],
        "MissingWorkflows": [n for n in names if n not in state["workflows"]],
    }


@_action("AWSGlue.StartWorkflowRun")
def _start_workflow_run(body):
    name = body["Name"]
    with _lock:
        state = _load()
        wf = _require(state, "workflows", name)
        starts = [
            t
            for t in state["triggers"].values()
            if t.get("WorkflowName") == name and t["Type"] in ("ON_DEMAND", "SCHEDULED")
        ]
        if not starts:
            raise GlueError(
                "InvalidInputException", f"Workflow {name} has no start trigger"
            )
        run_id = "wr_" + uuid.uuid4().hex
        run = {
            "Name": name,
            "WorkflowRunId": run_id,
            "WorkflowRunProperties": {
                **(wf.get("DefaultRunProperties") or {}),
                **(body.get("RunProperties") or {}),
            },
            "StartedOn": time.time(),
            "Status": "RUNNING",
            "Statistics": {},
            "nodes": [],
            "fired": [],
        }
        state["runs"].setdefault(name, []).insert(0, run)
        _save(state)
    threading.Thread(target=_drive, args=(name, run_id, starts), daemon=True).start()
    return {"RunId": run_id}


def _find_workflow_run(state: dict, name: str, run_id: str) -> dict:
    _require(state, "workflows", name)
    for run in state["runs"].get(name) or []:
        if run["WorkflowRunId"] == run_id:
            return run
    raise _not_found(f"Workflow run {run_id} not found")


@_action("AWSGlue.GetWorkflowRun")
def _get_workflow_run(body):
    state = _load()
    run = _find_workflow_run(state, body["Name"], body["RunId"])
    return {
        "Run": _public_run(state, body["Name"], run, bool(body.get("IncludeGraph")))
    }


@_action("AWSGlue.GetWorkflowRuns")
def _get_workflow_runs(body):
    state = _load()
    _require(state, "workflows", body["Name"])
    runs = state["runs"].get(body["Name"]) or []
    return {
        "Runs": [
            _public_run(state, body["Name"], r, bool(body.get("IncludeGraph")))
            for r in runs
        ]
    }


@_action("AWSGlue.GetWorkflowRunProperties")
def _get_workflow_run_properties(body):
    run = _find_workflow_run(_load(), body["Name"], body["RunId"])
    return {"RunProperties": run.get("WorkflowRunProperties") or {}}


@_action("AWSGlue.PutWorkflowRunProperties")
def _put_workflow_run_properties(body):
    with _lock:
        state = _load()
        run = _find_workflow_run(state, body["Name"], body["RunId"])
        run["WorkflowRunProperties"] = {
            **(run.get("WorkflowRunProperties") or {}),
            **(body.get("RunProperties") or {}),
        }
        _save(state)
    return {}


@_action("AWSGlue.StopWorkflowRun")
def _stop_workflow_run(body):
    with _lock:
        state = _load()
        run = _find_workflow_run(state, body["Name"], body["RunId"])
        if run["Status"] != "RUNNING":
            raise GlueError(
                "IllegalWorkflowStateException", "The workflow run is not running"
            )
        run["Status"] = "STOPPING"
        _save(state)
    return {}


def _update_run(name: str, run_id: str, **fields) -> dict:
    with _lock:
        state = _load()
        run = _find_workflow_run(state, name, run_id)
        run.update(fields)
        _save(state)
        return run


def _statistics(nodes: list[dict], states: dict[str, str | None]) -> dict:
    counts = {"SUCCEEDED": 0, "FAILED": 0, "STOPPED": 0, "TIMEOUT": 0, "ERROR": 0}
    running = 0
    for n in nodes:
        s = states.get(n["id"])
        if s is None:
            running += 1
        elif s == "CANCELLED":
            counts["STOPPED"] += 1
        elif s in counts:
            counts[s] += 1
    return {
        "TotalActions": len(nodes),
        "TimeoutActions": counts["TIMEOUT"],
        "FailedActions": counts["FAILED"],
        "StoppedActions": counts["STOPPED"],
        "SucceededActions": counts["SUCCEEDED"],
        "RunningActions": running,
        "ErroredActions": counts["ERROR"],
        "WaitingActions": 0,
    }


def _drive(name: str, run_id: str, starts: list[dict]) -> None:
    """Run a workflow: fire its start triggers, then each conditional one that holds."""
    nodes: list[dict] = []
    fired: list[str] = []
    try:
        for trigger in starts:
            nodes += _fire(trigger, name, run_id)
            fired.append(trigger["Name"])
        _update_run(name, run_id, nodes=nodes, fired=fired)
        while True:
            time.sleep(0.5)
            states = {n["id"]: _node_state(n) for n in nodes}
            done: dict[tuple[str, str], str] = {
                (n["type"], n["name"]): got for n in nodes if (got := states[n["id"]])
            }
            run = _update_run(name, run_id, Statistics=_statistics(nodes, states))
            if run["Status"] == "STOPPING":
                if all(states.values()):
                    _update_run(name, run_id, Status="STOPPED", CompletedOn=time.time())
                    return
                continue
            for trigger in _load()["triggers"].values():
                if (
                    trigger.get("WorkflowName") == name
                    and trigger["Type"] == "CONDITIONAL"
                    and trigger["Name"] not in fired
                    and _holds(trigger.get("Predicate") or {}, done)
                ):
                    nodes += _fire(trigger, name, run_id)
                    fired.append(trigger["Name"])
                    _update_run(name, run_id, nodes=nodes, fired=fired)
            states = {n["id"]: _node_state(n) for n in nodes}
            if all(states.values()):
                _update_run(
                    name,
                    run_id,
                    Status="COMPLETED",
                    CompletedOn=time.time(),
                    Statistics=_statistics(nodes, states),
                )
                return
    except Exception as e:  # the run stops with the error, as Glue reports it
        _update_run(
            name,
            run_id,
            Status="ERROR",
            ErrorMessage=str(e)[:2000],
            CompletedOn=time.time(),
        )


# ---------------------------------------------------------------------------
# Activated triggers outside a run: schedules, and conditions on any job or crawl
# ---------------------------------------------------------------------------
_watcher_started = False


def start_watcher() -> None:
    """Start the loop for scheduled and standalone conditional triggers (once)."""
    global _watcher_started
    with _lock:
        if _watcher_started:
            return
        _watcher_started = True
    threading.Thread(target=_watch_loop, daemon=True).start()


def due_scheduled(
    state: dict, now: datetime.datetime, fired: dict[str, float]
) -> list[dict]:
    """Return the activated scheduled triggers whose cron matches ``now``, once a minute."""
    from oblako.engines.eventbridge.app import cron_matches

    minute = now.replace(second=0, microsecond=0).timestamp()
    due = []
    for trigger in state["triggers"].values():
        if trigger["Type"] != "SCHEDULED" or trigger.get("State") != "ACTIVATED":
            continue
        if (
            cron_matches(trigger.get("Schedule", ""), now)
            and fired.get(trigger["Name"]) != minute
        ):
            fired[trigger["Name"]] = minute
            due.append(trigger)
    return due


def _finished_since(seen: dict) -> dict[tuple[str, str], str]:
    """Job runs and crawls that finished since the last look: {(type, name): state}."""
    from oblako.engines.glue_catalog import crawlers, jobs

    done: dict[tuple[str, str], str] = {}
    for job, runs in jobs._load()["runs"].items():
        for run in runs:
            if run["JobRunState"] in _JOB_DONE and run["Id"] not in seen:
                seen[run["Id"]] = 1
                done[("JOB", job)] = run["JobRunState"]
    for name, crawler in crawlers._load()["crawlers"].items():
        last = crawler.get("LastCrawl") or {}
        key = f"crawl:{name}:{last.get('StartTime')}"
        if crawler.get("State") == "READY" and last and key not in seen:
            seen[key] = 1
            done[("CRAWLER", name)] = last.get("Status", "")
    return done


def _watch_loop() -> None:
    fired: dict[str, float] = {}
    seen: dict = {}
    _finished_since(seen)  # what finished before the engine started doesn't count
    while True:
        time.sleep(2)
        try:
            state = _load()
            now = datetime.datetime.now(datetime.timezone.utc)
            for trigger in due_scheduled(state, now, fired):
                if trigger.get("WorkflowName"):
                    _start_workflow_run({"Name": trigger["WorkflowName"]})
                else:
                    _fire(trigger, None, None)
            done = _finished_since(seen)
            if not done:
                continue
            for trigger in state["triggers"].values():
                if (
                    trigger["Type"] == "CONDITIONAL"
                    and trigger.get("State") == "ACTIVATED"
                    and not trigger.get("WorkflowName")
                    and _holds(trigger.get("Predicate") or {}, done)
                ):
                    _fire(trigger, None, None)
        except Exception as e:  # keep the loop alive
            print(f"oblako glue: triggers: {e!r}", flush=True)
