"""Studio's error and warning boxes have the Copy button too: Studio installs the same host filter
(``procedo.ui.error_copy``) at start, and its tabs' ``show_error`` / ``show_warning`` boxes get
the button, copy their whole text and stay open until the user answers them."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from procedo.ui import error_copy
from procedo.studio.tabs.base_tab import BaseTab

from .test_main_window_characterization import qapp as qapp

#: These tests show REAL static boxes and close them themselves (procedo.core.testing).
pytestmark = pytest.mark.real_modals


@pytest.fixture
def hook(qapp):
    error_copy._FILTER = None
    assert error_copy.install_error_copy(qapp) is True
    yield
    qapp.removeEventFilter(error_copy._FILTER)
    error_copy._FILTER = None


@pytest.mark.parametrize("show", ["show_error", "show_warning"])
def test_a_studio_error_box_copies_its_text_and_stays_open(qapp, hook, show):
    tab = BaseTab(MagicMock())
    QApplication.clipboard().clear()
    seen = {}

    def poke():
        box = next(
            w for w in qapp.topLevelWidgets() if isinstance(w, QMessageBox) and w.isVisible()
        )
        copy = box.findChild(QPushButton, error_copy.COPY_BUTTON_NAME)
        if copy is not None:
            copy.click()
            seen["open_after_copy"] = box.isVisible()
            seen["clipboard"] = QApplication.clipboard().text()
        box.button(QMessageBox.StandardButton.Ok).click()

    QTimer.singleShot(0, poke)
    getattr(tab, show)("Quick Parse failed", "Line 3: unknown verb 'Mesure'")

    assert seen == {
        "open_after_copy": True,
        "clipboard": "Quick Parse failed\n\nLine 3: unknown verb 'Mesure'",
    }
    tab.deleteLater()
