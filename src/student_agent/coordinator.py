from __future__ import annotations

from typing import Any

from .mcp_gateway import EvidenceGateway
from .specialists import (
    ConflictResolverAgent,
    EntityResolverAgent,
    PaymentSpecialistAgent,
    ShipmentSpecialistAgent,
)
from .trace import TraceWriter
from .verifier import VerifierAgent


class MultiAgentCoordinator:
    """Coordinator agent managing investigation tasks, handoffs, specialist dispatch,

    evidence aggregation, and verifier integration.
    """

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace
        self.entity_resolver = EntityResolverAgent(gateway, trace)
        self.shipment_specialist = ShipmentSpecialistAgent(gateway, trace)
        self.payment_specialist = PaymentSpecialistAgent(gateway, trace)
        self.conflict_resolver = ConflictResolverAgent(gateway, trace)
        self.verifier = VerifierAgent(trace)

    async def coordinate(self, case: dict[str, Any]) -> dict[str, Any]:
        case_id = case["case_id"]
        context: dict[str, Any] = {"case": case}
        accumulated_evidence_refs: list[str] = []

        # 1. Dispatch to Entity Resolver
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=self.entity_resolver.name,
            attributes={"task": "entity_resolution"},
        )
        entity_result = await self.entity_resolver.run(context)
        context["entity_resolution"] = entity_result
        accumulated_evidence_refs.extend(self.entity_resolver.collected_evidence_refs)

        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=self.entity_resolver.name,
            target="specialist_investigation",
            attributes={"resolved_orders_count": len(entity_result.get("resolved_order_ids", []))},
        )

        # 2. Dispatch to Specialist Agents (Shipment & Payment)
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=self.shipment_specialist.name,
            attributes={"task": "shipment_investigation"},
        )
        shipment_result = await self.shipment_specialist.run(context)
        context["shipment_analysis"] = shipment_result
        accumulated_evidence_refs.extend(self.shipment_specialist.collected_evidence_refs)

        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=self.payment_specialist.name,
            attributes={"task": "payment_investigation"},
        )
        payment_result = await self.payment_specialist.run(context)
        context["payment_analysis"] = payment_result
        accumulated_evidence_refs.extend(self.payment_specialist.collected_evidence_refs)

        # 3. Dispatch to Conflict & Root Cause Resolver
        self.trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=self.conflict_resolver.name,
            attributes={"task": "conflict_resolution"},
        )
        conflict_result = await self.conflict_resolver.run(context)
        context["conflict_resolution"] = conflict_result
        accumulated_evidence_refs.extend(self.conflict_resolver.collected_evidence_refs)

        self.trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor=self.conflict_resolver.name,
            decision_code=conflict_result.get("primary_issue", "insufficient_evidence"),
            attributes={"primary_issue": conflict_result.get("primary_issue")},
        )

        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=self.conflict_resolver.name,
            target=self.verifier.name,
            attributes={"primary_issue": conflict_result.get("primary_issue")},
        )

        # 4. Construct Intermediate Output
        intermediate_output: dict[str, Any] = {
            "schema_version": "day09-l3b-output-v2",
            "case_id": case_id,
            "assessment": {
                "primary_issue": conflict_result.get("primary_issue", "insufficient_evidence"),
                "secondary_issues": conflict_result.get("secondary_issues", []),
                "case_status": "no_action",
                "confidence": 0.85,
            },
            "affected_entities": {
                "order_ids": entity_result.get("resolved_order_ids", []),
                "item_ids": shipment_result.get("affected_item_ids", []),
                "seller_ids": shipment_result.get("affected_seller_ids", []),
                "payment_references": payment_result.get("affected_payment_references", []),
                "shipment_ids": shipment_result.get("affected_shipment_ids", []),
            },
            "entity_resolution": {
                "status": entity_result.get("status", "resolved"),
                "resolved_order_ids": entity_result.get("resolved_order_ids", []),
                "rejected_candidates": entity_result.get("rejected_candidates", []),
                "confidence": entity_result.get("confidence", 0.90),
            },
            "customer_context": {
                "customer_unique_id": entity_result.get("customer_unique_id"),
                "related_order_ids": entity_result.get("related_order_ids", []),
            },
            "shipment_analysis": {
                "verdict": shipment_result.get("verdict", "on_time"),
                "late_seller_ids": shipment_result.get("late_seller_ids", []),
                "timeline_complete": shipment_result.get("timeline_complete", True),
            },
            "payment_analysis": {
                "verdict": payment_result.get("verdict", "reconciled"),
                "captured_total_brl": payment_result.get("captured_total_brl"),
                "refunded_total_brl": payment_result.get("refunded_total_brl"),
                "refundable_total_brl": payment_result.get("refundable_total_brl"),
            },
            "root_cause_analysis": {
                "ranked_causes": conflict_result.get("ranked_causes", []),
                "responsible_parties": conflict_result.get("responsible_parties", []),
            },
            "evidence_refs": list(dict.fromkeys(accumulated_evidence_refs)),
            "data_conflicts": conflict_result.get("data_conflicts", []),
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": payment_result.get("recommended_refund_brl", 0.0),
                "refund_lines": payment_result.get("refund_lines", []),
            },
            "resolution_actions": conflict_result.get("resolution_actions", []),
        }

        # 5. Invoke Verifier to check invariants, consistency and emit verification_completed
        finalized_output = self.verifier.verify_and_align(case_id, intermediate_output)
        return finalized_output
