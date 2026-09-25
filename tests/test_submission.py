from __future__ import annotations

import json
import zipfile
from pathlib import Path
from shutil import copytree

import pytest

from student_agent.cases import CaseSet
from student_agent.contracts import Contracts
from student_agent.submission import package_submission, validate_artifacts
from student_agent.trace import TraceWriter


def _output(case_id: str, refs: list[str] | None = None) -> dict:
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": 0.2,
        },
        "affected_entities": {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "entity_resolution": {
            "status": "not_found",
            "resolved_order_ids": [],
            "rejected_candidates": [],
            "confidence": 0.0,
        },
        "customer_context": {"customer_unique_id": None, "related_order_ids": []},
        "shipment_analysis": {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        },
        "payment_analysis": {
            "verdict": "insufficient_evidence",
            "captured_total_brl": None,
            "refunded_total_brl": None,
            "refundable_total_brl": None,
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": refs or [],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": ["investigate_missing_evidence"],
    }


def _write_fixture(tmp_path: Path, *, events: tuple[str, ...], refs: list[str] | None = None):
    repo = Path(__file__).resolve().parents[1]
    contracts = Contracts(repo / "contracts" / "schemas")
    case_id = "CASE_001"
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    (output_dir / f"{case_id}.json").write_text(
        json.dumps(_output(case_id, refs)), encoding="utf-8"
    )
    trace = TraceWriter(tmp_path / "traces" / "trace.jsonl", contracts)
    for event_type in events:
        if event_type == "tool_result_consumed":
            trace.emit(
                case_id=case_id,
                event_type=event_type,
                actor="order-agent",
                tool_name="get_order",
                evidence_refs=["ev_" + "b" * 20],
            )
        else:
            trace.emit(case_id=case_id, event_type=event_type, actor="coordinator")
    return CaseSet("test-v1", "l3b", (case_id,), {}), contracts


LIFECYCLE = (
    "case_received",
    "task_assigned",
    "tool_result_consumed",
    "handoff",
    "policy_decided",
    "verification_completed",
    "case_finalized",
)


def test_artifact_validator_accepts_complete_trace(tmp_path: Path) -> None:
    case_set, contracts = _write_fixture(tmp_path, events=LIFECYCLE)
    outputs, trace_lines = validate_artifacts(tmp_path, case_set, contracts)
    assert len(outputs) == 1
    assert len(trace_lines) == len(LIFECYCLE)


def test_artifact_validator_rejects_missing_handoff(tmp_path: Path) -> None:
    events = tuple(event for event in LIFECYCLE if event != "handoff")
    case_set, contracts = _write_fixture(tmp_path, events=events)
    with pytest.raises(ValueError, match="missing handoff"):
        validate_artifacts(tmp_path, case_set, contracts)


def test_artifact_validator_rejects_missing_consumed_event(tmp_path: Path) -> None:
    events = tuple(event for event in LIFECYCLE if event != "tool_result_consumed")
    case_set, contracts = _write_fixture(tmp_path, events=events)
    with pytest.raises(ValueError, match="missing tool_result_consumed"):
        validate_artifacts(tmp_path, case_set, contracts)


def test_artifact_validator_rejects_unconsumed_output_ref(tmp_path: Path) -> None:
    ref = "ev_" + "a" * 20
    case_set, contracts = _write_fixture(tmp_path, events=LIFECYCLE, refs=[ref])
    with pytest.raises(ValueError, match="without a consumed tool result"):
        validate_artifacts(tmp_path, case_set, contracts)


def test_artifact_validator_rejects_openrouter_key(tmp_path: Path) -> None:
    case_set, contracts = _write_fixture(tmp_path, events=LIFECYCLE)
    path = tmp_path / "outputs" / "CASE_001.json"
    output = json.loads(path.read_text(encoding="utf-8"))
    output["resolution_actions"].append("sk-or-v1-abcdefghijklmnop")
    path.write_text(json.dumps(output), encoding="utf-8")
    with pytest.raises(ValueError, match="API key"):
        validate_artifacts(tmp_path, case_set, contracts)


def test_package_has_only_root_manifest_trace_and_case_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_set, _contracts = _write_fixture(tmp_path, events=LIFECYCLE)
    copytree(
        Path(__file__).resolve().parents[1] / "contracts" / "schemas",
        tmp_path / "contracts" / "schemas",
    )
    monkeypatch.setattr("student_agent.cases.load_case_set", lambda _root: case_set)
    destination = package_submission(tmp_path, tmp_path / "dist" / "submission.zip")
    with zipfile.ZipFile(destination) as archive:
        assert set(archive.namelist()) == {
            "manifest.json",
            "trace.jsonl",
            "outputs/CASE_001.json",
        }
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["variant_id"] == "l3b"
        assert manifest["output_schema_version"] == "day09-l3b-output-v2"
