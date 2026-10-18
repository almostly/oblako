"""Run a Glue job script with Glue's job bookmarks, which the local Glue libs lack.

oblako's Glue engine starts each job as ``spark-submit run_job.py <args>``; this
module patches awsglue, then runs the job's script (``/scripts/job.py``) as
``__main__`` with the same arguments.

With ``--job-bookmark-option job-bookmark-enable``, a read that names
``jobBookmarkKeys`` (``create_dynamic_frame.from_options`` or ``from_catalog``,
with a ``transformation_ctx``) returns only the rows past the bookmark, as Glue
does for JDBC sources: keys greater than the last committed values
(``jobBookmarkKeysSortOrder`` ``asc``, the default) or less (``desc``), compared
as a tuple when there are several keys, and no further than the values this run
saw. ``job.commit()`` saves them through the engine. ``job-bookmark-pause`` reads
past the bookmark without moving it; ``job-bookmark-disable``, Glue's default,
reads everything.
"""

import datetime
import decimal
import inspect
import json
import os
import runpy
import sys
import urllib.error
import urllib.request

from awsglue.context import GlueContext
from awsglue.dynamicframe import DynamicFrame
from awsglue.job import Job
from pyspark.sql import functions as F

SCRIPT = "/scripts/job.py"


def _argument(name: str, default: str = "") -> str:
    """Return a ``--name value`` argument of the run."""
    argv = sys.argv[1:]
    for i, arg in enumerate(argv[:-1]):
        if arg == f"--{name}":
            return argv[i + 1]
    return default


OPTION = _argument("job-bookmark-option", "job-bookmark-disable")
_committed: dict = {}
_pending: dict = {}


def _glue(action: str, body: dict) -> dict:
    """Call an action on oblako's Glue engine."""
    request = urllib.request.Request(
        os.environ["AWS_ENDPOINT_URL_GLUE"],
        data=json.dumps(body).encode(),
        headers={
            "X-Amz-Target": f"AWSGlue.{action}",
            "Content-Type": "application/x-amz-json-1.1",
        },
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read() or b"{}")


def _plain(value):
    """Return a key's value as JSON can hold it; a cast brings it back."""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    return value


def _beyond(df, keys: list, values: list, op: str):
    """Return the condition that the keys, as a tuple, compare ``op`` to the values."""
    cols = [F.col(k) for k in keys]
    lits = [F.lit(v).cast(df.schema[k].dataType) for k, v in zip(keys, values)]
    condition = None
    for i in range(len(keys)):
        term = getattr(cols[i], op)(lits[i])
        for j in range(i):
            term = term & (cols[j] == lits[j])
        condition = term if condition is None else condition | term
    return condition


def _since_bookmark(frame, ctx: str, options: dict):
    """Return the frame's rows past the bookmark, noting how far this run reads."""
    keys = options.get("jobBookmarkKeys")
    if OPTION == "job-bookmark-disable" or not keys or not ctx:
        return frame
    keys = list(keys)
    desc = str(options.get("jobBookmarkKeysSortOrder", "asc")).lower() == "desc"
    df = frame.toDF()
    seen = _committed.get(ctx)
    if seen and seen["keys"] == keys:
        df = df.filter(
            _beyond(df, keys, seen["values"], "__lt__" if desc else "__gt__")
        )
    order = [F.col(k).asc() if desc else F.col(k).desc() for k in keys]
    last = df.select(*keys).dropna().orderBy(*order).first()
    if last is None:
        return DynamicFrame.fromDF(df.limit(0), frame.glue_ctx, frame.name)
    values = [_plain(v) for v in last]
    # no further than what this read saw, so rows arriving meanwhile wait for the next run
    df = df.filter(~_beyond(df, keys, values, "__gt__" if not desc else "__lt__"))
    _pending[ctx] = {"keys": keys, "values": values}
    return DynamicFrame.fromDF(df, frame.glue_ctx, frame.name)


def _bookmarked(read, options_parameter: str):
    """Wrap a GlueContext read so it honors the job's bookmark."""
    signature = inspect.signature(read)

    def wrapper(self, *args, **kwargs):
        """Read as Glue does, then keep only the rows past the bookmark."""
        frame = read(self, *args, **kwargs)
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        return _since_bookmark(
            frame,
            bound.arguments.get("transformation_ctx") or "",
            bound.arguments.get(options_parameter) or {},
        )

    return wrapper


_init = Job.init
_commit = Job.commit


def _job_init(self, job_name, args=None):
    """Start the job and load its bookmark from the engine."""
    # the bookmarks are kept here, so the local Glue libs never see the option
    _init(
        self, job_name, {**(args or {}), "job_bookmark_option": "job-bookmark-disable"}
    )
    self._oblako_name = job_name
    if OPTION != "job-bookmark-disable":
        try:
            entry = _glue("GetJobBookmark", {"JobName": job_name})["JobBookmarkEntry"]
            _committed.update(json.loads(entry.get("JobBookmark") or "{}"))
        except (urllib.error.HTTPError, KeyError):
            pass  # no bookmark yet: the first run reads everything


def _job_commit(self):
    """Commit the job and save how far its reads went."""
    _commit(self)
    if OPTION == "job-bookmark-enable" and _pending:
        _glue(
            "OblakoCommitJobBookmark",
            {
                "JobName": getattr(self, "_oblako_name", _argument("JOB_NAME")),
                "RunId": _argument("JOB_RUN_ID"),
                "JobBookmark": json.dumps({**_committed, **_pending}),
            },
        )
        _committed.update(_pending)
        _pending.clear()


GlueContext.create_dynamic_frame_from_options = _bookmarked(
    GlueContext.create_dynamic_frame_from_options, "connection_options"
)
GlueContext.create_dynamic_frame_from_catalog = _bookmarked(
    GlueContext.create_dynamic_frame_from_catalog, "additional_options"
)
Job.init = _job_init
Job.commit = _job_commit

sys.argv[0] = SCRIPT
runpy.run_path(SCRIPT, run_name="__main__")
