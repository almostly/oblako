"""ELBv2 service: ALB control plane (moto) + a real reverse-proxy per load balancer.

On AWS an Application Load Balancer is a managed reverse proxy: a listener on a
port forwards to a target group, which load-balances over registered targets and
health-checks them. oblako makes that real and local the same way it makes an EC2
instance a container: **each load balancer is its own Caddy container**, listening
on an oblako-assigned host port and reverse-proxying to the targets (ECS tasks
published on the host), with the target group's health-check path.

moto owns the control-plane metadata (describe load balancers / target groups /
listeners); oblako owns the data plane (the proxy actually routes traffic). The
LB's ``DNSName`` resolves to ``localhost:<listener-port>``, so a stack's
``ServiceURL`` output is curlable.
"""

from __future__ import annotations

import socket
import uuid
from pathlib import Path

from .boto import BotoService, client
from .moto import MotoService
from .backends import publish

SERVICE_LABEL = "oblako.service"
LB_LABEL = "oblako.elbv2.lb-arn"
ELB_DIR = Path.home() / ".oblako" / "elbv2"
PROXY_PORT = 8080  # the listen port inside each ALB proxy container


def _docker():
    """Return a Docker client for the configured container backend."""
    from .backends import docker_client

    return docker_client()


def _free_port() -> int:
    """Return a free TCP port on the host."""
    s = socket.socket()
    try:
        s.bind(("", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _proxy_name(lb_id: str) -> str:
    """Return the proxy container name for a load balancer."""
    return f"oblako-elb-{lb_id[:12]}"


def _alb_caddyfile(upstreams: list[str], health_path: str) -> str:
    """Return a Caddy config: listen on PROXY_PORT, round-robin to the targets."""
    lines = ["{", "    auto_https off", "    admin off", "}", "", f":{PROXY_PORT} {{"]
    if upstreams:
        lines.append(f"    reverse_proxy {' '.join(upstreams)} {{")
        lines.append("        lb_policy round_robin")
        if health_path:
            lines.append(f"        health_uri {health_path}")
            lines.append("        health_interval 10s")
        lines.append("    }")
    else:
        # No targets yet: answer 503 rather than fail to load the config.
        lines.append("    respond 503")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


class Elbv2Service(BotoService):
    """Application Load Balancer, moto control plane + a real Caddy proxy per LB."""

    aws_services = ("elbv2",)

    name = "elbv2"

    def __init__(self, moto: MotoService):
        """Wire to the shared moto endpoint (moto owns ELBv2 metadata)."""
        self.moto = moto
        self._lbs: dict[str, dict] = {}  # lb_arn -> {id, host_port}
        self._tgs: dict[str, dict] = {}  # tg_arn -> {health_path, targets: list[str]}
        self._listeners: dict[str, dict] = {}  # listener_arn -> {lb_arn, tg_arn}

    @property
    def endpoint_url(self) -> str:
        """Moto serves ELBv2 at the same endpoint as every other AWS API."""
        return self.moto.endpoint_url

    def _default_subnets(self) -> list[str]:
        """Subnet IDs from moto's default VPC (an ALB needs at least two)."""
        ec2 = client("ec2", self.endpoint_url)
        subs = ec2.describe_subnets(
            Filters=[{"Name": "default-for-az", "Values": ["true"]}]
        )["Subnets"]
        if not subs:
            subs = ec2.describe_subnets()["Subnets"]
        return [s["SubnetId"] for s in subs][:2]

    def _default_vpc(self) -> str:
        """Return the default VPC id from moto (target groups require one)."""
        ec2 = client("ec2", self.endpoint_url)
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])[
            "Vpcs"
        ]
        if not vpcs:
            vpcs = ec2.describe_vpcs()["Vpcs"]
        return vpcs[0]["VpcId"]

    # Control plane (moto) + local wiring
    def create_load_balancer(self, **kwargs) -> dict:
        """Record the LB in moto and assign it a host listener port.

        ``Subnets`` are optional locally: if omitted, moto's default-VPC subnets
        are used, so the caller doesn't have to know any subnet IDs.
        """
        if "Subnets" not in kwargs and "SubnetMappings" not in kwargs:
            kwargs["Subnets"] = self._default_subnets()
        lb = self.get_client().create_load_balancer(**kwargs)["LoadBalancers"][0]
        arn = lb["LoadBalancerArn"]
        self._lbs[arn] = {"id": uuid.uuid4().hex, "host_port": _free_port()}
        lb["DNSName"] = self.dns_name(arn)  # local, curlable name
        return lb

    def create_target_group(self, **kwargs) -> dict:
        """Record the target group in moto and track its health-check path.

        ``VpcId`` is optional locally: the default VPC is used when omitted.
        """
        if "VpcId" not in kwargs:
            kwargs["VpcId"] = self._default_vpc()
        tg = self.get_client().create_target_group(**kwargs)["TargetGroups"][0]
        self._tgs[tg["TargetGroupArn"]] = {
            "health_path": kwargs.get("HealthCheckPath", "/"),
            "targets": [],
        }
        return tg

    def create_listener(self, **kwargs) -> dict:
        """Record the listener in moto and map its LB to the forwarded target group."""
        listener = self.get_client().create_listener(**kwargs)["Listeners"][0]
        lb_arn = kwargs["LoadBalancerArn"]
        tg_arn = next(
            (
                a.get("TargetGroupArn")
                for a in kwargs.get("DefaultActions", [])
                if a.get("TargetGroupArn")
            ),
            None,
        )
        self._listeners[listener["ListenerArn"]] = {"lb_arn": lb_arn, "tg_arn": tg_arn}
        self._apply(lb_arn)
        return listener

    def register_targets(self, target_group_arn: str, host_ports: list[int]) -> None:
        """Register ECS-task host ports as targets and refresh the proxy."""
        tg = self._tgs.setdefault(target_group_arn, {"health_path": "/", "targets": []})
        for hp in host_ports:
            upstream = f"host.docker.internal:{hp}"
            if upstream not in tg["targets"]:
                tg["targets"].append(upstream)
        for lb_arn in self._lbs_for_tg(target_group_arn):
            self._apply(lb_arn)

    def deregister_targets(self, target_group_arn: str, host_ports: list[int]) -> None:
        """Remove targets and refresh the proxy."""
        tg = self._tgs.get(target_group_arn)
        if not tg:
            return
        drop = {f"host.docker.internal:{hp}" for hp in host_ports}
        tg["targets"] = [t for t in tg["targets"] if t not in drop]
        for lb_arn in self._lbs_for_tg(target_group_arn):
            self._apply(lb_arn)

    def dns_name(self, lb_arn: str) -> str:
        """Return the local hostname for an LB, so ``http://<DNSName>`` is curlable."""
        return f"localhost:{self._lbs[lb_arn]['host_port']}"

    def lb_url(self, lb_arn: str) -> str:
        """Full URL for the load balancer."""
        return f"http://{self.dns_name(lb_arn)}"

    def delete_load_balancer(self, lb_arn: str) -> None:
        """Remove the proxy container, the moto LB, and forget it (idempotent)."""
        info = self._lbs.pop(lb_arn, None)
        # Drop the proxy container (by registry id, else by label across processes).
        try:
            if info:
                _docker().containers.get(_proxy_name(info["id"])).remove(force=True)
            else:
                for c in _docker().containers.list(
                    all=True, filters={"label": f"{LB_LABEL}={lb_arn}"}
                ):
                    c.remove(force=True)
        except Exception:  # already gone
            pass
        self._listeners = {
            k: v for k, v in self._listeners.items() if v["lb_arn"] != lb_arn
        }
        try:  # also clear the moto-side record so the name frees up
            self.get_client().delete_load_balancer(LoadBalancerArn=lb_arn)
        except Exception:
            pass

    # Internals
    def _lbs_for_tg(self, tg_arn: str) -> list[str]:
        """Return the ARNs of the load balancers that forward to a target group."""
        return [
            ln["lb_arn"] for ln in self._listeners.values() if ln["tg_arn"] == tg_arn
        ]

    def _apply(self, lb_arn: str) -> None:
        """(Re)create the LB's proxy container with its current upstreams."""
        info = self._lbs.get(lb_arn)
        if not info:
            return
        tg_arn = next(
            (ln["tg_arn"] for ln in self._listeners.values() if ln["lb_arn"] == lb_arn),
            None,
        )
        tg = self._tgs.get(tg_arn, {"health_path": "/", "targets": []})
        ELB_DIR.mkdir(parents=True, exist_ok=True)
        conf_dir = ELB_DIR / info["id"]
        conf_dir.mkdir(parents=True, exist_ok=True)
        (conf_dir / "Caddyfile").write_text(
            _alb_caddyfile(tg["targets"], tg["health_path"])
        )

        client = _docker()
        name = _proxy_name(info["id"])
        try:
            client.containers.get(name).remove(force=True)
        except Exception:  # not present yet
            pass
        client.containers.run(
            "caddy:alpine",
            detach=True,
            name=name,
            ports=publish({f"{PROXY_PORT}/tcp": info["host_port"]}, client),
            volumes={str(conf_dir): {"bind": "/etc/caddy", "mode": "ro"}},
            extra_hosts={"host.docker.internal": "host-gateway"},
            labels={SERVICE_LABEL: "elbv2", LB_LABEL: lb_arn},
        )

    # Lifecycle (moto-backed; proxies are per-LB, not one service)
    def start(self) -> None:
        """Bring moto up if it isn't already (no own service container)."""
        self.moto.wait_ready(timeout=2) or self.moto.start()

    def stop(self) -> None:
        """No-op: moto owns the control plane; proxies are managed per-LB."""

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """Defer readiness to moto."""
        return self.moto.wait_ready(timeout=timeout)

    def status(self):
        """Defer status to moto."""
        return self.moto.status()
