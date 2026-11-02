"""A skill the bundle build copied in is discovered in a project via the BUNDLED
tier, plus the build-side discovery in procedo.core.bundle_generator. The copy
itself is the build's (generate_all --add-folder), tested in the host.
Pure tmp_path — no real build, no Qt.
"""

import json
import shutil

from procedo.core.bundle_generator import enabled_pack_roots, wizard_flow
from procedo.studio.authoring import SkillSource, load_skills

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

    # 3. the bundle build copies the skill folder into the bundle
    shutil.copytree(
        pack / "authoring_skills" / "fake_skill",
        project / "bundle" / "authoring_skills" / "fake_skill",
    )

    # 4. the consumer discovers it in the project via the BUNDLED tier
    skills = load_skills(project_root=project)
    bundled = [s for s in skills if s.source is SkillSource.BUNDLED]
    match = [s for s in bundled if s.skill_id == "fake_skill"]
    assert match, f"fake_skill not discovered as bundled; got {[s.skill_id for s in skills]}"
    assert match[0].title == "Fake Bundled Skill"
    assert match[0].target == "dcdc"


def test_enabled_pack_roots_resolves_only_enabled(tmp_path):
    (tmp_path / "fake_pack").mkdir()
    reg = tmp_path / "drivers_registry.json"
    reg.write_text(
        json.dumps(
            {
                "packs": [
                    {"id": "p1", "enabled": True, "wheel": {"project_root": "fake_pack"}},
                    {"id": "p2", "enabled": False, "wheel": {"project_root": "fake_pack"}},
                    {"id": "p3", "enabled": True, "rules": {"source": {"path": "fake_pack"}}},
                    {"id": "p4", "enabled": True, "wheel": {"project_root": "missing_dir"}},
                ]
            }
        ),
        encoding="utf-8",
    )
    roots = enabled_pack_roots(reg)
    # p1 + p3 resolve to the existing dir; p2 disabled; p4 missing dir dropped.
    assert all(r.name == "fake_pack" for r in roots)
    assert len(roots) == 2


def test_wizard_flow_groups_stages_by_flow(tmp_path):
    # stages sharing flow:dcdc are ONE user-facing wizard; a standalone wizard -> None
    for sid in ("stage_a", "stage_b"):
        d = tmp_path / "wizards" / sid
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {sid}\nflow: dcdc\n---\nbody", encoding="utf-8")
    solo = tmp_path / "wizards" / "solo"
    solo.mkdir(parents=True)
    (solo / "SKILL.md").write_text("---\nname: solo\n---\nbody", encoding="utf-8")
    assert wizard_flow(tmp_path / "wizards" / "stage_a") == "dcdc"
    assert wizard_flow(tmp_path / "wizards" / "stage_b") == "dcdc"
    assert wizard_flow(solo) is None
