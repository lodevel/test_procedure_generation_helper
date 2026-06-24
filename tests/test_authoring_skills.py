"""Tests for the authoring-skill core (model / loader / discovery).

Pure-Python: uses tmp_path only — no Qt, no project_services, no bundle.
"""
import pytest

from workflow_editor.authoring import (
    Skill,
    SkillLoadError,
    SkillSource,
    discover_skills,
    load_skill,
    split_frontmatter,
)
from workflow_editor.authoring.skill_loader import find_skill_file

# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #

_FRONTMATTER = """\
---
kind: authoring
name: DCDC bring-up test
target: dcdc
version: 0.1.0
when-to-use: The board has DC-DC regulators.
---
You author a rough draft test procedure for one DC-DC regulator.
"""


def _make_skill(folder, body=_FRONTMATTER, filename="SKILL.md", tools=False):
    """Create a skill folder under ``folder`` and return its path."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_text(body, encoding="utf-8")
    if tools:
        (folder / "tools.py").write_text("def t():\n    return 1\n", encoding="utf-8")
    return folder


# --------------------------------------------------------------------------- #
# split_frontmatter                                                            #
# --------------------------------------------------------------------------- #

def test_split_frontmatter_parses_yaml_and_body():
    meta, body = split_frontmatter(_FRONTMATTER)
    assert meta["kind"] == "authoring"
    assert meta["name"] == "DCDC bring-up test"
    assert meta["when-to-use"].startswith("The board")
    assert body.startswith("You author")


def test_split_frontmatter_no_frontmatter_is_all_body():
    meta, body = split_frontmatter("just prose, no fence")
    assert meta == {}
    assert body == "just prose, no fence"


def test_split_frontmatter_unterminated_fence_is_all_body():
    text = "---\nkind: authoring\nno closing fence here"
    meta, body = split_frontmatter(text)
    assert meta == {}
    assert body == text


def test_split_frontmatter_malformed_yaml_raises():
    with pytest.raises(SkillLoadError):
        split_frontmatter("---\nkind: [unclosed\n---\nbody")


def test_split_frontmatter_non_mapping_raises():
    with pytest.raises(SkillLoadError):
        split_frontmatter("---\n- just\n- a\n- list\n---\nbody")


# --------------------------------------------------------------------------- #
# find_skill_file / load_skill                                                 #
# --------------------------------------------------------------------------- #

def test_find_skill_file_accepts_both_casings(tmp_path):
    # Both casings must resolve to a real file. The returned name's casing is
    # filesystem-dependent (case-insensitive on Windows), so assert the file is
    # found and lower-cases to "skill.md" rather than an exact casing.
    upper = _make_skill(tmp_path / "a", filename="SKILL.md")
    lower = _make_skill(tmp_path / "b", filename="skill.md")
    for folder in (upper, lower):
        found = find_skill_file(folder)
        assert found is not None and found.is_file()
        assert found.name.lower() == "skill.md"
    assert find_skill_file(tmp_path / "missing") is None


def test_load_skill_populates_fields(tmp_path):
    folder = _make_skill(tmp_path / "dcdc_bringup")
    skill = load_skill(folder, SkillSource.USER)
    assert skill.skill_id == "dcdc_bringup"          # identity = folder name
    assert skill.title == "DCDC bring-up test"        # display = frontmatter name
    assert skill.target == "dcdc"
    assert skill.version == "0.1.0"
    assert skill.kind == "authoring"
    assert skill.source is SkillSource.USER
    assert skill.system_prompt.startswith("You author")
    assert skill.has_tools is False


def test_load_skill_title_falls_back_to_folder_name(tmp_path):
    body = "---\nkind: authoring\n---\nbody text here"
    folder = _make_skill(tmp_path / "no_name", body=body)
    skill = load_skill(folder, SkillSource.USER)
    assert skill.title == "no_name"


def test_load_skill_detects_tools(tmp_path):
    folder = _make_skill(tmp_path / "withtools", tools=True)
    skill = load_skill(folder, SkillSource.BUNDLED)
    assert skill.has_tools is True
    assert skill.tools_path.name == "tools.py"


def test_load_skill_missing_file_raises(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SkillLoadError):
        load_skill(empty, SkillSource.USER)


def test_load_skill_empty_body_raises(tmp_path):
    folder = _make_skill(tmp_path / "blank", body="---\nkind: authoring\n---\n   \n")
    with pytest.raises(SkillLoadError):
        load_skill(folder, SkillSource.USER)


# --------------------------------------------------------------------------- #
# discover_skills                                                              #
# --------------------------------------------------------------------------- #

def test_discover_finds_and_sorts_by_title(tmp_path):
    root = tmp_path / "user"
    _make_skill(root / "zebra", body="---\nname: Zebra skill\n---\nbody")
    _make_skill(root / "alpha", body="---\nname: Alpha skill\n---\nbody")
    skills = discover_skills([(root, SkillSource.USER)])
    assert [s.title for s in skills] == ["Alpha skill", "Zebra skill"]


def test_discover_precedence_higher_source_wins(tmp_path):
    bundled = tmp_path / "bundle"
    project = tmp_path / "project"
    _make_skill(bundled / "dcdc", body="---\nname: Bundled\n---\nbundled body")
    _make_skill(project / "dcdc", body="---\nname: Project\n---\nproject body")
    skills = discover_skills(
        [(bundled, SkillSource.BUNDLED), (project, SkillSource.PROJECT)]
    )
    assert len(skills) == 1
    assert skills[0].source is SkillSource.PROJECT
    assert skills[0].title == "Project"


def test_discover_precedence_independent_of_root_order(tmp_path):
    bundled = tmp_path / "bundle"
    project = tmp_path / "project"
    _make_skill(bundled / "dcdc", body="---\nname: Bundled\n---\nb")
    _make_skill(project / "dcdc", body="---\nname: Project\n---\np")
    # project listed FIRST — bundled must still not override it.
    skills = discover_skills(
        [(project, SkillSource.PROJECT), (bundled, SkillSource.BUNDLED)]
    )
    assert skills[0].source is SkillSource.PROJECT


def test_discover_skips_broken_folder_without_raising(tmp_path):
    root = tmp_path / "user"
    _make_skill(root / "good", body="---\nname: Good\n---\nbody")
    # malformed frontmatter — must be skipped, not fatal.
    _make_skill(root / "bad", body="---\nname: [unclosed\n---\nbody")
    skills = discover_skills([(root, SkillSource.USER)])
    assert [s.title for s in skills] == ["Good"]


def test_discover_ignores_non_skill_folders(tmp_path):
    root = tmp_path / "user"
    _make_skill(root / "real", body="---\nname: Real\n---\nbody")
    (root / "not_a_skill").mkdir()
    (root / "not_a_skill" / "readme.txt").write_text("nope", encoding="utf-8")
    skills = discover_skills([(root, SkillSource.USER)])
    assert [s.title for s in skills] == ["Real"]


def test_discover_empty_when_no_roots():
    assert discover_skills([]) == []


def test_discover_logs_same_source_duplicate(tmp_path, caplog):
    root = tmp_path / "user"
    _make_skill(root / "dup", body="---\nname: First\n---\nbody")
    # A second folder cannot share a name on one FS, so simulate a same-source
    # id collision across two roots both tagged USER.
    other = tmp_path / "user2"
    _make_skill(other / "dup", body="---\nname: Second\n---\nbody")
    with caplog.at_level("WARNING"):
        skills = discover_skills([(root, SkillSource.USER), (other, SkillSource.USER)])
    assert len(skills) == 1                      # first seen kept
    assert skills[0].title == "First"
    assert any("duplicate skill_id" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# encoding / line-ending edge cases (load-bearing, easy to silently regress)   #
# --------------------------------------------------------------------------- #

def test_load_skill_handles_utf8_bom(tmp_path):
    folder = tmp_path / "bom"
    folder.mkdir()
    # Write a UTF-8 BOM before the opening fence — utf-8-sig must strip it so
    # the first line is still "---".
    (folder / "SKILL.md").write_bytes(
        b"\xef\xbb\xbf---\nname: BOM skill\n---\nbody text"
    )
    skill = load_skill(folder, SkillSource.USER)
    assert skill.title == "BOM skill"
    assert skill.system_prompt == "body text"


def test_load_skill_handles_crlf(tmp_path):
    folder = tmp_path / "crlf"
    folder.mkdir()
    (folder / "SKILL.md").write_bytes(
        b"---\r\nname: CRLF skill\r\ntarget: dcdc\r\n---\r\nline one\r\nline two\r\n"
    )
    skill = load_skill(folder, SkillSource.USER)
    assert skill.title == "CRLF skill"
    assert skill.target == "dcdc"
    assert "line one" in skill.system_prompt and "\r" not in skill.system_prompt


def test_load_skill_empty_frontmatter_block(tmp_path):
    # "---\n\n---\n" → yaml.safe_load returns None → guarded to {}.
    folder = _make_skill(tmp_path / "emptyfm", body="---\n\n---\nplain body")
    skill = load_skill(folder, SkillSource.USER)
    assert skill.title == "emptyfm"          # falls back to folder name
    assert skill.system_prompt == "plain body"
