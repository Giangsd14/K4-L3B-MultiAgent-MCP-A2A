from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.cases import CaseSet
from student_agent.cli import _execute_cases, parser
from student_agent.contracts import Contracts
from student_agent.evidence_journal import RecordingGateway, ReplayGateway
from student_agent.ports import UnrecordedEvidenceCall


class StubGateway:
    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        if tool_name == "get_refund_timeline":
            raise RuntimeError("MCP tool get_refund_timeline failed: no refund records")
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_" + "a" * 24,
            "result_hash": "sha256:" + "b" * 64,
            "domain": "order",
            "data": {"order_id": arguments["order_id"], "case_id": case_id},
        }


def contracts() -> Contracts:
    return Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")


def test_record_and_replay_preserve_envelope_and_tool_error(tmp_path: Path) -> None:
    journal = tmp_path / "evidence_snapshots" / "run-1"
    recording = RecordingGateway(StubGateway(), journal)

    async def record() -> dict[str, Any]:
        envelope = await recording.call("get_order", case_id="L3B_CASE_001", order_id="order-1")
        with pytest.raises(RuntimeError, match="no refund records"):
            await recording.call("get_refund_timeline", case_id="L3B_CASE_001", order_id="order-1")
        return envelope

    original = asyncio.run(record())
    replay = ReplayGateway(journal, contracts())

    async def repeat() -> dict[str, Any]:
        envelope = await replay.call("get_order", case_id="L3B_CASE_001", order_id="order-1")
        with pytest.raises(RuntimeError, match="no refund records"):
            await replay.call("get_refund_timeline", case_id="L3B_CASE_001", order_id="order-1")
        with pytest.raises(UnrecordedEvidenceCall):
            await replay.call("get_order", case_id="L3B_CASE_002", order_id="order-1")
        return envelope

    assert asyncio.run(repeat()) == original
    lines = (journal / "L3B_CASE_001.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["response"] == original
    with pytest.raises(ValueError, match="not empty"):
        RecordingGateway(StubGateway(), journal)


def test_replay_rejects_corrupt_envelope(tmp_path: Path) -> None:
    journal = tmp_path / "evidence"
    journal.mkdir()
    record = {
        "schema_version": "case-evidence-journal-v1",
        "case_id": "L3B_CASE_001",
        "tool_name": "get_order",
        "arguments": {"order_id": "order-1"},
        "response": {"evidence_ref": "fake"},
    }
    (journal / "L3B_CASE_001.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        ReplayGateway(journal, contracts())


def test_cli_exposes_record_and_replay() -> None:
    record_args = parser().parse_args(["run", "--record-evidence", "evidence_snapshots/run-1"])
    replay_args = parser().parse_args(["replay", "--evidence-dir", "evidence_snapshots/run-1"])
    assert record_args.record_evidence == Path("evidence_snapshots/run-1")
    assert replay_args.output_root == Path("replay_runs/latest")


def test_failed_mcp_run_keeps_previous_artifacts(tmp_path: Path) -> None:
    class FailingGateway:
        async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
            raise RuntimeError(f"MCP tool {tool_name} failed: Error executing tool")

    case_id = "L3B_CASE_001"
    case_set = CaseSet(
        version="test",
        variant_id="l3b",
        case_ids=(case_id,),
        cases={
            case_id: {
                "case_id": case_id,
                "policy_version": "EC_POLICY_V2",
                "candidate_order_ids": ["order-1"],
                "customer_request": {
                    "claimed_order_id": "order-1",
                    "claims": [{"claim_id": "claim-1", "topic": "unsupported_claim"}],
                },
            }
        },
    )
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    prior_output = output_root / f"{case_id}.json"
    prior_output.write_text("previous output", encoding="utf-8")
    trace_path = tmp_path / "traces" / "trace.jsonl"
    trace_path.parent.mkdir()
    trace_path.write_text("previous trace", encoding="utf-8")

    with pytest.raises(RuntimeError, match="no usable evidence"):
        asyncio.run(
            _execute_cases(case_set, contracts(), FailingGateway(), output_root, trace_path, 1)
        )

    assert prior_output.read_text(encoding="utf-8") == "previous output"
    assert trace_path.read_text(encoding="utf-8") == "previous trace"
