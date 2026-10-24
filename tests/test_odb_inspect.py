"""Studio's board pictures go through the host's one ODB image client."""

import json

from procedo.core import board_images
from procedo.studio.core import odb_inspect


def _project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    (project / "board.tgz").write_bytes(b"synthetic archive")
    (project / "config" / "config.json").write_text(json.dumps(
        {"image_generator": {"max_workers": 6, "batch_size": 25, "parallel_export": False}}),
        encoding="utf-8")
    cache = tmp_path / "cache"
    monkeypatch.setattr(odb_inspect._project_local_state, "media_cache", lambda _root: cache)
    return project, cache


def test_a_studio_render_asks_the_shared_daemon_with_the_users_settings(tmp_path, monkeypatch):
    project, cache = _project(tmp_path, monkeypatch)
    asked = []

    def render(request, timeout, cancelled):
        asked.append(request)
        for view in ("zoomed", "wide"):
            (cache / view / "images").mkdir(parents=True, exist_ok=True)
            (cache / view / "images" / "U1_pad3.png").write_bytes(b"png")
        return {"ok": True}

    monkeypatch.setattr(board_images._render_daemon, "request", render)

    zoomed, wide = odb_inspect.render_target(project, "U1", "3")

    assert (zoomed, wide) == (cache / "zoomed" / "images" / "U1_pad3.png",
                              cache / "wide" / "images" / "U1_pad3.png")
    request = asked[0]
    assert request["targets"] == ["U1:3"]
    assert (request["max_workers"], request["batch_size"], request["parallel_export"]) == (6, 25, False)


def test_a_pin_is_never_answered_with_its_components_picture(tmp_path, monkeypatch):
    project, cache = _project(tmp_path, monkeypatch)
    images = cache / "zoomed" / "images"
    images.mkdir(parents=True)
    (images / "U1.png").write_bytes(b"png")

    assert odb_inspect.cached_image_paths(project, "U1", "3") == (None, None)
    assert odb_inspect.cached_image_paths(project, "U1", None) == (images / "U1.png", None)
    (images / "U1_3.png").write_bytes(b"png")  # the legacy pin name
    assert odb_inspect.cached_image_paths(project, "U1", "3") == (images / "U1_3.png", None)
