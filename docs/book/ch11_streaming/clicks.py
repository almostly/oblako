import datetime
import json
import random

PAGES = ["/", "/pricing", "/docs", "/signup"]


def clicks(n, seed):
    rng = random.Random(seed)
    now = datetime.datetime.now(datetime.timezone.utc)
    for _ in range(n):
        yield {
            "user": f"user-{rng.randint(1, 6)}",
            "page": rng.choice(PAGES),
            "at": now.isoformat(timespec="seconds"),
        }


def as_records(events):
    return [
        {
            "Data": json.dumps(e).encode(),
            "PartitionKey": e["user"],
        }
        for e in events
    ]
