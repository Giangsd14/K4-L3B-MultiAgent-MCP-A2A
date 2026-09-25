from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from student_agent import model


class FakeResponse:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self.body = body

    def json(self) -> Any:
        return self.body


class FakeClient:
    calls: list[dict[str, Any]] = []
    response: FakeResponse | Exception = FakeResponse(
        200,
        {
            "choices": [
                {
                    "message": {
                        "content": '{"topics":["late_delivery_logistics","requested_full_refund"]}'
                    }
                }
            ]
        },
    )

    def __init__(self, *, timeout: float) -> None:
        assert timeout <= 15.0

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(
        self, url: str, *, headers: dict[str, str], json: dict[str, Any]
    ) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture(autouse=True)
def isolate_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(model, "_BUDGET_PATH", tmp_path / "traces" / "quota.sqlite3")
    monkeypatch.setattr(model.httpx2, "AsyncClient", FakeClient)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    FakeClient.calls = []
    FakeClient.response = FakeResponse(
        200,
        {
            "choices": [
                {
                    "message": {
                        "content": '{"topics":["late_delivery_logistics","requested_full_refund"]}'
                    }
                }
            ]
        },
    )


def test_missing_key_and_empty_message_make_no_request() -> None:
    assert asyncio.run(model.interpret_claim_topics("Đơn giao trễ")) == ()
    assert asyncio.run(model.interpret_claim_topics("", key="sk-or-v1-example")) == ()
    assert asyncio.run(model.interpret_claim_topics("Đơn giao trễ", key="sk-team-wrong-key")) == ()
    assert FakeClient.calls == []
    assert not model._BUDGET_PATH.exists()


def test_request_uses_exact_model_and_redacts_obvious_pii() -> None:
    raw = (
        "Đơn af0bbb47f125381ce9f3597dc70ef07b giao trễ; "
        "email an@example.com, phone +55 11 99999 1234, "
        "address: 123 Main Street; charge BRL 123.45, "
        "key sk-or-v1-1234567890abcdef, yêu cầu hoàn tiền."
    )
    topics = asyncio.run(model.interpret_claim_topics(raw, key="sk-or-v1-test-key"))
    assert topics == ("late_delivery_logistics", "requested_full_refund")
    assert len(FakeClient.calls) == 1
    call = FakeClient.calls[0]
    assert call["url"] == model.CHAT_COMPLETIONS_URL
    assert call["json"]["model"] == "meta-llama/llama-3.1-8b-instruct:free"
    assert "tools" not in call["json"]
    user_content = call["json"]["messages"][1]["content"]
    for secret in (
        "af0bbb47f125381ce9f3597dc70ef07b",
        "an@example.com",
        "+55 11 99999 1234",
        "123 Main Street",
        "BRL 123.45",
        "sk-or-v1-1234567890abcdef",
    ):
        assert secret not in user_content
    assert "[REDACTED]" in user_content

    with sqlite3.connect(model._BUDGET_PATH) as connection:
        rows = connection.execute("SELECT utc_day, requests FROM daily_usage").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == 1


def test_model_output_is_strictly_allowlisted_and_deduplicated() -> None:
    FakeClient.response = FakeResponse(
        200,
        {
            "choices": [
                {
                    "message": {
                        "content": '["late_delivery_seller","invented_ref","late_delivery_seller",'
                        '"payment_mismatch"]'
                    }
                }
            ]
        },
    )
    assert asyncio.run(model.interpret_claim_topics("Giao trễ và trừ tiền sai", key="x")) == (
        "late_delivery_seller",
        "payment_mismatch",
    )
    FakeClient.response = FakeResponse(200, {"choices": [{"message": {"content": "not JSON"}}]})
    assert asyncio.run(model.interpret_claim_topics("Giao trễ", key="x")) == ()


def test_rate_limit_and_transport_failure_fall_back_without_retry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    FakeClient.response = FakeResponse(429, {"error": {"message": "rate limit"}})
    assert asyncio.run(model.interpret_claim_topics("Giao trễ", key="x")) == ()
    FakeClient.response = TimeoutError("provider failed with sensitive response")
    assert asyncio.run(model.interpret_claim_topics("Giao trễ", key="x")) == ()
    assert len(FakeClient.calls) == 2
    assert capsys.readouterr() == ("", "")
    with sqlite3.connect(model._BUDGET_PATH) as connection:
        count = connection.execute("SELECT requests FROM daily_usage").fetchone()[0]
    assert count == 2


def test_persistent_utc_day_budget_caps_at_45_and_resets_next_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_day = ["2026-09-25"]
    monkeypatch.setattr(model, "_utc_day", lambda: current_day[0])
    for _ in range(46):
        asyncio.run(model.interpret_claim_topics("Giao trễ", key="x"))
    assert len(FakeClient.calls) == 45
    assert model._BUDGET_PATH.exists()

    current_day[0] = "2026-09-26"
    asyncio.run(model.interpret_claim_topics("Giao trễ", key="x"))
    assert len(FakeClient.calls) == 46
    with sqlite3.connect(model._BUDGET_PATH) as connection:
        rows = connection.execute(
            "SELECT utc_day, requests FROM daily_usage ORDER BY utc_day"
        ).fetchall()
    assert rows == [("2026-09-25", 45), ("2026-09-26", 1)]
