from __future__ import annotations

import asyncio
from typing import Any

from student_agent.specialists import investigate


def _tool(name: str, *arguments: str) -> dict[str, Any]:
    properties = {key: {"type": "string"} for key in ("case_id", *arguments)}
    return {
        "name": name,
        "description": name,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": ["case_id", *arguments],
            "additionalProperties": False,
        },
    }


CATALOG = {
    name: _tool(name, *arguments)
    for name, arguments in {
        "get_order": ("order_id",),
        "get_order_items": ("order_id",),
        "get_shipment": ("order_id",),
        "get_payment": ("order_id",),
        "get_refund": ("order_id",),
        "get_customer_history": ("customer_unique_id",),
        "get_product": ("product_id",),
        "get_seller": ("seller_id",),
        "get_policy": ("policy_version",),
    }.items()
}

LIVE_CATALOG = {
    name: _tool(name, *arguments)
    for name, arguments in {
        "get_order": ("order_id",),
        "get_order_items": ("order_id",),
        "get_order_payments": ("order_id",),
        "get_payment_timeline": ("order_id",),
        "get_refund_timeline": ("order_id",),
        "get_shipment_summary": ("order_id",),
        "get_sellers": ("order_id",),
        "get_product_context": ("order_id",),
        "get_policy": ("policy_version",),
        "get_customer_history": ("customer_unique_id",),
    }.items()
}


def _evidence(domain: str, data: Any, n: int) -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_" + str(n).zfill(20),
        "result_hash": "sha256:" + format(n, "064x"),
        "domain": domain,
        "data": data,
    }


def _case() -> dict[str, Any]:
    return {
        "case_id": "L3B_CASE_001",
        "customer_request": {"claimed_order_id": "good", "claims": []},
        "candidate_order_ids": ["good", "bad"],
        "customer_unique_id_hint": "customer-1",
        "policy_version": "EC_POLICY_V2",
        "investigation_scope": {
            "include_customer_history": True,
            "include_product_context": True,
        },
    }


class FakeGateway:
    def __init__(self, responses: dict[tuple[str, str], Any], catalog: dict[str, Any] = CATALOG):
        self.responses = responses
        self.catalog = catalog
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def tool_catalog(self) -> dict[str, Any]:
        return self.catalog

    async def call(self, name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((name, case_id, arguments))
        lookup = arguments.get("order_id", arguments.get("product_id", ""))
        result = self.responses[(name, lookup)]
        if isinstance(result, Exception):
            raise result
        return result


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> None:
        self.events.append(event)


def test_resolved_identity_collects_scoped_domain_evidence_and_trace() -> None:
    responses = {
        ("get_order", "good"): _evidence(
            "order", {"order_id": "good", "customer_unique_id": "customer-1"}, 1
        ),
        ("get_order", "bad"): RuntimeError("order not found"),
        ("get_order_items", "good"): _evidence(
            "item", [{"order_id": "good", "product_id": "product-1", "seller_id": "seller-1"}], 2
        ),
        ("get_shipment", "good"): _evidence("shipment", {"status": "delivered"}, 3),
        ("get_payment", "good"): _evidence("payment", {"amount": 100}, 4),
        ("get_refund", "good"): _evidence("refund", {"amount": 0}, 5),
        ("get_customer_history", ""): _evidence("customer", {"order_ids": ["good"]}, 6),
        ("get_product", "product-1"): _evidence("product", {"product_id": "product-1"}, 7),
        ("get_seller", ""): _evidence("seller", {"seller_id": "seller-1"}, 8),
        ("get_policy", ""): _evidence("policy", {"version": "EC_POLICY_V2"}, 9),
    }
    gateway = FakeGateway(responses)
    trace = FakeTrace()

    result = asyncio.run(investigate(_case(), gateway, trace))

    assert result.identity.status == "resolved"
    assert result.identity.resolved_order_ids == ("good",)
    assert result.identity.rejected_candidates == ("bad",)
    assert len(result.facts) == 9
    assert result.facts_for("shipment")[0].data == {"status": "delivered"}
    assert result.facts_for("payment")[0].evidence_ref == "ev_" + "4".zfill(20)
    assert all(fact.case_id == "L3B_CASE_001" for fact in result.facts)
    consumed_events = [
        event for event in trace.events if event.get("event_type") == "tool_result_consumed"
    ]
    assert len(consumed_events) == len(result.facts)
    assert [event["evidence_refs"][0] for event in consumed_events] == list(result.evidence_refs)
    assert all(case_id == "L3B_CASE_001" for _, case_id, _ in gateway.calls)
    assert all(
        set(arguments) <= set(CATALOG[name]["inputSchema"]["properties"]) - {"case_id"}
        for name, _, arguments in gateway.calls
    )


def test_ambiguous_identity_blocks_downstream_domain_calls() -> None:
    gateway = FakeGateway(
        {
            ("get_order", "good"): _evidence("order", {"order_id": "good"}, 1),
            ("get_order", "bad"): _evidence("order", {"order_id": "bad"}, 2),
        }
    )
    trace = FakeTrace()

    result = asyncio.run(investigate(_case(), gateway, trace))

    assert result.identity.status == "ambiguous"
    assert result.identity.resolved_order_ids == ("good", "bad")
    assert {name for name, _, _ in gateway.calls} == {"get_order"}
    assert len(trace.events) == 2


def test_missing_orders_and_mcp_errors_do_not_create_evidence_refs() -> None:
    gateway = FakeGateway(
        {
            ("get_order", "good"): _evidence("order", {"found": False}, 1),
            ("get_order", "bad"): RuntimeError("server unavailable"),
        }
    )
    trace = FakeTrace()

    result = asyncio.run(investigate(_case(), gateway, trace))

    assert result.identity.status == "ambiguous"
    assert result.identity.rejected_candidates == ("good",)
    assert len(result.facts) == 1
    assert len(trace.events) == 1
    assert len(result.errors) == 1
    assert "bad" not in result.identity.rejected_candidates


def test_no_catalog_never_guesses_tool_names_or_calls_gateway() -> None:
    gateway = FakeGateway({}, catalog={})
    trace = FakeTrace()

    result = asyncio.run(investigate(_case(), gateway, trace))

    assert result.identity.status == "ambiguous"
    assert result.facts == ()
    assert gateway.calls == []
    assert trace.events == []
    assert result.warnings


def test_unidentified_order_response_cannot_resolve_customer_claim() -> None:
    gateway = FakeGateway(
        {
            ("get_order", "good"): _evidence("order", {"customer_id": "c1"}, 1),
            ("get_order", "bad"): RuntimeError("404 order not found"),
        }
    )
    trace = FakeTrace()

    result = asyncio.run(investigate(_case(), gateway, trace))

    assert result.identity.status == "ambiguous"
    assert result.identity.rejected_candidates == ("bad",)
    assert len(result.facts) == 1
    assert {name for name, _, _ in gateway.calls} == {"get_order"}


def test_invalid_envelope_does_not_produce_a_ref_or_trace_event() -> None:
    gateway = FakeGateway(
        {
            ("get_order", "good"): {"domain": "order", "data": {"order_id": "good"}},
            ("get_order", "bad"): RuntimeError("order not found"),
        }
    )
    trace = FakeTrace()

    result = asyncio.run(investigate(_case(), gateway, trace))

    assert result.identity.status == "ambiguous"
    assert result.facts == ()
    assert trace.events == []
    assert result.errors == ("order tool get_order returned an incomplete envelope",)


def test_no_candidate_identifiers_remains_unresolved() -> None:
    case = _case()
    case["customer_request"] = {"claims": []}
    case["candidate_order_ids"] = []
    gateway = FakeGateway({})
    trace = FakeTrace()

    result = asyncio.run(investigate(case, gateway, trace))

    assert result.identity.status == "ambiguous"
    assert gateway.calls == []
    assert trace.events == []


def test_live_catalog_uses_order_scoped_context_and_payment_timeline_once() -> None:
    responses = {
        ("get_order", "good"): _evidence(
            "order", {"order_id": "good", "customer_unique_id": "customer-1"}, 1
        ),
        ("get_order", "bad"): RuntimeError("order not found"),
        ("get_order_items", "good"): _evidence("item", [{"order_id": "good"}], 2),
        ("get_shipment_summary", "good"): _evidence("shipment", {"status": "delivered"}, 3),
        ("get_payment_timeline", "good"): _evidence("payment", {"captured": 100}, 4),
        ("get_refund_timeline", "good"): _evidence("refund", {"refunded": 0}, 5),
        ("get_customer_history", ""): _evidence("customer", {"orders": ["good"]}, 6),
        ("get_product_context", "good"): _evidence("product", {"products": []}, 7),
        ("get_sellers", "good"): _evidence("seller", {"sellers": []}, 8),
        ("get_policy", ""): _evidence("policy", {"version": "EC_POLICY_V2"}, 9),
    }
    gateway = FakeGateway(responses, catalog=LIVE_CATALOG)
    trace = FakeTrace()

    result = asyncio.run(investigate(_case(), gateway, trace))

    assert result.identity.status == "resolved"
    called = [name for name, _, _ in gateway.calls]
    assert called.count("get_product_context") == 1
    assert called.count("get_sellers") == 1
    assert called.count("get_payment_timeline") == 1
    assert "get_order_payments" not in called
    assert len(result.facts) == 9
    assert len(
        [event for event in trace.events if event.get("event_type") == "tool_result_consumed"]
    ) == len(result.facts)
