import argparse
import fcntl
import http.client
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="SSE Proxy Studio — independent recording and timed replay proxy")
    parser.add_argument("--proxy-port", type=int, default=8088)
    parser.add_argument("--ui-port", type=int, default=9090)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--data-dir", type=Path, default=Path.home() / "Library/Application Support/SSEProxyStudio")
    arguments = parser.parse_args()
    if not all(1024 <= port <= 65535 for port in (arguments.proxy_port, arguments.ui_port)):
        parser.error("Ports must be between 1024 and 65535.")
    if arguments.proxy_port == arguments.ui_port:
        parser.error("Proxy and UI ports must be different.")
    executable = shutil.which("mitmdump")
    if not executable:
        parser.error("mitmdump not found. Install with: brew install mitmproxy")
    root = Path(__file__).resolve().parent
    data_dir = arguments.data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    data_lock = (data_dir / ".lock").open("a")
    try:
        fcntl.flock(data_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("This recording directory is already in use by another instance.")
    print(f"\n  SSE Proxy Studio\n  Console  http://127.0.0.1:{arguments.ui_port}")
    print(f"  Proxy    {arguments.listen_host}:{arguments.proxy_port}\n  Data     {data_dir}\n", flush=True)
    command = [
        executable, "-s", str(root / "addon.py"), "--listen-host", arguments.listen_host,
        "--listen-port", str(arguments.proxy_port), "--set", f"sse_ui_port={arguments.ui_port}",
        "--set", f"confdir={data_dir / 'certificates'}",
        "--set", "connection_strategy=lazy", "--set", "upstream_cert=false",
        "--set", "flow_detail=0",
    ]
    environment = os.environ | {"SSE_PROXY_TOKEN": secrets.token_urlsafe(32)}
    console = subprocess.Popen([
        sys.executable, str(root / "server.py"), "--data-dir", str(data_dir),
        "--ui-port", str(arguments.ui_port), "--proxy-port", str(arguments.proxy_port),
        "--listen-host", arguments.listen_host,
    ], env=environment)
    process = None
    try:
        ready = False
        startup_error = ""
        for _ in range(50):
            if console.poll() is not None:
                return console.returncode or 1
            connection = http.client.HTTPConnection("127.0.0.1", arguments.ui_port, timeout=1)
            try:
                connection.request("GET", "/api/state", headers={"X-SSE-Key": environment["SSE_PROXY_TOKEN"]})
                response = connection.getresponse()
                detail = response.read()
                if response.status == 200:
                    ready = True
                    break
                startup_error = f"HTTP {response.status}: {detail.decode(errors='replace')}"
            except OSError as error:
                startup_error = str(error)
            finally:
                connection.close()
            time.sleep(.1)
        if not ready:
            print(f"Console could not start: {startup_error}", file=sys.stderr)
            return 1
        process = subprocess.Popen(command, env=environment)
        while process.poll() is None and console.poll() is None:
            time.sleep(.25)
        return process.returncode if process.returncode is not None else 1
    except KeyboardInterrupt:
        return 0
    finally:
        for child in (process, console):
            if child and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    sys.exit(main())
