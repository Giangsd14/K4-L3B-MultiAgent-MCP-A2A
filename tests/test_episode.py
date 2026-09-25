from __future__ import annotations

from decimal import Decimal

import pytest

from student_agent.decision import CaseFacts, _payment_finding, _shipment_finding
from student_agent.episode import reconcile_claim_episode


def case(topic: str) -> dict:
    return {
        "opened_at": "2018-05-13T09:00:00-03:00",
        "customer_request": {"claims": [{"topic": topic}]},
    }


def episode(purchase: str, status: str = "delivered", delivered: str | None = None) -> dict:
    return {
        "order_id": "order-1",
        "order_status": status,
        "order_purchase_timestamp": purchase,
        "order_delivered_customer_date": delivered,
        "order_estimated_delivery_date": "2018-05-12T09:00:00-03:00",
    }


def capture(amount: str, when: str) -> dict:
    return {"event_type": "captured", "amount_brl": amount, "event_at": when}


def test_split_payment_cohort_excludes_failed_refund_for_other_purchase() -> None:
    older = episode("2018-04-01T09:00:00-03:00")
    target = episode("2018-05-01T09:00:00-03:00")
    facts = CaseFacts(
        entity_status="resolved",
        resolved_order_ids=["order-1"],
        customer_history={"orders": [older, target]},
        order=older,
        payment={
            "payments": [
                {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "52"},
                {
                    "payment_sequential": "1",
                    "payment_type": "credit_card",
                    "payment_value": "44.50",
                },
                {"payment_sequential": "2", "payment_type": "voucher", "payment_value": "44.50"},
            ],
            "events": [
                capture("52", "2018-04-01T10:00:00-03:00"),
                capture("44.50", "2018-05-01T10:00:00-03:00"),
                capture("44.50", "2018-05-01T11:00:00-03:00"),
            ],
        },
        refund={"events": [{"status": "failed", "amount_brl": "52"}]},
    )

    reconcile_claim_episode(case("valid_split_payment"), facts)

    verdict, captured, _, _, payments = _payment_finding(facts)
    assert facts.history_episode_selected
    assert facts.order["order_purchase_timestamp"] == target["order_purchase_timestamp"]
    assert verdict == "reconciled"
    assert captured == Decimal("89.00")
    assert len(payments) == 2
    assert facts.refund == {"events": []}


def test_on_time_episode_excludes_other_purchase_late_event() -> None:
    target = episode("2018-05-01T09:00:00-03:00", delivered="2018-05-10T09:00:00-03:00")
    other = episode("2018-04-01T09:00:00-03:00", delivered="2018-04-20T09:00:00-03:00")
    other["order_estimated_delivery_date"] = "2018-04-15T09:00:00-03:00"
    facts = CaseFacts(
        entity_status="resolved",
        resolved_order_ids=["order-1"],
        customer_history={"orders": [other, target]},
        order=other,
        shipment={
            "order_status": "delivered",
            "delivered_customer_at": other["order_delivered_customer_date"],
            "estimated_delivery_at": other["order_estimated_delivery_date"],
            "events": [{"event_type": "delivered_late", "event_at": "2018-04-20T09:00:00-03:00"}],
        },
        payment={
            "payments": [{"payment_value": "16"}, {"payment_value": "89"}],
            "events": [
                capture("16", "2018-04-01T10:00:00-03:00"),
                capture("89", "2018-05-01T10:00:00-03:00"),
            ],
        },
        refund={"events": []},
    )

    reconcile_claim_episode(case("unsupported_claim"), facts)

    assert facts.history_episode_selected
    assert _shipment_finding(facts, [])[0] == "on_time"
    assert _payment_finding(facts)[1] == Decimal("89")


def test_canceled_episode_excludes_other_purchase_delivery() -> None:
    other = episode("2018-04-01T09:00:00-03:00", delivered="2018-04-20T09:00:00-03:00")
    target = episode("2018-05-01T09:00:00-03:00", status="canceled")
    facts = CaseFacts(
        entity_status="resolved",
        resolved_order_ids=["order-1"],
        customer_history={"orders": [other, target]},
        order=other,
        shipment={
            "order_status": "delivered",
            "delivered_customer_at": other["order_delivered_customer_date"],
            "events": [{"event_type": "delivered_late", "event_at": "2018-04-20T09:00:00-03:00"}],
        },
        payment={
            "payments": [{"payment_value": "18"}, {"payment_value": "79"}],
            "events": [
                capture("18", "2018-04-01T10:00:00-03:00"),
                capture("79", "2018-05-01T10:00:00-03:00"),
            ],
        },
        refund={"events": []},
    )

    reconcile_claim_episode(case("canceled_order_paid"), facts)

    assert facts.history_episode_selected
    assert facts.order["order_status"] == "canceled"
    assert _shipment_finding(facts, [])[0] == "insufficient_evidence"
    assert _payment_finding(facts)[1] == Decimal("79")


@pytest.mark.parametrize(
    ("topic", "actor", "expected_verdict", "late_amount"),
    [
        ("late_delivery_seller", "seller", "seller_delay", "18"),
        ("late_delivery_logistics", "logistics_provider", "logistics_delay", "16"),
    ],
)
def test_late_delivery_episode_excludes_other_purchase_payment(
    topic: str, actor: str, expected_verdict: str, late_amount: str
) -> None:
    target = episode("2018-05-01T09:00:00-03:00", delivered="2018-05-14T09:00:00-03:00")
    other = episode("2018-04-01T09:00:00-03:00", status="canceled")
    facts = CaseFacts(
        entity_status="resolved",
        resolved_order_ids=["order-1"],
        customer_history={"orders": [other, target]},
        order=other,
        shipment={
            "order_status": "canceled",
            "delivered_customer_at": None,
            "events": [
                {
                    "event_type": "delivered_late",
                    "actor": actor,
                    "event_at": "2018-05-14T09:00:00-03:00",
                }
            ],
        },
        payment={
            "payments": [{"payment_value": "79"}, {"payment_value": late_amount}],
            "events": [
                capture("79", "2018-04-01T10:00:00-03:00"),
                capture(late_amount, "2018-05-01T10:00:00-03:00"),
            ],
        },
        refund={"events": []},
    )

    reconcile_claim_episode(case(topic), facts)

    assert facts.history_episode_selected
    assert facts.order["order_status"] == "delivered"
    assert _shipment_finding(facts, ["seller-1"])[0] == expected_verdict
    assert _payment_finding(facts)[1] == Decimal(late_amount)


def test_pending_refund_excludes_other_purchase_reconciliation_error() -> None:
    other = episode("2018-04-01T09:00:00-03:00")
    target = episode("2018-05-01T09:00:00-03:00")
    facts = CaseFacts(
        entity_status="resolved",
        resolved_order_ids=["order-1"],
        customer_history={"orders": [other, target]},
        order=other,
        payment={
            "payments": [{"payment_value": "35"}, {"payment_value": "89"}],
            "events": [
                capture("35", "2018-04-01T10:00:00-03:00"),
                {
                    "event_type": "reconciliation_mismatch",
                    "amount_brl": "35",
                    "event_at": "2018-04-01T12:00:00-03:00",
                },
                capture("89", "2018-05-01T10:00:00-03:00"),
            ],
        },
        refund={"events": [{"status": "pending", "amount_brl": "89"}]},
    )

    reconcile_claim_episode(case("refund_pending"), facts)

    verdict, captured, _, _, _ = _payment_finding(facts)
    assert facts.history_episode_selected
    assert verdict == "refund_pending"
    assert captured == Decimal("89")
