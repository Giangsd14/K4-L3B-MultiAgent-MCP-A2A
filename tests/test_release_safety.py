from pathlib import Path


def test_repository_contains_no_competition_payload() -> None:
    root = Path(__file__).resolve().parents[1]
    assert not (root / "case-set.json").exists()
    # The competition bundle may provide its manifest under inputs/ while the
    # case payloads live one level deeper at inputs/inputs/.
    permitted_manifest = root / "inputs" / "case-set.json"
    assert all(path == permitted_manifest for path in (root / "inputs").glob("*.json"))
    # Outputs are runtime artifacts and are ignored by git; local smoke runs
    # may leave them behind while this safety test checks release sources.
    forbidden = {"oracles", "reference-outputs", "private-partitions.json", "mcp-access.json"}
    assert not any(path.name in forbidden for path in root.rglob("*"))


def test_example_environment_has_no_real_key() -> None:
    root = Path(__file__).resolve().parents[1]
    content = (root / ".env.example").read_text(encoding="utf-8")
    assert "sk-team-replace_me" in content
    assert content.count("sk-team-") == 1
