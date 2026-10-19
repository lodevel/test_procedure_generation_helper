"""Studio's rules root never resolves to ``<project>/rules`` (owner 2026-10-19):
a project has no root rules/ — its rules come from its bundle — and an old one
on disk is left alone, not read."""

from procedo.studio.core.project_manager import ProjectManager


def _project(tmp_path, *rule_dirs):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    for rel in rule_dirs:
        (root / rel).mkdir(parents=True)
        (root / rel / "rule.md").write_text("rule", encoding="utf-8")
    return root


def test_a_root_rules_folder_is_not_a_rules_root(tmp_path):
    root = _project(tmp_path, "rules")
    pm = ProjectManager()
    assert pm.set_project_root(root)

    assert pm.detect_rules_root() is False
    assert pm.rules_root is None
    assert (root / "rules" / "rule.md").read_text(encoding="utf-8") == "rule"


def test_config_rules_still_wins_over_a_root_rules_folder(tmp_path):
    root = _project(tmp_path, "rules", "config/rules")
    pm = ProjectManager()
    assert pm.set_project_root(root)

    assert pm.detect_rules_root() is True
    assert pm.rules_root == root / "config" / "rules"
