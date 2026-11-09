import argparse
import base64
import fnmatch
import json
import os
import secrets
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from storage import TRANSPORT_HEADERS, Store


def match_key(request, include_query):
    url = urlsplit(request["url"])
    return (
        request["method"].upper(), url.scheme.lower(), url.hostname,
        url.port or (443 if url.scheme == "https" else 80),
        url.path or "/", url.query if include_query else "",
    )


class Application:
    def __init__(self, data_dir, ui_port, proxy_port, listen_host):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data_dir / "recordings.sqlite3")
        self.ui_port = ui_port
        self.proxy_port = proxy_port
        self.listen_host = listen_host
        self.token = os.environ.get("SSE_PROXY_TOKEN") or secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.settings = {
            "mode": "off", "selected": None, "speed": 1,
            "host_filter": "*", "path_filter": "*", "include_query": False,
        }
        self.tickets = {}
        self.replay_hits = 0
        self.last_error = ""
        self.server = None

    def snapshot(self):
        with self.lock:
            settings = dict(self.settings)
            hits, error = self.replay_hits, self.last_error
        return {
            "settings": settings, "recordings": self.store.list(),
            "replay_hits": hits, "last_error": error,
            "proxy": f"{self.listen_host}:{self.proxy_port}",
            "data_dir": str(self.data_dir),
        }

    def configure(self, updates):
        with self.lock:
            if not isinstance(updates, dict) or set(updates) - set(self.settings):
                raise ValueError("设置包含未知字段。")
            settings = self.settings | updates
            if settings["mode"] not in ("off", "record", "replay"):
                raise ValueError("无效的请求模式。")
            if type(settings["speed"]) not in (float, int) or settings["speed"] not in (0, .5, 1, 2, 4):
                raise ValueError("不支持的回放速度。")
            if type(settings["include_query"]) is not bool:
                raise ValueError("include_query 必须是布尔值。")
            for key in ("host_filter", "path_filter"):
                if not isinstance(settings[key], str) or not 1 <= len(settings[key]) <= 500:
                    raise ValueError("Host/Path 过滤器不能为空，最多 500 个字符。")
            if settings["selected"] is not None:
                document, _ = self.store.read(settings["selected"])
                if document["outcome"] == "recording":
                    raise ValueError("请等待录制结束后再选用。")
            if settings["mode"] == "replay" and settings["selected"] is None:
                raise ValueError("请先选择历史录制或导入 JSON。")
            self.settings = settings

    def route(self, request):
        with self.lock:
            settings = dict(self.settings)
        url = urlsplit(request["url"])
        record = (
            settings["mode"] == "record"
            and fnmatch.fnmatchcase(url.hostname or "", settings["host_filter"])
            and fnmatch.fnmatchcase(url.path or "/", settings["path_filter"])
        )
        if settings["mode"] != "replay":
            return record, None
        document, chunks = self.store.read(settings["selected"])
        if match_key(request, settings["include_query"]) != match_key(document["request"], settings["include_query"]):
            return False, None
        ticket = secrets.token_urlsafe(24)
        with self.lock:
            self.tickets = {key: value for key, value in self.tickets.items() if value[0] > time.monotonic()}
            self.tickets[ticket] = (time.monotonic() + 60, document, chunks, settings["speed"])
            self.replay_hits += 1
        return False, ticket

    def take_ticket(self, ticket):
        with self.lock:
            entry = self.tickets.pop(ticket, None)
        if entry is None or entry[0] < time.monotonic():
            raise KeyError("回放请求已失效。")
        return entry[1:]

    def delete(self, identifier):
        with self.lock:
            document, _ = self.store.read(identifier)
            if document["outcome"] == "recording":
                raise ValueError("请先停止请求，再删除录制。")
            self.store.delete(identifier)
            if self.settings["selected"] == identifier:
                self.settings["selected"] = None
                if self.settings["mode"] == "replay":
                    self.settings["mode"] = "off"

    def start(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", self.ui_port), Handler)
        self.server.daemon_threads = True
        self.server.application = self
        threading.Thread(target=self.server.serve_forever, name="sse-console", daemon=True).start()

    def stop(self):
        self.stopping.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.store.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def app(self):
        return self.server.application

    def log_message(self, *_):
        pass

    def send_bytes(self, data, status=200, content_type="application/json; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data, status=200):
        self.send_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"), status)

    def authorized(self, token_required):
        allowed = {f"127.0.0.1:{self.app.ui_port}", f"localhost:{self.app.ui_port}"}
        if self.headers.get("Host") not in allowed:
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in allowed}:
            return False
        return not token_required or secrets.compare_digest(self.headers.get("X-SSE-Key", ""), self.app.token)

    def body_json(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("控制台 API 不接受 chunked 请求。")
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 32 * 1024 * 1024:
            raise ValueError("JSON 文件大小需介于 1 字节和 32 MB。")
        data = self.rfile.read(size)
        if len(data) != size:
            raise ValueError("请求正文不完整。")
        return json.loads(data)

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def do_DELETE(self):
        self.dispatch()

    def dispatch(self):
        self.connection.settimeout(30)
        route = urlsplit(self.path)
        try:
            needs_token = route.path.startswith(("/api/", "/__replay/"))
            if not self.authorized(needs_token):
                self.close_connection = True
                self.send_json({"error": "控制台仅允许本机访问，并需要有效的会话密钥。"}, 403)
                return
            if self.command == "GET" and route.path in ("/", "/app.js", "/style.css"):
                name = "index.html" if route.path == "/" else route.path[1:]
                data = (Path(__file__).parent / "static" / name).read_bytes()
                content_type = {
                    "index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
                    "style.css": "text/css; charset=utf-8",
                }[name]
                if name == "index.html":
                    data = data.replace(b"__API_TOKEN__", self.app.token.encode("ascii"))
                self.send_bytes(data, content_type=content_type)
            elif self.command == "GET" and route.path == "/api/state":
                self.send_json(self.app.snapshot())
            elif self.command == "POST" and route.path == "/api/settings":
                self.app.configure(self.body_json())
                self.send_json(self.app.snapshot())
            elif self.command == "POST" and route.path == "/api/import":
                identifier = self.app.store.import_document(self.body_json())
                self.app.configure({"selected": identifier})
                self.send_json({"id": identifier}, 201)
            elif self.command == "POST" and route.path.startswith("/api/internal/"):
                self.internal(route.path, self.body_json())
            elif route.path.startswith("/api/recordings/"):
                identifier = route.path.removeprefix("/api/recordings/")
                if self.command == "DELETE":
                    self.app.delete(identifier)
                    self.send_json({"ok": True})
                elif self.command == "GET":
                    editable = parse_qs(route.query).get("editable") == ["1"]
                    self.send_json(self.app.store.document(identifier, editable))
                else:
                    self.send_json({"error": "不支持的方法。"}, 405)
            elif self.command == "GET" and route.path == "/api/certificate":
                data = (self.app.data_dir / "certificates" / "mitmproxy-ca-cert.pem").read_bytes()
                self.send_bytes(data, content_type="application/x-pem-file", extra={
                    "Content-Disposition": 'attachment; filename="sse-proxy-ca.pem"',
                })
            elif self.command == "GET" and route.path.startswith("/__replay/"):
                self.replay(*self.app.take_ticket(route.path.removeprefix("/__replay/")))
            else:
                self.send_json({"error": "未找到。"}, 404)
        except (ValueError, TypeError, AttributeError) as error:
            self.close_connection = True
            self.send_json({"error": str(error)}, 400)
        except KeyError as error:
            self.send_json({"error": str(error)}, 404)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception as error:
            self.app.last_error = str(error)
            self.close_connection = True
            self.send_json({"error": str(error)}, 500)

    def internal(self, path, data):
        if path == "/api/internal/route":
            record, ticket = self.app.route(data)
            self.send_json({"record": record, "ticket": ticket})
        elif path == "/api/internal/begin":
            identifier = self.app.store.begin(data["request"], data["response"], data["offset"])
            self.send_json({"id": identifier})
        elif path == "/api/internal/chunk":
            self.app.store.append(data["id"], data["offset"], base64.b64decode(data["base64"], validate=True))
            self.send_json({"ok": True})
        elif path == "/api/internal/finish":
            self.app.store.finish(data["id"], data["duration"], data["outcome"], data["error"])
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "未找到。"}, 404)

    def replay(self, document, chunks, speed):
        started = time.monotonic()
        self.close_connection = True
        self.wait_for(document["response_offset"], started, speed)
        self.send_response(document["response"]["status"])
        for name, value in document["response"]["headers"].items():
            if name.lower() not in TRANSPORT_HEADERS:
                self.send_header(name, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.send_header("X-SSE-Proxy", "replay")
        self.end_headers()
        self.wfile.flush()
        for offset, content in chunks:
            self.wait_for(offset, started, speed)
            if content:
                self.wfile.write(f"{len(content):x}\r\n".encode("ascii") + content + b"\r\n")
                self.wfile.flush()
        self.wait_for(document["duration"], started, speed)
        if document["outcome"] == "completed":
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    def wait_for(self, offset, started, speed):
        target = started + offset / speed if speed else started
        while time.monotonic() < target:
            if self.app.stopping.is_set():
                raise ConnectionResetError("代理正在停止。")
            remaining = min(.25, target - time.monotonic())
            readable, _, _ = select.select([self.connection], [], [], max(0, remaining))
            if readable and not self.connection.recv(1, socket.MSG_PEEK):
                raise ConnectionResetError("回放客户端已断开。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--ui-port", type=int, required=True)
    parser.add_argument("--proxy-port", type=int, required=True)
    parser.add_argument("--listen-host", required=True)
    arguments = parser.parse_args()
    application = Application(arguments.data_dir, arguments.ui_port, arguments.proxy_port, arguments.listen_host)
    try:
        application.start()
        while not application.stopping.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        application.stop()
