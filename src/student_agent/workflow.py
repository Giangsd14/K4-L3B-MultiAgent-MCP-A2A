"""Coordinator for the scoped L3B investigation workflow."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .mcp_gateway import EvidenceGateway
from .model import ALLOWED_TOPICS, interpret_claim_topics
from .policy import decide_policy, normalize_policy_facts
from .specialists import InvestigationResult, investigate
from .trace import TraceWriter
from .verifier import verify_output


async def _claim_topics(case: Mapping[str, Any]) -> tuple[str, ...]:
    """Use a free-text model only when the input lacks structured claim topics.

    Topics are unverified hints about investigation order. They cannot establish
    a policy verdict, supply an evidence ref, or change tool arguments.
    """
    request = case.get("customer_request")
    if not isinstance(request, Mapping):
        return ()
    claims = request.get("claims")
    if isinstance(claims, list):
        topics = tuple(
            dict.fromkeys(
                claim["topic"]
                for claim in claims
                if isinstance(claim, Mapping)
                and isinstance(claim.get("topic"), str)
                and claim["topic"] in ALLOWED_TOPICS
            )
        )
        if topics:
            return topics
    message = request.get("message")
    if isinstance(message, str) and message.strip():
        return await interpret_claim_topics(message)
    return ()


def _priority_domains(topics: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for topic in topics:
        if topic in {"late_delivery_seller", "late_delivery_logistics"}:
            domains = ("shipment",)
        elif topic in {"refund_pending", "refund_failed", "requested_full_refund"}:
            domains = ("payment", "refund")
        elif topic in {
            "canceled_order_paid",
            "unavailable_order_paid",
            "duplicate_charge",
            "payment_mismatch",
            "valid_split_payment",
        }:
            domains = ("payment",)
        else:
            domains = ()
        for domain in domains:
            if domain not in result:
                result.append(domain)
    return tuple(result)


def _nodes(value: Any, *, limit: int = 300) -> Iterable[Mapping[str, Any]]:
    stack = [value]
    visited = 0
    while stack and visited < limit:
        node = stack.pop()
        visited += 1
        if isinstance(node, Mapping):
            yield node
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list | tuple):
            stack.extend(reversed(node))


def _ids(data: Any, *keys: str) -> list[str]:
    values: list[str] = []
    for node in _nodes(data):
        for key in keys:
            raw = node.get(key)
            candidates = raw if isinstance(raw, list | tuple) else [raw]
            for candidate in candidates:
                if (
                    isinstance(candidate, str)
                    and 0 < len(candidate) <= 128
                    and candidate not in values
                ):
                    values.append(candidate)
                if len(values) == 20:
                    return values
    return values


def _domain_data(result: InvestigationResult, *domains: str) -> list[Any]:
    resolved = set(result.identity.resolved_order_ids)
    return [
        fact.data
        for fact in result.facts
        if fact.domain in domains
        and (
            fact.domain != "order"
            or (
                result.identity.status == "resolved"
                and (fact.queried_order_id is None or fact.queried_order_id in resolved)
            )
        )
    ]


def _item_ids(result: InvestigationResult, order_id: str | None) -> list[str]:
    ids: list[str] = []
    for data in _domain_data(result, "item"):
        for value in _ids(data, "item_id", "order_item_id"):
            if value not in ids:
                ids.append(value)
        if order_id:
            for node in _nodes(data):
                item_number = node.get("order_item_id")
                if (
                    isinstance(item_number, (int, str))
                    and not isinstance(item_number, bool)
                    and str(item_number).isdigit()
                    and int(item_number) > 0
                ):
                    composite = f"{order_id}:{item_number}"
                    if composite not in ids and len(composite) <= 128:
                        ids.append(composite)
        if len(ids) >= 20:
            break
    return ids[:20]


def _payment_references(result: InvestigationResult, order_id: str | None) -> list[str]:
    refs: list[str] = []
    for data in _domain_data(result, "payment"):
        for value in _ids(data, "payment_reference", "payment_id", "transaction_id"):
            if value not in refs:
                refs.append(value)
        if order_id:
            for node in _nodes(data):
                sequence = node.get("payment_sequential")
                if (
                    isinstance(sequence, (int, str))
                    and not isinstance(sequence, bool)
                    and str(sequence).isdigit()
                    and int(sequence) > 0
                ):
                    composite = f"{order_id}:{sequence}"
                    if composite not in refs and len(composite) <= 128:
                        refs.append(composite)
        if len(refs) >= 20:
            break
    return refs[:20]


def _customer_context(result: InvestigationResult, affected_orders: list[str]) -> dict[str, Any]:
    customer_ids: list[str] = []
    for data in _domain_data(result, "order", "customer"):
        for value in _ids(data, "customer_unique_id"):
            if value not in customer_ids:
                customer_ids.append(value)
    history_ids: list[str] = []
    for data in _domain_data(result, "customer"):
        for value in _ids(data, "order_id", "related_order_ids"):
            if value not in affected_orders and value not in history_ids:
                history_ids.append(value)
    return {
        "customer_unique_id": customer_ids[0] if len(customer_ids) == 1 else None,
        "related_order_ids": history_ids[:20],
    }


def _affected_entities(result: InvestigationResult, seller_ids: list[str]) -> dict[str, Any]:
    order_ids = list(result.identity.resolved_order_ids)[:20]
    current_order = order_ids[0] if len(order_ids) == 1 else None
    return {
        "order_ids": order_ids,
        "item_ids": _item_ids(result, current_order),
        "seller_ids": seller_ids[:20],
        "payment_references": _payment_references(result, current_order),
        "shipment_ids": list(
            dict.fromkeys(
                value
                for data in _domain_data(result, "shipment")
                for value in _ids(data, "shipment_id", "tracking_id")
            )
        )[:20],
    }


def _relevant_refs(
    result: InvestigationResult, output: Mapping[str, Any], customer_context: Mapping[str, Any]
) -> list[str]:
    refs: list[str] = []
    affected = output["affected_entities"]
    shipment = output["shipment_analysis"]
    payment = output["payment_analysis"]
    issue = output["assessment"]["primary_issue"]
    for fact in result.facts:
        useful = fact.domain == "order" and fact.queried_order_id in {
            *result.identity.resolved_order_ids,
            *result.identity.rejected_candidates,
        }
        if fact.domain == "item":
            useful = bool(affected["item_ids"] or affected["seller_ids"])
        elif fact.domain == "seller":
            useful = bool(affected["seller_ids"])
        elif fact.domain == "shipment":
            useful = shipment["verdict"] != "insufficient_evidence" or shipment["timeline_complete"]
        elif fact.domain in {"payment", "refund"}:
            useful = payment["verdict"] != "insufficient_evidence" or any(
                payment[key] is not None
                for key in ("captured_total_brl", "refunded_total_brl", "refundable_total_brl")
            )
        elif fact.domain == "customer":
            useful = bool(
                customer_context["customer_unique_id"] or customer_context["related_order_ids"]
            )
        elif fact.domain == "policy":
            useful = issue != "insufficient_evidence" or (
                output["financial_resolution"]["recommended_refund_brl"] > 0
            )
        elif fact.domain == "product":
            useful = False  # No product-context field exists in the public L3B schema.
        if useful and fact.evidence_ref not in refs:
            refs.append(fact.evidence_ref)
    return refs[:30]


def _secondary_issues(facts: Mapping[str, Any], primary: str) -> list[str]:
    issues: list[str] = []
    shipment = facts["shipment_analysis"]["verdict"]
    payment = facts["payment_analysis"]["verdict"]
    candidates = {
        "seller_delay": "late_delivery_seller",
        "logistics_delay": "late_delivery_logistics",
        "duplicate_capture": "duplicate_charge",
        "capture_mismatch": "payment_mismatch",
        "refund_pending": "refund_pending",
        "refund_failed": "refund_failed",
    }
    for verdict in (shipment, payment):
        issue = candidates.get(verdict)
        if issue and issue != primary and issue not in issues:
            issues.append(issue)
    return issues


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Investigate one case and return only fields allowed by the L3B contract."""
    case_id = case["case_id"]
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="entity-agent",
        decision_code="INVESTIGATE",
    )
    topics = await _claim_topics(case)
    catalog = await gateway.tool_catalog()
    investigation = await investigate(
        case, gateway, trace, catalog, priority_domains=_priority_domains(topics)
    )
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="policy-agent",
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity-agent",
        target="policy-agent",
        decision_code=investigation.identity.status.upper(),
    )
    normalized = normalize_policy_facts(case, investigation)
    decision = decide_policy(case, normalized)
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=decision["assessment"]["primary_issue"],
    )

    affected = _affected_entities(investigation, normalized["affected_seller_ids"])
    customer = _customer_context(investigation, affected["order_ids"])
    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": decision["assessment"],
        "affected_entities": affected,
        "entity_resolution": normalized["entity_resolution"],
        "customer_context": customer,
        "shipment_analysis": normalized["shipment_analysis"],
        "payment_analysis": normalized["payment_analysis"],
        "root_cause_analysis": decision["root_cause_analysis"],
        "evidence_refs": [],
        "data_conflicts": decision["data_conflicts"],
        "financial_resolution": decision["financial_resolution"],
        "resolution_actions": decision["resolution_actions"],
    }
    output["assessment"]["secondary_issues"] = _secondary_issues(
        normalized, output["assessment"]["primary_issue"]
    )
    output["evidence_refs"] = _relevant_refs(investigation, output, customer)
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="verifier-agent",
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy-agent",
        target="verifier-agent",
    )
    validated = verify_output(
        output,
        case_id=case_id,
        evidence_records=[fact.__dict__ for fact in investigation.facts],
        consumed_evidence_refs=set(investigation.evidence_refs),
        contracts=trace.contracts,
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier-agent",
        target="coordinator",
        decision_code="VERIFIED",
    )
    return validated
