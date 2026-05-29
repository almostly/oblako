"""Text2SQL — Bedrock + Redshift Data API, end-to-end against oblako.

Mirrors the rencode self-service pattern: a natural-language question is
translated to SQL by a Bedrock-hosted model, then executed against Redshift
through the Data API. Both APIs use plain boto3 — the same code would run
against real AWS if `AWS_ENDPOINT_URL_*` were unset.

What's local here:
  * Bedrock-Runtime is oblako's in-process `bedrock_runtime` server, backed
    by Ollama (model: qwen2.5:0.5b by default).
  * Redshift Data API is oblako's in-process `redshift_data` server, which
    forwards execute_statement to the pgredshift container.

Run from the repo root:

    uv run python examples/python/redshift/text2sql.py
"""

from __future__ import annotations

import re
import textwrap
import time

from oblako.bedrock.adapter import BedrockAdapter
from oblako.services import RedshiftService

CLUSTER = "rencode-dw"
DATABASE = "oblako"
USER = "oblako"
MODEL_ID = "qwen2.5:0.5b"

SCHEMA_DDL = textwrap.dedent("""
    CREATE TABLE IF NOT EXISTS public.applications (
        id          INT,
        applicant   VARCHAR(64),
        amount      NUMERIC(10, 2),
        status      VARCHAR(16),   -- 'approved' | 'rejected' | 'pending'
        product     VARCHAR(32),   -- 'card' | 'loan' | 'mortgage'
        created_at  DATE
    );
""").strip()

SEED_ROWS = [
    (1, "alice",   1200.00, "approved", "card",     "2025-04-01"),
    (2, "bob",     5000.00, "rejected", "loan",     "2025-04-02"),
    (3, "carol",   3500.00, "approved", "loan",     "2025-04-08"),
    (4, "david",   8000.00, "approved", "mortgage", "2025-04-10"),
    (5, "eve",      900.00, "pending",  "card",     "2025-04-12"),
    (6, "frank",   2100.00, "approved", "card",     "2025-04-12"),
    (7, "grace",   4400.00, "rejected", "loan",     "2025-04-14"),
    (8, "heidi",  12000.00, "approved", "mortgage", "2025-04-15"),
]


def ensure_cluster_and_table(rs: RedshiftService) -> None:
    """Create the cluster + seed the demo table (idempotent)."""
    redshift = rs.get_client()
    try:
        redshift.create_cluster(
            ClusterIdentifier=CLUSTER, NodeType="ra3.xlplus", NumberOfNodes=2,
            MasterUsername=USER, MasterUserPassword="Oblako123", DBName=DATABASE,
        )
    except redshift.exceptions.ClusterAlreadyExistsFault:
        pass

    data = rs.get_data_client()
    run_sql(data, SCHEMA_DDL)
    # Refill: TRUNCATE then INSERT, so the example is repeatable.
    run_sql(data, "TRUNCATE public.applications")
    values_sql = ", ".join(
        f"({i},'{a}',{amt},'{s}','{p}','{d}')"
        for i, a, amt, s, p, d in SEED_ROWS
    )
    run_sql(data, f"INSERT INTO public.applications VALUES {values_sql}")


def run_sql(data, sql: str):
    """Execute SQL via the Redshift Data API; return rows (or None for DML)."""
    stmt_id = data.execute_statement(
        ClusterIdentifier=CLUSTER, Database=DATABASE, DbUser=USER, Sql=sql,
    )["Id"]
    while True:
        desc = data.describe_statement(Id=stmt_id)
        if desc["Status"] == "FINISHED":
            break
        if desc["Status"] in ("FAILED", "ABORTED"):
            raise RuntimeError(f"SQL failed: {desc.get('Error', '?')}\nSQL: {sql}")
        time.sleep(0.2)
    if not desc.get("HasResultSet"):
        return None
    result = data.get_statement_result(Id=stmt_id)
    columns = [c["name"] for c in result["ColumnMetadata"]]
    rows = []
    for row in result.get("Records", []):
        rows.append({c: next(iter(cell.values()), None) if cell and "isNull" not in cell else None
                     for c, cell in zip(columns, row)})
    return rows


def generate_sql(adapter: BedrockAdapter, question: str) -> str:
    """Ask the local Bedrock model to translate the NL question into SQL."""
    system = (
        "You generate read-only SQL for Amazon Redshift. "
        "Reply with ONLY the SQL — no commentary, no markdown, no semicolons. "
        "Only SELECT statements are allowed."
    )
    prompt = textwrap.dedent(f"""
        Schema:
        {SCHEMA_DDL}

        Question: {question}

        SQL:
    """).strip()
    result = adapter.converse(
        model_id=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        system=[{"text": system}],
        inference_config={"maxTokens": 200, "temperature": 0.0},
    )
    raw = result["output"]["message"]["content"][0]["text"]
    return extract_sql(raw)


def extract_sql(text: str) -> str:
    """Pull the first SELECT statement out of the model's reply (tolerates ```sql blocks, prose)."""
    fence = re.search(r"```(?:sql)?\s*(.+?)```", text, re.DOTALL | re.IGNORECASE)
    candidate = fence.group(1) if fence else text
    select = re.search(r"\bSELECT\b.+", candidate, re.DOTALL | re.IGNORECASE)
    if not select:
        return text.strip()
    sql = select.group(0).strip().rstrip(";")
    # Guard: only allow read-only verbs at the head.
    if not re.match(r"^\s*(SELECT|WITH)\b", sql, re.IGNORECASE):
        raise ValueError(f"Refusing to run non-SELECT SQL:\n{sql}")
    return sql


def pretty(rows: list[dict]) -> str:
    if not rows:
        return "(no rows)"
    cols = list(rows[0].keys())
    widths = [max(len(c), *(len(str(r[c])) for r in rows)) for c in cols]
    header = " | ".join(c.ljust(w) for c, w in zip(cols, widths))
    sep = "-+-".join("-" * w for w in widths)
    body = "\n".join(" | ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)) for r in rows)
    return f"{header}\n{sep}\n{body}"


QUESTIONS = [
    "How many approved applications are there in total?",
    "What is the total approved loan amount per product?",
    "Who has the largest approved amount?",
]


def main() -> None:
    rs = RedshiftService()
    rs.wait_ready(timeout=2) or rs.start()
    ensure_cluster_and_table(rs)
    data = rs.get_data_client()
    adapter = BedrockAdapter()

    for q in QUESTIONS:
        print(f"\nQ: {q}")
        try:
            sql = generate_sql(adapter, q)
        except Exception as e:  # noqa: BLE001
            print(f"  model didn't return SQL: {e}")
            continue
        print(f"SQL: {sql}")
        try:
            rows = run_sql(data, sql)
        except RuntimeError as e:
            print(f"  query failed: {e}")
            continue
        print("Result:")
        print(textwrap.indent(pretty(rows or []), "  "))


if __name__ == "__main__":
    main()
