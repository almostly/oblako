"""oblako CLI: manage local AWS ML services.

Usage:
    oblako up                  Start all services
    oblako up <service>        Start one service, e.g. s3 or s3vectors
    oblako down                Stop all services
    oblako status              Show service status
    oblako dashboard           Start web dashboard (http://localhost:8000)
    oblako logs <service>      Show logs for a service
    oblako pull <model>        Pull a model into the engine (default: qwen2.5:0.5b)
    oblako models              List available Ollama models
    oblako test                Run unit tests
    oblako test-integration    Run integration tests
"""

import argparse
import sys

from oblako import ports
from oblako.engines import host
from oblako.services.platform import Oblako


def _check_docker():
    """Verify Docker is reachable."""
    try:
        import docker

        docker.from_env().ping()
    except Exception:
        print("Error: Docker is not running. Start Docker and try again.")
        sys.exit(1)


# -----------------------------------------------------------------------------------------------
# Service Commands
# -----------------------------------------------------------------------------------------------
def cmd_up(args):
    """Start all services, or a single named service."""
    if args.service in host.ENGINES:  # an in-process engine: no container
        try:
            url = host.start(args.service)
        except RuntimeError as err:
            print(f"Error: {err}")
            sys.exit(1)
        print(
            f"{args.service} is running on {url} (logs: {host.logfile(args.service)})"
        )
        return
    _check_docker()
    oblako = Oblako()
    if args.service:
        svc = _get_service(oblako, args.service)
        svc.start()
        svc.wait_ready()
    else:
        oblako.up()
        print("Waiting for services...")
        readiness = oblako.wait_ready(timeout=60)
        for name, ready in readiness.items():
            status = "ready" if ready else "not ready"
            print(f"  {name}: {status}")


def cmd_down(args):
    """Stop all services, or a single named service."""
    if args.service in host.ENGINES:
        stopped = host.stop(args.service)
        print(
            f"{args.service} stopped"
            if stopped
            else f"{args.service} was not started by oblako up"
        )
        return
    _check_docker()
    oblako = Oblako()
    if args.service:
        svc = _get_service(oblako, args.service)
        svc.stop()
    else:
        oblako.down()


def cmd_status(args):
    """Print the status of all user-facing services."""
    _check_docker()
    oblako = Oblako()
    statuses = oblako.status()
    for name in host.ENGINES:  # in-process engines; sagemaker also has a count
        state, extra = host.status(name), statuses.get(name)
        statuses[name] = f"{state} ({extra})" if extra and extra != "idle" else state
    for name, state in statuses.items():
        print(f"  {name}: {state}")


def cmd_logs(args):
    """Print recent logs for a named service."""
    if args.service in host.ENGINES:
        log = host.logfile(args.service)
        lines = log.read_text(errors="replace").splitlines() if log.exists() else []
        print("\n".join(lines[-args.tail :]))
        return
    _check_docker()
    oblako = Oblako()
    svc = _get_service(oblako, args.service)
    print(svc.logs(tail=args.tail))


# -----------------------------------------------------------------------------------------------
# Model Commands
# -----------------------------------------------------------------------------------------------
def cmd_pull(args):
    """Pull a model into the Ollama engine."""
    _check_docker()
    from oblako.engines.bedrock.models import DEFAULT_MODEL

    oblako = Oblako()
    model = args.model or DEFAULT_MODEL
    print(f"Pulling {model}...")
    oblako.bedrock.pull_model(model)


def cmd_models(args):
    """List models available in the local Ollama engine."""
    _check_docker()
    oblako = Oblako()
    models = oblako.bedrock.list_models()
    if models:
        for m in models:
            print(f"  {m}")
    else:
        print("No models. Run: oblako pull")


# -----------------------------------------------------------------------------------------------
# Dashboard
# -----------------------------------------------------------------------------------------------
def cmd_dashboard(args):
    """Start the web dashboard and open it in the default browser."""
    _check_docker()
    import subprocess
    import webbrowser

    port = args.port or ports.DASHBOARD
    print(f"Starting oblako dashboard on http://localhost:{port}")
    webbrowser.open(f"http://localhost:{port}")
    subprocess.call(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "oblako.dashboard.api:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
        ]
    )


# Notebook
def cmd_notebook(args):
    """Launch JupyterLab pre-wired so unmodified boto3 hits oblako's local services."""
    try:
        import jupyterlab  # noqa: F401
    except ImportError:
        print(
            "JupyterLab isn't installed. Install the extra: pip install 'oblako[notebook]'"
        )
        sys.exit(1)
    from oblako import notebook

    port = args.port or ports.NOTEBOOK
    print(f"Launching JupyterLab on http://localhost:{port}")
    print(
        "kernel is pre-wired: boto3.client('s3') etc. hit oblako (run 'oblako up' for the services)"
    )
    sys.exit(notebook.launch(port=port, workdir=args.dir))


# -----------------------------------------------------------------------------------------------
# Redshift Data API
# -----------------------------------------------------------------------------------------------
def cmd_redshift_data(args):
    """Run the Redshift Data API server (boto3 'redshift-data' endpoint)."""
    import uvicorn

    port = args.port or ports.REDSHIFT_DATA
    print(f"Starting Redshift Data API on http://localhost:{port}")
    print("  point boto3 at it: boto3.client('redshift-data', endpoint_url=...)")
    from oblako.engines.redshift_data.app import app
    from oblako.engines.identity import identify

    uvicorn.run(identify(app, "redshift_data"), host="0.0.0.0", port=port)


def cmd_bedrock_runtime(args):
    """Run the Bedrock Runtime server (boto3 'bedrock-runtime' endpoint -> Ollama)."""
    import uvicorn

    port = args.port or ports.BEDROCK_RUNTIME
    print(f"Starting Bedrock Runtime on http://localhost:{port}")
    print("  point boto3 at it: boto3.client('bedrock-runtime', endpoint_url=...)")
    from oblako.engines.bedrock_runtime.app import app
    from oblako.engines.identity import identify

    uvicorn.run(identify(app, "bedrock_runtime"), host="0.0.0.0", port=port)


def cmd_rds_data(args):
    """Run the RDS Data API server (boto3 'rds-data' endpoint -> RDS engine)."""
    import uvicorn

    port = args.port or ports.RDS_DATA
    print(f"Starting RDS Data API on http://localhost:{port}")
    print("  point boto3 at it: boto3.client('rds-data', endpoint_url=...)")
    from oblako.engines.rds_data.app import app
    from oblako.engines.identity import identify

    uvicorn.run(identify(app, "rds_data"), host="0.0.0.0", port=port)


# CloudFormation
def cmd_cloudformation(args):
    """Run the CloudFormation server (boto3 'cloudformation' endpoint -> oblako engines).

    Long-lived so `aws cloudformation deploy` / `sam deploy` can target it:
        export AWS_ENDPOINT_URL_CLOUDFORMATION=http://localhost:5601
    """
    import uvicorn

    port = args.port or ports.CLOUDFORMATION
    print(f"Starting CloudFormation on http://localhost:{port}")
    print("point the AWS CLI / SAM at it:")
    print(f"export AWS_ENDPOINT_URL_CLOUDFORMATION=http://localhost:{port}")
    print(
        "supported resources: S3::Bucket, DynamoDB::Table, Redshift::Cluster, RDS::DBInstance"
    )
    from oblako.engines.cloudformation.app import app
    from oblako.engines.identity import identify

    uvicorn.run(identify(app, "cloudformation"), host="0.0.0.0", port=port)


# -----------------------------------------------------------------------------------------------
# Bedrock AgentCore (local runtime)
# -----------------------------------------------------------------------------------------------
def cmd_agentcore(args):
    """Run or invoke a local Bedrock AgentCore agent."""
    from oblako.engines import agentcore

    if args.action == "run":
        agentcore.run(args.target, port=args.port)
    else:  # invoke
        import json

        payload = json.loads(args.target) if args.target else {}
        print(json.dumps(agentcore.invoke(payload, port=args.port), indent=2))


# -----------------------------------------------------------------------------------------------
# Test Commands
# -----------------------------------------------------------------------------------------------
def cmd_test(args):
    """Run unit tests."""
    import subprocess

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_bedrock_adapter.py",
        "tests/test_services.py",
        "-v",
    ]
    sys.exit(subprocess.call(cmd))


def cmd_test_integration(args):
    """Run integration tests."""
    import subprocess

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/",
        "-v",
        "--ignore=tests/test_bedrock_adapter.py",
        "--ignore=tests/test_services.py",
    ]
    sys.exit(subprocess.call(cmd))


def cmd_trust(args):
    """Trust redshift-local's TLS cert in a venv's redshift-connector bundle."""
    from oblako.services import RedshiftService

    try:
        print(RedshiftService().trust_cert(python_exe=args.python))
    except Exception as e:  # noqa: BLE001 - surface a clear message, not a trace
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    print(
        "Now use sslmode=verify-ca: dbt profile `sslmode: verify-ca`, or "
        "redshift_connector.connect(..., ssl=True, sslmode='verify-ca'). "
        "Re-run after a redshift-connector reinstall."
    )


# -----------------------------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------------------------
def _get_service(oblako: Oblako, name: str):
    services = {
        "bedrock": oblako.bedrock,
        "ollama": oblako.bedrock,  # alias (engine)
        "opensearch": oblako.opensearch,
        "redshift": oblako.redshift,
        "rds": oblako.rds,
        "aurora": oblako.rds,  # alias (shares the rds engine + control plane)
        "moto": oblako.moto,
        "s3": oblako.s3,
        "dynamodb": oblako.dynamodb,
        "stepfunctions": oblako.stepfunctions,
    }
    if name not in services:
        print(f"Unknown service: {name}")
        print(f"Available: {', '.join([*services, *host.ENGINES])}")
        sys.exit(1)
    return services[name]


# -----------------------------------------------------------------------------------------------
# Entry Point
# -----------------------------------------------------------------------------------------------
def main():
    """Parse arguments and dispatch to the appropriate command handler."""
    parser = argparse.ArgumentParser(prog="oblako", description="Local AWS platform")
    sub = parser.add_subparsers(dest="command")

    p_up = sub.add_parser("up", help="Start services")
    p_up.add_argument("service", nargs="?", help="Start a specific service")
    p_up.set_defaults(func=cmd_up)

    p_down = sub.add_parser("down", help="Stop services")
    p_down.add_argument("service", nargs="?", help="Stop a specific service")
    p_down.set_defaults(func=cmd_down)

    p_status = sub.add_parser("status", help="Show service status")
    p_status.set_defaults(func=cmd_status)

    p_logs = sub.add_parser("logs", help="Show service logs")
    p_logs.add_argument("service", help="Service name")
    p_logs.add_argument("-n", "--tail", type=int, default=50, help="Number of lines")
    p_logs.set_defaults(func=cmd_logs)

    p_pull = sub.add_parser("pull", help="Pull an Ollama model")
    p_pull.add_argument("model", nargs="?", help="Model name (default: qwen2.5:0.5b)")
    p_pull.set_defaults(func=cmd_pull)

    p_models = sub.add_parser("models", help="List Ollama models")
    p_models.set_defaults(func=cmd_models)

    p_test = sub.add_parser("test", help="Run unit tests")
    p_test.set_defaults(func=cmd_test)

    p_ti = sub.add_parser("test-integration", help="Run integration tests")
    p_ti.set_defaults(func=cmd_test_integration)

    p_dash = sub.add_parser("dashboard", help="Start the web dashboard")
    p_dash.add_argument(
        "-p", "--port", type=int, default=8000, help="Port (default: 8000)"
    )
    p_dash.set_defaults(func=cmd_dashboard)

    p_nb = sub.add_parser(
        "notebook", help="Launch JupyterLab wired to oblako's services"
    )
    p_nb.add_argument(
        "-p", "--port", type=int, default=8888, help="Port (default: 8888)"
    )
    p_nb.add_argument(
        "--dir", help="Notebook workspace dir (default: ~/.oblako/notebooks)"
    )
    p_nb.set_defaults(func=cmd_notebook)

    p_rsd = sub.add_parser("redshift-data", help="Run the Redshift Data API server")
    p_rsd.add_argument(
        "-p", "--port", type=int, default=8002, help="Port (default: 8002)"
    )
    p_rsd.set_defaults(func=cmd_redshift_data)

    p_trust = sub.add_parser(
        "trust",
        help="Trust redshift-local's TLS cert so redshift-connector/dbt can use sslmode=verify-ca",
    )
    p_trust.add_argument(
        "--python",
        help="Interpreter of the venv to patch (default: the current one)",
    )
    p_trust.set_defaults(func=cmd_trust)

    p_brt = sub.add_parser("bedrock-runtime", help="Run the Bedrock Runtime server")
    p_brt.add_argument(
        "-p", "--port", type=int, default=8004, help="Port (default: 8004)"
    )
    p_brt.set_defaults(func=cmd_bedrock_runtime)

    p_rd = sub.add_parser("rds-data", help="Run the RDS Data API server")
    p_rd.add_argument(
        "-p", "--port", type=int, default=8006, help="Port (default: 8006)"
    )
    p_rd.set_defaults(func=cmd_rds_data)

    p_cfn = sub.add_parser("cloudformation", help="Run the CloudFormation server")
    p_cfn.add_argument(
        "-p", "--port", type=int, default=5601, help="Port (default: 5601)"
    )
    p_cfn.set_defaults(func=cmd_cloudformation)

    p_ac = sub.add_parser("agentcore", help="Run or invoke a local AgentCore agent")
    p_ac.add_argument(
        "action",
        choices=["run", "invoke"],
        help="run an agent file, or invoke a running one",
    )
    p_ac.add_argument("target", help="agent .py file (run) or JSON payload (invoke)")
    p_ac.add_argument(
        "-p", "--port", type=int, default=8080, help="Port (default: 8080)"
    )
    p_ac.set_defaults(func=cmd_agentcore)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
