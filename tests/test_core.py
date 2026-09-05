from __future__ import annotations

import io
import json
import os
import sys
import textwrap
import threading
import time
import types

import pytest

from avito import core
from avito.config import Browser
from avito.core import identity


@pytest.fixture
def child(monkeypatch):
    def run(code: str):
        monkeypatch.setattr(core, "CHILD", [sys.executable, "-c", textwrap.dedent(code)])
    return run


def cfg(tmp_path, **kwargs) -> Browser:
    return Browser.model_construct(profile_dir=tmp_path / "session", **kwargs)


def test_fetch_returns_what_the_child_printed(tmp_path, child):
    child("import sys; assert not sys.argv[2:]; "
          "sys.stdout.write('<html>' + sys.argv[1] + '</html>')")
    assert core.fetch(cfg(tmp_path), "ссылка") == "<html>ссылка</html>"


def test_detailed_fetch_sends_known_ids_over_stdin_and_reads_metadata(tmp_path, child):
    child(f"""
        import json, sys
        request = json.load(sys.stdin)
        assert sys.argv[1] == "ссылка с пробелом"
        assert sys.argv[2:] == [{core.PROTOCOL_FLAG!r}]
        assert not any("known" in arg for arg in sys.argv)
        meta = {{"pages": 3, "stop_reason": "known_page", "limit_reached": False}}
        sys.stdout.write({core.PROTOCOL_MAGIC!r} + json.dumps(meta) + "\\n")
        sys.stdout.write("<html>" + ",".join(request["known_ids"]) + "</html>")
    """)

    result = core.fetch(cfg(tmp_path), "ссылка с пробелом",
                        known_ids={"known/2", "known 1"}, detailed=True)

    assert result == core.FetchResult("<html>known 1,known/2</html>", 3, "known_page")


def test_antibot_exit_code_becomes_blocked(tmp_path, child):
    child("import sys; print('антибот Avito: HTTP 403', file=sys.stderr); sys.exit(3)")
    with pytest.raises(core.Blocked, match="HTTP 403"):
        core.fetch(cfg(tmp_path), "ссылка")


def test_child_crash_keeps_the_last_line_of_the_log(tmp_path, child):
    child("import sys; print('шум'); print('сеть отвалилась', file=sys.stderr); sys.exit(1)")
    with pytest.raises(RuntimeError, match="сеть отвалилась"):
        core.fetch(cfg(tmp_path), "ссылка")


def test_step_timeout_stays_retryable_for_the_watcher(tmp_path, child):
    child("import sys; print('поиск: клик: браузер не ответил за 60 с', file=sys.stderr);"
          f"sys.exit({core.TIMEOUT})")
    with pytest.raises(TimeoutError, match="поиск: клик"):
        core.fetch(cfg(tmp_path), "ссылка")


def test_hang_kills_the_child_together_with_the_browser(tmp_path, child):
    mark = tmp_path / "browser.pid"
    child(f"""
        import subprocess, time
        kid = subprocess.Popen(["sleep", "60"])
        open({str(mark)!r}, "w").write(str(kid.pid))
        time.sleep(60)
    """)
    with pytest.raises(TimeoutError):
        core.fetch(cfg(tmp_path, deadline_seconds=2), "ссылка")

    for _ in range(100):
        try:
            os.kill(int(mark.read_text()), 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail("процесс из группы пережил SIGKILL")


def test_shutdown_cancels_an_active_browser_child(tmp_path, child):
    mark = tmp_path / "started"
    child(f"""
        import time
        open({str(mark)!r}, "w").write("yes")
        time.sleep(60)
    """)
    stop = threading.Event()
    outcome = []

    def run():
        try:
            core.fetch(cfg(tmp_path), "ссылка", cancel_event=stop)
        except Exception as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    for _ in range(100):
        if mark.exists():
            break
        time.sleep(0.01)
    stop.set()
    core.cancel_all()
    worker.join(2)

    assert not worker.is_alive()
    assert isinstance(outcome[0], core.Cancelled)


def test_identity_reassembles_the_config_in_order():
    env = {"CAMOU_CONFIG_2": '"agent"}', "CAMOU_CONFIG_1": '{"navigator.userAgent": ',
           "DISPLAY": ":99"}
    assert identity.decode(env) == {"navigator.userAgent": "agent"}


def test_identity_is_written_once_and_read_back(tmp_path, monkeypatch):
    pkgman = types.ModuleType("camoufox.pkgman")
    pkgman.installed_verstr = lambda: "147.0"
    monkeypatch.setitem(sys.modules, "camoufox", types.ModuleType("camoufox"))
    monkeypatch.setitem(sys.modules, "camoufox.pkgman", pkgman)
    monkeypatch.setattr(identity, "_make", lambda cfg: {"navigator.userAgent": "агент"})
    settings = cfg(tmp_path)

    assert identity.config(settings)["navigator.userAgent"] == "агент"
    monkeypatch.setattr(identity, "_make", lambda cfg: {"navigator.userAgent": "другой"})
    assert identity.config(settings)["navigator.userAgent"] == "агент"


def test_new_firefox_retires_the_old_identity(tmp_path, monkeypatch):
    settings = cfg(tmp_path)
    identity.home(settings).mkdir(parents=True)
    (identity.profile(settings) / "cache2").mkdir(parents=True)
    (identity.home(settings) / identity.FILE).write_text(
        json.dumps({"firefox": "146.0", "config": {"navigator.userAgent": "старый"}}))

    pkgman = types.ModuleType("camoufox.pkgman")
    pkgman.installed_verstr = lambda: "147.0"
    monkeypatch.setitem(sys.modules, "camoufox", types.ModuleType("camoufox"))
    monkeypatch.setitem(sys.modules, "camoufox.pkgman", pkgman)
    monkeypatch.setattr(identity, "_make", lambda cfg: {"navigator.userAgent": "новый"})

    assert identity.config(settings)["navigator.userAgent"] == "новый"
    assert not identity.profile(settings).exists()


def test_trim_wipes_only_the_cache(tmp_path):
    settings = cfg(tmp_path)
    junk = identity.profile(settings) / "cache2"
    junk.mkdir(parents=True)
    (junk / "big").write_bytes(b"x" * 2048)
    (identity.profile(settings) / "cookies.sqlite").write_bytes(b"cookies")

    identity.trim(settings)

    assert not junk.exists()
    assert (identity.profile(settings) / "cookies.sqlite").exists()


@pytest.mark.parametrize("configured", [".", "data"])
def test_retire_never_removes_the_configured_directory(tmp_path, monkeypatch, configured):
    monkeypatch.chdir(tmp_path)
    settings = Browser(profile_dir=configured)
    home = identity.home(settings)
    identity.profile(settings).mkdir(parents=True)
    (identity.profile(settings) / "cookies.sqlite").write_text("cookies")
    (home / identity.FILE).write_text("{}")
    precious = home / "avito.db"
    precious.write_text("must survive")

    identity.retire(settings)

    assert home.exists()
    assert precious.read_text() == "must survive"
    assert not identity.profile(settings).exists()
    assert not (home / identity.FILE).exists()


def test_broken_identity_card_is_ignored_instead_of_crashing(tmp_path):
    card = tmp_path / "identity.json"
    card.write_text('{"firefox": "152.0', encoding="utf-8")
    assert identity._read(card) == {}

    card.write_text("[]", encoding="utf-8")
    assert identity._read(card) == {}

    assert identity._read(tmp_path / "нет.json") == {}


def test_child_maps_outcomes_to_exit_codes(monkeypatch, capsys):
    from avito.core import __main__ as child

    monkeypatch.setattr(sys, "argv", ["avito.core", "https://www.avito.ru/moskva"])

    monkeypatch.setattr("avito.core.session.grab", lambda cfg, link: "<html>вот</html>")
    assert child.main() == 0
    assert "вот" in capsys.readouterr().out

    def blocked(cfg, link):
        raise core.Blocked("антибот Avito: HTTP 403")

    monkeypatch.setattr("avito.core.session.grab", blocked)
    assert child.main() == core.BLOCKED
    assert "403" in capsys.readouterr().err

    def slow(cfg, link):
        raise TimeoutError("выдача: скролл: браузер не ответил")

    monkeypatch.setattr("avito.core.session.grab", slow)
    assert child.main() == core.TIMEOUT
    assert "скролл" in capsys.readouterr().err


def test_child_protocol_reads_request_only_when_flagged(monkeypatch, capsys):
    from avito.core import __main__ as child

    monkeypatch.setattr(
        sys, "argv", ["avito.core", "https://www.avito.ru/moskva", core.PROTOCOL_FLAG])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"known_ids":["2","1"],"max_pages":7}'))

    def paged(cfg, link, known_ids, max_pages, detailed):
        assert known_ids == {"1", "2"}
        assert max_pages == 7 and detailed is True
        return core.FetchResult("<html>две страницы</html>", 2, "known_page")

    monkeypatch.setattr("avito.core.session.grab", paged)

    assert child.main() == 0
    out = capsys.readouterr().out
    assert out.startswith(core.PROTOCOL_MAGIC)
    assert '"stop_reason":"known_page"' in out
    assert out.endswith("<html>две страницы</html>")
