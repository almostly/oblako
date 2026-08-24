"""Bedrock Guardrails: a local content-policy store + ApplyGuardrail evaluation.

Real Bedrock Guardrails run managed classifiers; oblako enforces the parts that
are deterministic locally - custom word filters, a small managed profanity list,
and denied topics matched by name/example - which is enough for guardrail-aware
code to create policies and see GUARDRAIL_INTERVENED when content trips them.
"""

from __future__ import annotations

import datetime
import threading
import uuid

_ACCOUNT = "000000000000"

# a tiny managed PROFANITY list stand-in (real Bedrock ships a large curated one)
_PROFANITY = {"damn", "hell", "crap"}


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class GuardrailStore:
    """In-memory guardrail registry + ApplyGuardrail evaluation."""

    def __init__(self, region: str = "us-east-1"):
        """Initialize the registry for a region."""
        self._region = region
        self._guardrails: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create(self, req: dict) -> dict:
        """Register a guardrail; return {guardrailId, guardrailArn, version, createdAt}."""
        guardrail_id = f"gr-{uuid.uuid4().hex[:11]}"
        arn = f"arn:aws:bedrock:{self._region}:{_ACCOUNT}:guardrail/{guardrail_id}"
        now = _now()
        with self._lock:
            self._guardrails[guardrail_id] = {
                "guardrailId": guardrail_id,
                "guardrailArn": arn,
                "name": req.get("name"),
                "description": req.get("description"),
                "version": "DRAFT",
                "status": "READY",
                "wordPolicy": req.get("wordPolicyConfig", {}),
                "topicPolicy": req.get("topicPolicyConfig", {}),
                "contentPolicy": req.get("contentPolicyConfig", {}),
                "blockedInputMessaging": req.get(
                    "blockedInputMessaging", "Sorry, the model cannot answer this."
                ),
                "blockedOutputsMessaging": req.get(
                    "blockedOutputsMessaging", "Sorry, the model cannot answer this."
                ),
                "createdAt": now,
                "updatedAt": now,
            }
        return {
            "guardrailId": guardrail_id,
            "guardrailArn": arn,
            "version": "DRAFT",
            "createdAt": now,
        }

    def get(self, identifier: str) -> dict | None:
        """Return a guardrail record (by id or ARN), or None if unknown."""
        with self._lock:
            record = self._resolve(identifier)
            return dict(record) if record else None

    def list(self) -> list[dict]:
        """Return a summary list of all guardrails."""
        with self._lock:
            return [
                {
                    "id": g["guardrailId"],
                    "arn": g["guardrailArn"],
                    "name": g["name"],
                    "status": g["status"],
                    "version": g["version"],
                    "createdAt": g["createdAt"],
                    "updatedAt": g["updatedAt"],
                }
                for g in self._guardrails.values()
            ]

    def delete(self, identifier: str) -> None:
        """Remove a guardrail (by id or ARN; idempotent)."""
        with self._lock:
            record = self._resolve(identifier)
            if record:
                self._guardrails.pop(record["guardrailId"], None)

    def apply(self, identifier: str, source: str, content: list[dict]) -> dict:
        """Evaluate content against a guardrail's policies (ApplyGuardrail)."""
        with self._lock:
            record = self._resolve(identifier)
        if record is None:
            raise KeyError(identifier)
        texts = [c["text"]["text"] for c in content if isinstance(c.get("text"), dict)]
        joined = " ".join(texts).lower()

        assessment: dict = {}
        # custom words + managed profanity
        words = {
            w["text"].lower()
            for w in record["wordPolicy"].get("wordsConfig", [])
            if w.get("text")
        }
        if any(
            m.get("type") == "PROFANITY"
            for m in record["wordPolicy"].get("managedWordListsConfig", [])
        ):
            words |= _PROFANITY
        matched_words = sorted(w for w in words if w in joined)
        if matched_words:
            assessment["wordPolicy"] = {
                "customWords": [
                    {"match": w, "action": "BLOCKED"} for w in matched_words
                ]
            }
        # denied topics (matched by name or example phrase)
        matched_topics = []
        for topic in record["topicPolicy"].get("topicsConfig", []):
            if topic.get("type", "DENY") != "DENY":
                continue
            phrases = [topic.get("name", "").lower(), *[
                e.lower() for e in topic.get("examples", [])
            ]]
            if any(p and p in joined for p in phrases):
                matched_topics.append(topic["name"])
        if matched_topics:
            assessment["topicPolicy"] = {
                "topics": [
                    {"name": n, "type": "DENY", "action": "BLOCKED"}
                    for n in matched_topics
                ]
            }

        intervened = bool(assessment)
        if intervened:
            message = record[
                "blockedInputMessaging"
                if source == "INPUT"
                else "blockedOutputsMessaging"
            ]
            outputs = [{"text": message}]
        else:
            outputs = [{"text": t} for t in texts]
        return {
            "action": "GUARDRAIL_INTERVENED" if intervened else "NONE",
            "outputs": outputs,
            "assessments": [assessment] if assessment else [],
            "usage": {
                "topicPolicyUnits": len(record["topicPolicy"].get("topicsConfig", [])),
                "contentPolicyUnits": 0,
                "wordPolicyUnits": len(words),
                "sensitiveInformationPolicyUnits": 0,
                "sensitiveInformationPolicyFreeUnits": 0,
                "contextualGroundingPolicyUnits": 0,
            },
        }

    def _resolve(self, identifier: str) -> dict | None:
        """Resolve a guardrail by id or ARN (call under the lock)."""
        if identifier in self._guardrails:
            return self._guardrails[identifier]
        return next(
            (g for g in self._guardrails.values() if g["guardrailArn"] == identifier),
            None,
        )
