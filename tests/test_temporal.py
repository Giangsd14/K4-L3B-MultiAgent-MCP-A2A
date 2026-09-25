from __future__ import annotations

from decimal import Decimal

from student_agent.decision import CaseFacts, _payment_finding, _primary_issue, _shipment_finding
from student_agent.temporal import normalize_temporal_facts


def order(status: str = "delivered") -> dict[str, str]:
    return {
        "order_status": status,
        "order_purchase_timestamp": "2018-05-01T09:00:00-03:00",
        "order_approved_at": "2018-05-01T10:00:00-03:00",
    }


def payment(value: str, sequence: str = "1", method: str = "credit_card") -> dict[str, str]:
    return {
        "payment_sequential": sequence,
        "payment_type": method,
        "payment_installments": "1",
        "payment_value": value,
    }


def capture(value: str, at: str) -> dict[str, str]:
    return {"event_type": "captured", "amount_brl": value, "event_at": at}


def test_stale_shipment_and_payment_rows_do_not_override_current_order() -> None:
    facts = CaseFacts(
        entity_status="resolved",
        resolved_order_ids=["order-1"],
        order=order(),
        items=[
            {"order_item_id": "item-1", "shipping_limit_date": "2018-05-04"},
            {"order_item_id": "item-1", "shipping_limit_date": "2018-01-04"},
        ],
        shipment={
            "delivered_customer_at": "2018-05-10T09:00:00-03:00",
            "estimated_delivery_at": "2018-05-11T09:00:00-03:00",
            "events": [
                {
                    "event_type": "delivered_late",
                    "actor": "logistics_provider",
                    "event_at": "2018-01-10T09:00:00-03:00",
                }
            ],
        },
        payment={
            "payments": [payment("89.00"), payment("16.00")],
            "events": [
                capture("89.00", "2018-05-01T10:00:00-03:00"),
                capture("16.00", "2018-01-01T10:00:00-03:00"),
            ],
        },
        refund={"events": []},
    )

    normalize_temporal_facts(facts)

    assert len(facts.items) == 1
    assert facts.items[0]["shipping_limit_date"] == "2018-05-04"
    assert _shipment_finding(facts, [])[0] == "on_time"
    verdict, captured, _, _, payments = _payment_finding(facts)
    assert verdict == "reconciled"
    assert captured == Decimal("89.00")
    assert len(payments) == 1


def test_split_payment_excludes_refund_linked_to_other_payment_cohort() -> None:
    facts = CaseFacts(
        entity_status="resolved",
        resolved_order_ids=["order-1"],
        order=order(),
        shipment={"events": []},
        payment={
            "payments": [
                payment("44.50"),
                payment("44.50", "2", "voucher"),
                payment("52.00"),
            ],
            "events": [
                capture("44.50", "2018-05-01T10:00:00-03:00"),
                capture("44.50", "2018-05-01T11:00:00-03:00"),
                capture("52.00", "2018-06-01T10:00:00-03:00"),
            ],
        },
        refund={
            "events": [
                {
                    "event_type": "refund_requested",
                    "status": "failed",
                    "amount_brl": "52.00",
                    "event_at": "2018-06-10T09:00:00-03:00",
                }
            ]
        },
    )

    normalize_temporal_facts(facts)

    assert facts.refund == {"events": []}
    verdict, captured, _, outstanding, payments = _payment_finding(facts)
    assert verdict == "reconciled"
    assert captured == Decimal("89.00")
    issue, _ = _primary_issue(
        facts, {"refund_failed"}, "on_time", verdict, captured, outstanding, payments
    )
    assert issue == "valid_split_payment"


def test_repeated_payment_cohort_is_duplicate_capture() -> None:
    rows = [payment("64.00"), payment("64.00", "2", "voucher")]
    facts = CaseFacts(
        entity_status="resolved",
        resolved_order_ids=["order-1"],
        order=order(),
        payment={
            "payments": rows + [dict(row) for row in rows],
            "events": [
                capture("64.00", "2018-05-01T10:00:00-03:00"),
                capture("64.00", "2018-05-01T11:00:00-03:00"),
                capture("64.00", "2018-06-01T10:00:00-03:00"),
                capture("64.00", "2018-06-01T11:00:00-03:00"),
            ],
        },
        refund={"events": []},
    )

    normalize_temporal_facts(facts)

    verdict, captured, _, _, payments = _payment_finding(facts)
    assert verdict == "duplicate_capture"
    assert captured == Decimal("256.00")
    assert len(payments) == 2


def test_canceled_order_cannot_have_delivered_late_event() -> None:
    facts = CaseFacts(
        order=order("canceled"),
        shipment={
            "delivered_customer_at": None,
            "events": [
                {
                    "event_type": "delivered_late",
                    "actor": "seller",
                    "event_at": "2018-05-15T09:00:00-03:00",
                }
            ],
        },
    )

    normalize_temporal_facts(facts)

    assert _shipment_finding(facts, ["seller-1"])[0] == "insufficient_evidence"
