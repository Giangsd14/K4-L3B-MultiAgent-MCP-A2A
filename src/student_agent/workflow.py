from __future__ import annotations

import asyncio
from typing import Any

from .decision import CaseFacts, build_output
from .evidence import CaseEvidence, EvidenceResult
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _history_order_ids(history: dict[str, Any] | None) -> set[str]:
    return {
        str(row["order_id"])
        for row in _rows(_mapping(history).get("orders"))
        if row.get("order_id")
    }


def _refund_lookup_absence(result: EvidenceResult, payment: dict[str, Any] | None) -> str | None:
    """Interpret an application-level absence without masking transport failures."""
    if not result.error:
        return None
    message = result.error.lower()
    absence_markers = (
        "404",
        "not found",
        "not_found",
        "no refund",
        "no records",
        "empty refund",
        "refund timeline unavailable",
    )
    if any(marker in message for marker in absence_markers):
        return "not_found"
    if result.error_type != "RuntimeError" or not message.startswith(
        "mcp tool get_refund_timeline failed:"
    ):
        return None
    fatal_markers = (
        "unauthorized",
        "forbidden",
        "permission",
        "denied",
        "timeout",
        "timed out",
        "rate limit",
        "internal",
        "invalid",
        "bad request",
        "malformed",
        "schema",
        "500",
        "502",
        "503",
        "504",
    )
    if any(marker in message for marker in fatal_markers):
        return None
    events = _rows(_mapping(payment).get("events"))
    no_refund_event = payment is not None and not any(
        "refund" in str(event.get("event_type", "")).lower() for event in events
    )
    return "inferred_absent" if no_refund_event else None


async def _resolve_entity(
    case: dict[str, Any], facts: CaseFacts, evidence: CaseEvidence, trace: TraceWriter
) -> None:
    case_id = case["case_id"]
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity_agent",
        attributes={"task": "entity_resolution"},
    )
    candidates = list(
        dict.fromkeys(str(value) for value in case.get("candidate_order_ids", []) if value)
    )
    customer_hint = case.get("customer_unique_id_hint")
    scope = _mapping(case.get("investigation_scope"))
    if customer_hint and scope.get("include_customer_history", True):
        history = await evidence.fetch(
            "get_customer_history", actor="entity_agent", customer_unique_id=str(customer_hint)
        )
        if history.available:
            facts.customer_history = _mapping(history.data)

    history_ids = _history_order_ids(facts.customer_history)
    claimed = _mapping(case.get("customer_request")).get("claimed_order_id")
    # The claimed order is verified directly. Customer history can exclude
    # unrelated candidates without another audited get_order call.
    history_confirms_claim = bool(history_ids and claimed in history_ids)
    to_check = [
        candidate
        for candidate in candidates
        if not history_confirms_claim or candidate in history_ids or candidate == claimed
    ]
    responses = await asyncio.gather(
        *(
            evidence.fetch("get_order", actor="entity_agent", order_id=candidate)
            for candidate in to_check
        )
    )
    valid: dict[str, dict[str, Any]] = {}
    for candidate, response in zip(to_check, responses, strict=True):
        order = _mapping(response.data) if response.available else {}
        owner = order.get("customer_unique_id")
        owner_conflict = bool(owner and customer_hint and owner != customer_hint)
        history_conflict = bool(
            history_ids and candidate not in history_ids and owner != customer_hint
        )
        if response.available and order and not owner_conflict and not history_conflict:
            valid[candidate] = order
        else:
            facts.rejected_candidates.append(candidate)
    for candidate in candidates:
        if candidate not in to_check:
            facts.rejected_candidates.append(candidate)
    facts.rejected_candidates = list(dict.fromkeys(facts.rejected_candidates))

    chosen = list(valid)
    if len(chosen) == 1:
        facts.resolved_order_ids = chosen
        facts.entity_status = "resolved"
        facts.order = valid[chosen[0]]
        if history_ids and chosen[0] in history_ids and chosen[0] == claimed:
            facts.entity_confidence = 1.0
        elif history_ids and chosen[0] in history_ids:
            facts.entity_confidence = 0.88
        else:
            facts.entity_confidence = 0.72
    elif len(chosen) > 1:
        facts.resolved_order_ids = chosen[:20]
        facts.entity_status = "ambiguous"
        facts.entity_confidence = 0.45
    else:
        facts.entity_status = "not_found"
        facts.entity_confidence = 0.35

    if claimed in facts.rejected_candidates and facts.customer_history is not None:
        facts.conflicts.append(
            {
                "field": "claimed_order_id",
                "sources": ["customer_claim", "customer_history"],
                "selected_source": "customer_history",
                "resolution_code": "claimed_order_not_verified",
            }
        )
    elif facts.rejected_candidates:
        selected_source = (
            "customer_history" if facts.customer_history is not None else "mcp_order_registry"
        )
        facts.conflicts.append(
            {
                "field": "order_id",
                "sources": ["candidate_list", selected_source],
                "selected_source": selected_source,
                "resolution_code": "unresolvable_candidate_rejected",
            }
        )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity_agent",
        target="order_agent" if facts.order_id else "policy_agent",
        attributes={"entity_status": facts.entity_status},
    )


async def _gather_specialist_evidence(
    case: dict[str, Any], facts: CaseFacts, evidence: CaseEvidence, trace: TraceWriter
) -> None:
    order_id = facts.order_id
    if order_id is None:
        return
    case_id = case["case_id"]
    for target, task in (
        ("order_agent", "order_items_and_products"),
        ("shipment_agent", "shipment_timeline"),
        ("payment_agent", "payment_and_refund_timeline"),
    ):
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=target,
            attributes={"task": task, "order_id": order_id},
        )

    claims = _rows(_mapping(case.get("customer_request")).get("claims"))
    topics = {claim.get("topic") for claim in claims}
    scope = _mapping(case.get("investigation_scope"))
    requests: dict[str, Any] = {
        "items": evidence.fetch("get_order_items", actor="order_agent", order_id=order_id),
        "shipment": evidence.fetch(
            "get_shipment_summary", actor="shipment_agent", order_id=order_id
        ),
        "payment": evidence.fetch("get_payment_timeline", actor="payment_agent", order_id=order_id),
    }
    if scope.get("include_product_context", True):
        requests["product"] = evidence.fetch(
            "get_product_context", actor="order_agent", order_id=order_id
        )
    refund_topics = {
        "refund_failed",
        "refund_pending",
        "requested_full_refund",
        "canceled_order_paid",
        "unavailable_order_paid",
    }
    if topics.intersection(refund_topics) or _mapping(facts.order).get("order_status") in {
        "canceled",
        "unavailable",
    }:
        requests["refund"] = evidence.fetch(
            "get_refund_timeline", actor="payment_agent", order_id=order_id
        )
    responses = await asyncio.gather(*requests.values())
    results = dict(zip(requests, responses, strict=True))

    if results["items"].available:
        facts.items = _rows(results["items"].data)
    if results["shipment"].available:
        facts.shipment = _mapping(results["shipment"].data)
    if results["payment"].available:
        facts.payment = _mapping(results["payment"].data)
    if "product" in results and results["product"].available:
        facts.products = _rows(results["product"].data)
    payment_events = _rows(_mapping(facts.payment).get("events"))
    if "refund" in results:
        refund_result = results["refund"]
        if refund_result.available:
            facts.refund = _mapping(refund_result.data)
            facts.refund_source = "tool"
        else:
            absence = _refund_lookup_absence(refund_result, facts.payment)
            if absence:
                facts.refund = {"events": []}
                facts.refund_source = absence

    if "refund" not in results and any(
        "refund" in str(event.get("event_type", "")) for event in payment_events
    ):
        refund = await evidence.fetch(
            "get_refund_timeline", actor="payment_agent", order_id=order_id
        )
        if refund.available:
            facts.refund = _mapping(refund.data)
            facts.refund_source = "tool"
        else:
            absence = _refund_lookup_absence(refund, facts.payment)
            if absence:
                facts.refund = {"events": []}
                facts.refund_source = absence

    shipment_events = _rows(_mapping(facts.shipment).get("events"))
    seller_relevant = "late_delivery_seller" in topics or any(
        event.get("actor") == "seller" for event in shipment_events
    )
    if seller_relevant or not any(item.get("seller_id") for item in facts.items):
        sellers = await evidence.fetch("get_sellers", actor="order_agent", order_id=order_id)
        if sellers.available:
            facts.sellers = _rows(sellers.data)

    order_status = _mapping(facts.order).get("order_status")
    shipment_status = _mapping(facts.shipment).get("order_status")
    if order_status and shipment_status and order_status != shipment_status:
        facts.conflicts.append(
            {
                "field": "order_status",
                "sources": ["order", "shipment"],
                "selected_source": "order",
                "resolution_code": "status_source_conflict",
            }
        )
    for actor, target in (
        ("order_agent", "shipment_agent"),
        ("shipment_agent", "payment_agent"),
        ("payment_agent", "policy_agent"),
    ):
        trace.emit(case_id=case_id, event_type="handoff", actor=actor, target=target)


def _verify_output(
    case: dict[str, Any],
    output: dict[str, Any],
    facts: CaseFacts,
    evidence: CaseEvidence,
    trace: TraceWriter,
) -> None:
    case_id = output["case_id"]
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="verifier",
        attributes={"task": "independent_verification"},
    )
    trace.contracts.validate_output(output, f"outputs/{case_id}.json")
    audited = set(evidence.all_refs())
    if not set(output["evidence_refs"]).issubset(audited):
        raise ValueError(f"{case_id}: output references unaudited evidence")
    for claim in output["claim_assessments"]:
        if not set(claim["evidence_refs"]).issubset(audited):
            raise ValueError(f"{case_id}: claim references unaudited evidence")
    if output["entity_resolution"]["resolved_order_ids"] != facts.resolved_order_ids:
        raise ValueError(f"{case_id}: inconsistent entity resolution")
    candidates = {str(value) for value in case.get("candidate_order_ids", []) if value}
    accounted = set(facts.resolved_order_ids) | set(facts.rejected_candidates)
    if candidates != accounted:
        raise ValueError(f"{case_id}: candidate resolution is incomplete")
    finance = output["financial_resolution"]
    refund_sum = round(sum(line["amount_brl"] for line in finance["refund_lines"]), 2)
    if refund_sum != finance["recommended_refund_brl"]:
        raise ValueError(f"{case_id}: refund lines do not balance")
    refundable = output["payment_analysis"]["refundable_total_brl"]
    if refundable is not None and finance["recommended_refund_brl"] > refundable:
        raise ValueError(f"{case_id}: recommended refund exceeds unrefunded capture")
    issue = output["assessment"]["primary_issue"]
    rule = _mapping(_mapping(facts.policy).get("rules")).get(issue)
    if isinstance(rule, dict):
        expected_status = rule.get("case_status")
        if expected_status and output["assessment"]["case_status"] != expected_status:
            raise ValueError(f"{case_id}: case status contradicts policy")
        expected_action = rule.get("recommended_action")
        if expected_action and output["resolution_actions"] != [expected_action]:
            raise ValueError(f"{case_id}: action contradicts policy")
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="VERIFICATION_PASSED",
        attributes={"status": "passed", "checks_passed": 7},
    )


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Investigate one case with case-scoped evidence and independent verification."""
    case_id = case["case_id"]
    evidence = CaseEvidence(case_id, gateway, trace)
    facts = CaseFacts()
    await _resolve_entity(case, facts, evidence, trace)
    await _gather_specialist_evidence(case, facts, evidence, trace)

    policy_version = str(case.get("policy_version", "EC_POLICY_V2"))
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="policy_agent",
        attributes={"task": "policy_evaluation", "policy_version": policy_version},
    )
    policy = await evidence.fetch("get_policy", actor="policy_agent", policy_version=policy_version)
    if policy.available:
        facts.policy = _mapping(policy.data)
    output = build_output(case, facts, evidence)
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy_agent",
        decision_code=output["assessment"]["primary_issue"],
        attributes={"policy_version": policy_version},
    )
    trace.emit(case_id=case_id, event_type="handoff", actor="policy_agent", target="verifier")
    _verify_output(case, output, facts, evidence, trace)
    return output
