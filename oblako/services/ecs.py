"""ECS service: AWS ECS control plane (moto) + real container-backed tasks.

moto owns the control plane (clusters, task definitions, services, task metadata)
with full describe fidelity. On top of that, oblako runs each task as a **real
container** on the active backend: ``run_task`` launches the task definition's
image, wires it to oblako's endpoints, publishes its container port on a host
port, and labels it so ``describe_tasks`` reflects real RUNNING/STOPPED state.

That is the "real behavior, simulated topology" move, the same one ec2.py makes
for instances. Fargate is the natural local target: there is no EC2 capacity to
simulate, a task is simply its container(s). So ``launchType="FARGATE"`` is the
default and the cpu/memory fields are recorded as metadata, not enforced.

The split mirrors Redshift: ``get_client()`` is the moto control plane (register
task definitions, create clusters, describe), while ``run_task`` / ``stop_task``
/ ``describe_tasks`` on this service are the data plane that actually runs the
container. The CloudFormation ``AWS::ECS::*`` providers call the same methods, so
a stack-provisioned task is just as real as a hand-launched one.
"""

from __future__ import annotations

import json
import socket
import uuid

from oblako import config, ports

from .boto import BotoService, client
from .moto import MotoService
from .backends import publish

TASK_LABEL = "oblako.ecs.task-arn"
CLUSTER_LABEL = "oblako.ecs.cluster"
SERVICE_NAME_LABEL = "oblako.ecs.service"
SERVICE_LABEL = "oblako.service"
CONTAINER_LABEL = "oblako.ecs.container"  # the container's name in its task definition
TASK_DEFINITION_LABEL = "oblako.ecs.task-definition"
LAUNCH_TYPE_LABEL = "oblako.ecs.launch-type"


def _docker():
    """Return a Docker client for the configured container backend."""
    from .backends import docker_client

    return (
        docker_client()
    )  # honours OBLAKO_CONTAINER_BACKEND (ECS needs a Docker socket)


def _free_port() -> int:
    """Grab an unused host port (tasks/ALBs are dynamic, not on the fixed map)."""
    s = socket.socket()
    try:
        s.bind(("", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _task_endpoint_env() -> dict[str, str]:
    """AWS_ENDPOINT_URL_* pointing at oblako on the host, for code inside a task.

    A task reaches oblako over ``host.docker.internal`` (the host gateway), so a
    container that talks to S3/DynamoDB/etc. needs no endpoint config of its own,
    the same contract the notebook kernel gets on the host.
    """
    host = "host.docker.internal"

    def u(port: int) -> str:
        """Return the URL for a port on the host gateway."""
        return f"http://{host}:{port}"

    return {
        "AWS_ENDPOINT_URL_S3": u(ports.S3),
        "AWS_ENDPOINT_URL_DYNAMODB": u(ports.DYNAMODB),
        "AWS_ENDPOINT_URL_CLOUDFORMATION": u(ports.CLOUDFORMATION),
        "AWS_ENDPOINT_URL_SFN": u(ports.STEPFUNCTIONS),
        "AWS_ENDPOINT_URL_REDSHIFT_DATA": u(ports.REDSHIFT_DATA),
        "AWS_ENDPOINT_URL_RDS_DATA": u(ports.RDS_DATA),
        "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": u(ports.BEDROCK_RUNTIME),
        "AWS_DEFAULT_REGION": config.region(),
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
    }


def _env_list_to_dict(pairs) -> dict[str, str]:
    """Map an ECS ``[{"name","value"}]`` environment list to a dict."""
    return {p["name"]: p["value"] for p in (pairs or [])}


# Fargate task sizes, as AWS accepts them: cpu units -> allowed memory in MiB
FARGATE_SIZES: dict[int, tuple[int, ...] | range] = {
    256: (512, 1024, 2048),
    512: range(1024, 4097, 1024),
    1024: range(2048, 8193, 1024),
    2048: range(4096, 16385, 1024),
    4096: range(8192, 30721, 1024),
    8192: range(16384, 61441, 4096),
    16384: range(32768, 122881, 8192),
}


def _units(value, unit: str) -> int | None:
    """Read ``"1024"``, ``1024``, ``"1 vCPU"`` or ``"2 GB"`` as CPU units or MiB."""
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    number, _, suffix = text.partition(" ")
    try:
        amount = float(number)
    except ValueError:
        return None
    return int(amount * 1024) if suffix.strip().lower() == unit else None


def fargate_size_error(task_definition: dict) -> str | None:
    """Return AWS's ClientException message if a Fargate task size is invalid."""
    if "FARGATE" not in (task_definition.get("requiresCompatibilities") or []):
        return None
    cpu_raw, memory_raw = task_definition.get("cpu"), task_definition.get("memory")
    if cpu_raw is None or memory_raw is None:
        return "Fargate requires that 'cpu' be defined at the task level."
    cpu, memory = _units(cpu_raw, "vcpu"), _units(memory_raw, "gb")
    if memory not in FARGATE_SIZES.get(cpu or 0, ()):
        return (
            f"No Fargate configuration exists for given values: {cpu_raw} CPU, "
            f"{memory_raw} memory. See the Amazon ECS documentation for the valid values."
        )
    return None


def _secret_value(value_from: str, endpoint_url: str) -> str:
    """Resolve a container secret's ``valueFrom`` from SSM or Secrets Manager.

    Accepts an SSM parameter name or ARN, and a Secrets Manager ARN with the
    optional ``:json-key:version-stage:version-id`` suffix, as ECS does.
    """
    if value_from.startswith("arn:aws:secretsmanager:"):
        parts = value_from.split(":")
        arn, extra = ":".join(parts[:7]), parts[7:] + ["", "", ""]
        json_key, stage, version = extra[:3]
        kwargs: dict = {"SecretId": arn}
        if stage:
            kwargs["VersionStage"] = stage
        if version:
            kwargs["VersionId"] = version
        secret = client("secretsmanager", endpoint_url).get_secret_value(**kwargs)
        text = secret.get("SecretString", "")
        return str(json.loads(text)[json_key]) if json_key else text
    ssm = client("ssm", endpoint_url)
    names = [value_from]
    if value_from.startswith("arn:aws:ssm:"):
        path = value_from.split(":parameter", 1)[1]
        names = [path, path.lstrip("/")]
    for name in names:
        try:
            return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"][
                "Value"
            ]
        except ssm.exceptions.ParameterNotFound:
            continue
    raise LookupError(f"parameter {value_from} not found")


def _container_name(task_id: str, container: str) -> str:
    """Return the Docker container name for a task's container."""
    return f"oblako-ecs-{task_id[:12]}-{container}"


class EcsService(BotoService):
    """AWS ECS, moto control plane + real container-backed Fargate tasks."""

    aws_services = ("ecs",)

    name = "ecs"

    def __init__(self, moto: MotoService, elbv2=None):
        """Wire to the shared moto endpoint; optionally an ELBv2 service for services.

        ``elbv2`` lets ``create_service`` register a task's published ports as ALB
        targets, so a service behind a load balancer routes for real.
        """
        self.moto = moto
        self.elbv2 = elbv2
        self._eni_by_task: dict[str, list] = {}  # awsvpc attachments per task

    @property
    def endpoint_url(self) -> str:
        """Moto serves ECS at the same endpoint as every other AWS API."""
        return self.moto.endpoint_url

    # Control-plane convenience (everything else: use get_client() directly)
    def register_task_definition(self, **kwargs) -> str:
        """Register a task definition in moto, returning its ARN."""
        error = fargate_size_error(kwargs)
        if error:
            raise ValueError(error)
        resp = self.get_client().register_task_definition(**kwargs)
        return resp["taskDefinition"]["taskDefinitionArn"]

    # Data plane: actually run the task's container(s)
    def run_task(
        self,
        task_definition: str,
        *,
        cluster: str = "default",
        count: int = 1,
        launch_type: str = "FARGATE",
        backed: bool = True,
        service_name: str | None = None,
        network_configuration: dict | None = None,
    ) -> dict:
        """Launch ``count`` real containers for ``task_definition``.

        Reads the task definition from moto, runs each container on the backend
        with oblako's endpoint env + host gateway, and publishes the first port
        mapping of each container on a host port. Returns a RunTask-shaped dict
        whose ``containers[].networkBindings`` carry the assigned host ports.
        ``backed=False`` records nothing real, just the metadata shape.
        ``service_name`` labels the containers so a service can reconcile them.

        For ``awsvpc`` task definitions oblako attaches a moto ENI (with a private
        IP) to each task so ``describe_tasks`` shows a faithful attachment, the
        container still runs on the bridge network. ``network_configuration`` is
        honoured when given, else oblako auto-fills the default-VPC subnet (the
        same lenient-local default the load balancer uses), rather than raising
        the AWS "network configuration must be provided" error.
        """
        td = self.get_client().describe_task_definition(taskDefinition=task_definition)[
            "taskDefinition"
        ]
        region = config.region()
        awsvpc = td.get("networkMode") == "awsvpc"
        tasks = []
        for _ in range(count):
            task_id = uuid.uuid4().hex
            account = config.account_id()
            task_arn = f"arn:aws:ecs:{region}:{account}:task/{cluster}/{task_id}"
            containers = []
            container_metas: dict[str, dict] = {}
            for cdef in td.get("containerDefinitions", []):
                if backed:
                    bindings, meta = self._run_container(
                        task_id,
                        task_arn,
                        cluster,
                        cdef,
                        service_name,
                        {
                            TASK_DEFINITION_LABEL: td["taskDefinitionArn"],
                            LAUNCH_TYPE_LABEL: launch_type,
                        },
                    )
                    container_metas[cdef["name"]] = meta
                else:
                    bindings = []
                containers.append(
                    {
                        "name": cdef["name"],
                        "lastStatus": "RUNNING" if backed else "PENDING",
                        "networkBindings": bindings,
                    }
                )
            if container_metas:
                self._register_metadata(
                    task_id, task_arn, cluster, td, launch_type, region, container_metas
                )
            attachments = (
                self._awsvpc_attachment(network_configuration)
                if backed and awsvpc
                else []
            )
            if attachments:
                self._eni_by_task[task_arn] = attachments
            tasks.append(
                {
                    "taskArn": task_arn,
                    "clusterArn": cluster,
                    "taskDefinitionArn": td["taskDefinitionArn"],
                    "lastStatus": "RUNNING" if backed else "PENDING",
                    "desiredStatus": "RUNNING",
                    "launchType": launch_type,
                    "containers": containers,
                    "attachments": attachments,
                }
            )
        return {"tasks": tasks, "failures": []}

    # awsvpc: a metadata-only ENI for describe fidelity (the container still runs
    # on the bridge network, the same way LocalStack and oblako's EC2 do it).
    def _awsvpc_attachment(self, network_configuration: dict | None) -> list[dict]:
        # Try the configured subnet, then a real default-VPC subnet (the template's
        # subnet may be a placeholder param that isn't a real moto subnet).
        """Create an ENI for an awsvpc task and return its attachment, or none."""
        ec2 = client("ec2", self.endpoint_url)
        for subnet in (
            self._subnet_from(network_configuration),
            self._default_subnet(),
        ):
            if not subnet:
                continue
            try:
                eni = ec2.create_network_interface(SubnetId=subnet)["NetworkInterface"]
            except Exception:  # realism only; never block the run
                continue
            return [
                {
                    "id": uuid.uuid4().hex,
                    "type": "ElasticNetworkInterface",
                    "status": "ATTACHED",
                    "details": [
                        {"name": "subnetId", "value": subnet},
                        {
                            "name": "networkInterfaceId",
                            "value": eni["NetworkInterfaceId"],
                        },
                        {"name": "macAddress", "value": eni.get("MacAddress", "")},
                        {
                            "name": "privateIPv4Address",
                            "value": eni.get("PrivateIpAddress", ""),
                        },
                    ],
                }
            ]
        return []

    def _subnet_from(self, network_configuration: dict | None) -> str | None:
        """First subnet from an awsvpc network configuration (either casing)."""
        if not network_configuration:
            return None
        cfg = network_configuration.get("awsvpcConfiguration") or (
            network_configuration.get("AwsvpcConfiguration") or {}
        )
        subnets = cfg.get("subnets") or cfg.get("Subnets") or []
        return subnets[0] if subnets else None

    def _default_subnet(self) -> str | None:
        """Return a default-VPC subnet from moto (so awsvpc just works locally)."""
        ec2 = client("ec2", self.endpoint_url)
        subs = ec2.describe_subnets(
            Filters=[{"Name": "default-for-az", "Values": ["true"]}]
        )["Subnets"]
        if not subs:
            subs = ec2.describe_subnets()["Subnets"]
        return subs[0]["SubnetId"] if subs else None

    def _run_container(
        self,
        task_id: str,
        task_arn: str,
        cluster: str,
        cdef: dict,
        service_name: str | None = None,
        extra_labels: dict[str, str] | None = None,
    ) -> tuple[list[dict], dict]:
        """Run one container of a task; return (networkBindings, container metadata)."""
        client = _docker()
        image = cdef["image"]
        try:
            client.images.get(image)
        except Exception:  # not present locally -> pull
            client.images.pull(image)

        env = _task_endpoint_env()
        # the ECS task metadata endpoint (v3/v4): served on the host, reached over
        # the host gateway, so task code that reads its own metadata works locally
        meta_base = f"http://host.docker.internal:{ports.ECS_METADATA}"
        env["ECS_CONTAINER_METADATA_URI"] = f"{meta_base}/{task_id}/{cdef['name']}"
        env["ECS_CONTAINER_METADATA_URI_V4"] = (
            f"{meta_base}/v4/{task_id}/{cdef['name']}"
        )
        env.update(_env_list_to_dict(cdef.get("environment")))
        for secret in cdef.get("secrets") or []:
            try:
                env[secret["name"]] = _secret_value(
                    secret["valueFrom"], self.endpoint_url
                )
            except Exception as e:  # missing parameter or secret, as AWS reports it
                raise RuntimeError(
                    "ResourceInitializationError: unable to pull secrets or registry "
                    f"auth: unable to retrieve secret {secret['valueFrom']}: {e}"
                ) from e

        port_bindings, bindings = {}, []
        for pm in cdef.get("portMappings", []):
            cport = pm["containerPort"]
            proto = pm.get("protocol", "tcp")
            hport = _free_port()
            port_bindings[f"{cport}/{proto}"] = hport
            bindings.append(
                {
                    "containerPort": cport,
                    "hostPort": hport,
                    "protocol": proto,
                    "bindIP": "127.0.0.1",
                }
            )

        labels = {
            SERVICE_LABEL: "ecs",
            TASK_LABEL: task_arn,
            CLUSTER_LABEL: cluster,
            CONTAINER_LABEL: cdef["name"],
            **(extra_labels or {}),
        }
        if service_name:
            labels[SERVICE_NAME_LABEL] = service_name
        container = client.containers.run(
            image,
            command=cdef.get("command") or None,
            detach=True,
            name=_container_name(task_id, cdef["name"]),
            environment=env,
            ports=publish(port_bindings or None, client),
            extra_hosts={"host.docker.internal": "host-gateway"},
            labels=labels,
        )
        container_meta = {
            "DockerId": container.id,
            "Name": cdef["name"],
            "DockerName": _container_name(task_id, cdef["name"]),
            "Image": image,
            "ImageID": "",
            "Labels": labels,
            "DesiredStatus": "RUNNING",
            "KnownStatus": "RUNNING",
            "Limits": {
                "CPU": cdef.get("cpu", 0),
                "Memory": cdef.get("memory", 0),
            },
            "Type": "NORMAL",
            "Networks": [{"NetworkMode": "bridge"}],
            "Ports": [
                {
                    "ContainerPort": b["containerPort"],
                    "HostPort": b["hostPort"],
                    "Protocol": b["protocol"],
                }
                for b in bindings
            ],
        }
        return bindings, container_meta

    @staticmethod
    def _register_metadata(
        task_id: str,
        task_arn: str,
        cluster: str,
        td: dict,
        launch_type: str,
        region: str,
        container_metas: dict[str, dict],
    ) -> None:
        """Publish a task's metadata to the ECS metadata endpoint (started lazily)."""
        from oblako.engines import ecs_metadata

        ecs_metadata.start_in_thread()
        task_metadata = {
            "Cluster": cluster,
            "TaskARN": task_arn,
            "Family": td.get("family"),
            "Revision": str(td.get("revision", 1)),
            "DesiredStatus": "RUNNING",
            "KnownStatus": "RUNNING",
            "Limits": {
                "CPU": float(td.get("cpu", 0) or 0),
                "Memory": int(td.get("memory", 0) or 0),
            },
            "AvailabilityZone": f"{region}a",
            "LaunchType": launch_type,
            "Containers": list(container_metas.values()),
        }
        ecs_metadata.register(task_id, task_metadata, container_metas)

    # Services: run desiredCount tasks and register them behind a load balancer
    def create_service(
        self,
        *,
        service_name: str,
        task_definition: str,
        cluster: str = "default",
        desired_count: int = 1,
        launch_type: str = "FARGATE",
        load_balancers: list[dict] | None = None,
        network_configuration: dict | None = None,
    ) -> dict:
        """Run ``desired_count`` tasks and register them as ALB targets.

        ``load_balancers`` mirrors the ECS API: a list of
        ``{"targetGroupArn", "containerName", "containerPort"}``. Each task's
        published host port for that container is registered into the target
        group, so the load balancer routes to the real containers.
        ``network_configuration`` is passed through to the tasks (awsvpc).
        """
        try:  # record in moto for describe fidelity (best-effort)
            self.get_client().create_service(
                cluster=cluster,
                serviceName=service_name,
                taskDefinition=task_definition,
                desiredCount=desired_count,
                launchType=launch_type,
            )
        except Exception:  # moto strictness shouldn't block the real run
            pass

        run = self.run_task(
            task_definition,
            cluster=cluster,
            count=desired_count,
            launch_type=launch_type,
            service_name=service_name,
            network_configuration=network_configuration,
        )
        for lb in load_balancers or []:
            host_ports = [
                nb["hostPort"]
                for task in run["tasks"]
                for cont in task["containers"]
                if cont["name"] == lb["containerName"]
                for nb in cont["networkBindings"]
                if nb["containerPort"] == lb["containerPort"]
            ]
            if self.elbv2 and host_ports:
                self.elbv2.register_targets(lb["targetGroupArn"], host_ports)
        return run

    def delete_service(self, service_name: str, cluster: str = "default") -> None:
        """Stop every task container of a service (idempotent)."""
        for c in self._task_containers():
            if c.labels.get(SERVICE_NAME_LABEL) == service_name:
                c.remove(force=True)
        try:
            self.get_client().delete_service(
                cluster=cluster, service=service_name, force=True
            )
        except Exception:  # already gone / moto strictness
            pass

    def describe_tasks(
        self, cluster: str = "default", tasks: list[str] | None = None
    ) -> dict:
        """Reflect real container state back as ECS task descriptions.

        One entry per task, with each container's name, status and exit code; a
        task is RUNNING while any of its containers runs, then STOPPED, as on ECS.
        """
        wanted = set(tasks or [])
        by_task: dict[str, list] = {}
        for c in self._task_containers():
            arn = c.labels.get(TASK_LABEL)
            if not arn or (wanted and arn not in wanted):
                continue
            by_task.setdefault(arn, []).append(c)
        out = []
        for arn, containers in by_task.items():
            labels = containers[0].labels
            running = any(c.status == "running" for c in containers)
            status = "RUNNING" if running else "STOPPED"
            described = []
            for c in containers:
                entry = {
                    "name": c.labels.get(CONTAINER_LABEL, c.name),
                    "lastStatus": "RUNNING" if c.status == "running" else "STOPPED",
                }
                if c.status != "running":
                    entry["exitCode"] = c.attrs.get("State", {}).get("ExitCode")
                described.append(entry)
            task = {
                "taskArn": arn,
                "clusterArn": labels.get(CLUSTER_LABEL, cluster),
                "taskDefinitionArn": labels.get(TASK_DEFINITION_LABEL, ""),
                "launchType": labels.get(LAUNCH_TYPE_LABEL, "FARGATE"),
                "lastStatus": status,
                "desiredStatus": "RUNNING" if running else "STOPPED",
                "containers": described,
                "attachments": self._eni_by_task.get(arn, []),
            }
            if not running:
                task["stopCode"] = "EssentialContainerExited"
                task["stoppedReason"] = "Essential container in task exited"
            out.append(task)
        found = {t["taskArn"] for t in out}
        failures = [{"arn": arn, "reason": "MISSING"} for arn in wanted - found]
        return {"tasks": out, "failures": failures}

    def list_tasks(self, cluster: str = "default") -> list[str]:
        """Task ARNs of all oblako ECS task containers in the cluster."""
        arns = []
        for c in self._task_containers():
            if c.labels.get(CLUSTER_LABEL) == cluster:
                arn = c.labels.get(TASK_LABEL)
                if arn and arn not in arns:
                    arns.append(arn)
        return arns

    def stop_task(self, task: str) -> None:
        """Stop and remove every container of a task (idempotent)."""
        from oblako.engines import ecs_metadata

        for c in self._task_containers():
            if c.labels.get(TASK_LABEL) == task:
                c.remove(force=True)
        ecs_metadata.deregister(task.split("/")[-1])

    def task_url(self, task: str) -> str | None:
        """Reachable URL for a task's first published port, or None."""
        import docker
        import docker.errors

        for c in self._task_containers():
            if c.labels.get(TASK_LABEL) != task:
                continue
            try:
                c.reload()
                ports_map = c.attrs["NetworkSettings"]["Ports"] or {}
            except docker.errors.NotFound:
                return None
            for binding in ports_map.values():
                if binding:
                    return f"http://localhost:{binding[0]['HostPort']}"
        return None

    def _task_containers(self) -> list:
        """Return every ECS task container (running or stopped)."""
        return _docker().containers.list(
            all=True, filters={"label": f"{SERVICE_LABEL}=ecs"}
        )

    # Lifecycle (moto-backed; task containers are managed per-arn, not as one service)
    def start(self) -> None:
        """Bring moto up if it isn't already (no own service container)."""
        self.moto.wait_ready(timeout=2) or self.moto.start()

    def stop(self) -> None:
        """No-op: moto owns the control plane; tasks are managed per-arn."""

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """Defer readiness to moto."""
        return self.moto.wait_ready(timeout=timeout)

    def status(self):
        """Defer status to moto."""
        return self.moto.status()
