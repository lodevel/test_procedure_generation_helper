"""Studio's rules are the open project's: the folder the user picked for it (the footer's Rules
link), else its ACTIVE bundle's rules, resolved as Bench resolves them
(``bundle_registry.active_rules_dir``), else none, never what the previous project had. A
project has no root ``rules/``, no ``config/rules/``, and a ``rules/`` beside it is not its
rules: none of them is ever read."""

import json

from procedo.studio.core.project_manager import ProjectManager, RulesState


def _project(tmp_path, name, *rule_dirs, bundle=True):
    """A project ``tmp_path/name``; *rule_dirs* (relative to it) each hold one rule doc;
    *bundle* gives it a loadable ``bundle/`` (manifest + defaults)."""
    root = tmp_path / name
    (root / "tests").mkdir(parents=True)
    if bundle:
        (root / "bundle").mkdir()
        (root / "bundle" / "bundle.json").write_text(
            json.dumps({"id": "baseline", "version": "2.10.0"}), encoding="utf-8")
        (root / "bundle" / "defaults.json").write_text("{}", encoding="utf-8")
    for rel in rule_dirs:
        (root / rel).mkdir(parents=True)
        (root / rel / "rule.md").write_text(f"rule of {name} in {rel}", encoding="utf-8")
    return root


def _open(pm, root):
    """What every project open does: set the root, then detect its rules."""
    assert pm.set_project_root(root)
    return pm.detect_rules_root()


def test_opening_a_project_reads_its_active_bundles_rules(tmp_path):
    root = _project(tmp_path, "a", "bundle/rules")
    pm = ProjectManager()

    assert _open(pm, root) is True
    assert pm.rules_root == root / "bundle" / "rules"
    assert pm.rules_state is RulesState.LOADED
    assert "rule of a in bundle/rules" in pm.get_rules_content()


def test_no_bundle_means_no_rules_and_old_locations_are_never_read(tmp_path):
    root = _project(tmp_path, "a", "rules", "config/rules", "../rules", bundle=False)
    pm = ProjectManager()

    assert _open(pm, root) is False
    assert pm.rules_root is None  # the footer shows "Rules: none"
    assert pm.get_rules_content() is None
    for rel in ("rules", "config/rules", "../rules"):
        assert (root / rel / "rule.md").read_text(encoding="utf-8") == f"rule of a in {rel}"


def test_a_bundle_folder_bench_would_not_load_gives_no_rules(tmp_path):
    root = _project(tmp_path, "a", "bundle/rules", bundle=False)  # rules/ but no manifest
    pm = ProjectManager()

    assert _open(pm, root) is False
    assert pm.rules_root is None


def test_opening_another_project_switches_to_its_rules(tmp_path):
    a = _project(tmp_path, "a", "bundle/rules")
    b = _project(tmp_path, "b", "bundle/rules")
    pm = ProjectManager()
    _open(pm, a)

    assert _open(pm, b) is True
    assert pm.rules_root == b / "bundle" / "rules"
    assert "rule of b" in pm.get_rules_content() and "rule of a" not in pm.get_rules_content()


def test_a_project_without_a_bundle_does_not_keep_the_previous_projects_rules(tmp_path):
    a = _project(tmp_path, "a", "bundle/rules")
    bare = _project(tmp_path, "bare", bundle=False)
    pm = ProjectManager()
    _open(pm, a)

    assert _open(pm, bare) is False
    assert pm.rules_root is None
    assert pm.rules_state is RulesState.NONE
    assert pm.get_rules_content() is None and pm.get_rules_files() == []


def test_a_picked_folder_holds_for_its_project_only(tmp_path):
    a = _project(tmp_path, "a", "bundle/rules", "picked")
    b = _project(tmp_path, "b", "bundle/rules")
    pm = ProjectManager()
    _open(pm, a)

    assert pm.set_rules_root(a / "picked")
    assert pm.detect_rules_root() is True  # opening a test detects again
    assert pm.rules_root == a / "picked"
    assert "rule of a in picked" in pm.get_rules_content()

    _open(pm, b)
    assert pm.rules_root == b / "bundle" / "rules"
    _open(pm, a)
    assert pm.rules_root == a / "bundle" / "rules"


def test_a_folder_without_rule_docs_is_refused_and_changes_nothing(tmp_path):
    a = _project(tmp_path, "a", "bundle/rules")
    (a / "empty").mkdir()
    pm = ProjectManager()
    _open(pm, a)

    assert pm.set_rules_root(a / "empty") is False
    assert pm.detect_rules_root() is True
    assert pm.rules_root == a / "bundle" / "rules"


def test_a_bundle_rules_folder_without_rule_docs_gives_no_rules(tmp_path):
    root = _project(tmp_path, "a")
    (root / "bundle" / "rules").mkdir()
    pm = ProjectManager()

    assert _open(pm, root) is False
    assert pm.rules_root is None
