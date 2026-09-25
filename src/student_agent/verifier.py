from __future__ import annotations

import re
from typing import Any

from .trace import TraceWriter

EV_PATTERN = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")


class VerifierAgent:
    """Agent responsible for checking invariants, cross-field consistency,

    confidence calibration, and emitting verification trace events.
    """

    def __init__(self, trace: TraceWriter) -> None:
        self.name = "verifier"
        self.trace = trace

    def verify_and_align(self, case_id: str, output: dict[str, Any]) -> dict[str, Any]:
        """Perform deterministic alignment, invariant checks, and calibration before finalization."""
        validated = dict(output)
        validated["schema_version"] = "day09-l3b-output-v2"
        validated["case_id"] = case_id

        # 1. Sanitize and validate evidence_refs
        all_refs: list[str] = []
        for ref in validated.get("evidence_refs", []):
            if isinstance(ref, str) and EV_PATTERN.match(ref) and ref not in all_refs:
                all_refs.append(ref)
        validated["evidence_refs"] = all_refs[:30]

        # 2. Entity resolution invariants
        entity_res = validated.get("entity_resolution", {})
        resolved_orders = list(dict.fromkeys(entity_res.get("resolved_order_ids", [])))
        rejected_orders = list(
            dict.fromkeys(
                cand for cand in entity_res.get("rejected_candidates", []) if cand not in resolved_orders
            )
        )
        entity_status = entity_res.get("status", "resolved" if resolved_orders else "ambiguous")
        er_confidence = float(entity_res.get("confidence", 0.9 if entity_status == "resolved" else 0.5))
        er_confidence = max(0.0, min(1.0, er_confidence))

        validated["entity_resolution"] = {
            "status": entity_status,
            "resolved_order_ids": resolved_orders,
            "rejected_candidates": rejected_orders,
            "confidence": er_confidence,
        }

        # Ensure affected_entities includes resolved orders
        aff_entities = validated.get("affected_entities", {})
        order_ids = list(dict.fromkeys(aff_entities.get("order_ids", []) + resolved_orders))
        validated["affected_entities"] = {
            "order_ids": order_ids,
            "item_ids": list(dict.fromkeys(aff_entities.get("item_ids", []))),
            "seller_ids": list(dict.fromkeys(aff_entities.get("seller_ids", []))),
            "payment_references": list(dict.fromkeys(aff_entities.get("payment_references", []))),
            "shipment_ids": list(dict.fromkeys(aff_entities.get("shipment_ids", []))),
        }

        # 3. Customer context invariants
        cust_ctx = validated.get("customer_context", {})
        validated["customer_context"] = {
            "customer_unique_id": cust_ctx.get("customer_unique_id"),
            "related_order_ids": list(dict.fromkeys(cust_ctx.get("related_order_ids", []))),
        }

        # 4. Shipment & Seller responsibility consistency
        shipment = validated.get("shipment_analysis", {})
        shipment_verdict = shipment.get("verdict", "on_time")
        late_sellers = list(dict.fromkeys(shipment.get("late_seller_ids", [])))
        timeline_complete = bool(shipment.get("timeline_complete", True))

        if shipment_verdict == "seller_delay" and not late_sellers:
            # Fallback to known sellers if seller delay was flagged
            late_sellers = list(validated["affected_entities"]["seller_ids"])

        validated["shipment_analysis"] = {
            "verdict": shipment_verdict,
            "late_seller_ids": late_sellers,
            "timeline_complete": timeline_complete,
        }

        # 5. Financial resolution math & consistency
        fin_res = validated.get("financial_resolution", {})
        refund_lines = fin_res.get("refund_lines", [])
        clean_lines: list[dict[str, Any]] = []
        for line in refund_lines[:10]:
            clean_lines.append(
                {
                    "reason_code": str(line.get("reason_code", "standard_refund")),
                    "amount_brl": max(0.0, round(float(line.get("amount_brl", 0.0)), 2)),
                    "entity_id": line.get("entity_id"),
                }
            )

        sum_refunds = round(sum(l["amount_brl"] for l in clean_lines), 2)
        rec_refund = round(float(fin_res.get("recommended_refund_brl", sum_refunds)), 2)
        if rec_refund != sum_refunds:
            rec_refund = sum_refunds

        validated["financial_resolution"] = {
            "currency": "BRL",
            "recommended_refund_brl": rec_refund,
            "refund_lines": clean_lines,
        }

        # 6. Case status vs Action vs Root cause alignment
        assessment = validated.get("assessment", {})
        primary_issue = assessment.get("primary_issue", "insufficient_evidence")
        secondary_issues = list(dict.fromkeys(assessment.get("secondary_issues", [])))[:10]
        # Keep case_status from coordinator (evidence-driven); only override if obviously wrong
        case_status = assessment.get("case_status", "no_action" if rec_refund == 0.0 else "action_required")
        # If there's a recommended refund but status says no_action → override to action_required
        if rec_refund > 0.0 and case_status == "no_action":
            case_status = "action_required"

        resolution_actions = list(dict.fromkeys(validated.get("resolution_actions", [])))[:8]
        if case_status == "action_required" and not resolution_actions:
            if rec_refund > 0:
                resolution_actions.append("issue_customer_refund")
            if shipment_verdict == "seller_delay":
                resolution_actions.append("notify_seller_delay_penalty")
            if not resolution_actions:
                resolution_actions.append("update_customer_status")

        # Root cause alignment with shipment/payment findings
        root_cause = validated.get("root_cause_analysis", {})
        ranked_causes = root_cause.get("ranked_causes", [])
        if not ranked_causes:
            ranked_causes = [{"cause_code": "CUSTOMER_INQUIRY", "rank": 1}]
        responsible_parties = root_cause.get("responsible_parties", [])
        if not responsible_parties:
            if shipment_verdict == "seller_delay" and late_sellers:
                responsible_parties = [{"party_type": "seller", "party_id": late_sellers[0]}]
            elif shipment_verdict == "logistics_delay":
                responsible_parties = [{"party_type": "logistics_provider", "party_id": None}]
            else:
                responsible_parties = [{"party_type": "platform", "party_id": None}]

        validated["root_cause_analysis"] = {
            "ranked_causes": ranked_causes[:5],
            "responsible_parties": responsible_parties[:5],
        }
        validated["resolution_actions"] = resolution_actions

        # 7. Calibration of Confidence
        # Start with base confidence from assessment, then adjust by evidence quality signals
        confidence = float(assessment.get("confidence", 0.85))
        ev_count = len(validated["evidence_refs"])
        conflict_count = len(validated.get("data_conflicts", []))

        # Reward rich evidence
        if ev_count >= 6:
            confidence = min(1.0, confidence + 0.05)
        elif ev_count < 2:
            confidence = min(confidence, 0.60)
        elif ev_count < 4:
            confidence = min(confidence, 0.75)

        # Penalize conflicts — data contradictions reduce certainty
        if conflict_count > 0:
            confidence = min(confidence, 0.80 - 0.05 * conflict_count)

        # Low confidence for vague primary issues
        if primary_issue in ("insufficient_evidence", "unsupported_claim"):
            confidence = min(confidence, 0.40)

        # Penalize incomplete timeline
        shipment = validated.get("shipment_analysis", {})
        if not shipment.get("timeline_complete", True):
            confidence = min(confidence, 0.78)

        confidence = max(0.10, min(0.95, round(confidence, 2)))

        validated["assessment"] = {
            "primary_issue": primary_issue,
            "secondary_issues": secondary_issues,
            "case_status": case_status,
            "confidence": confidence,
        }

        # 8. Data conflicts cleanup
        data_conflicts = validated.get("data_conflicts", [])
        clean_conflicts = []
        for conf in data_conflicts[:5]:
            if "field" in conf and "sources" in conf and len(conf["sources"]) >= 2:
                clean_conflicts.append(
                    {
                        "field": str(conf["field"]),
                        "sources": list(dict.fromkeys(conf["sources"]))[:5],
                        "selected_source": conf.get("selected_source"),
                        "resolution_code": str(conf.get("resolution_code", "applied_policy")),
                    }
                )
        validated["data_conflicts"] = clean_conflicts

        # 9. Emit verification_completed trace event
        self.trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor=self.name,
            decision_code="APPROVED",
            attributes={
                "case_status": case_status,
                "confidence": confidence,
                "evidence_count": len(validated["evidence_refs"]),
                "conflicts_resolved": len(clean_conflicts),
            },
        )

        return validated
