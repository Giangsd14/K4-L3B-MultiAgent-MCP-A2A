"""Optional, quota-bounded claim topic helper; never an evidence source."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2

MODEL_ID = "meta-llama/llama-3.1-8b-instruct:free"
CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_DAILY_REQUESTS = 45
MAX_MESSAGE_CHARS = 1500
MAX_TOPICS = 5

# These are the claim-topic labels present in the L3B case inputs. They are
# unverified customer claims, not policy conclusions or public output values.
ALLOWED_TOPICS = frozenset(
    {
        "canceled_order_paid",
        "duplicate_charge",
        "late_delivery_logistics",
        "late_delivery_seller",
        "payment_mismatch",
        "refund_failed",
        "refund_pending",
        "requested_full_refund",
        "unavailable_order_paid",
        "unsupported_claim",
        "valid_split_payment",
    }
)

_BUDGET_PATH = Path(__file__).resolve().parents[2] / "traces" / ".openrouter-quota.sqlite3"
_REDACTIONS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", re.IGNORECASE),
    re.compile(r"\bhttps?://\S+", re.IGNORECASE),
    re.compile(
        r"\b(?:address|shipping address|địa chỉ|endereço)\s*[:=]\s*[^\n,;.]{2,120}", re.IGNORECASE
    ),
    re.compile(
        r"\b(?:my name is|tên tôi là|full name|họ tên)\s*[:=]?\s+[^\n,;.]{2,80}", re.IGNORECASE
    ),
    re.compile(r"(?:R\$|\bBRL\b)\s*\d+(?:[.,]\d{1,2})?", re.IGNORECASE),
    re.compile(r"\b[0-9a-f]{24,64}\b", re.IGNORECASE),
    re.compile(r"(?<!\w)(?:\+?\d[\d\s()./-]{7,}\d)(?!\w)"),
    re.compile(r"\b(?=[A-Za-z0-9_-]{10,}\b)(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]+\b"),
)


def _utc_day() -> str:
    return datetime.now(UTC).date().isoformat()


def _redact_message(message: str) -> str:
    redacted = message
    for pattern in _REDACTIONS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted.strip()[:MAX_MESSAGE_CHARS]


def _reserve_request() -> bool:
    """Reserve one attempt atomically across processes using this workspace."""
    _BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(_BUDGET_PATH, timeout=5.0)) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS daily_usage "
            "(utc_day TEXT PRIMARY KEY, requests INTEGER NOT NULL)"
        )
        connection.execute("BEGIN IMMEDIATE")
        day = _utc_day()
        row = connection.execute(
            "SELECT requests FROM daily_usage WHERE utc_day = ?", (day,)
        ).fetchone()
        if row is not None and row[0] >= MAX_DAILY_REQUESTS:
            connection.rollback()
            return False
        if row is None:
            connection.execute("INSERT INTO daily_usage (utc_day, requests) VALUES (?, 1)", (day,))
        else:
            connection.execute(
                "UPDATE daily_usage SET requests = requests + 1 WHERE utc_day = ?", (day,)
            )
        connection.commit()
        return True


def _parse_topics(response: Any) -> tuple[str, ...]:
    if not isinstance(response, dict):
        return ()
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ()
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or len(content) > 4096:
        return ()
    try:
        decoded = json.loads(content)
    except (TypeError, ValueError):
        return ()
    if isinstance(decoded, dict) and set(decoded) == {"topics"}:
        decoded = decoded["topics"]
    if not isinstance(decoded, list):
        return ()
    topics: list[str] = []
    for item in decoded:
        if isinstance(item, str) and item in ALLOWED_TOPICS and item not in topics:
            topics.append(item)
            if len(topics) == MAX_TOPICS:
                break
    return tuple(topics)


async def interpret_claim_topics(message: str, *, key: str | None = None) -> tuple[str, ...]:
    """Return unverified claim topics, or an empty tuple when unavailable.

    The caller must not treat these labels as MCP evidence or policy decisions.
    Every attempted HTTP request consumes one local UTC-day quota slot, including
    failed requests. This helper never retries or logs the message or API key.
    """
    raw_key = key if key is not None else os.getenv("OPENROUTER_API_KEY", "")
    if not isinstance(raw_key, str):
        return ()
    token = raw_key.strip()
    if not token or token.startswith("sk-team-") or not isinstance(message, str):
        return ()
    redacted = _redact_message(message)
    if not redacted or redacted == "[REDACTED]":
        return ()
    try:
        if not _reserve_request():
            return ()
    except (OSError, sqlite3.Error):
        return ()

    prompt = (
        "Classify only the topics the customer claims, not verified facts. "
        'Return only JSON like {"topics":["label"]}; use zero to five labels from: '
        + ", ".join(sorted(ALLOWED_TOPICS))
        + ". Do not infer order IDs, responsibility, evidence or refund amounts."
    )
    payload = {
        "model": MODEL_ID,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": redacted},
        ],
        "temperature": 0,
        "max_tokens": 96,
    }
    try:
        async with httpx2.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                CHAT_COMPLETIONS_URL,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=payload,
            )
        if response.status_code != 200:
            return ()
        return _parse_topics(response.json())
    except Exception:
        # The model is optional; provider, transport and parsing failures should
        # not interrupt deterministic case investigation. Never print raw errors.
        return ()
