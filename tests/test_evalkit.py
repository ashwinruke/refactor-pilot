import json
from pathlib import Path

import pytest

from refactor_pilot.evalkit import manifest, score

FIXTURE = Path(__file__).parent.parent / "fixtures" / "legacy_inventory"


def write_manifest(tmp_path, items):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"items": items}))
    return path


def test_pattern_in_comment_does_not_count(tmp_path):
    (tmp_path / "a.cpp").write_text("// was NULL once\nint* p = nullptr; /* NULL */\n")
    item = manifest.Item("x", "R1", "a.cpp", r"\bNULL\b")
    assert manifest.is_resolved(item, tmp_path)
    (tmp_path / "a.cpp").write_text("int* p = NULL;\n")
    assert not manifest.is_resolved(item, tmp_path)


def test_deleted_file_counts_as_resolved(tmp_path):
    assert manifest.is_resolved(manifest.Item("x", "R6", "gone.cpp", "anything"), tmp_path)


def test_duplicate_ids_rejected(tmp_path):
    raw = {"id": "x", "recipe": "R1", "file": "a.cpp", "legacy_pattern": "a"}
    with pytest.raises(ValueError):
        manifest.load(write_manifest(tmp_path, [raw, raw]))


def test_untouched_fixture_scores_zero():
    """Every planted construct must be detected in the fixture as shipped;
    otherwise a pattern is wrong and the benchmark would hand out free points."""
    result = score.score(FIXTURE, FIXTURE / "manifest.json")
    assert result.resolved == []
    assert result.total >= 30
    assert set(result.by_recipe) == {"R1", "R2", "R3", "R4", "R5", "R6"}
