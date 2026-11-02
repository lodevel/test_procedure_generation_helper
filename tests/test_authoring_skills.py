"""Tests for the authoring-skill core (model / loader / discovery).

Pure-Python: uses tmp_path only — no Qt, no procedo.core, no bundle.
"""

import pytest

from procedo.studio.authoring import (
    Skill,
    SkillFolder,
    SkillLoadError,
    SkillSource,
    discover_skills,
    load_skill,
    skills_by_id,
    split_frontmatter,
)
from procedo.core.bundle_generator import skill_file

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


def _place(folder, source=SkillSource.PACKAGE, label="Pkg", skill_id=None):
    return SkillFolder(folder, skill_id or folder.name, source, label)


def _root(root, source=SkillSource.PACKAGE, label="Pkg"):
    """Every sub-folder of ``root`` as a skill folder."""
    return [_place(d, source, label) for d in sorted(root.iterdir())]


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
# skill_file / load_skill                                                      #
# --------------------------------------------------------------------------- #


def test_skill_file_accepts_both_casings(tmp_path):
    # Both casings must resolve to a real file. The returned name's casing is
    # filesystem-dependent (case-insensitive on Windows), so assert the file is
    # found and lower-cases to "skill.md" rather than an exact casing.
    upper = _make_skill(tmp_path / "a", filename="SKILL.md")
    lower = _make_skill(tmp_path / "b", filename="skill.md")
    for folder in (upper, lower):
        found = skill_file(folder)
        assert found is not None and found.is_file()
        assert found.name.lower() == "skill.md"
    assert skill_file(tmp_path / "missing") is None


def test_load_skill_populates_fields(tmp_path):
    folder = _make_skill(tmp_path / "dcdc_bringup")
    skill = load_skill(_place(folder))
    assert skill.skill_id == "dcdc_bringup"  # identity = folder name
    assert skill.title == "DCDC bring-up test"  # display = frontmatter name
    assert skill.target == "dcdc"
    assert skill.version == "0.1.0"
    assert skill.kind == "authoring"
    assert skill.source is SkillSource.PACKAGE
    assert skill.provenance == "Pkg"
    assert skill.system_prompt.startswith("You author")
    assert skill.has_tools is False


def test_load_skill_title_falls_back_to_folder_name(tmp_path):
    body = "---\nkind: authoring\n---\nbody text here"
    folder = _make_skill(tmp_path / "no_name", body=body)
    skill = load_skill(_place(folder))
    assert skill.title == "no_name"


def test_load_skill_accepts_description_alias(tmp_path):
    body = "---\nname: D\ndescription: Use for X.\n---\nbody text"
    skill = load_skill(_place(_make_skill(tmp_path / "d", body=body)))
    assert skill.when_to_use == "Use for X."


def test_when_to_use_takes_precedence_over_description(tmp_path):
    body = "---\nname: D\nwhen-to-use: WTU\ndescription: DESC\n---\nbody text"
    skill = load_skill(_place(_make_skill(tmp_path / "d2", body=body)))
    assert skill.when_to_use == "WTU"


def test_load_skill_detects_tools(tmp_path):
    folder = _make_skill(tmp_path / "withtools", tools=True)
    skill = load_skill(_place(folder, SkillSource.BUNDLED))
    assert skill.has_tools is True
    assert skill.tools_path.name == "tools.py"


def test_load_skill_missing_file_raises(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SkillLoadError):
        load_skill(_place(empty))


def test_load_skill_empty_body_raises(tmp_path):
    folder = _make_skill(tmp_path / "blank", body="---\nkind: authoring\n---\n   \n")
    with pytest.raises(SkillLoadError):
        load_skill(_place(folder))


# --------------------------------------------------------------------------- #
# discover_skills                                                              #
# --------------------------------------------------------------------------- #


def test_discover_finds_and_sorts_by_title(tmp_path):
    root = tmp_path / "user"
    _make_skill(root / "zebra", body="---\nname: Zebra skill\n---\nbody")
    _make_skill(root / "alpha", body="---\nname: Alpha skill\n---\nbody")
    skills = discover_skills(_root(root))
    assert [s.title for s in skills] == ["Alpha skill", "Zebra skill"]


def test_discover_keeps_every_copy_with_its_provenance(tmp_path):
    bundled = tmp_path / "bundle"
    project = tmp_path / "project"
    package = tmp_path / "package"
    _make_skill(bundled / "dcdc", body="---\nname: Same\n---\nbundled body")
    _make_skill(project / "dcdc", body="---\nname: Same\n---\nproject body")
    _make_skill(package / "dcdc", body="---\nname: Same\n---\npackage body")
    skills = discover_skills(
        _root(package, SkillSource.PACKAGE, "Acme lab")
        + _root(bundled, SkillSource.BUNDLED, "Bundle")
        + _root(project, SkillSource.PROJECT, "Project")
    )
    # sorted by title, then the higher precedence first
    assert [s.provenance for s in skills] == ["Project", "Bundle", "Acme lab"]
    assert [s.listing for s in skills] == [
        "Same — Project",
        "Same — Bundle",
        "Same — Acme lab",
    ]


def test_skills_by_id_takes_project_then_bundle_then_package(tmp_path):
    roots = {}
    for name, source, label in (
        ("package", SkillSource.PACKAGE, "Acme lab"),
        ("bundle", SkillSource.BUNDLED, "Bundle"),
        ("project", SkillSource.PROJECT, "Project"),
    ):
        _make_skill(tmp_path / name / "dcdc", body=f"---\nname: {label}\n---\nbody")
        roots[name] = _root(tmp_path / name, source, label)
    order = ("package", "bundle", "project")

    def picked(*names):
        folders = [f for n in names for f in roots[n]]
        return skills_by_id(discover_skills(folders))["dcdc"].provenance

    assert picked(*order) == "Project"
    assert picked("project", "bundle", "package") == "Project"  # not the listing order
    assert picked("package", "bundle") == "Bundle"
    assert picked("package") == "Acme lab"


def test_a_versioned_item_keeps_its_id(tmp_path):
    # a package item's folder is <id>/<version>: the id is the item's, not the folder's name.
    folder = _make_skill(tmp_path / "dcdc" / "1.2.0")
    skill = load_skill(SkillFolder(folder, "dcdc", SkillSource.PACKAGE, "Acme lab"))
    assert skill.skill_id == "dcdc" and skill.path == folder


def test_discover_skips_broken_folder_without_raising(tmp_path):
    root = tmp_path / "user"
    _make_skill(root / "good", body="---\nname: Good\n---\nbody")
    # malformed frontmatter — must be skipped, not fatal.
    _make_skill(root / "bad", body="---\nname: [unclosed\n---\nbody")
    skills = discover_skills(_root(root))
    assert [s.title for s in skills] == ["Good"]


def test_discover_ignores_non_skill_folders(tmp_path):
    root = tmp_path / "user"
    _make_skill(root / "real", body="---\nname: Real\n---\nbody")
    (root / "not_a_skill").mkdir()
    (root / "not_a_skill" / "readme.txt").write_text("nope", encoding="utf-8")
    skills = discover_skills(_root(root))
    assert [s.title for s in skills] == ["Real"]


def test_discover_empty_when_no_roots():
    assert discover_skills([]) == []


# --------------------------------------------------------------------------- #
# encoding / line-ending edge cases (load-bearing, easy to silently regress)   #
# --------------------------------------------------------------------------- #


def test_load_skill_handles_utf8_bom(tmp_path):
    folder = tmp_path / "bom"
    folder.mkdir()
    # Write a UTF-8 BOM before the opening fence — utf-8-sig must strip it so
    # the first line is still "---".
    (folder / "SKILL.md").write_bytes(b"\xef\xbb\xbf---\nname: BOM skill\n---\nbody text")
    skill = load_skill(_place(folder))
    assert skill.title == "BOM skill"
    assert skill.system_prompt == "body text"


def test_load_skill_handles_crlf(tmp_path):
    folder = tmp_path / "crlf"
    folder.mkdir()
    (folder / "SKILL.md").write_bytes(
        b"---\r\nname: CRLF skill\r\ntarget: dcdc\r\n---\r\nline one\r\nline two\r\n"
    )
    skill = load_skill(_place(folder))
    assert skill.title == "CRLF skill"
    assert skill.target == "dcdc"
    assert "line one" in skill.system_prompt and "\r" not in skill.system_prompt


def test_load_skill_empty_frontmatter_block(tmp_path):
    # "---\n\n---\n" → yaml.safe_load returns None → guarded to {}.
    folder = _make_skill(tmp_path / "emptyfm", body="---\n\n---\nplain body")
    skill = load_skill(_place(folder))
    assert skill.title == "emptyfm"  # falls back to folder name
    assert skill.system_prompt == "plain body"
