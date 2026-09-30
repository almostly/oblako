# Container runtimes

oblako isn't hard-wired to Docker. Every service runs through a pluggable
`ContainerBackend`, chosen with the `OBLAKO_CONTAINER_BACKEND` environment
variable, so the same `oblako up` works on Docker, Podman, Colima, a Kubernetes
cluster, or Apple's `container`.

| `OBLAKO_CONTAINER_BACKEND` | Runtime | How it's reached |
|---|---|---|
| `docker` *(default)* | Docker Engine | the Docker socket |
| `podman` | Podman | the Podman socket (auto-detected) |
| `colima` | Colima | the Colima Docker socket (auto-detected) |
| `kubernetes` / `k8s` | a Kubernetes cluster | pods + `kubectl port-forward` |
| `apple` | Apple `container` (macOS 26+) | lightweight per-container VMs |

`DOCKER_HOST` always takes precedence for the Docker-API runtimes.

## Docker (default)

Nothing to configure:

```bash
oblako up
```

## Podman / Colima

Both speak the Docker Engine API, so they share the Docker backend, oblako
auto-detects their socket (or honour `DOCKER_HOST`):

```bash
export OBLAKO_CONTAINER_BACKEND=podman   # or: colima
oblako up
```

## Kubernetes

Each service becomes a Deployment + Service in a namespace, and a
`kubectl port-forward` makes `localhost:PORT` reach the in-cluster Service, so
your boto3 code is unchanged. Needs `kubectl` and a cluster (minikube, kind,
EKS, …).

```bash
export OBLAKO_CONTAINER_BACKEND=kubernetes
export OBLAKO_K8S_NAMESPACE=oblako   # optional, defaults to "oblako"
oblako up
```

The per-task compute paths (Lambda exec, Glue jobs, SageMaker local) need a real
Docker daemon; under Kubernetes they fall back to the ambient Docker context.

## Apple `container` (macOS 26+)

Runs Linux containers in lightweight VMs. `container run -p` publishes to
`localhost` just like Docker, so oblako's fixed-endpoint contract holds, boto3
still hits `localhost:9000`, etc.

```bash
export OBLAKO_CONTAINER_BACKEND=apple
oblako up
```

**Limitations**

- The always-on services run on Apple `container`. The three **per-task compute
  services**, **Lambda**, **SageMaker**, and **Glue**, are the exception:
  each one launches *its own* container per job and talks to a Docker daemon
  directly (moto spawns the Lambda runtime, oblako's SageMaker engine and the SDK's
  v3 local modes start job and endpoint containers, and Glue runs the Spark image), so they need a Docker-API
  backend (Docker / Podman / Colima). This is an upstream coupling to Docker, not
  an oblako limit, so switch those workloads to a Docker backend.
- Apple `container` doesn't emulate amd64, so **amd64-only images won't run**:
  oblako's own images (e.g. Redshift) are multi-arch; third-party amd64-only
  images need a Docker backend.
- `host.docker.internal` isn't provided; oblako substitutes the vmnet gateway
  automatically.
