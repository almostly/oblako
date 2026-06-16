# redshift-local

A local **Amazon Redshift** stand-in: a PostgreSQL 16 image that *impersonates*
Redshift so Amazon's `redshift-connector` driver (and **dbt-redshift**) connect
**natively — no proxy or shim**. Part of [oblako](https://github.com/almostly/oblako),
a local AWS platform for your laptop.

Multi-arch (`amd64` + `arm64`) — runs on Docker **and** Apple `container`.

## Why

Stock PostgreSQL rejects redshift-connector's handshake with
`FATAL: unrecognized configuration parameter "client_protocol_version"`.
This image registers the Redshift-only startup parameters as accepted GUCs and
reports `server_version = 8.0.2`, so the driver just works.

## Run

```bash
docker run -d --name redshift \
  -p 5439:5432 \
  -e POSTGRES_USER=oblako \
  -e POSTGRES_PASSWORD=oblako \
  -e POSTGRES_DB=oblako \
  -e POSTGRES_HOST_AUTH_METHOD=md5 \
  deburky/redshift-local:16
```

`POSTGRES_HOST_AUTH_METHOD=md5` is required — Redshift uses md5 auth, which
redshift-connector expects.

## Connect

```python
# Amazon's driver — connects natively, no proxy
import redshift_connector
con = redshift_connector.connect(host="localhost", port=5439, database="oblako",
                                 user="oblako", password="oblako", ssl=False)

# or plain psycopg2
import psycopg2
con = psycopg2.connect(host="localhost", port=5439, dbname="oblako",
                       user="oblako", password="oblako")
```

dbt-redshift: point a `type: redshift` profile at `host: localhost`, `port: 5439`,
`sslmode: disable`.

## What it provides

- Redshift system tables (`stl_scan`, `stv_blocklist`, `stv_tbl_perm`)
- Redshift UDFs (`json_array_length`, `json_extract_path_text`, `median`, …)
- `SET query_group`
- `LANGUAGE plpythonu` / `plpython3u` for Python UDFs

## Good to know

- It's an **emulator for local dev**, not real Redshift — a modern PostgreSQL
  underneath (`version()` reports PG16; `server_version` reports 8.0.2).
- Redshift-**physical** DDL (`DISTKEY`/`SORTKEY`/`ENCODE`, late-binding views,
  `SUPER`) runs on PostgreSQL semantics, so it won't accept that syntax.
- Python UDFs run as **Python 3** (real Redshift's are Python 2, which Amazon is
  sunsetting).

## Alternatives considered

- **LocalStack Pro** — control-plane-only on the base tier (no queryable engine,
  redshift-data unlicensed, same `client_protocol_version` rejection); fine for
  mocking AWS APIs, not for running a warehouse.
- **`hearthsim/pgredshift`** — the prior art this borrows its shims from, but
  amd64-only (no Apple silicon), PG10/EOL, and still needs a wire proxy for
  redshift-connector.

## Tags

- `16` — PostgreSQL 16 base (current)
- `latest`

Built from [`oblako/images/redshift`](https://github.com/almostly/oblako/tree/main/oblako/images/redshift).
