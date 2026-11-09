import base64
import json
import os
import time
from http.client import HTTPConnection

from mitmproxy import ctx, http


SENSITIVE_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "api-key", "x-auth-token",
}


def clean_headers(headers):
    return {key.lower(): value for key, value in headers.items() if key.lower() not in SENSITIVE_HEADERS}


class SSEProxy:
    def __init__(self):
        self.connection = None
        self.token = os.environ.get("SSE_PROXY_TOKEN", "")

    def load(self, loader):
        loader.add_option("sse_ui_port", int, 9090, "Local SSE console port")

    def call(self, endpoint, data):
        if self.connection is None:
            self.connection = HTTPConnection("127.0.0.1", ctx.options.sse_ui_port, timeout=5)
        try:
            self.connection.request(
                "POST", f"/api/internal/{endpoint}", json.dumps(data).encode(),
                {"Content-Type": "application/json", "X-SSE-Key": self.token},
            )
            response = self.connection.getresponse()
            result = json.loads(response.read())
            if response.status != 200:
                raise RuntimeError(result.get("error", f"Console HTTP {response.status}"))
            return result
        except Exception:
            self.connection.close()
            self.connection = None
            raise

    def request(self, flow: http.HTTPFlow):
        request = {
            "method": flow.request.method, "url": flow.request.pretty_url,
        }
        try:
            decision = self.call("route", request)
            if decision["ticket"]:
                flow.metadata["sse_replay"] = True
                flow.request.scheme = "http"
                flow.request.host = "127.0.0.1"
                flow.request.port = ctx.options.sse_ui_port
                flow.request.path = f"/__replay/{decision['ticket']}"
                flow.request.method = "GET"
                flow.request.content = b""
                flow.request.headers = http.Headers(
                    host=f"127.0.0.1:{ctx.options.sse_ui_port}", **{"x-sse-key": self.token},
                )
            elif decision["record"]:
                content = flow.request.raw_content or b""
                request["headers"] = clean_headers(flow.request.headers)
                request["body"] = content[:65536].decode("utf-8", errors="replace")
                request["body_truncated"] = len(content) > 65536
                flow.metadata["sse_capture"] = {"request": request, "started": time.monotonic()}
                flow.request.headers["accept-encoding"] = "identity"
        except Exception as error:
            ctx.log.error(f"SSE Proxy routing failed: {error}")
            flow.response = http.Response.make(
                502, f"SSE Proxy: {error}".encode("utf-8"), {"content-type": "text/plain; charset=utf-8"},
            )

    def responseheaders(self, flow: http.HTTPFlow):
        if flow.metadata.get("sse_replay"):
            flow.response.stream = True
            return
        content_type = flow.response.headers.get("content-type", "").split(";")[0].strip().lower()
        capture = flow.metadata.get("sse_capture")
        if content_type != "text/event-stream":
            return
        if not capture:
            flow.response.stream = True
            return
        try:
            result = self.call("begin", {
                "request": capture["request"],
                "response": {"status": flow.response.status_code, "headers": clean_headers(flow.response.headers)},
                "offset": time.monotonic() - capture["started"],
            })
            capture["id"] = result["id"]
        except Exception as error:
            ctx.log.error(f"Cannot record SSE: {error}")
            flow.response.stream = True
            return

        def observe(content):
            if content and not capture.get("error"):
                try:
                    self.call("chunk", {
                        "id": capture["id"], "offset": time.monotonic() - capture["started"],
                        "base64": base64.b64encode(content).decode("ascii"),
                    })
                except Exception as error:
                    capture["error"] = str(error)
                    ctx.log.error(f"Cannot save SSE chunk: {error}")
            return content

        flow.response.stream = observe

    def response(self, flow: http.HTTPFlow):
        self.finish(flow, "completed")

    def error(self, flow: http.HTTPFlow):
        self.finish(flow, "error", str(flow.error or "连接中断"))

    def finish(self, flow, outcome, error=""):
        capture = flow.metadata.get("sse_capture", {})
        if "id" not in capture or capture.get("finished"):
            return
        capture["finished"] = True
        try:
            self.call("finish", {
                "id": capture["id"], "duration": time.monotonic() - capture["started"],
                "outcome": "error" if capture.get("error") else outcome,
                "error": capture.get("error", error),
            })
        except Exception as failure:
            ctx.log.error(f"Cannot finish SSE recording: {failure}")

    def done(self):
        if self.connection:
            self.connection.close()


addons = [SSEProxy()]
