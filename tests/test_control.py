from __future__ import annotations

import pytest

from avito import control


class Module:
    @staticmethod
    def start(watcher):
        return "handle"


def test_control_returns_started_handles(monkeypatch):
    monkeypatch.setattr(control.importlib, "import_module", lambda *args: Module)
    assert control.start(["web"], object()) == ["handle"]


def test_control_startup_failure_is_fatal(monkeypatch):
    def broken(*args):
        raise OSError("port is busy")

    monkeypatch.setattr(control.importlib, "import_module", broken)
    with pytest.raises(RuntimeError, match="port is busy"):
        control.start(["web"], object())
