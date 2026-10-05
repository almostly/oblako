"""Glue triggers and workflows on the Glue engine (no container, no S3).

Job runs go through a stubbed runner (``--fail`` makes one fail), and the crawler
has no targets, so a crawl finishes at once.
"""

from __future__ import annotations

import datetime
import json
import time

import pytest
from starlette.testclient import TestClient

from oblako.engines import glue_catalog
from oblako.engines.glue_catalog import crawlers, jobs, workflows
from oblako.engines.glue_catalog.store import GlueStore


class _Glue:
    """Call Glue actions on the in-process engine; ``calls`` are the job runs' argv."""

    def __init__(self, client: TestClient, calls: list):
        self.client, self.calls = client, calls

    def __call__(self, action: str, body: dict) -> dict:
        resp = self.client.post(
            "/", content=json.dumps(body), headers={"X-Amz-Target": f"AWSGlue.{action}"}
        )
        assert resp.status_code == 200, f"{action}: {resp.text}"
        return resp.json()


@pytest.fixture
def glue(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "STATE", tmp_path / "jobs.json")
    monkeypatch.setattr(jobs, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(crawlers, "STATE", tmp_path / "crawlers.json")
    monkeypatch.setattr(workflows, "STATE", tmp_path / "workflows.json")
    monkeypatch.setattr(glue_catalog, "_stores", [GlueStore(":memory:")])
    # no Iceberg REST catalog: these tests make Hive-style tables only
    import httpx

    monkeypatch.setattr(
        glue_catalog.httpx,
        "request",
        lambda *a, **k: httpx.Response(404, json={"error": {"message": "none"}}),
    )
    monkeypatch.setattr(jobs, "_read_script", lambda location: "print('ok')")
    monkeypatch.setattr(jobs, "_publish_logs", lambda *a: None)

    calls: list = []

    def submit_job(self, script, *, args=None, env=None, timeout=600):
        calls.append(args)
        failed = "--fail" in (args or [])
        return {
            "exit_code": 1 if failed else 0,
            "logs": "Error: boom" if failed else "",
            "stdout": "",
            "stderr": "",
        }

    from oblako.services.glue import GlueService

    monkeypatch.setattr(GlueService, "submit_job", submit_job)
    call = _Glue(TestClient(glue_catalog.create_app()), calls)
    for name in ("extract", "load", "alert"):
        call(
            "CreateJob",
            {
                "Name": name,
                "Role": "r",
                "Command": {"Name": "glueetl", "ScriptLocation": "s3://b/s.py"},
            },
        )
    call("CreateDatabase", {"DatabaseInput": {"Name": "lake"}})
    call(
        "CreateCrawler",
        {
            "Name": "catalog",
            "Role": "r",
            "DatabaseName": "lake",
            "Targets": {"S3Targets": []},
        },
    )
    return call


def _pipeline(call, extract_args=None):
    call("CreateWorkflow", {"Name": "nightly", "DefaultRunProperties": {"env": "dev"}})
    call(
        "CreateTrigger",
        {
            "Name": "start",
            "WorkflowName": "nightly",
            "Type": "ON_DEMAND",
            "Actions": [{"JobName": "extract", "Arguments": extract_args or {}}],
        },
    )
    call(
        "CreateTrigger",
        {
            "Name": "on-extract",
            "WorkflowName": "nightly",
            "Type": "CONDITIONAL",
            "Predicate": {
                "Conditions": [
                    {
                        "LogicalOperator": "EQUALS",
                        "JobName": "extract",
                        "State": "SUCCEEDED",
                    }
                ]
            },
            "Actions": [{"JobName": "load"}, {"CrawlerName": "catalog"}],
        },
    )
    call(
        "CreateTrigger",
        {
            "Name": "on-failure",
            "WorkflowName": "nightly",
            "Type": "CONDITIONAL",
            "Predicate": {
                "Logical": "ANY",
                "Conditions": [
                    {
                        "LogicalOperator": "EQUALS",
                        "JobName": "extract",
                        "State": "FAILED",
                    }
                ],
            },
            "Actions": [{"JobName": "alert"}],
        },
    )


def _finish(call, run_id):
    for _ in range(200):
        run = call("GetWorkflowRun", {"Name": "nightly", "RunId": run_id})["Run"]
        if run["Status"] != "RUNNING":
            return run
        time.sleep(0.05)
    raise AssertionError("workflow run never finished")


def _ran(call):
    return [args[1] for args in call.calls]  # --JOB_NAME <name>


def test_workflow_follows_its_graph(glue):
    _pipeline(glue)
    run_id = glue(
        "StartWorkflowRun", {"Name": "nightly", "RunProperties": {"day": "2026-10-05"}}
    )["RunId"]
    run = _finish(glue, run_id)
    assert run["Status"] == "COMPLETED"
    assert sorted(_ran(glue)) == ["extract", "load"]  # not the failure branch
    stats = run["Statistics"]
    assert (
        stats["TotalActions"],
        stats["SucceededActions"],
        stats["FailedActions"],
    ) == (3, 3, 0)
    extract = glue.calls[0]
    assert extract[extract.index("--WORKFLOW_NAME") + 1] == "nightly"
    assert extract[extract.index("--WORKFLOW_RUN_ID") + 1] == run_id
    props = glue("GetWorkflowRunProperties", {"Name": "nightly", "RunId": run_id})[
        "RunProperties"
    ]
    assert props == {"env": "dev", "day": "2026-10-05"}
    crawler = glue("GetCrawler", {"Name": "catalog"})["Crawler"]
    assert crawler["LastCrawl"]["Status"] == "SUCCEEDED"


def test_workflow_failure_branch(glue):
    _pipeline(glue, extract_args={"--fail": "1"})
    run = _finish(glue, glue("StartWorkflowRun", {"Name": "nightly"})["RunId"])
    assert run["Status"] == "COMPLETED"
    assert sorted(_ran(glue)) == ["alert", "extract"]
    assert run["Statistics"]["FailedActions"] == 1


def test_workflow_graph(glue):
    _pipeline(glue)
    wf = glue("GetWorkflow", {"Name": "nightly", "IncludeGraph": True})["Workflow"]
    nodes = {n["UniqueId"]: n["Type"] for n in wf["Graph"]["Nodes"]}
    assert nodes["trigger_start"] == "TRIGGER" and nodes["crawler_catalog"] == "CRAWLER"
    edges = {(e["SourceId"], e["DestinationId"]) for e in wf["Graph"]["Edges"]}
    assert ("trigger_start", "job_extract") in edges
    assert ("job_extract", "trigger_on-extract") in edges
    assert ("trigger_on-extract", "job_load") in edges


def test_standalone_conditional_trigger(glue):
    glue(
        "CreateTrigger",
        {
            "Name": "after-extract",
            "Type": "CONDITIONAL",
            "StartOnCreation": True,
            "Predicate": {
                "Conditions": [
                    {
                        "LogicalOperator": "EQUALS",
                        "JobName": "extract",
                        "State": "SUCCEEDED",
                    }
                ]
            },
            "Actions": [{"JobName": "load"}],
        },
    )
    assert (
        glue("GetTrigger", {"Name": "after-extract"})["Trigger"]["State"] == "ACTIVATED"
    )
    glue("StartJobRun", {"JobName": "extract"})
    for _ in range(100):
        if "load" in _ran(glue):
            break
        time.sleep(0.1)
    assert "load" in _ran(glue)


def test_on_demand_trigger_and_trigger_api(glue):
    glue(
        "CreateTrigger",
        {"Name": "now", "Type": "ON_DEMAND", "Actions": [{"JobName": "load"}]},
    )
    glue("StartTrigger", {"Name": "now"})
    for _ in range(50):
        if "load" in _ran(glue):
            break
        time.sleep(0.05)
    assert "load" in _ran(glue)
    assert glue("ListTriggers", {"DependentJobName": "load"})["TriggerNames"] == ["now"]
    glue("DeleteTrigger", {"Name": "now"})
    assert glue("ListTriggers", {})["TriggerNames"] == []


def test_scheduled_triggers_fire_once_a_minute():
    state = {
        "triggers": {
            "nightly": {
                "Name": "nightly",
                "Type": "SCHEDULED",
                "State": "ACTIVATED",
                "Schedule": "cron(0 2 * * ? *)",
            },
            "off": {
                "Name": "off",
                "Type": "SCHEDULED",
                "State": "DEACTIVATED",
                "Schedule": "cron(0 2 * * ? *)",
            },
        }
    }
    two = datetime.datetime(2026, 10, 5, 2, 0, 10, tzinfo=datetime.timezone.utc)
    fired: dict = {}
    assert [t["Name"] for t in workflows.due_scheduled(state, two, fired)] == [
        "nightly"
    ]
    assert workflows.due_scheduled(state, two, fired) == []
