from __future__ import annotations

from typing import Any

from .base_agent import BaseAgent


class ConflictResolverAgent(BaseAgent):
    """Member 3: Resolves multi-source data conflicts, determines authoritative primary issue,

    performs root-cause ranking, and recommends resolution actions.
    """

    def __init__(self, gateway: Any, trace: Any) -> None:
        super().__init__("conflict_resolver", gateway, trace)

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        case = context["case"]
        case_id = case["case_id"]
        claims: list[dict[str, Any]] = case.get("customer_request", {}).get("claims", [])
        shipment_analysis = context.get("shipment_analysis", {})
        payment_analysis = context.get("payment_analysis", {})

        # 1. Query policy via get_policy
        policy_version = case.get("policy_version", "EC_POLICY_V2")
        try:
            await self.call_tool("get_policy", case_id=case_id, policy_version=policy_version)
        except Exception:
            pass

        shipment_verdict = shipment_analysis.get("verdict", "on_time")
        payment_verdict = payment_analysis.get("verdict", "reconciled")
        late_sellers = shipment_analysis.get("late_seller_ids", [])
        affected_sellers = shipment_analysis.get("affected_seller_ids", [])
        affected_payments = payment_analysis.get("affected_payment_references", [])

        data_conflicts: list[dict[str, Any]] = []
        resolution_actions: list[str] = []
        ranked_causes: list[dict[str, Any]] = []
        responsible_parties: list[dict[str, Any]] = []

        claim_topics = [c.get("topic", "") for c in claims]
        # Filter out meta-topic "requested_full_refund" to get the actual complaint
        substantive_topics = [t for t in claim_topics if t != "requested_full_refund"]
        primary_claim_topic = substantive_topics[0] if substantive_topics else ""

        # ─── PRIMARY ISSUE DETERMINATION ────────────────────────────────────────
        # Decision hierarchy:
        #   1. MCP evidence from payment specialist (highest authority)
        #   2. MCP evidence from shipment specialist
        #   3. Customer claim topic (fallback when MCP has no clear signal)
        # ────────────────────────────────────────────────────────────────────────

        primary_issue = "insufficient_evidence"

        # ── Payment-domain issues (resolved by payment MCP data) ──────────────
        if payment_verdict == "duplicate_capture":
            primary_issue = "duplicate_charge"
            ranked_causes = [{"cause_code": "DUPLICATE_ACQUIRER_TRANSACTION", "rank": 1}]
            responsible_parties = [
                {"party_type": "payment_provider", "party_id": affected_payments[0] if affected_payments else None}
            ]
            resolution_actions = ["issue_customer_refund", "reconcile_payment_gateway"]

        elif payment_verdict == "refund_failed":
            primary_issue = "refund_failed"
            ranked_causes = [{"cause_code": "GATEWAY_REFUND_FAILURE", "rank": 1}]
            responsible_parties = [{"party_type": "payment_provider", "party_id": None}]
            resolution_actions = ["retrigger_refund_payment", "update_customer_status"]

        elif payment_verdict == "refund_pending":
            primary_issue = "refund_pending"
            ranked_causes = [{"cause_code": "REFUND_CLEARING_IN_PROGRESS", "rank": 1}]
            responsible_parties = [{"party_type": "platform", "party_id": None}]
            resolution_actions = ["expedite_refund_settlement", "update_customer_status"]

        elif payment_verdict == "capture_mismatch":
            primary_issue = "payment_mismatch"
            ranked_causes = [{"cause_code": "PARTIAL_CAPTURE_MISMATCH", "rank": 1}]
            responsible_parties = [{"party_type": "payment_provider", "party_id": None}]
            resolution_actions = ["reconcile_discrepancy_ledger", "notify_customer"]

        # ── Shipment-domain issues (resolved by shipment MCP data) ─────────────
        elif shipment_verdict == "seller_delay":
            primary_issue = "late_delivery_seller"
            seller_id = late_sellers[0] if late_sellers else (affected_sellers[0] if affected_sellers else None)
            ranked_causes = [{"cause_code": "SELLER_DISPATCH_OVERDUE", "rank": 1}]
            responsible_parties = [{"party_type": "seller", "party_id": seller_id}]
            resolution_actions = ["notify_seller_delay_penalty", "update_customer_status"]

        elif shipment_verdict == "logistics_delay":
            primary_issue = "late_delivery_logistics"
            ranked_causes = [{"cause_code": "CARRIER_TRANSIT_DELAY", "rank": 1}]
            responsible_parties = [{"party_type": "logistics_provider", "party_id": None}]
            resolution_actions = ["contact_logistics_provider", "update_customer_status"]

        # ── Claim-topic fallback (when MCP signals are inconclusive) ──────────
        elif primary_claim_topic == "duplicate_charge":
            primary_issue = "duplicate_charge"
            ranked_causes = [{"cause_code": "DUPLICATE_ACQUIRER_TRANSACTION", "rank": 1}]
            responsible_parties = [
                {"party_type": "payment_provider", "party_id": affected_payments[0] if affected_payments else None}
            ]
            resolution_actions = ["issue_customer_refund", "reconcile_payment_gateway"]

        elif primary_claim_topic == "refund_failed":
            primary_issue = "refund_failed"
            ranked_causes = [{"cause_code": "GATEWAY_REFUND_FAILURE", "rank": 1}]
            responsible_parties = [{"party_type": "payment_provider", "party_id": None}]
            resolution_actions = ["retrigger_refund_payment", "update_customer_status"]

        elif primary_claim_topic == "refund_pending":
            primary_issue = "refund_pending"
            ranked_causes = [{"cause_code": "REFUND_CLEARING_IN_PROGRESS", "rank": 1}]
            responsible_parties = [{"party_type": "platform", "party_id": None}]
            resolution_actions = ["expedite_refund_settlement", "update_customer_status"]

        elif primary_claim_topic == "payment_mismatch":
            primary_issue = "payment_mismatch"
            ranked_causes = [{"cause_code": "PARTIAL_CAPTURE_MISMATCH", "rank": 1}]
            responsible_parties = [{"party_type": "payment_provider", "party_id": None}]
            resolution_actions = ["reconcile_discrepancy_ledger", "notify_customer"]

        elif primary_claim_topic == "valid_split_payment":
            primary_issue = "valid_split_payment"
            ranked_causes = [{"cause_code": "VALID_SPLIT_TENDER", "rank": 1}]
            responsible_parties = [{"party_type": "customer", "party_id": None}]
            resolution_actions = ["explain_split_tender_policy"]
            data_conflicts.append(
                {
                    "field": "payment_structure",
                    "sources": ["customer_claim", "mcp_payment_gateway"],
                    "selected_source": "mcp_payment_gateway",
                    "resolution_code": "split_tender_policy_applied",
                }
            )

        elif primary_claim_topic == "late_delivery_seller":
            primary_issue = "late_delivery_seller"
            seller_id = late_sellers[0] if late_sellers else (affected_sellers[0] if affected_sellers else None)
            ranked_causes = [{"cause_code": "SELLER_DISPATCH_OVERDUE", "rank": 1}]
            responsible_parties = [{"party_type": "seller", "party_id": seller_id}]
            resolution_actions = ["notify_seller_delay_penalty", "update_customer_status"]

        elif primary_claim_topic == "late_delivery_logistics":
            primary_issue = "late_delivery_logistics"
            ranked_causes = [{"cause_code": "CARRIER_TRANSIT_DELAY", "rank": 1}]
            responsible_parties = [{"party_type": "logistics_provider", "party_id": None}]
            resolution_actions = ["contact_logistics_provider", "update_customer_status"]

        elif primary_claim_topic == "canceled_order_paid":
            primary_issue = "canceled_order_paid"
            ranked_causes = [{"cause_code": "ORDER_CANCELED_BEFORE_FULFILLMENT", "rank": 1}]
            responsible_parties = [{"party_type": "platform", "party_id": None}]
            resolution_actions = ["issue_customer_refund"]

        elif primary_claim_topic == "unavailable_order_paid":
            primary_issue = "unavailable_order_paid"
            ranked_causes = [{"cause_code": "INVENTORY_UNAVAILABLE_AFTER_PAYMENT", "rank": 1}]
            responsible_parties = [
                {"party_type": "seller", "party_id": affected_sellers[0] if affected_sellers else None}
            ]
            resolution_actions = ["issue_customer_refund"]

        elif primary_claim_topic == "unsupported_claim":
            primary_issue = "unsupported_claim"
            ranked_causes = [{"cause_code": "CLAIM_NOT_SUPPORTED_BY_EVIDENCE", "rank": 1}]
            responsible_parties = [{"party_type": "customer", "party_id": None}]
            resolution_actions = ["close_case_no_action"]

        else:
            primary_issue = "unsupported_claim"
            ranked_causes = [{"cause_code": "CLAIM_NOT_SUPPORTED_BY_EVIDENCE", "rank": 1}]
            responsible_parties = [{"party_type": "customer", "party_id": None}]
            resolution_actions = ["close_case_no_action"]

        # 3. Check for conflicts between claims and verified MCP reality
        if "late_delivery_logistics" in claim_topics and shipment_verdict == "seller_delay":
            data_conflicts.append(
                {
                    "field": "delay_attribution",
                    "sources": ["customer_claim", "seller_dispatch_log"],
                    "selected_source": "seller_dispatch_log",
                    "resolution_code": "seller_shipping_limit_precedence",
                }
            )

        if "late_delivery_seller" in claim_topics and shipment_verdict == "on_time":
            data_conflicts.append(
                {
                    "field": "delivery_timeline",
                    "sources": ["customer_claim", "carrier_tracking_log"],
                    "selected_source": "carrier_tracking_log",
                    "resolution_code": "carrier_timestamp_precedence",
                }
            )

        if "duplicate_charge" in claim_topics and payment_verdict == "reconciled":
            data_conflicts.append(
                {
                    "field": "payment_charge",
                    "sources": ["customer_claim", "banking_gateway"],
                    "selected_source": "banking_gateway",
                    "resolution_code": "payment_ledger_precedence",
                }
            )

        # 4. Secondary issues: remaining substantive topics after primary
        secondary_issues = [
            t for t in substantive_topics if t != primary_issue
        ][:10]

        return {
            "primary_issue": primary_issue,
            "secondary_issues": secondary_issues,
            "data_conflicts": data_conflicts[:5],
            "ranked_causes": ranked_causes[:5],
            "responsible_parties": responsible_parties[:5],
            "resolution_actions": list(dict.fromkeys(resolution_actions))[:8],
        }
