"""Unit tests for GlueService.run_workflow orchestration.

The runner (submit_job) needs the ~5 GB Glue image + Docker, so we stub it and
test the sequencing/gating logic directly: a step runs only if its predecessors
succeeded, and the first failure stops the pipeline (the rest are SKIPPED).
"""

from __future__ import annotations

from oblako.services.glue import GlueService


def _stub_runner(exit_codes):
    """Return a submit_job stub that yields the given exit codes in order."""
    calls = iter(exit_codes)

    def submit_job(script, *, args=None, env=None, timeout=600):
        return {"exit_code": next(calls), "logs": f"ran: {script[:20]}"}

    return submit_job


def test_workflow_all_succeed():
    g = GlueService()
    g.submit_job = _stub_runner([0, 0, 0])
    steps = [{"name": n, "script": f"print('{n}')"} for n in ("a", "b", "c")]
    result = g.run_workflow("wf", steps)
    assert result["status"] == "SUCCEEDED"
    assert [s["status"] for s in result["steps"]] == ["SUCCEEDED"] * 3


def test_workflow_stops_after_failure():
    g = GlueService()
    g.submit_job = _stub_runner([0, 1])  # second step fails; third never runs
    steps = [{"name": n, "script": f"print('{n}')"} for n in ("a", "b", "c")]
    result = g.run_workflow("wf", steps)
    assert result["status"] == "FAILED"
    assert [s["status"] for s in result["steps"]] == ["SUCCEEDED", "FAILED", "SKIPPED"]
    assert result["steps"][1]["exitCode"] == 1


def test_workflow_empty_script_step_fails():
    g = GlueService()
    g.submit_job = _stub_runner([0])  # only the first step ever calls the runner
    steps = [
        {"name": "a", "script": "print('a')"},
        {"name": "b", "script": "   "},  # empty -> FAILED, not run
        {"name": "c", "script": "print('c')"},
    ]
    result = g.run_workflow("wf", steps)
    assert [s["status"] for s in result["steps"]] == ["SUCCEEDED", "FAILED", "SKIPPED"]
    assert result["status"] == "FAILED"


def test_workflow_runner_exception_is_captured():
    g = GlueService()

    def boom(script, *, args=None, env=None, timeout=600):
        raise RuntimeError("docker unavailable")

    g.submit_job = boom
    result = g.run_workflow("wf", [{"name": "a", "script": "print('a')"}])
    assert result["status"] == "FAILED"
    assert result["steps"][0]["status"] == "FAILED"
    assert "docker unavailable" in result["steps"][0]["logs"]
