"""SageMaker local mode service: training, endpoints, and processing via Docker."""

from __future__ import annotations

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
    """Wrapper around SageMaker SDK local mode.

    SageMaker local mode uses Docker directly (no separate container needed).
    This class provides helpers for managing training images, checking status,
    and cleaning up local mode artifacts.
    """

    def __init__(self):
        """Initialize SageMaker local mode with a deferred Docker client."""
        self._client: docker.DockerClient | None = None

    @property
    def client(self) -> docker.DockerClient:
        """Return (or lazily create) the Docker client."""
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def get_session(self):
        """Return a SageMaker LocalSession for local mode training/inference."""
        from sagemaker.local import LocalSession

        return LocalSession()

    def build_image(self, path: str, tag: str) -> str:
        """Build a training/inference Docker image."""
        image, logs = self.client.images.build(path=path, tag=tag)
        for chunk in logs:
            if "stream" in chunk:
                print(chunk["stream"], end="")
        return image.tags[0]

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
        """The domain's notebook EC2 instance id (by Name tag), or None."""
        resp = self._ec2().describe_instances(
            Filters=[{"Name": "tag:Name", "Values": [f"{name}-notebook"]}]
        )
        for res in resp.get("Reservations", []):
            for inst in res["Instances"]:
                if inst["State"]["Name"] != "terminated":
                    return inst["InstanceId"]
        return None

    def create_domain(self, name: str = "studio", instance_type: str = "t3.medium",
                       notebook_port: int = NOTEBOOK_PORT) -> dict:
        """Create a Studio domain as a CloudFormation stack (S3 + EC2 + EBS).

        The notebook instance is a real container (Jupyter image) publishing
        JupyterLab on ``notebook_port``. Returns the domain status.
        """
        self.ensure_notebook_image()  # build the slim image if needed (local tag)
        bucket = f"oblako-sagemaker-{name}"
        template = json.dumps({"Resources": {
            "Artifacts": {"Type": "AWS::S3::Bucket", "Properties": {"BucketName": bucket}},
            "Notebook": {"Type": "AWS::EC2::Instance", "Properties": {
                "InstanceType": instance_type,
                "Image": NOTEBOOK_IMAGE,                 # oblako extension
                "Ports": {"8888/tcp": notebook_port},    # oblako extension
                "Tags": [{"Key": "Name", "Value": f"{name}-notebook"}],
            }},
        }})
        cfn, stack = self._cfn(), _domain_stack(name)
        cfn.create_change_set(StackName=stack, TemplateBody=template,
                              ChangeSetName="create", ChangeSetType="CREATE")
        cfn.execute_change_set(StackName=stack, ChangeSetName="create")
        return self.domain_status(name)

    def domain_status(self, name: str = "studio") -> dict:
        """Return {domain, stack, status, artifactsBucket, instanceId} (status NONE if absent)."""
        stack = _domain_stack(name)
        try:
            s = self._cfn().describe_stacks(StackName=stack)["Stacks"][0]
        except Exception:  # noqa: BLE001
            return {"domain": name, "status": "NONE"}
        return {
            "domain": name, "stack": stack, "status": s["StackStatus"],
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
            "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ENDPOINT_URL_S3": f"http://{host}:{P.S3}",
            "AWS_ENDPOINT_URL_DYNAMODB": f"http://{host}:{P.DYNAMODB}",
            "AWS_ENDPOINT_URL_SAGEMAKER": f"http://{host}:{P.MOTO}",
            "AWS_ENDPOINT_URL_CLOUDFORMATION": f"http://{host}:{P.CLOUDFORMATION}",
        }
        container.exec_run(
            ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", "--no-browser",
             "--allow-root", "--ServerApp.token=oblako", f"--notebook-dir={EBS_MOUNT}"],
            environment=env, detach=True,
        )
        return {"instanceId": iid, "url": f"http://localhost:{port}/lab?token=oblako"}

    def delete_domain(self, name: str = "studio") -> None:
        """Tear down the domain's CloudFormation stack (S3 bucket + EC2 + EBS)."""
        try:
            self._cfn().delete_stack(StackName=_domain_stack(name))
        except Exception:  # noqa: BLE001
            pass
