from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


def _case() -> dict[str, Any]:
    return {
        "case_id": "L3B_CASE_001",
        "customer_request": {
            "claimed_order_id": "order-1",
            "message": "Please check this order and its split payment.",
            "claims": [{"claim_id": "claim-1", "topic": "valid_split_payment"}],
        },
        "candidate_order_ids": ["order-1"],
        "policy_version": "EC_POLICY_V2",
        "investigation_scope": {
            "include_customer_history": True,
            "include_product_context": True,
        },
    }


class FakeGateway:
    def __init__(self, *, available: bool) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        names = {
            "get_order": "order_id",
            "get_order_items": "order_id",
            "get_shipment_summary": "order_id",
            "get_payment_timeline": "order_id",
            "get_refund_timeline": "order_id",
            "get_customer_history": "customer_unique_id",
            "get_product_context": "order_id",
            "get_sellers": "order_id",
            "get_policy": "policy_version",
        }
        self.catalog = (
            {
                name: {
                    "name": name,
                    "description": name,
                    "inputSchema": {
                        "type": "object",
                        "properties": {"case_id": {"type": "string"}, arg: {"type": "string"}},
                        "required": ["case_id", arg],
                    },
                }
                for name, arg in names.items()
            }
            if available
            else {}
        )

    async def tool_catalog(self) -> dict[str, dict[str, Any]]:
        return self.catalog

    async def call(self, name: str, *, case_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((name, case_id, kwargs))
        data: dict[str, Any] | list[dict[str, Any]]
        domain = {
            "get_order": "order",
            "get_order_items": "item",
            "get_shipment_summary": "shipment",
            "get_payment_timeline": "payment",
            "get_refund_timeline": "refund",
            "get_customer_history": "customer",
            "get_product_context": "product",
            "get_sellers": "seller",
            "get_policy": "policy",
        }[name]
        data = {
            "get_order": {
                "order_id": "order-1",
                "order_status": "delivered",
                "customer_unique_id": "customer-1",
            },
            "get_order_items": [{"order_item_id": 1, "seller_id": "seller-1"}],
            "get_shipment_summary": {"verdict": "on_time", "timeline_complete": True},
            "get_payment_timeline": {
                "verdict": "reconciled",
                "valid_split_payment": True,
                "captured_total_brl": 100.0,
                "refunded_total_brl": 0.0,
                "refundable_total_brl": 100.0,
            },
            "get_refund_timeline": {"refund_events": []},
            "get_customer_history": {
                "customer_unique_id": "customer-1",
                "orders": [{"order_id": "order-0"}],
            },
            "get_product_context": {"products": []},
            "get_sellers": [{"seller_id": "seller-1"}],
            "get_policy": {
                "policy_version": "EC_POLICY_V2",
                "currency": "BRL",
                "rules": {
                    "valid_split_payment": {
                        "case_status": "no_action",
                        "recommended_action": "document_no_action",
                        "refund_brl": 0,
                        "responsible_parties": [{"party_type": "customer", "party_id": None}],
                    }
                },
            },
        }[name]
        digest = hashlib.sha256(name.encode()).hexdigest()
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{digest[:32]}",
            "result_hash": f"sha256:{digest}",
            "domain": domain,
            "data": data,
        }


def test_workflow_uses_scoped_evidence_and_validates_output(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway = FakeGateway(available=True)
    case = _case()
    trace.emit(case_id=case["case_id"], event_type="case_received", actor="coordinator")
    output = asyncio.run(solve_case(case, gateway, trace))
    trace.emit(case_id=case["case_id"], event_type="case_finalized", actor="coordinator")

    contracts.validate_output(output, "test output")
    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    assert output["entity_resolution"]["status"] == "resolved"
    assert output["customer_context"]["related_order_ids"] == ["order-0"]
    assert all(call_case == case["case_id"] for _, call_case, _ in gateway.calls)
    events = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    consumed = {
        ref
        for event in events
        if event["event_type"] == "tool_result_consumed"
        for ref in event["evidence_refs"]
    }
    assert set(output["evidence_refs"]) <= consumed
    assert events[0]["event_type"] == "case_received"
    assert events[-1]["event_type"] == "case_finalized"
    handoff_targets = {event.get("target") for event in events if event["event_type"] == "handoff"}
    assert {
        "entity-agent",
        "order-item-agent",
        "shipment-agent",
        "payment-agent",
        "customer-agent",
        "product-agent",
        "policy-agent",
        "verifier-agent",
    } <= handoff_targets


def test_workflow_without_catalog_returns_investigation_result(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway = FakeGateway(available=False)
    output = asyncio.run(solve_case(_case(), gateway, trace))

    contracts.validate_output(output, "test output")
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["evidence_refs"] == []
    assert gateway.calls == []


def test_free_text_model_only_prioritizes_scoped_investigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def classify(message: str) -> tuple[str, ...]:
        assert message == "I was charged twice."
        return ("duplicate_charge",)

    monkeypatch.setattr("student_agent.workflow.interpret_claim_topics", classify)
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway = FakeGateway(available=True)
    case = _case()
    case["customer_request"]["claims"] = []
    case["customer_request"]["message"] = "I was charged twice."

    output = asyncio.run(solve_case(case, gateway, trace))

    contracts.validate_output(output, "test output")
    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    names = [name for name, _case_id, _kwargs in gateway.calls]
    assert names.index("get_payment_timeline") < names.index("get_shipment_summary")
    assert all(case_id == case["case_id"] for _, case_id, _kwargs in gateway.calls)


def test_structured_claims_do_not_use_model_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unexpected_call(_message: str) -> tuple[str, ...]:
        raise AssertionError("structured claims must not call OpenRouter")

    monkeypatch.setattr("student_agent.workflow.interpret_claim_topics", unexpected_call)
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    output = asyncio.run(solve_case(_case(), FakeGateway(available=True), trace))
    assert output["assessment"]["primary_issue"] == "valid_split_payment"


def test_rejected_candidate_order_cannot_pollute_verified_order(tmp_path: Path) -> None:
    class WrongCandidateGateway(FakeGateway):
        async def call(self, name: str, *, case_id: str, **kwargs: Any) -> dict[str, Any]:
            if name == "get_order" and kwargs.get("order_id") == "bad":
                self.calls.append((name, case_id, kwargs))
                digest = hashlib.sha256(b"bad-order").hexdigest()
                return {
                    "schema_version": "day09-mcp-evidence-v1",
                    "evidence_ref": f"ev_{digest[:32]}",
                    "result_hash": f"sha256:{digest}",
                    "domain": "order",
                    "data": {
                        "order_id": "someone-else",
                        "order_status": "canceled",
                        "customer_unique_id": "customer-other",
                    },
                }
            return await super().call(name, case_id=case_id, **kwargs)

    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    case = _case()
    case["candidate_order_ids"].append("bad")

    output = asyncio.run(solve_case(case, WrongCandidateGateway(available=True), trace))

    assert output["entity_resolution"]["status"] == "resolved"
    assert output["entity_resolution"]["rejected_candidates"] == ["bad"]
    assert output["customer_context"]["customer_unique_id"] == "customer-1"
    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    assert output["data_conflicts"] == []
