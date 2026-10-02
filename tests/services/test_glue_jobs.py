"""Unit tests for the Glue job API on the Glue engine (no container, no S3).

The runner (GlueService.submit_job) needs Docker and the ~5 GB Glue image, so it
is stubbed, as is the script download; these tests cover the API, the run states
and the arguments a script receives.
"""

from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient

from oblako.engines import glue_catalog
from oblako.engines.glue_catalog import jobs


@pytest.fixture
def glue(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "STATE", tmp_path / "jobs.json")
    monkeypatch.setattr(jobs, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(jobs, "_read_script", lambda location: "print('hi')")
    monkeypatch.setattr(jobs, "_publish_logs", lambda *a: None)
    calls = []

    def submit_job(self, script, *, args=None, env=None, timeout=600):
        calls.append({"script": script, "args": args, "timeout": timeout})
        failing = "--fail" in (args or [])
        return {
            "exit_code": 1 if failing else 0,
            "logs": "out\nValueError: bad input" if failing else "out",
            "stdout": "out",
            "stderr": "ValueError: bad input" if failing else "",
        }

    from oblako.services.glue import GlueService

    monkeypatch.setattr(GlueService, "submit_job", submit_job)
    http = TestClient(glue_catalog.create_app())

    def call(action, body):
        resp = http.post("/", json=body, headers={"X-Amz-Target": f"AWSGlue.{action}"})
        return resp.status_code, resp.json()

    call.calls = calls
    return call


def _wait(glue, name, run_id):
    for _ in range(100):
        _, body = glue("GetJobRun", {"JobName": name, "RunId": run_id})
        if body["JobRun"]["JobRunState"] in jobs._TERMINAL:
            return body["JobRun"]
        time.sleep(0.02)
    raise TimeoutError(run_id)


def _create(glue, name="etl", **extra):
    return glue(
        "CreateJob",
        {
            "Name": name,
            "Role": "arn:aws:iam::123456789012:role/glue",
            "Command": {"Name": "glueetl", "ScriptLocation": "s3://b/job.py"},
            "DefaultArguments": {"--source": "s3://b/raw/"},
            **extra,
        },
    )


def test_create_get_list(glue):
    assert _create(glue) == (200, {"Name": "etl"})
    status, body = glue("GetJob", {"JobName": "etl"})
    assert status == 200 and body["Job"]["GlueVersion"] == "5.0"
    assert glue("ListJobs", {})[1] == {"JobNames": ["etl"]}
    status, body = _create(glue)
    assert status == 400 and body["__type"] == "AlreadyExistsException"


def test_run_passes_glue_arguments_and_succeeds(glue):
    _create(glue)
    _, body = glue(
        "StartJobRun", {"JobName": "etl", "Arguments": {"--target": "s3://b/out/"}}
    )
    run = _wait(glue, "etl", body["JobRunId"])
    assert run["JobRunState"] == "SUCCEEDED"
    args = glue.calls[0]["args"]
    assert args[:4] == ["--JOB_NAME", "etl", "--JOB_RUN_ID", body["JobRunId"]]
    assert "--source" in args and "--target" in args
    assert glue.calls[0]["timeout"] == jobs.DEFAULT_TIMEOUT_MINUTES * 60


def test_failed_run_reports_the_error(glue):
    _create(glue)
    _, body = glue("StartJobRun", {"JobName": "etl", "Arguments": {"--fail": "1"}})
    run = _wait(glue, "etl", body["JobRunId"])
    assert run["JobRunState"] == "FAILED"
    assert run["ErrorMessage"] == "ValueError: bad input"


def test_unknown_job_and_delete(glue):
    status, body = glue("StartJobRun", {"JobName": "nope"})
    assert status == 400 and body["__type"] == "EntityNotFoundException"
    _create(glue)
    assert glue("DeleteJob", {"JobName": "etl"})[1] == {"JobName": "etl"}
    assert glue("ListJobs", {})[1] == {"JobNames": []}


def test_error_message_reads_either_stream():
    logs = "starting\nTraceback (most recent call last):\nKeyError: 'source'\nshutting down"
    assert jobs._error_message(logs, 1) == "KeyError: 'source'"
    assert jobs._error_message("nothing useful", 3) == "exit code 3"
