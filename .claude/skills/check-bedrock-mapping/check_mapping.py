#!/usr/bin/env python3
"""Check oblako's Bedrock -> OpenRouter model map against the LIVE OpenRouter catalog.

`OPENROUTER_MODEL_MAP` (oblako/bedrock/models.py) is pinned to OpenRouter's
catalog, which drifts as models are added/retired. This flags any mapped slug
that OpenRouter no longer offers and suggests same-provider alternatives so the
map can be refreshed. Exits non-zero if anything is stale (CI-friendly).

Run from the repo root:  uv run python .claude/skills/check-bedrock-mapping/check_mapping.py
"""

import json
import sys
import urllib.request

from oblako.bedrock.models import OPENROUTER_MODEL_MAP

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


def live_slugs() -> set[str]:
    """Fetch the set of available OpenRouter model slugs (public, no auth/cost)."""
    with urllib.request.urlopen(OPENROUTER_MODELS_URL, timeout=20) as resp:
        data = json.load(resp)
    return {m["id"] for m in data.get("data", [])}


def main() -> int:
    try:
        live = live_slugs()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: could not fetch the OpenRouter catalog: {e}")
        return 2

    targets = sorted(set(OPENROUTER_MODEL_MAP.values()))
    stale = [s for s in targets if s not in live]

    print(f"OpenRouter catalog: {len(live)} models")
    print(
        f"Mapped slugs: {len(targets)} total — {len(targets) - len(stale)} live, {len(stale)} stale"
    )

    if not stale:
        print("\nAll mapped OpenRouter slugs are live. OK")
        return 0

    print("\nSTALE — these mapped slugs are no longer on OpenRouter:")
    for slug in stale:
        bedrock_ids = sorted(b for b, s in OPENROUTER_MODEL_MAP.items() if s == slug)
        provider = slug.split("/")[0] + "/"
        alternatives = sorted(s for s in live if s.startswith(provider))
        print(f"  {slug}")
        print(f"      used by Bedrock id(s): {bedrock_ids}")
        print(f"      same-provider alternatives: {alternatives}")
    print("\nUpdate OPENROUTER_MODEL_MAP in oblako/bedrock/models.py, then re-run.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
