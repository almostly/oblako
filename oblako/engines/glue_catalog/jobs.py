"""Glue jobs on the Glue engine: CreateJob, StartJobRun, GetJobRun and the rest.

A job is a definition: a script in S3 (``Command.ScriptLocation``), default
arguments, a timeout. ``StartJobRun`` downloads the script from oblako's S3 and
runs it in the Glue 5 container through :class:`oblako.services.glue.GlueService`,
in a background thread, as Glue does. The script gets the same arguments Glue
passes (``--JOB_NAME``, ``--JOB_RUN_ID`` and the job's arguments), so
``getResolvedOptions`` works unchanged, and its ``s3://`` paths reach oblako's S3
through the runner's Spark settings, with no endpoint in the script.

A run's output goes where Glue sends it: the script's stdout to the CloudWatch
Logs group ``/aws-glue/jobs/output`` and stderr to ``/aws-glue/jobs/error``, one
stream per run id (in moto, when it is running), and to
``~/.oblako/logs/glue-jobs/<run id>.log``.

Definitions and runs are kept in ``~/.oblako/glue/jobs.json``.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from oblako import ports
from oblako.engines.glue_catalog import GlueError, _action, _not_found

STATE = Path.home() / ".oblako" / "glue" / "jobs.json"
LOG_DIR = Path.home() / ".oblako" / "logs" / "glue-jobs"
DEFAULT_TIMEOUT_MINUTES = 2880  # Glue's default: 48 hours
_TERMINAL = ("SUCCEEDED", "FAILED", "TIMEOUT", "STOPPED", "ERROR")

_lock = threading.RLock()


# ---------------------------------------------------------------------------
# State: {"jobs": {name: job}, "runs": {name: [run, ...]}}
# ---------------------------------------------------------------------------
def _load() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"jobs": {}, "runs": {}}


def _save(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, default=str))
    tmp.replace(STATE)


def _require_job(state: dict, name: str) -> dict:
    job = state["jobs"].get(name)
    if job is None:
        raise _not_found(f"Job {name} not found.")
    return job


def _update_run(job_name: str, run_id: str, **fields) -> None:
    with _lock:
        state = _load()
        for run in state["runs"].get(job_name, []):
            if run["Id"] == run_id:
                run.update(fields, LastModifiedOn=time.time())
        _save(state)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
_JOB_FIELDS = (
    "Description",
    "Role",
    "Command",
    "DefaultArguments",
    "NonOverridableArguments",
    "MaxRetries",
    "Timeout",
    "GlueVersion",
    "WorkerType",
    "NumberOfWorkers",
    "MaxCapacity",
    "ExecutionProperty",
)


@_action("AWSGlue.CreateJob")
def _create_job(body):
    name = body["Name"]
    if not (body.get("Command") or {}).get("ScriptLocation"):
        raise GlueError("InvalidInputException", "Command.ScriptLocation is required.")
    with _lock:
        state = _load()
        if name in state["jobs"]:
            raise GlueError("AlreadyExistsException", f"Job {name} already exists.")
        now = time.time()
        job = {k: body[k] for k in _JOB_FIELDS if k in body}
        job.update(Name=name, CreatedOn=now, LastModifiedOn=now)
        job.setdefault("GlueVersion", "5.0")
        job.setdefault("Timeout", DEFAULT_TIMEOUT_MINUTES)
        state["jobs"][name] = job
        _save(state)
    return {"Name": name}


@_action("AWSGlue.GetJob")
def _get_job(body):
    return {"Job": _require_job(_load(), body["JobName"])}


@_action("AWSGlue.GetJobs")
def _get_jobs(_body):
    return {"Jobs": sorted(_load()["jobs"].values(), key=lambda j: j["Name"])}


@_action("AWSGlue.ListJobs")
def _list_jobs(_body):
    return {"JobNames": sorted(_load()["jobs"])}


@_action("AWSGlue.UpdateJob")
def _update_job(body):
    name = body["JobName"]
    with _lock:
        state = _load()
        job = _require_job(state, name)
        update = body.get("JobUpdate") or {}
        job.update({k: update[k] for k in _JOB_FIELDS if k in update})
        job["LastModifiedOn"] = time.time()
        _save(state)
    return {"JobName": name}


@_action("AWSGlue.DeleteJob")
def _delete_job(body):
    name = body["JobName"]
    with _lock:
        state = _load()
        state["jobs"].pop(name, None)
        state["runs"].pop(name, None)
        _save(state)
    return {"JobName": name}  # idempotent, as on Glue


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
@_action("AWSGlue.StartJobRun")
def _start_job_run(body):
    name = body["JobName"]
    with _lock:
        state = _load()
        job = _require_job(state, name)
        run_id = "jr_" + uuid.uuid4().hex + uuid.uuid4().hex[:32]
        arguments = {
            **(job.get("DefaultArguments") or {}),
            **(body.get("Arguments") or {}),
        }
        arguments.update(job.get("NonOverridableArguments") or {})
        timeout = int(
            body.get("Timeout") or job.get("Timeout") or DEFAULT_TIMEOUT_MINUTES
        )
        now = time.time()
        run = {
            "Id": run_id,
            "Attempt": 0,
            "JobName": name,
            "StartedOn": now,
            "LastModifiedOn": now,
            "JobRunState": "STARTING",
            "Arguments": arguments,
            "Timeout": timeout,
            "GlueVersion": job.get("GlueVersion", "5.0"),
            "ExecutionTime": 0,
            "LogGroupName": "/aws-glue/jobs",
        }
        state["runs"].setdefault(name, []).insert(0, run)
        _save(state)
    script_location = job["Command"]["ScriptLocation"]
    threading.Thread(
        target=_execute,
        args=(name, run_id, script_location, arguments, timeout),
        daemon=True,
    ).start()
    return {"JobRunId": run_id}


def _find_run(name: str, run_id: str) -> dict:
    state = _load()
    _require_job(state, name)
    for run in state["runs"].get(name, []):
        if run["Id"] == run_id:
            return run
    raise _not_found(f"Job run {run_id} not found.")


@_action("AWSGlue.GetJobRun")
def _get_job_run(body):
    return {"JobRun": _find_run(body["JobName"], body["RunId"])}


@_action("AWSGlue.GetJobRuns")
def _get_job_runs(body):
    state = _load()
    _require_job(state, body["JobName"])
    return {"JobRuns": state["runs"].get(body["JobName"], [])}


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _argv(name: str, run_id: str, arguments: dict) -> list[str]:
    """Build the script's arguments as Glue passes them: ``--key value`` pairs."""
    argv = ["--JOB_NAME", name, "--JOB_RUN_ID", run_id]
    for key, value in arguments.items():
        argv += [key if key.startswith("--") else f"--{key}", str(value)]
    return argv


def _read_script(location: str) -> str:
    from oblako.services.boto import client

    bucket, _, key = location.removeprefix("s3://").partition("/")
    s3 = client("s3", f"http://localhost:{ports.S3}")
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()


def _error_message(stderr: str, exit_code: int) -> str:
    lines = [ln for ln in stderr.splitlines() if "Error" in ln or "Exception" in ln]
    return (lines[-1].strip() if lines else f"exit code {exit_code}")[:2000]


def _publish_logs(run_id: str, stdout: str, stderr: str) -> None:
    """Send the run's streams to CloudWatch Logs in moto, as Glue does (best effort)."""
    try:
        from oblako.services.boto import client

        logs = client("logs", f"http://localhost:{ports.MOTO}")
        for group, text in (
            ("/aws-glue/jobs/output", stdout),
            ("/aws-glue/jobs/error", stderr),
        ):
            try:
                logs.create_log_group(logGroupName=group)
            except logs.exceptions.ResourceAlreadyExistsException:
                pass
            logs.create_log_stream(logGroupName=group, logStreamName=run_id)
            now = int(time.time() * 1000)
            events = [
                {"timestamp": now, "message": line}
                for line in text.splitlines()
                if line.strip()
            ][-5000:]
            if events:
                logs.put_log_events(
                    logGroupName=group, logStreamName=run_id, logEvents=events
                )
    except Exception:
        pass  # moto not running: the log file still has everything


def _execute(name: str, run_id: str, location: str, arguments: dict, timeout: int):
    from oblako.services.glue import GlueService

    started = time.time()
    _update_run(name, run_id, JobRunState="RUNNING")
    try:
        script = _read_script(location)
        result = GlueService().submit_job(
            script, args=_argv(name, run_id, arguments), timeout=timeout * 60
        )
    except Exception as err:
        state = "TIMEOUT" if "timed out" in str(err).lower() else "FAILED"
        _update_run(
            name,
            run_id,
            JobRunState=state,
            ErrorMessage=str(err)[:2000],
            CompletedOn=time.time(),
            ExecutionTime=int(time.time() - started),
        )
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / f"{run_id}.log").write_text(result["logs"])
    _publish_logs(run_id, result.get("stdout", ""), result.get("stderr", ""))
    ok = result["exit_code"] == 0
    fields = {
        "JobRunState": "SUCCEEDED" if ok else "FAILED",
        "CompletedOn": time.time(),
        "ExecutionTime": int(time.time() - started),
    }
    if not ok:
        fields["ErrorMessage"] = _error_message(
            result.get("stderr", result["logs"]), result["exit_code"]
        )
    _update_run(name, run_id, **fields)
