"""SageMaker local mode service: training, endpoints, and processing via Docker."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

import docker
from docker.errors import NotFound

# A "Studio domain" notebook instance runs a Jupyter-capable image. The default
# is oblako's own slim image (JupyterLab + boto3 pre-wired), built on demand from
# oblako/images/notebook. Override with OBLAKO_NOTEBOOK_IMAGE (any pullable image).
OBLAKO_NOTEBOOK_IMAGE = "oblako/sagemaker-notebook:latest"
NOTEBOOK_IMAGE = os.environ.get("OBLAKO_NOTEBOOK_IMAGE", OBLAKO_NOTEBOOK_IMAGE)
NOTEBOOK_PORT = 8889  # host port the in-instance JupyterLab is published on


def _domain_stack(name: str) -> str:
    return f"oblako-sagemaker-{name}"


class SageMakerService:
    """Local SageMaker execution: oblako drives Docker directly.

    Rather than depending on the SageMaker SDK's local mode, oblako builds and
    runs the training/inference containers itself against SageMaker's ``/opt/ml``
    contract (see ``run_training``), so it's independent of the SDK version. This
    class also manages images, container status, and cleanup, and the Studio
    domain (composed from CloudFormation + EC2).
    """

    def __init__(self):
        """Initialize SageMaker local mode with a deferred Docker client."""
        self._client: docker.DockerClient | None = None

    @property
    def client(self) -> docker.DockerClient:
        """Return (or lazily create) the Docker client for the configured backend."""
        if self._client is None:
            from .backends import docker_client

            self._client = docker_client()
        return self._client

    def get_session(self):
        """Return a SageMaker LocalSession (client helper).

        v3 relocated it from ``sagemaker.local`` to ``sagemaker.core.local``.
        oblako's own execution no longer uses it (see ``run_training``); this
        stays for client code that wants the SDK's local session.
        """
        from sagemaker.core.local import LocalSession

        return LocalSession()

    def get_client(self):
        """Return a boto3 ``sagemaker`` client wired to the local control plane.

        Auto-starts the in-process server (``ports.SAGEMAKER``) that answers the
        boto3 ``sagemaker`` API and runs training jobs locally in Docker, so
        unmodified boto3 / SageMaker SDK code targets oblako.
        """
        import boto3

        from oblako import ports
        from oblako.engines import sagemaker as sagemaker_engine

        sagemaker_engine.start_in_thread(port=ports.SAGEMAKER)
        return boto3.client(
            "sagemaker",
            endpoint_url=f"http://localhost:{ports.SAGEMAKER}",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
        )

    def get_runtime_client(self):
        """Return a boto3 ``sagemaker-runtime`` client wired to the local server.

        ``invoke_endpoint`` is proxied to the local serving container that
        ``create_endpoint`` started.
        """
        import boto3

        from oblako import ports
        from oblako.engines import sagemaker as sagemaker_engine

        sagemaker_engine.start_in_thread(port=ports.SAGEMAKER)
        return boto3.client(
            "sagemaker-runtime",
            endpoint_url=f"http://localhost:{ports.SAGEMAKER}",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
        )

    def get_featurestore_runtime_client(self):
        """Return a boto3 ``sagemaker-featurestore-runtime`` client (local).

        ``put_record`` / ``get_record`` hit the local Feature Store: an in-process
        online store plus an S3 Parquet offline store.
        """
        import boto3

        from oblako import ports
        from oblako.engines import sagemaker as sagemaker_engine

        sagemaker_engine.start_in_thread(port=ports.SAGEMAKER)
        return boto3.client(
            "sagemaker-featurestore-runtime",
            endpoint_url=f"http://localhost:{ports.SAGEMAKER}",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
        )

    def build_image(self, path: str, tag: str) -> str:
        """Build a training/inference Docker image."""
        image, logs = self.client.images.build(path=path, tag=tag)
        for chunk in logs:
            if "stream" in chunk:
                print(chunk["stream"], end="")
        return image.tags[0]

    def run_training(
        self,
        image: str,
        channels: dict[str, str],
        hyperparameters: dict | None = None,
        environment: dict | None = None,
        gpus: bool = False,
        timeout: int = 1800,
        return_logs: bool = False,
        on_container=None,
    ) -> dict[str, bytes] | tuple[dict[str, bytes], str]:
        """Run a SageMaker training container per the ``/opt/ml`` contract.

        oblako drives Docker itself (no SageMaker SDK), so it works the same on
        Docker Desktop and Linux and doesn't depend on the SDK's local mode.
        Input is copied *into* the container and the model copied *out* with the
        ``docker cp`` mechanism (``put_archive``/``get_archive``), so there are no
        host bind-mounts to share.

        ``channels`` maps a channel name to a local directory whose files are
        placed under ``/opt/ml/input/data/<channel>/``. ``hyperparameters`` is
        written (values stringified) to ``/opt/ml/input/config/hyperparameters.json``
        as the container expects. Returns the collected ``/opt/ml/model`` as a
        ``{relative_path: bytes}`` map. Raises on a non-zero exit. With
        ``return_logs=True`` returns ``(files, container_logs)`` instead, so a
        caller (e.g. automatic model tuning) can scrape an objective metric from
        the container's stdout via a ``MetricDefinitions`` regex. ``on_container``,
        if given, is called with the container right after it is created (before
        it starts), so a caller can record it and later ``StopTrainingJob`` by
        killing it.
        """
        import io
        import json
        import tarfile
        import uuid

        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as tar:
            hp = json.dumps(
                {k: str(v) for k, v in (hyperparameters or {}).items()}
            ).encode()
            info = tarfile.TarInfo("opt/ml/input/config/hyperparameters.json")
            info.size = len(hp)
            tar.addfile(info, io.BytesIO(hp))
            for channel, directory in channels.items():
                for entry in sorted(os.listdir(directory)):
                    fpath = os.path.join(directory, entry)
                    if not os.path.isfile(fpath):
                        continue
                    with open(fpath, "rb") as fh:
                        data = fh.read()
                    info = tarfile.TarInfo(f"opt/ml/input/data/{channel}/{entry}")
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
        payload.seek(0)

        # instance_type="local_gpu" -> request all GPUs (nvidia-docker). Real on
        # Linux+NVIDIA; on a host without it Docker rejects it, which surfaces as a
        # clear training error rather than a silent CPU run.
        device_requests = (
            [docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])]
            if gpus
            else None
        )
        container = self.client.containers.create(
            image,
            environment=environment or {},
            name=f"sagemaker-local-train-{uuid.uuid4().hex[:12]}",
            detach=True,
            device_requests=device_requests,
        )
        try:
            if on_container is not None:
                on_container(container)
            container.put_archive("/", payload.getvalue())
            container.start()
            result = container.wait(timeout=timeout)
            code = result.get("StatusCode", 1)
            logs = container.logs().decode("utf-8", "replace")
            if code != 0:
                raise RuntimeError(f"training container exited {code}:\n{logs[-4000:]}")
            bits, _ = container.get_archive("/opt/ml/model")
            model_tar = io.BytesIO(b"".join(bits))
            model_tar.seek(0)
            files: dict[str, bytes] = {}
            with tarfile.open(fileobj=model_tar) as tar:
                for member in tar.getmembers():
                    if not member.isfile():
                        continue
                    # strip the leading "model/" that get_archive prepends
                    name = (
                        member.name.split("/", 1)[1]
                        if "/" in member.name
                        else member.name
                    )
                    files[name] = tar.extractfile(member).read()
            return (files, logs) if return_logs else files
        finally:
            with contextlib.suppress(Exception):
                container.remove(force=True)

    def list_training_containers(self) -> list[dict]:
        """List running SageMaker local mode containers."""
        containers = self.client.containers.list(filters={"name": "sagemaker-local"})
        return [
            {
                "id": c.short_id,
                "name": c.name,
                "status": c.status,
                "image": c.image.tags,
            }
            for c in containers
        ]

    def list_endpoint_containers(self) -> list[dict]:
        """List running SageMaker local endpoint containers."""
        containers = self.client.containers.list(filters={"name": "sagemaker-local"})
        return [
            {"id": c.short_id, "name": c.name, "status": c.status, "ports": c.ports}
            for c in containers
            if any("8080" in str(p) for p in c.ports.values())
        ]

    def cleanup(self) -> int:
        """Remove stopped SageMaker local mode containers."""
        removed = 0
        containers = self.client.containers.list(
            all=True, filters={"name": "sagemaker-local"}
        )
        for c in containers:
            if c.status != "running":
                c.remove(force=True)
                removed += 1
        return removed

    def image_exists(self, tag: str) -> bool:
        """Check if a training/inference image exists locally."""
        try:
            self.client.images.get(tag)
            return True
        except NotFound:
            return False

    # SageMaker Studio domain — composed from real resources via CloudFormation.
    #
    # AWS provisions a Studio domain opaquely (EFS + network + IAM). oblako makes
    # the topology concrete: create_domain deploys a CloudFormation stack with an
    # S3 artifacts bucket + an EC2 notebook instance (a real container) with an
    # EBS volume. launch_notebook then runs JupyterLab *inside* that instance,
    # EBS-as-home, pre-wired to oblako's services.
    def _cfn(self):
        from .cloudformation import CloudFormationService

        return CloudFormationService().get_client()

    def _ec2(self):
        from .ec2 import Ec2Service
        from .moto import MotoService

        return Ec2Service(MotoService()).get_client()

    def ensure_notebook_image(self) -> str:
        """Build oblako's slim notebook image (JupyterLab + boto3) if it's absent.

        Only builds the bundled image; a custom OBLAKO_NOTEBOOK_IMAGE is left to be
        pulled by the instance container as usual.
        """
        if NOTEBOOK_IMAGE != OBLAKO_NOTEBOOK_IMAGE:
            return NOTEBOOK_IMAGE  # user override — not ours to build
        try:
            self.client.images.get(NOTEBOOK_IMAGE)
            return NOTEBOOK_IMAGE
        except NotFound:
            pass
        context = Path(__file__).resolve().parents[1] / "images" / "notebook"
        print(f"Building {NOTEBOOK_IMAGE} (JupyterLab + boto3) — first time only…")
        self.build_image(str(context), NOTEBOOK_IMAGE)
        return NOTEBOOK_IMAGE

    def _notebook_instance_id(self, name: str) -> str | None:
        """Return the domain's notebook EC2 instance id (by Name tag), or None."""
        resp = self._ec2().describe_instances(
            Filters=[{"Name": "tag:Name", "Values": [f"{name}-notebook"]}]
        )
        for res in resp.get("Reservations", []):
            for inst in res["Instances"]:
                if inst["State"]["Name"] != "terminated":
                    return inst["InstanceId"]
        return None

    def create_domain(
        self,
        name: str = "studio",
        instance_type: str = "t3.medium",
        notebook_port: int = NOTEBOOK_PORT,
    ) -> dict:
        """Create a Studio domain as a CloudFormation stack (S3 + EC2 + EBS).

        The notebook instance is a real container (Jupyter image) publishing
        JupyterLab on ``notebook_port``. Returns the domain status.
        """
        self.ensure_notebook_image()  # build the slim image if needed (local tag)
        bucket = f"oblako-sagemaker-{name}"
        template = json.dumps(
            {
                "Resources": {
                    "Artifacts": {
                        "Type": "AWS::S3::Bucket",
                        "Properties": {"BucketName": bucket},
                    },
                    "Notebook": {
                        "Type": "AWS::EC2::Instance",
                        "Properties": {
                            "InstanceType": instance_type,
                            "Image": NOTEBOOK_IMAGE,  # oblako extension
                            "Ports": {"8888/tcp": notebook_port},  # oblako extension
                            "Tags": [{"Key": "Name", "Value": f"{name}-notebook"}],
                        },
                    },
                }
            }
        )
        cfn, stack = self._cfn(), _domain_stack(name)
        cfn.create_change_set(
            StackName=stack,
            TemplateBody=template,
            ChangeSetName="create",
            ChangeSetType="CREATE",
        )
        cfn.execute_change_set(StackName=stack, ChangeSetName="create")
        return self.domain_status(name)

    def domain_status(self, name: str = "studio") -> dict:
        """Return {domain, stack, status, artifactsBucket, instanceId} (status NONE if absent)."""
        stack = _domain_stack(name)
        try:
            s = self._cfn().describe_stacks(StackName=stack)["Stacks"][0]
        except Exception:
            return {"domain": name, "status": "NONE"}
        return {
            "domain": name,
            "stack": stack,
            "status": s["StackStatus"],
            "artifactsBucket": f"oblako-sagemaker-{name}",
            "instanceId": self._notebook_instance_id(name),
        }

    def launch_notebook(self, name: str = "studio", port: int = NOTEBOOK_PORT) -> dict:
        """Run JupyterLab inside the domain's notebook instance (EBS as home, pre-wired).

        boto3 in the kernel hits oblako's services on the host via
        host.docker.internal — unmodified AWS code runs against oblako.
        """
        from oblako import ports as P
        from .ec2 import EBS_MOUNT, _container_name

        iid = self._notebook_instance_id(name)
        if not iid:
            raise RuntimeError(f"domain {name!r} has no running notebook instance")
        container = self.client.containers.get(_container_name(iid))
        host = "host.docker.internal"
        env = {
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ENDPOINT_URL_S3": f"http://{host}:{P.S3}",
            "AWS_ENDPOINT_URL_DYNAMODB": f"http://{host}:{P.DYNAMODB}",
            "AWS_ENDPOINT_URL_SAGEMAKER": f"http://{host}:{P.SAGEMAKER}",
            "AWS_ENDPOINT_URL_SAGEMAKER_RUNTIME": f"http://{host}:{P.SAGEMAKER}",
            "AWS_ENDPOINT_URL_SAGEMAKER_FEATURESTORE_RUNTIME": f"http://{host}:{P.SAGEMAKER}",
            "AWS_ENDPOINT_URL_CLOUDFORMATION": f"http://{host}:{P.CLOUDFORMATION}",
        }
        container.exec_run(
            [
                "jupyter",
                "lab",
                "--ip=0.0.0.0",
                "--port=8888",
                "--no-browser",
                "--allow-root",
                "--ServerApp.token=oblako",
                f"--notebook-dir={EBS_MOUNT}",
            ],
            environment=env,
            detach=True,
        )
        return {"instanceId": iid, "url": f"http://localhost:{port}/lab?token=oblako"}

    def delete_domain(self, name: str = "studio") -> None:
        """Tear down the domain's CloudFormation stack (S3 bucket + EC2 + EBS)."""
        try:
            self._cfn().delete_stack(StackName=_domain_stack(name))
        except Exception:
            pass
