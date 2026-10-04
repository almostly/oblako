# Contributing to oblako

Thanks for helping. Issues and pull requests are welcome.

## Set up

oblako uses [uv](https://docs.astral.sh/uv/) and needs a container runtime
(Docker, Podman, Colima or Apple `container`).

```bash
git clone https://github.com/almostly/oblako.git
cd oblako
uv sync --extra dev
uvx pre-commit install --hook-type pre-commit --hook-type commit-msg
```

## Test

```bash
uv run pytest -m "not integration"   # unit tests, no containers
uv run oblako up                      # start the services
uv run oblako trust                   # verified TLS for redshift_connector
uv run pytest -m integration          # tests against the running services
```

Integration tests run against real engines. A change to one service only needs
that service's tests, for example `uv run pytest tests/redshift`.

## Style

- `ruff check` and `ruff format` (run by pre-commit), and pydocstyle for the
  `oblako/` package.
- `uvx ty check` on the files you change.
- Commit messages and pull request titles follow `Service(+|~|-): description`:
  `+` adds, `~` changes, `-` removes. For example,
  `Redshift(+): CREATE TABLE ... USING ICEBERG writes through the Glue catalog`.

## Pull requests

Keep a pull request to one topic, with tests for the behavior it adds or fixes.
Say how you verified it, especially for behavior that should match AWS.

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
