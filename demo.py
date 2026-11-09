import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Demo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests = 0

    def do_GET(self):
        if self.path != "/count":
            self.send_error(404)
            return
        body = json.dumps({"upstream_requests": Demo.requests}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.split("?")[0] != "/stream":
            self.send_error(404)
            return
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        Demo.requests += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        chunks = [
            b": connected\r\n\r\n",
            'id: 1\nevent: message\ndata: {"text":"Hello "}\n\n'.encode(),
            'id: 2\nevent: message\ndata: {"text":"真实上游 👋"}\n\n'.encode(),
            b"event: done\ndata: [DONE]\n\n",
        ]
        try:
            for chunk in chunks:
                time.sleep(.3)
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9101)
    port = parser.parse_args().port
    print(f"Demo SSE upstream: http://127.0.0.1:{port}/stream", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Demo).serve_forever()
