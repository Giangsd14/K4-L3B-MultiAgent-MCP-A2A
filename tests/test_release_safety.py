import subprocess
from pathlib import Path


def test_repository_contains_no_competition_payload() -> None:
    root = Path(__file__).resolve().parents[1]
    tracked = {
        Path(name)
        for name in subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
        .decode()
        .split("\0")
        if name
    }
    assert Path("case-set.json") not in tracked
    assert not any(
        path.suffix == ".json" and path.parts[0] in {"inputs", "outputs"} for path in tracked
    )
    forbidden = {"oracles", "reference-outputs", "private-partitions.json", "mcp-access.json"}
    assert not any(path.name in forbidden for path in tracked)


def test_example_environment_has_no_real_key() -> None:
    root = Path(__file__).resolve().parents[1]
    content = (root / ".env.example").read_text(encoding="utf-8")
    assert "sk-team-replace_me" in content
    assert content.count("sk-team-") == 1
