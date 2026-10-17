"""
web.py
======
Appens egen nettside (åpnes fra sidepanelet i Home Assistant via "Ingress")
og helsesjekken til Supervisor sin watchdog. Bare standardbiblioteket.

Kommandoer og verktøy er bare tilgjengelige via Home Assistant sin
innlogging (Ingress kommer alltid fra Supervisor, 172.30.32.2). /health er
åpen så watchdog-en kan nå den.
"""

import collections
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("xcomfort.web")

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.path.join(HERE, "web", "index.html")

ALLOWED_CLIENTS = {"172.30.32.2", "127.0.0.1", "::1"}


class Ring(logging.Handler):
    """Holder de siste N linjene i minnet med et løpenummer, så nettsiden
    kan hente bare det som er nytt."""

    def __init__(self, size=600):
        super().__init__()
        self.lines = collections.deque(maxlen=size)
        self.counter = 0
        self._ring_lock = threading.Lock()

    def emit(self, record):
        try:
            self.add(self.format(record), record.levelname)
        except Exception:  # noqa: BLE001
            pass

    def add(self, text, level="INFO"):
        with self._ring_lock:
            self.counter += 1
            self.lines.append({"n": self.counter, "t": text, "l": level})

    def since(self, n):
        with self._ring_lock:
            return [line for line in self.lines if line["n"] > n]


LOG_RING = Ring(800)
RAW_RING = Ring(800)


def make_handler(api):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _client_ok(self):
            return self.client_address[0] in ALLOWED_CLIENTS or os.environ.get("XCOMFORT_WEB_OPEN") == "1"

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _path(self):
            path = self.path.split("?", 1)[0]
            query = {}
            if "?" in self.path:
                for part in self.path.split("?", 1)[1].split("&"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        query[k] = v
            return path.rstrip("/") or "/", query

        def do_GET(self):  # noqa: N802
            path, query = self._path()
            if path == "/health":
                problems = api.health()
                return self._send(200 if not problems else 503, {"ok": not problems, "problemer": problems})
            if not self._client_ok():
                return self._send(403, {"feil": "bare via Home Assistant"})
            try:
                if path in ("/", "/index.html"):
                    with open(INDEX_FILE, "rb") as f:
                        return self._send(200, f.read(), "text/html")
                if path == "/api/status":
                    return self._send(200, api.status())
                if path == "/api/log":
                    return self._send(200, LOG_RING.since(int(query.get("after", 0))))
                if path == "/api/raw":
                    return self._send(200, RAW_RING.since(int(query.get("after", 0))))
                if path == "/api/devices":
                    return self._send(200, api.devices())
                if path == "/api/scan/all":
                    return self._send(200, api.scan_all())
                if path == "/api/scan":
                    return self._send(200, api.scan_info())
                if path == "/api/cache":
                    return self._send(200, api.cache())
            except Exception as e:  # noqa: BLE001
                log.exception("Feil i nettsiden")
                return self._send(500, {"feil": str(e)})
            return self._send(404, {"feil": "finnes ikke"})

        def do_POST(self):  # noqa: N802
            if not self._client_ok():
                return self._send(403, {"feil": "bare via Home Assistant"})
            path, _ = self._path()
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if path == "/api/cmd":
                    return self._send(200, api.command(body))
                if path == "/api/scan/start":
                    return self._send(200, api.scan_start())
                if path == "/api/extra":
                    return self._send(200, api.add_extra(body))
            except Exception as e:  # noqa: BLE001
                log.exception("Feil i nettsiden")
                return self._send(500, {"feil": str(e)})
            return self._send(404, {"feil": "finnes ikke"})

    return Handler


def start(api, port):
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(api))
    except OSError as e:
        log.warning("Klarte ikke starte nettsiden på port %d: %s", port, e)
        return None
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    log.info("Nettside og helsesjekk kjører på port %d", port)
    return server


def ts():
    return time.strftime("%H:%M:%S")
