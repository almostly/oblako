"""Bedrock batch model-invocation jobs, backed by S3 (S3Proxy) + the adapter.

A job reads JSONL records from an S3 input location, runs each record's
``modelInput`` through the local Bedrock engine, and writes JSONL results to an
S3 output location — mirroring real Bedrock batch inference, but local.
"""

from __future__ import annotations

import builtins
import datetime
import json
import threading
import uuid

from .adapter import BedrockAdapter


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """'s3://bucket/prefix/' -> ('bucket', 'prefix/')."""
    rest = uri.split("s3://", 1)[-1]
    bucket, _, prefix = rest.partition("/")
    return bucket, prefix


class ModelInvocationJob:
    """A single batch job. Runs in a daemon thread; status tracked on `details`."""

    def __init__(self, details: dict, adapter: BedrockAdapter, s3_factory):
        """Initialize with job details dict, the Bedrock adapter, and an S3 client factory."""
        self.details = details
        self.adapter = adapter
        self._s3_factory = s3_factory
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def job_id(self) -> str:
        """Return the short job ID extracted from the job ARN."""
        return self.details["jobArn"].split("/")[-1]

    def start(self) -> None:
        """Launch the job in a background daemon thread."""
        self._thread = threading.Thread(
            target=self._run, name=f"bedrock-job-{self.job_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the job to stop and update its status to Stopping."""
        self._stop.set()
        self.details["status"] = "Stopping"

    def _touch(self) -> None:
        self.details["lastModifiedTime"] = datetime.datetime.now(datetime.timezone.utc)

    def _run(self) -> None:
        self.details["status"] = "InProgress"
        self._touch()
        try:
            s3 = self._s3_factory()
            in_bucket, in_prefix = _parse_s3_uri(
                self.details["inputDataConfig"]["s3InputDataConfig"]["s3Uri"]
            )
            out_bucket, out_prefix = _parse_s3_uri(
                self.details["outputDataConfig"]["s3OutputDataConfig"]["s3Uri"]
            )
            model_id = self.details["modelId"].split("/")[-1]

            listing = s3.list_objects_v2(Bucket=in_bucket, Prefix=in_prefix)
            total = processed = success = errors = 0
            for obj in listing.get("Contents", []):
                if self._stop.is_set():
                    break
                key = obj["Key"]
                if not key.endswith(".jsonl"):
                    continue
                raw = (
                    s3.get_object(Bucket=in_bucket, Key=key)["Body"]
                    .read()
                    .decode("utf-8")
                )
                out_lines = []
                for i, line in enumerate(raw.splitlines()):
                    line = line.strip()
                    if not line:
                        continue
                    total += 1
                    record = json.loads(line)
                    record_id = record.get("recordId", str(i))
                    try:
                        model_output = self.adapter.invoke_model(
                            model_id, json.dumps(record.get("modelInput", record))
                        )
                        out_lines.append(
                            json.dumps(
                                {
                                    "recordId": record_id,
                                    "modelInput": record.get("modelInput"),
                                    "modelOutput": model_output,
                                }
                            )
                        )
                        success += 1
                    except Exception as e:  # record-level error
                        out_lines.append(
                            json.dumps(
                                {
                                    "recordId": record_id,
                                    "modelInput": record.get("modelInput"),
                                    "error": str(e),
                                }
                            )
                        )
                        errors += 1
                    processed += 1
                base = key.rsplit("/", 1)[-1]
                out_key = f"{out_prefix}{self.job_id}/{base}.out"
                s3.put_object(
                    Bucket=out_bucket,
                    Key=out_key,
                    Body="\n".join(out_lines).encode("utf-8"),
                )

            self.details["totalRecordCount"] = total
            self.details["processedRecordCount"] = processed
            self.details["successRecordCount"] = success
            self.details["errorRecordCount"] = errors
            self.details["status"] = "Stopped" if self._stop.is_set() else "Completed"
        except Exception as e:  # job-level failure
            self.details["status"] = "Failed"
            self.details["message"] = str(e)
        finally:
            self.details["endTime"] = datetime.datetime.now(datetime.timezone.utc)
            self._touch()


class JobStore:
    """In-memory store of model-invocation jobs."""

    def __init__(self):
        """Initialize an empty in-memory job store."""
        self._jobs: dict[str, ModelInvocationJob] = {}
        self._lock = threading.Lock()

    def create(
        self, details: dict, adapter: BedrockAdapter, s3_factory
    ) -> ModelInvocationJob:
        """Create and register a new ModelInvocationJob from the given details."""
        job = ModelInvocationJob(details, adapter, s3_factory)
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> ModelInvocationJob | None:
        """Return the job with the given ID, or None if not found."""
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> builtins.list[ModelInvocationJob]:
        """Return all registered jobs."""
        with self._lock:
            return list(self._jobs.values())


def new_job_details(
    *,
    job_name: str,
    model_id: str,
    role_arn: str,
    input_config: dict,
    output_config: dict,
    region: str = "us-east-1",
    account_id: str = "000000000000",
    client_request_token: str | None = None,
    timeout_hours: int | None = None,
) -> dict:
    """Build the GetModelInvocationJob-shaped details dict for a new job."""
    job_uid = uuid.uuid4().hex[:16]
    now = datetime.datetime.now(datetime.timezone.utc)
    return {
        "jobArn": f"arn:aws:bedrock:{region}:{account_id}:model-invocation-job/{job_uid}",
        "jobName": job_name,
        "modelId": f"arn:aws:bedrock:{region}::foundation-model/{model_id}",
        "clientRequestToken": client_request_token or uuid.uuid4().hex,
        "roleArn": role_arn,
        "status": "Submitted",
        "submitTime": now,
        "lastModifiedTime": now,
        "inputDataConfig": input_config,
        "outputDataConfig": output_config,
        "timeoutDurationInHours": timeout_hours or 24,
        "totalRecordCount": 0,
        "processedRecordCount": 0,
        "successRecordCount": 0,
        "errorRecordCount": 0,
    }
