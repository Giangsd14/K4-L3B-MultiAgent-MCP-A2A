"""Conservative L3B policy reducer over MCP backed investigation facts.

The public scoring rubric is deliberately not used as a refund policy.  A
positive refund needs an explicit recommendation from a policy evidence record
and a known refundable balance from payment evidence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

_SHIPMENT_VERDICTS = {
    "on_time",
    "seller_delay",
    "logistics_delay",
    "lost",
    "returned",
    "conflicting",
    "insufficient_evidence",
}
_PAYMENT_VERDICTS = {
    "reconciled",
    "capture_mismatch",
    "duplicate_capture",
    "refund_pending",
    "refund_failed",
    "refunded",
    "insufficient_evidence",
}
_CENTS = Decimal("0.01")


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {
        key: getattr(value, key)
        for key in (
            "case_id",
            "domain",
            "tool_name",
            "evidence_ref",
            "data",
            "warnings",
            "queried_order_id",
            "status",
            "resolved_order_ids",
            "rejected_candidates",
            "confidence",
        )
        if hasattr(value, key)
    }


def _nodes(value: Any, *, limit: int = 200) -> Iterable[Mapping[str, Any]]:
    """Inspect a bounded amount of a tool response without guessing its schema."""
    stack = [value]
    visited = 0
    while stack and visited < limit:
        current = stack.pop()
        visited += 1
        if isinstance(current, Mapping):
            yield current
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list | tuple):
            stack.extend(reversed(current))


def _money(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    try:
        if not amount.is_finite() or amount < 0 or amount != amount.quantize(_CENTS):
            return None
    except InvalidOperation:
        return None
    return amount


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _first(nodes: Iterable[Mapping[str, Any]], *keys: str) -> Any:
    for node in nodes:
        for key in keys:
            if node.get(key) is not None:
                return node[key]
    return None


def _rows(data: Any, key: str) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for node in _nodes(data):
        value = node.get(key)
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, Mapping))
    if not rows and isinstance(data, list):
        rows = [item for item in data if isinstance(item, Mapping)]
    return rows


def _sum_money(values: Iterable[Any]) -> Decimal | None:
    amounts = [_money(value) for value in values]
    if not amounts or any(amount is None for amount in amounts):
        return None
    return sum((amount for amount in amounts if amount is not None), Decimal(0))


def _ids(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and item))[:20]


def _source(record: Mapping[str, Any]) -> str:
    return str(record.get("tool_name") or record.get("domain") or "mcp")[:80]


def normalize_policy_facts(case: dict[str, Any], investigation: Any) -> dict[str, Any]:
    """Extract only recognizable public-domain facts from specialist records.

    Unknown MCP payload keys remain unknown. The raw records and their original
    refs stay available for the coordinator to select relevant citations.
    """
    identity = _mapping(getattr(investigation, "identity", None))
    records = [_mapping(record) for record in getattr(investigation, "facts", ())]
    by_domain: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        by_domain.setdefault(str(record.get("domain", "")), []).append(record)

    # Candidate lookups are consumed for identity resolution, but their order
    # status must not become a fact about the one verified order.
    resolved_orders = set(_ids(identity.get("resolved_order_ids", [])))
    by_domain["order"] = [
        record
        for record in by_domain.get("order", ())
        if identity.get("status") == "resolved"
        and (
            record.get("queried_order_id") is None
            or record.get("queried_order_id") in resolved_orders
        )
    ]

    order_statuses: dict[str, str] = {}
    seller_ids: list[str] = []
    for record in by_domain.get("order", ()):
        status = _first(_nodes(record.get("data")), "order_status")
        if isinstance(status, str):
            source = f"{_source(record)}/{str(record.get('evidence_ref', ''))[:16]}"[:80]
            order_statuses[source] = status.lower()
    for record in (*by_domain.get("item", ()), *by_domain.get("seller", ())):
        for node in _nodes(record.get("data")):
            seller_id = node.get("seller_id")
            if isinstance(seller_id, str) and seller_id not in seller_ids:
                seller_ids.append(seller_id)

    conflicts: list[dict[str, Any]] = []
    if len(set(order_statuses.values())) > 1:
        conflicts.append(
            {
                "field": "order_status",
                "sources": list(order_statuses)[:5],
                "selected_source": None,
                "resolution_code": "UNRESOLVED_CONFLICT",
            }
        )
    order_status = next(iter(order_statuses.values()), None)
    if len(set(order_statuses.values())) > 1:
        order_status = None

    shipment: dict[str, Any] = {
        "verdict": "insufficient_evidence",
        "late_seller_ids": [],
        "timeline_complete": False,
    }
    purchase_at = _date(
        _first(
            (node for record in by_domain.get("order", ()) for node in _nodes(record.get("data"))),
            "order_purchase_timestamp",
        )
    )
    for record in by_domain.get("shipment", ()):
        nodes = list(_nodes(record.get("data")))
        verdict = _first(nodes, "verdict")
        if verdict in _SHIPMENT_VERDICTS:
            shipment["verdict"] = verdict
            shipment["late_seller_ids"] = _ids(_first(nodes, "late_seller_ids"))
            shipment["timeline_complete"] = _first(nodes, "timeline_complete") is True
            break
        status = _first(nodes, "shipment_status")
        if status in {"lost", "returned"}:
            shipment.update(verdict=status, timeline_complete=True)
            break
        delivered = _date(
            _first(nodes, "order_delivered_customer_date", "delivered_customer_at", "delivered_at")
        )
        estimated = _date(_first(nodes, "order_estimated_delivery_date", "estimated_delivery_at"))
        carrier = _date(
            _first(
                nodes,
                "order_delivered_carrier_date",
                "delivered_carrier_at",
                "carrier_handoff_at",
            )
        )
        limits = _rows(record.get("data"), "shipping_limits")
        if not limits:
            limits = [
                node
                for item_record in by_domain.get("item", ())
                for node in _nodes(item_record.get("data"))
                if _first([node], "shipping_limit_date", "shipping_limit_at", "seller_ship_by")
            ]
        event_rows = _rows(record.get("data"), "events")
        late_events = [
            event
            for event in event_rows
            if str(event.get("event_type", "")).lower() in {"delivered_late", "late_delivery"}
            and str(event.get("status", "")).lower() in {"confirmed", "completed", "resolved"}
        ]
        invalid_timestamps = any(
            timestamp is not None and purchase_at is not None and timestamp < purchase_at
            for timestamp in (
                *(
                    _date(row.get("shipping_limit_at", row.get("shipping_limit_date")))
                    for row in limits
                ),
                *(_date(event.get("event_at")) for event in event_rows),
            )
        )
        try:
            if delivered and estimated:
                shipment["timeline_complete"] = carrier is not None
                if invalid_timestamps or (
                    delivered <= estimated
                    and any(_date(event.get("event_at")) for event in late_events)
                ):
                    shipment["verdict"] = "conflicting"
                    conflicts.append(
                        {
                            "field": "shipment_timeline",
                            "sources": ["get_shipment_summary/events", "get_order/timestamps"],
                            "selected_source": None,
                            "resolution_code": "TIMELINE_CONFLICT",
                        }
                    )
                elif delivered <= estimated:
                    shipment["verdict"] = "on_time"
                elif carrier and limits:
                    late_sellers: list[str] = []
                    valid_limits = 0
                    for limit in limits:
                        deadline = _date(
                            limit.get("shipping_limit_at", limit.get("shipping_limit_date"))
                        )
                        seller_id = limit.get("seller_id")
                        if deadline is None or (purchase_at and deadline < purchase_at):
                            continue
                        valid_limits += 1
                        if carrier > deadline and isinstance(seller_id, str) and seller_id:
                            late_sellers.append(seller_id)
                    if late_sellers:
                        shipment["verdict"] = "seller_delay"
                        shipment["late_seller_ids"] = list(dict.fromkeys(late_sellers))[:20]
                    elif valid_limits:
                        shipment["verdict"] = "logistics_delay"
                    else:
                        shipment["verdict"] = "insufficient_evidence"
                break
        except TypeError:
            # An offset-aware timestamp cannot be compared with a naive one.
            pass

    payment: dict[str, Any] = {
        "verdict": "insufficient_evidence",
        "captured_total_brl": None,
        "refunded_total_brl": None,
        "refundable_total_brl": None,
    }
    valid_split_payment = False
    claims_resolved_unsupported = False
    payment_rows: list[Mapping[str, Any]] = []
    payment_events: list[Mapping[str, Any]] = []
    refund_events: list[Mapping[str, Any]] = []
    explicit_payment_verdict: str | None = None
    for record in (*by_domain.get("payment", ()), *by_domain.get("refund", ())):
        nodes = list(_nodes(record.get("data")))
        verdict = _first(nodes, "verdict")
        if verdict in _PAYMENT_VERDICTS:
            explicit_payment_verdict = verdict
        valid_split_payment |= _first(nodes, "valid_split_payment") is True
        claims_resolved_unsupported |= _first(nodes, "claims_resolved_unsupported") is True
        for public_key, raw_keys in (
            ("captured_total_brl", ("captured_total_brl", "captured_amount_brl")),
            ("refunded_total_brl", ("refunded_total_brl", "refunded_amount_brl")),
            ("refundable_total_brl", ("refundable_total_brl", "refundable_balance_brl")),
        ):
            amount = _money(_first(nodes, *raw_keys))
            if amount is not None:
                payment[public_key] = float(amount)
        if record.get("domain") == "payment":
            data = record.get("data")
            payment_rows.extend(
                _rows(data, "payments")
                or (
                    [row for row in data if isinstance(row, Mapping)]
                    if isinstance(data, list)
                    else []
                )
            )
            events = _rows(data, "events")
            if (
                isinstance(data, list)
                and data
                and any("event_type" in row for row in data if isinstance(row, Mapping))
            ):
                events = [row for row in data if isinstance(row, Mapping)]
            payment_events.extend(events)
            refund_events.extend(
                event for event in events if "refund" in str(event.get("event_type", "")).lower()
            )
        elif record.get("domain") == "refund":
            data = record.get("data")
            events = _rows(data, "events")
            if isinstance(data, list):
                events = [row for row in data if isinstance(row, Mapping)]
            refund_events.extend(events)

    payment_values = _sum_money(row.get("payment_value") for row in payment_rows)
    capture_events = [
        event
        for event in payment_events
        if str(event.get("event_type", "")).lower() in {"captured", "capture", "payment_captured"}
        and str(event.get("status", "")).lower() in {"confirmed", "completed", "succeeded"}
    ]
    event_capture_values = _sum_money(
        event.get("amount_brl", event.get("amount")) for event in capture_events
    )
    item_rows: list[Mapping[str, Any]] = []
    for record in by_domain.get("item", ()):
        data = record.get("data")
        item_rows.extend(
            _rows(data, "items")
            or ([row for row in data if isinstance(row, Mapping)] if isinstance(data, list) else [])
        )
    item_amounts: list[Decimal] = []
    for row in item_rows:
        price = _money(row.get("price"))
        freight = _money(row.get("freight_value"))
        if price is None or freight is None:
            item_amounts = []
            break
        item_amounts.append(price + freight)
    item_total = _sum_money(item_amounts)
    captured = event_capture_values if event_capture_values is not None else payment_values
    if (
        payment_values is not None
        and event_capture_values is not None
        and payment_values != event_capture_values
    ):
        explicit_payment_verdict = "capture_mismatch"
        conflicts.append(
            {
                "field": "captured_total_brl",
                "sources": ["get_order_payments", "get_payment_timeline/events"],
                "selected_source": None,
                "resolution_code": "CAPTURE_TOTAL_MISMATCH",
            }
        )
    if captured is not None and item_total is not None and captured != item_total:
        explicit_payment_verdict = "capture_mismatch"
        conflicts.append(
            {
                "field": "payment_total_brl",
                "sources": ["get_order_items", "get_payment_timeline"],
                "selected_source": None,
                "resolution_code": "ORDER_PAYMENT_TOTAL_MISMATCH",
            }
        )
    if (
        capture_events
        and len(capture_events) > len(payment_rows)
        and event_capture_values is not None
        and payment_values is not None
        and event_capture_values > payment_values
    ):
        explicit_payment_verdict = "duplicate_capture"
    if captured is not None:
        payment["captured_total_brl"] = float(captured)
    refunded = _sum_money(
        event.get("amount_brl", event.get("refund_brl", event.get("amount")))
        for event in refund_events
        if str(event.get("status", "")).lower() in {"confirmed", "completed", "succeeded"}
    )
    if refunded is None and payment_events and not refund_events:
        # The payment timeline describes its events as authoritative; no refund
        # event in this complete lifecycle means the captured amount is intact.
        refunded = Decimal(0)
    if refunded is not None:
        payment["refunded_total_brl"] = float(refunded)
    if captured is not None and refunded is not None and refunded <= captured:
        payment["refundable_total_brl"] = float(captured - refunded)
    payment["verdict"] = explicit_payment_verdict or "insufficient_evidence"
    if (
        explicit_payment_verdict is None
        and captured is not None
        and item_total is not None
        and captured == item_total
        and (event_capture_values is None or event_capture_values == payment_values)
    ):
        payment["verdict"] = "reconciled"
        valid_split_payment = len(payment_rows) > 1
    for event in refund_events:
        event_type = str(event.get("event_type", "")).lower()
        event_status = str(event.get("status", "")).lower()
        if "fail" in event_type or event_status == "failed":
            payment["verdict"] = "refund_failed"
            break
        if "pending" in event_type or event_status == "pending":
            payment["verdict"] = "refund_pending"
            break
        if "refund" in event_type and event_status in {"confirmed", "completed", "succeeded"}:
            payment["verdict"] = "refunded"

    policy_decision: dict[str, Any] | None = None
    expected_version = case.get("policy_version")
    valid_rule_issues = {
        "canceled_order_paid",
        "duplicate_charge",
        "late_delivery_logistics",
        "late_delivery_seller",
        "payment_mismatch",
        "refund_failed",
        "refund_pending",
        "unavailable_order_paid",
        "unsupported_claim",
        "valid_split_payment",
    }
    for record in by_domain.get("policy", ()):
        for node in _nodes(record.get("data")):
            version = node.get("policy_version")
            if version is not None and version != expected_version:
                continue
            raw_rules = node.get("rules")
            if isinstance(raw_rules, Mapping):
                rules: dict[str, dict[str, Any]] = {}
                for issue, raw_rule in raw_rules.items():
                    if issue not in valid_rule_issues or not isinstance(raw_rule, Mapping):
                        continue
                    refund_amount = _money(raw_rule.get("refund_brl"))
                    parties = raw_rule.get("responsible_parties", [])
                    if not isinstance(parties, list):
                        continue
                    valid_parties = [
                        {
                            "party_type": party.get("party_type"),
                            "party_id": party.get("party_id"),
                        }
                        for party in parties
                        if isinstance(party, Mapping)
                        and isinstance(party.get("party_type"), str)
                        and (
                            party.get("party_id") is None or isinstance(party.get("party_id"), str)
                        )
                    ]
                    case_status = raw_rule.get("case_status")
                    action = raw_rule.get("recommended_action")
                    if (
                        case_status in {"action_required", "no_action", "needs_investigation"}
                        and isinstance(action, str)
                        and refund_amount is not None
                    ):
                        rules[issue] = {
                            "case_status": case_status,
                            "recommended_action": action,
                            "refund_brl": float(refund_amount),
                            "responsible_parties": valid_parties,
                        }
                if rules:
                    policy_decision = {
                        "policy_version": version,
                        "currency": node.get("currency"),
                        "rules": rules,
                        "evidence_ref": record.get("evidence_ref"),
                    }
                    break
            if (
                not isinstance(raw_rules, Mapping)
                and "recommended_refund_brl" in node
                and "refund_lines" in node
            ):
                policy_decision = dict(node)
                policy_decision["evidence_ref"] = record.get("evidence_ref")
                break
        if policy_decision is not None:
            break

    warnings = [str(warning) for record in records for warning in record.get("warnings", ())]
    warnings.extend(str(warning) for warning in getattr(investigation, "warnings", ()))
    return {
        "entity_resolution": {
            "status": identity.get("status", "not_found"),
            "resolved_order_ids": _ids(identity.get("resolved_order_ids", [])),
            "rejected_candidates": _ids(identity.get("rejected_candidates", [])),
            "confidence": identity.get("confidence", 0.0),
        },
        "order_status": order_status,
        "affected_seller_ids": seller_ids[:20],
        "shipment_analysis": shipment,
        "payment_analysis": payment,
        "valid_split_payment": valid_split_payment,
        "claims_resolved_unsupported": claims_resolved_unsupported,
        "policy_decision": policy_decision,
        "data_conflicts": conflicts,
        "warnings": warnings,
        "evidence_records": records,
    }


def _authorized_refund(facts: Mapping[str, Any]) -> dict[str, Any]:
    empty = {"currency": "BRL", "recommended_refund_brl": 0.0, "refund_lines": []}
    policy = _mapping(facts.get("policy_decision"))
    if not policy.get("evidence_ref"):
        return empty
    issue = facts.get("selected_issue")
    rules = _mapping(policy.get("rules"))
    rule = _mapping(rules.get(issue)) if isinstance(issue, str) else {}
    if rule:
        amount = _money(rule.get("refund_brl"))
        balance = _money(_mapping(facts.get("payment_analysis")).get("refundable_total_brl"))
        order_ids = _ids(_mapping(facts.get("entity_resolution")).get("resolved_order_ids", []))
        if amount is None or amount == 0 or balance is None or amount > balance:
            return empty
        return {
            "currency": str(policy.get("currency") or "BRL"),
            "recommended_refund_brl": float(amount),
            "refund_lines": [
                {
                    "reason_code": str(issue).upper(),
                    "amount_brl": float(amount),
                    "entity_id": order_ids[0] if len(order_ids) == 1 else None,
                }
            ],
        }
    approved = (
        policy.get("refund_eligible") is True
        or policy.get("eligible") is True
        or "recommended_refund_brl" in policy
    )
    approved |= policy.get("decision") in {"refund", "approve_refund", "approved"}
    if not approved:
        return empty
    amount = _money(policy.get("recommended_refund_brl"))
    balance = _money(_mapping(facts.get("payment_analysis")).get("refundable_total_brl"))
    lines = policy.get("refund_lines")
    if amount is None or balance is None or amount > balance or not isinstance(lines, list):
        return empty
    parsed_lines: list[dict[str, Any]] = []
    for line in lines[:10]:
        line = _mapping(line)
        line_amount = _money(line.get("amount_brl"))
        reason = line.get("reason_code")
        entity_id = line.get("entity_id")
        if (
            line_amount is None
            or not isinstance(reason, str)
            or not reason
            or len(reason) > 80
            or (entity_id is not None and (not isinstance(entity_id, str) or len(entity_id) > 128))
        ):
            return empty
        parsed_lines.append(
            {
                "reason_code": reason,
                "amount_brl": float(line_amount),
                "entity_id": entity_id,
            }
        )
    if (
        len(parsed_lines) != len(lines)
        or sum((_money(line["amount_brl"]) or Decimal(0) for line in parsed_lines), Decimal(0))
        != amount
    ):
        return empty
    return {
        "currency": "BRL",
        "recommended_refund_brl": float(amount),
        "refund_lines": parsed_lines,
    }


def decide_policy(case: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    """Return only schema-ready policy fragments for the L3B coordinator.

    ``facts`` is produced by :func:`normalize_policy_facts`, or by an equivalent
    specialist normalizer with the same named keys. Claims in ``case`` never
    establish a primary issue or a refund without MCP evidence.
    """
    del case  # Local customer claims are not authoritative.
    identity = _mapping(facts.get("entity_resolution"))
    shipment = _mapping(facts.get("shipment_analysis"))
    payment = _mapping(facts.get("payment_analysis"))
    order_status = facts.get("order_status")
    captured = _money(payment.get("captured_total_brl"))
    refunded = _money(payment.get("refunded_total_brl"))
    paid_balance = captured - refunded if captured is not None and refunded is not None else None
    shipment_verdict = shipment.get("verdict")
    payment_verdict = payment.get("verdict")
    issue = "insufficient_evidence"

    if identity.get("status") == "resolved":
        if order_status == "canceled" and paid_balance is not None and paid_balance > 0:
            issue = "canceled_order_paid"
        elif (
            order_status in {"unavailable", "unavailable_order"}
            and paid_balance
            and paid_balance > 0
        ):
            issue = "unavailable_order_paid"
        elif payment_verdict == "duplicate_capture":
            issue = "duplicate_charge"
        elif payment_verdict == "capture_mismatch":
            issue = "payment_mismatch"
        elif payment_verdict == "refund_failed":
            issue = "refund_failed"
        elif payment_verdict == "refund_pending":
            issue = "refund_pending"
        elif shipment_verdict == "seller_delay":
            issue = "late_delivery_seller"
        elif shipment_verdict == "logistics_delay":
            issue = "late_delivery_logistics"
        elif payment_verdict == "reconciled" and facts.get("valid_split_payment") is True:
            issue = "valid_split_payment"
        elif (
            payment_verdict == "reconciled"
            and shipment_verdict == "on_time"
            and facts.get("claims_resolved_unsupported") is True
        ):
            issue = "unsupported_claim"

    policy = _mapping(facts.get("policy_decision"))
    rules = _mapping(policy.get("rules"))
    has_issue_rules = bool(rules)
    rule = _mapping(rules.get(issue))
    if issue != "insufficient_evidence" and has_issue_rules and not rule:
        issue = "insufficient_evidence"
        rule = {}

    seller_ids = _ids(facts.get("affected_seller_ids", []))
    parties: list[dict[str, Any]] = list(rule.get("responsible_parties", []))
    if not parties:
        parties = []
    if issue == "late_delivery_seller":
        parties = [{"party_type": "seller", "party_id": seller_id} for seller_id in seller_ids[:5]]
    elif issue == "late_delivery_logistics":
        parties = [{"party_type": "logistics_provider", "party_id": None}]
    elif issue in {"duplicate_charge", "payment_mismatch", "refund_failed", "refund_pending"}:
        parties = [{"party_type": "payment_provider", "party_id": None}]
    elif issue in {"canceled_order_paid", "unavailable_order_paid"}:
        parties = [{"party_type": "unknown", "party_id": None}]
    if issue == "late_delivery_seller" and not parties:
        issue = "insufficient_evidence"

    actions_by_issue = {
        "canceled_order_paid": "review_refund_for_canceled_order",
        "unavailable_order_paid": "review_refund_for_unavailable_order",
        "duplicate_charge": "reconcile_duplicate_capture",
        "payment_mismatch": "reconcile_payment_capture",
        "refund_failed": "reprocess_failed_refund",
        "refund_pending": "follow_up_pending_refund",
        "late_delivery_seller": "review_seller_delay",
        "late_delivery_logistics": "review_logistics_delay",
        "insufficient_evidence": "investigate_missing_evidence",
    }
    status = rule.get("case_status") or (
        "needs_investigation"
        if issue == "insufficient_evidence"
        else "no_action"
        if issue in {"unsupported_claim", "valid_split_payment"}
        else "action_required"
    )
    refund_facts = {**facts, "selected_issue": issue}
    financial = (
        _authorized_refund(refund_facts)
        if status == "action_required"
        else {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        }
    )
    action = rule.get("recommended_action") or actions_by_issue.get(issue)
    actions = [action] if isinstance(action, str) and status != "no_action" else []
    if financial["recommended_refund_brl"] > 0:
        actions.append("issue_authorized_refund")
    confidence = 0.3 if issue == "insufficient_evidence" else 0.78
    if issue in {"unsupported_claim", "valid_split_payment"}:
        confidence = 0.68
    if facts.get("warnings") or facts.get("data_conflicts"):
        confidence = min(confidence, 0.5)
    if identity.get("status") != "resolved":
        confidence = min(confidence, 0.3)
    return {
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": [],
            "case_status": status,
            "confidence": confidence,
        },
        "root_cause_analysis": {
            "ranked_causes": []
            if issue == "insufficient_evidence"
            else [
                {"cause_code": issue.upper(), "rank": 1},
            ],
            "responsible_parties": parties if issue != "insufficient_evidence" else [],
        },
        "data_conflicts": list(facts.get("data_conflicts", []))[:5],
        "financial_resolution": financial,
        "resolution_actions": actions[:8],
    }
