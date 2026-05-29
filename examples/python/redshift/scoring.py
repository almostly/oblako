"""Amazon Redshift as a local data warehouse for batch scoring.

Mirrors the pattern from credit-risk-modeling batch_scoring:
store customer scores and limit decisions in Redshift.

Backed by hearthsim/pgredshift on port 5439, so Redshift-isms like
`SET query_group` and the JSON UDFs work the same as on real Redshift.

Prerequisites:
    make up
"""

import random

from oblako.services import RedshiftService

rs = RedshiftService()
conn = rs.connect()
conn.autocommit = True
cur = conn.cursor()

# Tag this batch job's queries, the way a real Redshift batch run would.
cur.execute("SET query_group TO 'batch_scoring'")

# Create scoring tables (Redshift-compatible SQL)
cur.execute("""
    CREATE TABLE IF NOT EXISTS customer_scores (
        customer_id TEXT PRIMARY KEY,
        score FLOAT NOT NULL,
        pd FLOAT NOT NULL,
        segment TEXT,
        scored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")

cur.execute("""
    CREATE TABLE IF NOT EXISTS limit_decisions (
        customer_id TEXT PRIMARY KEY,
        current_limit FLOAT,
        new_limit FLOAT,
        decision TEXT NOT NULL,
        reason TEXT,
        decided_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")
print("Created tables: customer_scores, limit_decisions")

# Simulate batch scoring
customers = []
for i in range(50):
    cid = f"CUST-{i:04d}"
    score = random.gauss(650, 80)
    pd = 1.0 / (1.0 + 2.718 ** (-(score - 600) / 50))
    segment = "prime" if score > 700 else "near-prime" if score > 600 else "subprime"
    customers.append((cid, round(score, 1), round(pd, 4), segment))

cur.executemany(
    "INSERT INTO customer_scores (customer_id, score, pd, segment) VALUES (%s, %s, %s, %s) ON CONFLICT (customer_id) DO UPDATE SET score=EXCLUDED.score, pd=EXCLUDED.pd, segment=EXCLUDED.segment",
    customers,
)
print(f"Scored {len(customers)} customers")

# Apply limit decisions (business rules)
cur.execute("SELECT customer_id, score FROM customer_scores")
decisions = []
for cid, score in cur.fetchall():
    current_limit = random.uniform(1000, 10000)
    if score > 700:
        new_limit = current_limit * 1.2
        decision, reason = "INCREASE", "high score"
    elif score < 550:
        new_limit = current_limit * 0.8
        decision, reason = "DECREASE", "low score"
    else:
        new_limit = current_limit
        decision, reason = "KEEP", "stable"
    decisions.append(
        (cid, round(current_limit, 2), round(new_limit, 2), decision, reason)
    )

cur.executemany(
    "INSERT INTO limit_decisions (customer_id, current_limit, new_limit, decision, reason) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (customer_id) DO UPDATE SET current_limit=EXCLUDED.current_limit, new_limit=EXCLUDED.new_limit, decision=EXCLUDED.decision, reason=EXCLUDED.reason",
    decisions,
)
print(f"Applied {len(decisions)} limit decisions")

# Summary query
cur.execute("""
    SELECT d.decision, COUNT(*), ROUND(AVG(s.score)::numeric, 1)
    FROM limit_decisions d
    JOIN customer_scores s ON d.customer_id = s.customer_id
    GROUP BY d.decision
    ORDER BY d.decision
""")
print("\nDecision summary:")
print(f"{'Decision':<12} {'Count':>6} {'Avg Score':>10}")
for decision, count, avg_score in cur.fetchall():
    print(f"{decision:<12} {count:>6} {avg_score:>10}")

# Segment distribution
cur.execute(
    "SELECT segment, COUNT(*) FROM customer_scores GROUP BY segment ORDER BY segment"
)
print("\nSegment distribution:")
for segment, count in cur.fetchall():
    print(f"{segment}: {count}")

cur.close()
conn.close()
