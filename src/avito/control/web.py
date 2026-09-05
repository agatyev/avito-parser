from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, request, send_file
from waitress import create_server

from .. import url as urls
from ..models import Sub

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("page.html")
LOCAL = {"127.0.0.1", "::1", "localhost"}
MAX_NAME = 120
MAX_URL = 8192
SUB_ID = re.compile(r"^[0-9a-f]{8}$")


def _local(value: str) -> bool:
    return (urlparse(value).hostname or "").lower() in LOCAL


def state(watcher) -> dict:
    subs = watcher.db.subs()
    live = [s for s in subs if s.enabled]
    queue = watcher.db.delivery_status()
    queue["unavailable_targets"] = sorted(
        watcher.db.pending_targets() - set(watcher.notifier.target_keys()))
    return {
        "status": {
            **watcher.state,
            "next_at": min([_next_at(watcher, s.id) for s in live] or [0.0]),
        },
        "notify": {"enabled": watcher.notifier.enabled, "targets": watcher.notifier.schemes,
                   "emails": watcher.notifier.recipients()},
        "queue": queue,
        "searches": [{
            "id": s.id, "name": s.name, "url": s.url, "enabled": s.enabled,
            "last_at": s.last_at, "next_at": _next_at(watcher, s.id), "errors": s.errors,
        } for s in subs],
    }


def _next_at(watcher, sub_id: str) -> float:
    job = watcher.sched.get_job(watcher.job_id(sub_id))
    return job.next_run_time.timestamp() if job and job.next_run_time else 0.0


def create_app(watcher) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

    @app.after_request
    def secure(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.before_request
    def guard():
        if not _local(f"//{request.headers.get('Host', '')}"):
            abort(403, "страница доступна только с этой машины")
        origin = request.headers.get("Origin")
        if request.method == "POST" and origin and not _local(origin):
            abort(403, "запрос пришёл с чужой страницы")
        if request.method == "POST" and not request.is_json:
            abort(415, "нужен JSON-запрос")

    @app.errorhandler(ValueError)
    def bad_request(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(Exception)
    def failed(exc):
        code = getattr(exc, "code", 500)
        message = getattr(exc, "description", None) or str(exc)
        if code >= 500:
            log.exception("Ошибка в %s", request.path)
        return jsonify(error=message), code

    @app.get("/")
    def page():
        return send_file(PAGE)

    @app.get("/api/state")
    def api_state():
        return jsonify(state(watcher))

    @app.post("/api/save")
    def api_save():
        data = request.get_json(silent=True) or {}
        link = (data.get("url") or "").strip()
        if len(link) > MAX_URL:
            raise ValueError("ссылка слишком длинная")
        urls.check_search(link)
        sub_id = str(data.get("id") or "")
        if sub_id and (not SUB_ID.fullmatch(sub_id) or watcher.db.sub(sub_id) is None):
            raise ValueError("такой подписки нет")
        name = " ".join(((data.get("name") or "").strip() or urls.name(link)).split())
        if len(name) > MAX_NAME:
            raise ValueError(f"название длиннее {MAX_NAME} символов")
        sub = watcher.save_sub(Sub(
            id=sub_id,
            name=name,
            url=link,
            enabled=bool(data.get("enabled", True)),
            period_minutes=watcher.cfg.watch.period_minutes))
        if data.get("run_now"):
            watcher.trigger(sub.id)
        log.info("Подписка сохранена: %s", sub.name)
        return jsonify(ok=True)

    @app.post("/api/delete")
    def api_delete():
        sub_id = str((request.get_json(silent=True) or {}).get("id") or "")
        watcher.delete_sub(sub_id)
        log.info("Подписка удалена вместе с находками")
        return jsonify(ok=True)

    @app.post("/api/run")
    def api_run():
        watcher.trigger()
        return jsonify(ok=True)

    @app.post("/api/recipients")
    def api_recipients():
        data = request.get_json(silent=True) or {}
        action = data.get("action")
        addr = (data.get("email") or "").strip().lower()
        emails = list(watcher.notifier.recipients())
        if action == "add":
            emails.append(addr)
        elif action == "remove":
            if addr not in emails:
                raise ValueError("такого адреса нет в рассылке")
            if len(emails) == 1:
                raise ValueError("нужен хотя бы один адрес — иначе письма некуда слать")
            emails.remove(addr)
        else:
            raise ValueError("неизвестное действие")
        saved = watcher.notifier.set_recipients(emails)
        log.info("Рассылка: %s", ", ".join(saved))
        return jsonify(emails=saved)

    return app


def _serve(server) -> None:
    server.run()


def start(watcher) -> threading.Thread:
    cfg = watcher.cfg.web
    server = create_server(create_app(watcher), host=cfg.host, port=cfg.port, threads=4)
    thread = threading.Thread(target=_serve, args=(server,),
                              daemon=True, name="web")
    thread.start()
    log.info("Страница управления: http://127.0.0.1:%d", cfg.port)
    return thread
