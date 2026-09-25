from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from .cases import CaseSet, load_case_set
from .config import Settings
from .contracts import Contracts
from .evidence_journal import RecordingGateway, ReplayGateway
from .mcp_gateway import connect_gateway
from .ports import EvidenceClient
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _execute_cases(
    case_set: CaseSet,
    contracts: Contracts,
    gateway: EvidenceClient,
    output_root: Path,
    trace_path: Path,
    concurrency: int,
) -> None:
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="day09-stage-", dir=output_root.parent) as stage_name:
        stage = Path(stage_name)
        stage_outputs = stage / "outputs"
        stage_outputs.mkdir()
        stage_trace = stage / "trace.jsonl"
        trace = TraceWriter(stage_trace, contracts)
        semaphore = asyncio.Semaphore(concurrency)
        total = len(case_set.case_ids)
        completed = 0
        progress_lock = asyncio.Lock()

        async def process_case(case_id: str) -> None:
            nonlocal completed
            async with semaphore:
                case = case_set.cases[case_id]
                trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                output = await solve_case(case, gateway, trace)
                if not output["evidence_refs"]:
                    raise RuntimeError(f"{case_id}: MCP returned no usable evidence; run aborted")
                contracts.validate_output(output, f"outputs/{case_id}.json")
                if output.get("case_id") != case_id:
                    raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                (stage_outputs / f"{case_id}.json").write_text(
                    json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
                async with progress_lock:
                    completed += 1
                    print(f"[{completed:3d}/{total}] Finished {case_id}")

        await asyncio.gather(*(process_case(case_id) for case_id in case_set.case_ids))
        output_root.mkdir(parents=True, exist_ok=True)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        for stale in output_root.glob("*.json"):
            stale.unlink()
        for staged in stage_outputs.glob("*.json"):
            staged.replace(output_root / staged.name)
        stage_trace.replace(trace_path)


async def _run(root: Path, concurrency: int = 5, record_evidence: Path | None = None) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    if record_evidence is not None:
        reserved = [root / name for name in ("outputs", "traces", "inputs", "dist")]
        if record_evidence.resolve() == root.resolve() or any(
            record_evidence.resolve().is_relative_to(path.resolve()) for path in reserved
        ):
            raise ValueError("evidence journal must be outside submission and input directories")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
        client: EvidenceClient = (
            RecordingGateway(gateway, record_evidence) if record_evidence else gateway
        )
        await _execute_cases(
            case_set,
            contracts,
            client,
            root / "outputs",
            root / "traces" / "trace.jsonl",
            concurrency,
        )


async def _replay(root: Path, evidence_dir: Path, output_root: Path, concurrency: int) -> None:
    reserved = [root / name for name in ("outputs", "traces", "inputs", "dist")]
    if output_root.resolve() == root.resolve() or any(
        output_root.resolve().is_relative_to(path.resolve()) for path in reserved
    ):
        raise ValueError("replay output must be separate from submission artifacts")
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    gateway = ReplayGateway(evidence_dir, contracts)
    await _execute_cases(
        case_set,
        contracts,
        gateway,
        output_root / "outputs",
        output_root / "trace.jsonl",
        concurrency,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run_cmd = commands.add_parser("run", help="run the implemented workflow for all cases")
    run_cmd.add_argument(
        "--concurrency",
        "-c",
        type=int,
        default=5,
        help="number of concurrent cases (default: 5)",
    )
    run_cmd.add_argument(
        "--record-evidence",
        type=Path,
        help="write case-scoped MCP envelopes to a new directory for offline replay",
    )
    replay_cmd = commands.add_parser("replay", help="run offline from a recorded evidence journal")
    replay_cmd.add_argument("--evidence-dir", type=Path, required=True)
    replay_cmd.add_argument("--output-root", type=Path, default=Path("replay_runs/latest"))
    replay_cmd.add_argument("--concurrency", "-c", type=int, default=5)
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            record_evidence = (
                (root / args.record_evidence).resolve() if args.record_evidence else None
            )
            asyncio.run(_run(root, concurrency=args.concurrency, record_evidence=record_evidence))
        elif args.command == "replay":
            asyncio.run(
                _replay(root, root / args.evidence_dir, root / args.output_root, args.concurrency)
            )
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
