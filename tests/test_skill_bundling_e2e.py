"""End-to-end: a pack that ships authoring_skills/ gets bundled, then the skill
is discovered in a project via the BUNDLED tier.

Crosses the two repos: project_services.bundle_generator (build-side copy) +
workflow_editor.authoring (consumer-side discovery). Pure tmp_path — no real
build, no Qt.
"""
import json

from project_services.bundle_generator import (
    _dirs_with,
    bundle_skills_for_registry,
    copy_pack_skills,
    copy_skill_dirs,
    discover_bundleable_skills,
    discover_bundleable_wizards,
    enabled_pack_roots,
)
from workflow_editor.authoring import SkillSource, load_skills

_SKILL_MD = """\
---
kind: authoring
name: Fake Bundled Skill
target: dcdc
version: 0.1.0
---
Author a rough-draft test procedure for the fake rail.
"""


def _pack_with_skill(pack_root, skill_id="fake_skill"):
    d = pack_root / "authoring_skills" / skill_id
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    return pack_root


def test_pack_skill_is_bundled_then_discovered_in_project(tmp_path):
    # 1. a fake pack shipping a skill
    pack = _pack_with_skill(tmp_path / "fake_pack")
    # 2. a fake project with a bundle dir
    project = tmp_path / "proj"
    (project / "bundle").mkdir(parents=True)

    # 3. THE BUNDLING PATH: copy the pack's skills into the project bundle
    copied = copy_pack_skills([pack], project / "bundle")
    assert [p.name for p in copied] == ["fake_skill"]
    assert (project / "bundle" / "authoring_skills" / "fake_skill" / "SKILL.md").is_file()

    # 4. the consumer discovers it in the project via the BUNDLED tier
    skills = load_skills(project_root=project)
    bundled = [s for s in skills if s.source is SkillSource.BUNDLED]
    match = [s for s in bundled if s.skill_id == "fake_skill"]
    assert match, f"fake_skill not discovered as bundled; got {[s.skill_id for s in skills]}"
    assert match[0].title == "Fake Bundled Skill"
    assert match[0].target == "dcdc"


def test_bundle_skills_for_registry_is_the_build_seam(tmp_path):
    # The build seam the dialog calls: registry → enabled packs → copy skills.
    _pack_with_skill(tmp_path / "fake_pack")
    reg = tmp_path / "drivers_registry.json"
    reg.write_text(json.dumps({"packs": [
        {"id": "p1", "enabled": True, "wheel": {"project_root": "fake_pack"}},
    ]}), encoding="utf-8")
    out = tmp_path / "bundle" / "myid" / "1.0.0"
    copied = bundle_skills_for_registry(reg, out)
    assert [p.name for p in copied] == ["fake_skill"]
    assert (out / "authoring_skills" / "fake_skill" / "SKILL.md").is_file()


def test_copy_is_noop_for_pack_without_skills(tmp_path):
    (tmp_path / "plain_pack").mkdir()
    assert copy_pack_skills([tmp_path / "plain_pack"], tmp_path / "out") == []
    assert not (tmp_path / "out" / "authoring_skills").exists()


def test_later_pack_wins_on_name_clash(tmp_path):
    a = _pack_with_skill(tmp_path / "packA")
    b = tmp_path / "packB" / "authoring_skills" / "fake_skill"
    b.mkdir(parents=True)
    (b / "SKILL.md").write_text("---\nname: From B\n---\nbody", encoding="utf-8")
    out = tmp_path / "bundle"
    copy_pack_skills([tmp_path / "packA", tmp_path / "packB"], out)
    text = (out / "authoring_skills" / "fake_skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "From B" in text  # later pack overwrote


def _local_skill(local_base, skill_id, name="L"):
    d = local_base / skill_id
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody", encoding="utf-8")
    return d


def _registry_for(tmp_path, project_root_rel="fake_pack"):
    reg = tmp_path / "drivers_registry.json"
    reg.write_text(json.dumps({"packs": [
        {"id": "p1", "enabled": True, "wheel": {"project_root": project_root_rel}},
    ]}), encoding="utf-8")
    return reg


def test_discover_bundleable_skills_local_and_pack(tmp_path):
    _pack_with_skill(tmp_path / "fake_pack", "from_pack")
    reg = _registry_for(tmp_path)
    local = tmp_path / "local_pkgs" / "authoring_skills"
    _local_skill(local, "from_local")
    by_id = {s["id"]: s for s in discover_bundleable_skills(reg, local_base=local)}
    assert by_id["from_pack"]["source"] == "pack:fake_pack"
    assert by_id["from_local"]["source"] == "local"


def test_discover_bundleable_includes_builtin_library(tmp_path):
    reg = _registry_for(tmp_path)
    builtin = tmp_path / "builtin"
    _local_skill(builtin, "lib_skill")
    found = {
        s["id"]: s for s in discover_bundleable_skills(
            reg, local_base=tmp_path / "empty_local", builtin_base=builtin
        )
    }
    assert found["lib_skill"]["source"] == "builtin"


def test_discover_local_overrides_pack_on_name_clash(tmp_path):
    _pack_with_skill(tmp_path / "fake_pack", "dup")
    reg = _registry_for(tmp_path)
    local = tmp_path / "local_pkgs" / "authoring_skills"
    _local_skill(local, "dup")
    by_id = {s["id"]: s for s in discover_bundleable_skills(reg, local_base=local)}
    assert by_id["dup"]["source"] == "local"   # standalone overrides pack-embedded


def test_copy_skill_dirs_copies_selected_only(tmp_path):
    a = _local_skill(tmp_path / "src", "a")
    _local_skill(tmp_path / "src", "b")   # exists but not selected
    out = tmp_path / "bundle"
    copied = copy_skill_dirs([a], out)
    assert [p.name for p in copied] == ["a"]
    assert (out / "authoring_skills" / "a" / "SKILL.md").is_file()
    assert not (out / "authoring_skills" / "b").exists()


def test_enabled_pack_roots_resolves_only_enabled(tmp_path):
    (tmp_path / "fake_pack").mkdir()
    reg = tmp_path / "drivers_registry.json"
    reg.write_text(json.dumps({"packs": [
        {"id": "p1", "enabled": True, "wheel": {"project_root": "fake_pack"}},
        {"id": "p2", "enabled": False, "wheel": {"project_root": "fake_pack"}},
        {"id": "p3", "enabled": True, "rules": {"source": {"path": "fake_pack"}}},
        {"id": "p4", "enabled": True, "wheel": {"project_root": "missing_dir"}},
    ]}), encoding="utf-8")
    roots = enabled_pack_roots(reg)
    # p1 + p3 resolve to the existing dir; p2 disabled; p4 missing dir dropped.
    assert all(r.name == "fake_pack" for r in roots)
    assert len(roots) == 2


def test_copy_skill_dirs_honors_dest_subdir(tmp_path):
    # The one copy serves skills, wizards AND tools via dest_subdir (P3).
    wz = _local_skill(tmp_path / "src", "wz1")
    out = tmp_path / "bundle"
    copy_skill_dirs([wz], out, dest_subdir="authoring_wizards")
    assert (out / "authoring_wizards" / "wz1" / "SKILL.md").is_file()
    tl = tmp_path / "src" / "tl1"
    tl.mkdir()
    (tl / "tools.json").write_text('{"server":"s","tools":["t"]}', encoding="utf-8")
    copy_skill_dirs([tl], out, dest_subdir="tools")
    assert (out / "tools" / "tl1" / "tools.json").is_file()
    # default subdir is still authoring_skills
    copy_skill_dirs([wz], out)
    assert (out / "authoring_skills" / "wz1" / "SKILL.md").is_file()


def test_dirs_with_marker_selects_by_file(tmp_path):
    # builtin_wizard_dirs / builtin_tool_dirs are _dirs_with over the right marker.
    (tmp_path / "a").mkdir(); (tmp_path / "a" / "SKILL.md").write_text("x")
    (tmp_path / "b").mkdir()  # no marker -> excluded
    (tmp_path / "c").mkdir(); (tmp_path / "c" / "tools.json").write_text("{}")
    assert [d.name for d in _dirs_with(tmp_path, ("SKILL.md", "skill.md"))] == ["a"]
    assert [d.name for d in _dirs_with(tmp_path, ("tools.json",))] == ["c"]
    assert _dirs_with(tmp_path / "missing", ("SKILL.md",)) == []


def test_discover_bundleable_wizards_builtin_pack_local(tmp_path):
    # wizards are selectable like skills: builtin lib + pack-embedded + local drop-in
    builtin = tmp_path / "builtin_wizards"
    _local_skill(builtin, "lib_wiz")
    pack = tmp_path / "fake_pack"
    pw = pack / "authoring_wizards" / "pack_wiz"
    pw.mkdir(parents=True)
    (pw / "SKILL.md").write_text("---\nname: P\n---\nb", encoding="utf-8")
    reg = _registry_for(tmp_path)
    local = tmp_path / "local_pkgs" / "wizards"
    _local_skill(local, "local_wiz")
    by_id = {w["id"]: w for w in discover_bundleable_wizards(
        reg, local_base=local, builtin_base=builtin)}
    assert by_id["lib_wiz"]["source"] == "builtin"
    assert by_id["pack_wiz"]["source"] == "pack:fake_pack"
    assert by_id["local_wiz"]["source"] == "local"
