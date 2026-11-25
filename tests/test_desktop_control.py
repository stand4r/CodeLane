"""Живое управление компьютером: сценарии, подтверждения и снимки экрана.

Run: python tests/test_desktop_control.py
"""
import json
import os
import sys
import zlib
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import desktop_control
from core import system_monitor
from services.agent_tools import LocalToolbox, TOOL_DEFINITIONS, ToolError


def _names():
    return {item["function"]["name"] for item in TOOL_DEFINITIONS}


def _toolbox(tmp_path, confirm=None, access_mode="full"):
    return LocalToolbox(str(tmp_path), confirm=confirm, access_mode=access_mode)


def test_tools_are_registered():
    names = _names()
    assert "computer_control" in names
    assert "process_monitor" in names
    schema = next(item["function"] for item in TOOL_DEFINITIONS
                  if item["function"]["name"] == "computer_control")
    assert schema["parameters"]["required"] == ["actions"]
    assert schema["parameters"]["properties"]["actions"]["type"] == "array"


def test_control_needs_full_access(tmp_path):
    box = _toolbox(tmp_path, access_mode="read_only")
    assert box._allowed("computer_control") is False
    with pytest.raises(ToolError):
        box.computer_control([{"type": "move", "x": 10, "y": 10}])


def test_control_needs_confirmation(tmp_path):
    box = _toolbox(tmp_path, confirm=None)
    with pytest.raises(ToolError):
        box.computer_control([{"type": "move", "x": 10, "y": 10}])


def test_control_rejected_by_user(tmp_path):
    calls = []
    box = _toolbox(tmp_path, confirm=lambda *args: calls.append(args) or False)
    with pytest.raises(ToolError):
        box.computer_control([{"type": "click", "x": 10, "y": 10}])
    assert calls and calls[0][0] == "computer_control"


def test_unknown_action_is_rejected(tmp_path):
    box = _toolbox(tmp_path, confirm=lambda *args: True)
    with pytest.raises(ToolError):
        box._normalize_control_steps([{"type": "format_disk"}])
    with pytest.raises(ToolError):
        box._normalize_control_steps([{"type": "click", "x": 5}])
    with pytest.raises(ToolError):
        box._normalize_control_steps([{"type": "drag", "x": 1, "y": 1}])
    with pytest.raises(ToolError):
        box._normalize_control_steps([])


def test_steps_are_normalized_and_described(tmp_path):
    box = _toolbox(tmp_path)
    steps = box._normalize_control_steps([
        {"type": "click", "x": 100, "y": 200, "button": "RIGHT", "count": 2},
        {"type": "Type", "text": "привет"},
        {"action": "key", "keys": "ctrl+s"},
    ])
    assert [item["type"] for item in steps] == ["click", "type", "key"]
    assert steps[0]["button"] == "right"
    assert steps[2]["keys"] == ["ctrl+s"]
    description = box._describe_control_steps(steps)
    assert "клик" in description and "ввод текста" in description


def test_control_runs_with_mocked_desktop(tmp_path, monkeypatch):
    executed = []

    monkeypatch.setattr(desktop_control, "IS_WINDOWS", True)
    monkeypatch.setattr(desktop_control, "mouse_move",
                        lambda x, y: executed.append(("move", x, y)) or {"moved_to": [x, y]})
    monkeypatch.setattr(desktop_control, "mouse_click",
                        lambda button="left", count=1, x=None, y=None: executed.append(
                            ("click", button, count)) or {"click": button})
    monkeypatch.setattr(desktop_control, "type_text",
                        lambda text, interval=0.0: executed.append(("type", text)) or {"ok": True})
    monkeypatch.setattr(desktop_control, "screen_rect",
                        lambda: {"left": 0, "top": 0, "width": 1920, "height": 1080, "monitors": 1})
    monkeypatch.setattr(desktop_control, "cursor_position", lambda: (0, 0))
    monkeypatch.setattr(desktop_control, "capture_png",
                        lambda max_width=None, region=None, grayscale=False: {
                            "png": b"\x89PNG\r\n\x1a\nfake", "width": 800, "height": 600,
                            "scale": 0.5})

    approvals = []
    box = _toolbox(tmp_path, confirm=lambda name, title, details: approvals.append(details) or True)
    payload = box.computer_control([
        {"type": "move", "x": 10, "y": 20},
        {"type": "click", "x": 10, "y": 20},
        {"type": "type", "text": "hello"},
    ], max_width=800, interval_sec=0)
    assert executed == [("move", 10, 20), ("click", "left", 1), ("type", "hello")]
    assert payload["media_type"] == "image/png"
    summary = json.loads(payload["description"])
    assert [step["ok"] for step in summary["steps"]] == [True, True, True]
    assert "observation" in summary
    assert len(approvals) == 1, "серия действий подтверждается один раз"

    # Второй вызов в пределах выданного разрешения не спрашивает снова.
    box.computer_control([{"type": "move", "x": 11, "y": 21}], interval_sec=0)
    assert len(approvals) == 1


def test_control_requires_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_control, "IS_WINDOWS", False)
    box = _toolbox(tmp_path, confirm=lambda *args: True)
    with pytest.raises(ToolError):
        box.computer_control([{"type": "move", "x": 1, "y": 1}])


def test_control_reports_desktop_errors(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise desktop_control.DesktopControlError("X=99999 вне экрана")

    monkeypatch.setattr(desktop_control, "IS_WINDOWS", True)
    monkeypatch.setattr(desktop_control, "mouse_move", boom)
    monkeypatch.setattr(desktop_control, "screen_rect",
                        lambda: {"left": 0, "top": 0, "width": 1920, "height": 1080, "monitors": 1})
    monkeypatch.setattr(desktop_control, "cursor_position", lambda: (0, 0))
    monkeypatch.setattr(desktop_control, "capture_png",
                        lambda **kwargs: {"png": b"x", "width": 10, "height": 10, "scale": 1.0})
    box = _toolbox(tmp_path, confirm=lambda *args: True)
    payload = box.computer_control([{"type": "move", "x": 99999, "y": 1}], interval_sec=0)
    summary = json.loads(payload["description"])
    assert summary["steps"][0]["ok"] is False
    assert "вне экрана" in summary["steps"][0]["error"]


def test_png_encoder_produces_valid_image():
    rows = [bytes([255, 0, 0] * 4), bytes([0, 255, 0] * 4)]
    png = desktop_control.encode_png(4, 2, rows)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (4, 2)
    start = png.index(b"IDAT")
    length = struct.unpack(">I", png[start - 4:start])[0]
    raw = zlib.decompress(png[start + 4:start + 4 + length])
    assert raw == b"\x00" + rows[0] + b"\x00" + rows[1]

    QtGui = pytest.importorskip("PyQt6.QtGui")
    image = QtGui.QImage.fromData(png, "PNG")
    assert not image.isNull()
    assert (image.width(), image.height()) == (4, 2)


@pytest.mark.skipif(os.name != "nt", reason="GDI-снимок доступен только в Windows")
def test_live_capture_limits_width():
    frame = desktop_control.capture_png(max_width=320)
    assert frame["width"] <= 320
    assert frame["png"].startswith(b"\x89PNG\r\n\x1a\n")
    assert frame["source"]["width"] >= frame["width"]


def test_process_monitor_snapshot(tmp_path):
    box = _toolbox(tmp_path, access_mode="read_only")
    assert box._allowed("process_monitor") is True
    data = json.loads(box.process_monitor("snapshot", top=3))
    assert "cpu_percent" in data and "memory" in data
    assert data["process_count"] > 0
    assert len(data["top_processes"]) <= 3


def test_process_monitor_find_and_watch(tmp_path):
    box = _toolbox(tmp_path, access_mode="full")
    found = json.loads(box.process_monitor("find", pattern=os.path.basename(sys.executable), top=5))
    assert found["count"] >= 1
    assert all("pid" in row for row in found["processes"])

    watched = json.loads(box.process_monitor("watch", duration_sec=0.6, interval_sec=0.2, top=2))
    assert watched["samples"], "нужен хотя бы один кадр наблюдения"
    assert "started" in watched and "stopped" in watched

    with pytest.raises(ToolError):
        box.process_monitor("explode")


def test_screenshot_saves_to_project(tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr(desktop_control, "IS_WINDOWS", True)
    monkeypatch.setattr(desktop_control, "save_screenshot",
                        lambda path=None, region=None, max_width=None:
                        saved.update(path=path, region=region, max_width=max_width)
                        or {"path": path, "width": 800, "height": 600})
    box = _toolbox(tmp_path, confirm=lambda *args: True)
    result = json.loads(box.screenshot(max_width=640))
    assert result["path"].startswith(str(tmp_path / "screenshoots"))
    assert saved["max_width"] == 640


def test_desktop_stream_combines_screen_and_processes(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_control, "IS_WINDOWS", True)
    monkeypatch.setattr(desktop_control, "watch_screen",
                        lambda **kwargs: {
                            "duration_sec": 0.2, "count": 1,
                            "frames": [{"index": 0, "width": 800, "height": 600}],
                            "last_png": b"\x89PNG\r\n\x1a\nfake",
                        })
    monkeypatch.setattr(system_monitor, "watch_processes",
                        lambda **kwargs: {"duration_sec": 0.2, "samples": 1,
                                          "started": [], "stopped": []})
    box = _toolbox(tmp_path, confirm=lambda *args: True)
    payload = box.desktop_stream(duration_sec=0.2, interval_sec=0.1,
                                 save_screenshots=False)
    assert payload["media_type"] == "image/png"
    summary = json.loads(payload["description"])
    assert summary["screen"]["count"] == 1
    assert summary["processes"]["samples"] == 1


def test_screenshot_and_desktop_stream_dispatch_via_run(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop_control, "IS_WINDOWS", True)
    monkeypatch.setattr(desktop_control, "save_screenshot",
                        lambda **kwargs: {"path": str(tmp_path / "run.png"),
                                          "width": 1, "height": 1})
    monkeypatch.setattr(desktop_control, "watch_screen",
                        lambda **kwargs: {"count": 1, "frames": [],
                                          "last_png": b"fake-png"})
    monkeypatch.setattr(system_monitor, "watch_processes",
                        lambda **kwargs: {"samples": 1, "started": [], "stopped": []})
    box = _toolbox(tmp_path, confirm=lambda *args: True)
    assert json.loads(box.run("screenshot", "{}"))["path"]
    payload = box.run("desktop_stream", json.dumps({
        "duration_sec": 0.2, "interval_sec": 0.1, "save_screenshots": False}))
    assert payload["media_type"] == "image/png"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
