"""Pluggable container backend so oblako isn't hard-wired to Docker.

``Service`` (see base.py) drives containers through a ``ContainerBackend`` instead
of calling docker-py directly. Docker, Podman, and Colima all speak the Docker
Engine API, so they share ``DockerBackend`` — the only difference is which socket
it talks to (honoured via ``DOCKER_HOST`` or auto-detected per runtime).
Kubernetes is a genuinely different control plane, so it gets its own backend.

Select with ``OBLAKO_CONTAINER_BACKEND`` = docker (default) | podman | colima |
kubernetes. ``DOCKER_HOST`` always wins for the Docker-API runtimes.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess

# container status normalised across backends
RUNNING = "running"
STOPPED = "stopped"
ABSENT = "absent"

# Kubernetes backend: namespace + a registry of live `kubectl port-forward`
# processes (so localhost:host_port reaches the in-cluster Service), keyed by name.
K8S_NAMESPACE = os.environ.get("OBLAKO_K8S_NAMESPACE") or "oblako"
_port_forwards: dict[str, list] = {}

# Best-effort socket locations when DOCKER_HOST is unset (the runtime's `start`
# command usually creates these). DOCKER_HOST overrides all of them.
_CANDIDATE_SOCKETS = {
    "colima": ["~/.colima/default/docker.sock"],
    "podman": [
        "$XDG_RUNTIME_DIR/podman/podman.sock",
        "~/.local/share/containers/podman/machine/podman.sock",
        "/run/podman/podman.sock",
    ],
}


class ContainerBackend:
    """Minimal container lifecycle interface a Service needs."""

    name = "container"

    def ensure_image(self, image: str) -> None:
        """Pull the image if it is not present locally."""
        raise NotImplementedError

    def run(self, *, name, image, ports, environment, volumes, extra_hosts,
            command, working_dir, user) -> None:
        """Create and start a detached container with the given spec."""
        raise NotImplementedError

    def status(self, name: str) -> str:
        """Return RUNNING, STOPPED, or ABSENT for the named container."""
        raise NotImplementedError

    def remove(self, name: str) -> None:
        """Force-remove the container if present."""
        raise NotImplementedError

    def stop(self, name: str) -> None:
        """Stop and remove the container (no-op if absent)."""
        raise NotImplementedError

    def logs(self, name: str, tail: int = 50) -> str:
        """Return the container's recent logs."""
        raise NotImplementedError


class DockerBackend(ContainerBackend):
    """Docker Engine API backend — also serves Podman and Colima via their sockets."""

    name = "docker"

    def __init__(self, base_url: str | None = None):
        """Initialize against an explicit socket URL, else the ambient Docker context."""
        self._base_url = base_url
        self._client = None

    @property
    def client(self):
        """Lazily create the docker-py client, honouring DOCKER_HOST or the active context."""
        if self._client is None:
            import docker

            self._client = docker.DockerClient(base_url=self._base_url) if self._base_url \
                else docker.from_env()
        return self._client

    def ensure_image(self, image: str) -> None:
        """Pull the image if it is not already present."""
        from docker.errors import NotFound

        try:
            self.client.images.get(image)
        except NotFound:
            print(f"Pulling {image}...")
            self.client.images.pull(image)

    def run(self, *, name, image, ports, environment, volumes, extra_hosts,
            command, working_dir, user) -> None:
        """Create and start a detached container."""
        self.client.containers.run(
            image, name=name, detach=True, ports=ports, environment=environment,
            volumes=volumes, extra_hosts=extra_hosts, command=command,
            working_dir=working_dir, user=user,
        )

    def status(self, name: str) -> str:
        """Return RUNNING/STOPPED/ABSENT for the container."""
        from docker.errors import NotFound

        try:
            container = self.client.containers.get(name)
        except NotFound:
            return ABSENT
        return RUNNING if container.status == "running" else STOPPED

    def remove(self, name: str) -> None:
        """Force-remove the container if present."""
        from docker.errors import NotFound

        try:
            self.client.containers.get(name).remove(force=True)
        except NotFound:
            pass

    def stop(self, name: str) -> None:
        """Stop and remove the container (no-op if absent)."""
        from docker.errors import NotFound

        try:
            container = self.client.containers.get(name)
        except NotFound:
            return
        container.stop(timeout=10)
        container.remove()

    def logs(self, name: str, tail: int = 50) -> str:
        """Return recent logs, or empty string if the container is gone."""
        from docker.errors import NotFound

        try:
            return self.client.containers.get(name).logs(tail=tail).decode("utf-8")
        except NotFound:
            return ""


def _k8s_name(value: str) -> str:
    """Sanitize a string to a DNS-1123 label for use as a k8s resource/volume name."""
    out = "".join(c if (c.isalnum() or c == "-") else "-" for c in value.lower())
    return out.strip("-") or "vol"


def _args_list(command) -> list | None:
    """Map a docker command (str or list) to k8s container args."""
    if command is None:
        return None
    return command if isinstance(command, list) else command.split()


def _run_as_user(user) -> int | None:
    """Map a docker user (e.g. 'root' or a uid) to a k8s runAsUser, else None."""
    if user is None:
        return None
    if user == "root":
        return 0
    try:
        return int(user)
    except (TypeError, ValueError):
        return None


def build_k8s_manifests(*, name, image, ports, environment, volumes, extra_hosts,
                        command, working_dir, user, namespace) -> dict:
    """Build a Deployment + Service ``List`` manifest for a Service (pure, no cluster)."""
    container_ports = [int(str(spec).split("/")[0]) for spec in ports]
    mounts, volume_defs = [], []
    for i, (vol, mount) in enumerate(volumes.items()):
        vname = f"{_k8s_name(vol)}-{i}"
        mounts.append({"name": vname, "mountPath": mount["bind"]})
        volume_defs.append({"name": vname, "emptyDir": {}})

    container = {
        "name": name, "image": image, "imagePullPolicy": "IfNotPresent",
        "env": [{"name": k, "value": str(v)} for k, v in environment.items()],
        "ports": [{"containerPort": cp} for cp in container_ports],
    }
    args = _args_list(command)
    if args:
        container["args"] = args
    if working_dir:
        container["workingDir"] = working_dir
    if mounts:
        container["volumeMounts"] = mounts
    uid = _run_as_user(user)
    if uid is not None:
        container["securityContext"] = {"runAsUser": uid}

    pod_spec = {"containers": [container]}
    if volume_defs:
        pod_spec["volumes"] = volume_defs
    # Concrete host IPs become hostAliases; docker's "host-gateway" has no k8s
    # equivalent here (use host.minikube.internal in the manifest if needed).
    aliases = [{"ip": ip, "hostnames": [host]} for host, ip in (extra_hosts or {}).items()
               if ip and ip != "host-gateway"]
    if aliases:
        pod_spec["hostAliases"] = aliases

    deployment = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": {"app": name}},
        "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                 "template": {"metadata": {"labels": {"app": name}}, "spec": pod_spec}},
    }
    service = {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"selector": {"app": name},
                 "ports": [{"name": f"p{cp}", "port": cp, "targetPort": cp}
                           for cp in container_ports]},
    }
    return {"apiVersion": "v1", "kind": "List", "items": [deployment, service]}


class KubernetesBackend(ContainerBackend):
    """Run oblako services as Kubernetes workloads (e.g. on minikube), via kubectl.

    Each Service maps to a Deployment + Service; ``kubectl port-forward`` binds
    localhost:host_port to the in-cluster Service so oblako's boto3 clients work
    unchanged. Port-forwards live as long as the oblako process — after a restart,
    re-run start (or restart) on a service to re-establish them.
    """

    name = "kubernetes"

    def __init__(self, namespace: str | None = None, kubectl: str = "kubectl"):
        """Initialize for the given namespace (default OBLAKO_K8S_NAMESPACE / 'oblako')."""
        self.namespace = namespace or K8S_NAMESPACE
        self.kubectl = kubectl

    def _run(self, *args, stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([self.kubectl, *args], input=stdin, text=True,
                              capture_output=True)

    def _ns(self, *args) -> subprocess.CompletedProcess:
        return self._run("-n", self.namespace, *args)

    def _require_kubectl(self) -> None:
        if shutil.which(self.kubectl) is None:
            raise RuntimeError(
                "kubectl not found — install it and point it at a cluster (e.g. minikube start).")

    def ensure_image(self, image: str) -> None:
        """No image pre-pull: the cluster pulls it (use `minikube image load` for local images)."""
        self._require_kubectl()

    def _ensure_namespace(self) -> None:
        if self._run("get", "namespace", self.namespace).returncode != 0:
            self._run("create", "namespace", self.namespace)

    def run(self, *, name, image, ports, environment, volumes, extra_hosts,
            command, working_dir, user) -> None:
        """Apply a Deployment + Service, wait for it, and port-forward each port."""
        self._require_kubectl()
        self._ensure_namespace()
        manifest = build_k8s_manifests(
            name=name, image=image, ports=ports, environment=environment, volumes=volumes,
            extra_hosts=extra_hosts, command=command, working_dir=working_dir, user=user,
            namespace=self.namespace,
        )
        applied = self._run("apply", "-f", "-", stdin=json.dumps(manifest))
        if applied.returncode != 0:
            raise RuntimeError(f"kubectl apply failed: {applied.stderr.strip()}")
        self._ns("wait", "--for=condition=available", f"deployment/{name}", "--timeout=180s")
        self._start_port_forward(name, ports)

    def _start_port_forward(self, name: str, ports: dict) -> None:
        self._stop_port_forward(name)
        procs = []
        for spec, host_port in ports.items():
            container_port = int(str(spec).split("/")[0])
            # Self-healing: a Deployment reports "available" before the pod is
            # actually serving, and a port-forward that races the Service's
            # endpoints just exits. A shell loop restarts it until it sticks; the
            # wait_ready health check then succeeds once the pod serves.
            loop = (f"while true; do {self.kubectl} -n {self.namespace} port-forward "
                    f"service/{name} {host_port}:{container_port} >/dev/null 2>&1; sleep 1; done")
            procs.append(subprocess.Popen(["sh", "-c", loop], start_new_session=True))
        _port_forwards[name] = procs

    def _stop_port_forward(self, name: str) -> None:
        for proc in _port_forwards.pop(name, []):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)  # kill the loop + kubectl
            except (ProcessLookupError, PermissionError):
                pass

    def status(self, name: str) -> str:
        """Return RUNNING/STOPPED/ABSENT from the deployment's ready replicas."""
        result = self._ns("get", "deployment", name, "-o", "jsonpath={.status.readyReplicas}")
        if result.returncode != 0:
            return ABSENT
        ready = (result.stdout or "").strip()
        return RUNNING if ready and int(ready) >= 1 else STOPPED

    def remove(self, name: str) -> None:
        """Delete the Deployment + Service and stop its port-forwards."""
        self._stop_port_forward(name)
        self._ns("delete", "deployment,service", name, "--ignore-not-found")

    def stop(self, name: str) -> None:
        """Stop the service (delete its workload + port-forwards)."""
        self.remove(name)

    def logs(self, name: str, tail: int = 50) -> str:
        """Return recent logs from the deployment's pods."""
        result = self._ns("logs", f"deployment/{name}", f"--tail={tail}", "--all-containers")
        return result.stdout if result.returncode == 0 else ""


def _socket_for(runtime: str) -> str | None:
    """The Docker-API socket URL for a runtime (podman/colima), or None."""
    if os.environ.get("DOCKER_HOST"):
        return None  # explicit DOCKER_HOST wins (docker-py honours it)
    for candidate in _CANDIDATE_SOCKETS.get(runtime, []):
        path = os.path.expanduser(os.path.expandvars(candidate))
        if os.path.exists(path):
            return f"unix://{path}"
    return None


def _docker_backend_for(runtime: str) -> DockerBackend:
    return DockerBackend(base_url=_socket_for(runtime))


def docker_client():
    """A docker-py client for the configured Docker-API backend.

    The per-task compute paths (Lambda exec, Glue jobs, SageMaker local, EC2
    instances, the Studio notebook) talk to Docker directly rather than through a
    Service's ``ContainerBackend``. Routing them through here means they target
    the SAME daemon the services use — so ``OBLAKO_CONTAINER_BACKEND=podman``/
    ``colima`` works for compute too, without having to also set ``DOCKER_HOST``.
    (Kubernetes has no docker socket; the compute paths need a Docker-API runtime,
    so under ``kubernetes`` this falls back to the ambient Docker context.)
    """
    import docker

    choice = (os.environ.get("OBLAKO_CONTAINER_BACKEND") or "docker").lower()
    socket = _socket_for(choice) if choice in ("podman", "colima") else None
    return docker.DockerClient(base_url=socket) if socket else docker.from_env()


def get_backend() -> ContainerBackend:
    """Return the configured container backend (OBLAKO_CONTAINER_BACKEND, default docker)."""
    choice = (os.environ.get("OBLAKO_CONTAINER_BACKEND") or "docker").lower()
    if choice in ("kubernetes", "k8s"):
        return KubernetesBackend()
    if choice in ("podman", "colima"):
        return _docker_backend_for(choice)
    return DockerBackend()
