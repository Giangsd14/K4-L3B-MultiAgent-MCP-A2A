from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .contracts import Contracts
from .ports import EvidenceClient, UnrecordedEvidenceCall

JOURNAL_VERSION = "case-evidence-journal-v1"
ERROR_TYPES: dict[str, type[Exception]] = {
    "RuntimeError": RuntimeError,
    "ValueError": ValueError,
    "ConnectionError": ConnectionError,
    "TimeoutError": TimeoutError,
    "OSError": OSError,
}


def _key(tool_name: str, arguments: dict[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
    return tool_name, tuple(sorted(arguments.items()))


class RecordingGateway:
    """Persist case-scoped MCP envelopes for local replay, outside the submission."""

    def __init__(self, delegate: EvidenceClient, directory: Path) -> None:
        self.delegate = delegate
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        if any(self.directory.iterdir()):
            raise ValueError(f"evidence journal directory is not empty: {self.directory}")
        self._lock = threading.Lock()

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema_version": JOURNAL_VERSION,
            "case_id": case_id,
            "tool_name": tool_name,
            "arguments": arguments,
        }
        try:
            response = await self.delegate.call(tool_name, case_id=case_id, **arguments)
        except Exception as exc:
            record["error"] = {"type": type(exc).__name__, "message": str(exc)}
            self._append(case_id, record)
            raise
        record["response"] = response
        self._append(case_id, record)
        return response

    def _append(self, case_id: str, record: dict[str, Any]) -> None:
        if not case_id or "/" in case_id or "\\" in case_id or case_id.startswith("."):
            raise ValueError("invalid case_id for evidence journal")
        path = self.directory / f"{case_id}.jsonl"
        with self._lock, path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


class ReplayGateway:
    """Serve a recorded MCP run without contacting the gateway."""

    def __init__(self, directory: Path, contracts: Contracts) -> None:
        self.directory = directory.resolve()
        self.contracts = contracts
        self._records: dict[tuple[str, str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
        if not self.directory.is_dir():
            raise ValueError(f"evidence journal directory does not exist: {self.directory}")
        for path in sorted(self.directory.glob("*.jsonl")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{number}: invalid JSON") from exc
                if record.get("schema_version") != JOURNAL_VERSION:
                    raise ValueError(f"{path}:{number}: unsupported journal version")
                case_id = record.get("case_id")
                tool_name = record.get("tool_name")
                arguments = record.get("arguments")
                if (
                    case_id != path.stem
                    or not isinstance(tool_name, str)
                    or not isinstance(arguments, dict)
                ):
                    raise ValueError(f"{path}:{number}: invalid case/tool/arguments")
                if not all(isinstance(k, str) and isinstance(v, str) for k, v in arguments.items()):
                    raise ValueError(f"{path}:{number}: arguments must be strings")
                key = (case_id, *_key(tool_name, arguments))
                if key in self._records:
                    raise ValueError(f"{path}:{number}: duplicate recorded call")
                response = record.get("response")
                error = record.get("error")
                if (response is None) == (error is None):
                    raise ValueError(f"{path}:{number}: expected one response or error")
                if response is not None:
                    self.contracts.validate_evidence(response, f"{path}:{number}")
                elif (
                    not isinstance(error, dict)
                    or not isinstance(error.get("type"), str)
                    or not isinstance(error.get("message"), str)
                ):
                    raise ValueError(f"{path}:{number}: invalid error record")
                self._records[key] = record
        if not self._records:
            raise ValueError(f"evidence journal contains no calls: {self.directory}")

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        record = self._records.get((case_id, *_key(tool_name, arguments)))
        if record is None:
            raise UnrecordedEvidenceCall(
                f"{case_id}: no replay record for {tool_name} with {arguments}"
            )
        if "error" in record:
            error = record["error"]
            error_type = ERROR_TYPES.get(error["type"], RuntimeError)
            raise error_type(error["message"])
        return record["response"]
