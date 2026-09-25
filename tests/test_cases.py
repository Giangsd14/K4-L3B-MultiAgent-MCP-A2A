import json
from pathlib import Path

import pytest

from student_agent.cases import load_case_set


def test_missing_official_case_set_is_reported_explicitly(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="case-set.json: required JSON file is missing"):
        load_case_set(tmp_path)


def test_loads_bundle_extracted_inside_inputs_folder(tmp_path: Path) -> None:
    bundle_root = tmp_path / "inputs"
    case_root = bundle_root / "inputs"
    case_root.mkdir(parents=True)
    (bundle_root / "case-set.json").write_text(
        json.dumps(
            {
                "case_set_version": "l3b-competition-v1",
                "variant_id": "l3b",
                "case_ids": ["L3B_CASE_001"],
            }
        ),
        encoding="utf-8",
    )
    (case_root / "L3B_CASE_001.json").write_text(
        json.dumps({"case_id": "L3B_CASE_001"}), encoding="utf-8"
    )

    case_set = load_case_set(tmp_path, expected_count=1)

    assert case_set.version == "l3b-competition-v1"
    assert case_set.case_ids == ("L3B_CASE_001",)
