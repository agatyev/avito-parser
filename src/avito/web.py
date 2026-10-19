from __future__ import annotations

import logging
import threading
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, request, send_file
from waitress import create_server

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("page.html")
LOCAL = {"127.0.0.1", "::1", "localhost"}
MAX_URL = 8192


def _local(value: str) -> bool:
    return (urlparse(value).hostname or "").lower() in LOCAL


def snapshot(service) -> dict:
    searches = service.store.searches()
    return {
        "status": {**service.state, "every_minutes": service.cfg.every_minutes,
                   "waiting": service.store.waiting()},
        "mail": {"ready": service.mailer.ready,
                 "to": [{"email": person.email, "searches": person.searches}
                        for person in service.store.recipients()]},
        "searches": [{"id": s.id, "name": s.name, "url": s.url,
                      "enabled": s.enabled, "last_at": s.last_at} for s in searches],
        "found": [{"title": row["title"], "url": row["url"], "price_text": row["price_text"],
                   "address": row["address"], "date_text": row["date_text"],
                   "search": row["search_name"], "sent": bool(row["sent"]),
                   "first_seen": row["first_seen"]} for row in service.store.recent()],
    }


def create_app(service) -> Flask:
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
        if code >= 500:
            log.exception("Ошибка в %s", request.path)
        return jsonify(error=getattr(exc, "description", None) or str(exc)), code

    @app.get("/")
    def page():
        return send_file(PAGE)

    @app.get("/api/state")
    def api_state():
        return jsonify(snapshot(service))

    @app.post("/api/add")
    def api_add():
        data = request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        if len(url) > MAX_URL:
            raise ValueError("ссылка слишком длинная")
        service.add(data.get("name") or "", url)
        return jsonify(ok=True)

    @app.post("/api/toggle")
    def api_toggle():
        data = request.get_json(silent=True) or {}
        service.store.toggle(str(data.get("id") or ""), bool(data.get("enabled")))
        return jsonify(ok=True)

    @app.post("/api/delete")
    def api_delete():
        service.store.delete(str((request.get_json(silent=True) or {}).get("id") or ""))
        log.info("Поиск удалён вместе с находками")
        return jsonify(ok=True)

    @app.post("/api/run")
    def api_run():
        service.trigger()
        return jsonify(ok=True)

    @app.post("/api/recipients")
    def api_recipients():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip().lower()
        if data.get("action") == "add":
            service.add_recipient(email)
        elif data.get("action") == "remove":
            service.store.remove_recipient(email)
            log.info("Адрес убран из рассылки: %s", email)
        else:
            raise ValueError("неизвестное действие")
        return jsonify(ok=True)

    @app.post("/api/subscribe")
    def api_subscribe():
        data = request.get_json(silent=True) or {}
        service.store.subscribe((data.get("email") or "").strip().lower(),
                                str(data.get("id") or ""), bool(data.get("on")))
        return jsonify(ok=True)

    return app


def start(service) -> threading.Thread:
    server = create_server(create_app(service), host=service.cfg.bind,
                           port=service.cfg.port, threads=4)
    thread = threading.Thread(target=server.run, daemon=True, name="web")
    thread.start()
    log.info("Страница: http://127.0.0.1:%d", service.cfg.port)
    return thread
