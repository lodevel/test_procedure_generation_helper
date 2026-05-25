"""Per-task section-ownership override plumbing tests.

Covers the wiring added so a task/button's ``TaskConfig.llm_owned_sections``
threads through BOTH the prompt emit-list and reconstruction (before-validate
and at-apply) via the single resolver ``LLMTabMixin._task_section_override``:

  1. ``TaskConfig.from_dict``/``to_dict`` round-trip ``llm_owned_sections``
     (a list, and absent → None).
  2. ``LLMTabMixin._task_section_override`` returns the config's value, and
     None when there's no config / a None task — and is the ONLY TaskConfig
     lookup seam.
  3. ``validate_response(..., task_override=[...])`` reaches the text
     handler's reconstruction (``reconstruct_for_pipeline``) — asserted via a
     spy on the reconstruction module.

The ``_task_section_override`` resolver lives on the Qt-dependent
``LLMTabMixin`` (its module imports the worker, which imports PySide6), so this
file imports the REAL package rather than the ``_qt_stub`` namespace shim — the
stub replaces ``workflow_editor.llm`` with an empty module, which would shadow
the mixin's ``from ..llm import LLMTask``. The method itself is pure logic; we
bind the unbound method to a fake carrying only the attributes it reads.

Runs without conftest fixtures (PySide6 must be importable):
    <venv>/python -m pytest tests/test_task_section_override.py --noconftest -q
"""
from __future__ import annotations

import types
import unittest

from workflow_editor.core.task_config import TaskConfig


# --------------------------------------------------------------------------- #
# 1. TaskConfig field round-trip                                              #
# --------------------------------------------------------------------------- #


class TaskConfigLlmOwnedSectionsRoundTripTests(unittest.TestCase):
    def _base(self, **extra) -> dict:
        d = {
            "id": "derive_json_from_text",
            "name": "Derive JSON from Text",
            "button_label": "Text → JSON",
        }
        d.update(extra)
        return d

    def test_round_trip_list(self) -> None:
        cfg = TaskConfig.from_dict(self._base(llm_owned_sections=["steps", "equipment"]))
        self.assertEqual(cfg.llm_owned_sections, ["steps", "equipment"])
        # to_dict (asdict) carries it back out verbatim.
        self.assertEqual(cfg.to_dict()["llm_owned_sections"], ["steps", "equipment"])
        # Full round-trip is stable.
        again = TaskConfig.from_dict(cfg.to_dict())
        self.assertEqual(again.llm_owned_sections, ["steps", "equipment"])

    def test_round_trip_empty_list_is_authoritative(self) -> None:
        # [] means "LLM authors nothing" — must NOT collapse to None.
        cfg = TaskConfig.from_dict(self._base(llm_owned_sections=[]))
        self.assertEqual(cfg.llm_owned_sections, [])
        self.assertEqual(cfg.to_dict()["llm_owned_sections"], [])

    def test_absent_defaults_to_none(self) -> None:
        # Older configs without the field still load; default is None
        # (use the bundle default — current behavior).
        cfg = TaskConfig.from_dict(self._base())
        self.assertIsNone(cfg.llm_owned_sections)
        self.assertIsNone(cfg.to_dict()["llm_owned_sections"])

    def test_unknown_keys_still_dropped(self) -> None:
        # Tolerant constructor keeps filtering unknown keys.
        cfg = TaskConfig.from_dict(self._base(llm_owned_sections=["steps"], bogus=1))
        self.assertEqual(cfg.llm_owned_sections, ["steps"])
        self.assertNotIn("bogus", cfg.to_dict())


# --------------------------------------------------------------------------- #
# 2. _task_section_override resolver                                          #
# --------------------------------------------------------------------------- #


class _FakeTaskConfigManager:
    """Minimal stand-in for TaskConfigManager.get_task_config."""

    def __init__(self, by_value: dict) -> None:
        self._by_value = by_value
        self.calls: list[tuple] = []

    def get_task_config(self, tab_id, task_value):
        self.calls.append((tab_id, task_value))
        return self._by_value.get(task_value)


class _FakeTask:
    def __init__(self, value: str) -> None:
        self.value = value


class TaskSectionOverrideResolverTests(unittest.TestCase):
    """``_task_section_override`` is pure logic (no Qt widgets), so we bind the
    unbound mixin method to a SimpleNamespace carrying just the two attributes
    it reads (``task_config_manager`` + ``tab_id``)."""

    def setUp(self) -> None:
        # Import here so a Qt-import failure surfaces as an error on THIS
        # test only, not at module collection time.
        from workflow_editor.tabs.llm_tab_mixin import LLMTabMixin
        self._resolve = LLMTabMixin._task_section_override

    def _obj(self, manager) -> types.SimpleNamespace:
        return types.SimpleNamespace(task_config_manager=manager, tab_id="text_json")

    def test_returns_config_value(self) -> None:
        task = _FakeTask("derive_json_from_text")
        mgr = _FakeTaskConfigManager({
            "derive_json_from_text": TaskConfig(
                id="derive_json_from_text", name="n", button_label="b",
                llm_owned_sections=["steps"],
            ),
        })
        self.assertEqual(self._resolve(self._obj(mgr), task), ["steps"])
        self.assertEqual(mgr.calls, [("text_json", "derive_json_from_text")])

    def test_empty_list_is_returned_verbatim(self) -> None:
        task = _FakeTask("t")
        mgr = _FakeTaskConfigManager({
            "t": TaskConfig(id="t", name="n", button_label="b",
                            llm_owned_sections=[]),
        })
        self.assertEqual(self._resolve(self._obj(mgr), task), [])

    def test_none_when_config_has_none(self) -> None:
        task = _FakeTask("t")
        mgr = _FakeTaskConfigManager({
            "t": TaskConfig(id="t", name="n", button_label="b"),
        })
        self.assertIsNone(self._resolve(self._obj(mgr), task))

    def test_none_when_no_config_found(self) -> None:
        task = _FakeTask("missing")
        mgr = _FakeTaskConfigManager({})
        self.assertIsNone(self._resolve(self._obj(mgr), task))

    def test_none_task_returns_none(self) -> None:
        mgr = _FakeTaskConfigManager({})
        self.assertIsNone(self._resolve(self._obj(mgr), None))
        # No lookup should be attempted for a None task.
        self.assertEqual(mgr.calls, [])

    def test_none_manager_returns_none(self) -> None:
        task = _FakeTask("t")
        self.assertIsNone(self._resolve(self._obj(None), task))


# --------------------------------------------------------------------------- #
# 3. validate_response threads task_override into reconstruction              #
# --------------------------------------------------------------------------- #


class ValidateResponseThreadsOverrideTests(unittest.TestCase):
    """The override must reach the text handler's reconstruction call. We spy
    on ``reconstruction.reconstruct_for_pipeline`` (the only consumer) and
    assert the ``task_override`` kwarg lands."""

    def setUp(self) -> None:
        from workflow_editor.llm import validator_dispatch
        from workflow_editor.llm.backend_base import LLMResponse, LLMProposal
        self.vd = validator_dispatch
        self.response = LLMResponse(success=True)
        self.response.procedure_text = LLMProposal(
            mode="replace",
            content="## Equipment\n\n## Steps\n1. Do a thing.\n\n## Expected\n",
        )
        self.current = {"text": "# T\nd\n\n## Meta\nformat_version: 2.0.1\n",
                        "json": "", "code": ""}

    def test_override_reaches_reconstruction(self) -> None:
        captured: dict = {}

        class _Recon:
            success = True
            text = "# T\nd\n\n## Meta\nformat_version: 2.0.1\n## Steps\n1. x\n"
            errors: list = []

        def _spy(proposed_text, prior_text, *, task_override=None, project_root=None):
            captured["task_override"] = task_override
            return _Recon()

        # validate_fn is a no-op clean report; the handler still runs recon first.
        class _CleanReport:
            ok = True
            errors: list = []

        orig_recon = self.vd.reconstruction.reconstruct_for_pipeline
        orig_import = self.vd._import_validator
        # Force the deterministic path "available" + a capturing validate_fn,
        # so we exercise the real handler dispatch without a wheel.
        self.vd.reconstruction.reconstruct_for_pipeline = _spy
        try:
            outcome = self.vd._validate_proposed_text(
                self.response, self.current, None,
                lambda **kw: _CleanReport(),
                task_override=["steps"],
            )
        finally:
            self.vd.reconstruction.reconstruct_for_pipeline = orig_recon
            self.vd._import_validator = orig_import

        self.assertIsNotNone(outcome)
        self.assertEqual(captured.get("task_override"), ["steps"])

    def test_none_override_threads_none(self) -> None:
        captured: dict = {}

        class _Recon:
            success = True
            text = "# T\nd\n\n## Meta\nformat_version: 2.0.1\n## Steps\n1. x\n"
            errors: list = []

        def _spy(proposed_text, prior_text, *, task_override=None, project_root=None):
            captured["task_override"] = task_override
            captured["seen"] = True
            return _Recon()

        class _CleanReport:
            ok = True
            errors: list = []

        orig_recon = self.vd.reconstruction.reconstruct_for_pipeline
        self.vd.reconstruction.reconstruct_for_pipeline = _spy
        try:
            # No task_override → None propagated (bundle-default path).
            self.vd._validate_proposed_text(
                self.response, self.current, None,
                lambda **kw: _CleanReport(),
            )
        finally:
            self.vd.reconstruction.reconstruct_for_pipeline = orig_recon

        self.assertTrue(captured.get("seen"))
        self.assertIsNone(captured.get("task_override"))


if __name__ == "__main__":
    unittest.main()
