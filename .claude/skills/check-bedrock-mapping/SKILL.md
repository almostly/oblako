---
name: check-bedrock-mapping
description: >-
  Validate or refresh oblako's Bedrock→OpenRouter model mapping against the live
  OpenRouter catalog. Use when checking OPENROUTER_MODEL_MAP, after OpenRouter's
  catalog changes, when an OpenRouter slug might be stale/retired, when a
  bedrock-runtime OpenRouter call fails with an unknown-model error, or before
  shipping changes to the Bedrock→OpenRouter mapping.
---

# Check Bedrock → OpenRouter mapping

`OPENROUTER_MODEL_MAP` in `oblako_ml/bedrock/models.py` maps Bedrock model ids to
OpenRouter slugs. It is **pinned** to OpenRouter's catalog, which changes over
time (models get added and retired), so mapped slugs can go stale.

## Steps

1. Run the checker from the repo root:

   ```bash
   uv run python .claude/skills/check-bedrock-mapping/check_mapping.py
   ```

   It fetches the live OpenRouter catalog (`https://openrouter.ai/api/v1/models`
   — a free, unauthenticated GET, no key or cost) and compares every mapped slug
   against it.

2. **If it prints "All mapped OpenRouter slugs are live. OK"** — the mapping is
   healthy; you're done.

3. **If it lists STALE slugs** — for each stale slug it shows the Bedrock id(s)
   that point at it and the same-provider alternatives currently on OpenRouter.
   Edit `OPENROUTER_MODEL_MAP` to repoint those Bedrock ids at the nearest
   current model in the same family (prefer stable dated slugs; for fast-moving
   Claude/Llama tiers pick a current one). Then:

   ```bash
   uv run python .claude/skills/check-bedrock-mapping/check_mapping.py   # re-check (should be clean)
   uv run --extra dev pytest tests/test_bedrock_backends.py -q           # mapping unit tests still pass
   ```

   Update the `test_resolve_openrouter` assertions in
   `tests/test_bedrock_backends.py` if you changed a slug it checks.

## Notes
- Only the OpenRouter *target slugs* are validated (the things that can 404).
  Embeddings/image Bedrock models are intentionally unmapped (they have no
  OpenRouter chat equivalent and raise a clear error).
- The exit code is non-zero when stale slugs exist, so this doubles as a CI check.
