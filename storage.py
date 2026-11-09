import base64
import binascii
import json
import math
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit


FORMAT = "sse-proxy/v1"
SENSITIVE_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "api-key", "x-auth-token",
}
TRANSPORT_HEADERS = {
    "content-length", "transfer-encoding", "connection", "keep-alive",
    "proxy-connection", "trailer", "upgrade",
}


def clean_headers(headers):
    return {
        str(key).lower(): str(value)
        for key, value in headers.items()
        if str(key).lower() not in SENSITIVE_HEADERS
    }


def finite_time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("时间必须是秒数。")
    if not math.isfinite(value) or not 0 <= value <= 86400:
        raise ValueError("时间必须介于 0 和 86400 秒之间。")
    return float(value)


def event_bytes(frame):
    lines = []
    for comment in frame.get("comments", []):
        lines.extend(": " + line for line in str(comment).split("\n"))
    for key in ("id", "event", "retry"):
        if frame.get(key) is not None:
            value = str(frame[key])
            if "\n" in value or "\r" in value or "\0" in value:
                raise ValueError(f"{key} 不允许换行或 NUL。")
            lines.append(f"{key}: {value}")
    if "data" in frame:
        data = frame["data"]
        if not isinstance(data, str):
            data = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        lines.extend("data: " + line for line in data.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def parse_events(chunks):
    buffer = bytearray()
    frames = []
    frame = {}
    data_lines = []
    raw_lines = []

    def consume(line, offset):
        nonlocal frame, data_lines, raw_lines
        text = line.decode("utf-8", errors="replace")
        if not frames and not raw_lines:
            text = text.removeprefix("\ufeff")
        if not text:
            if raw_lines:
                if data_lines:
                    frame["data"] = "\n".join(data_lines)
                frame["offset"] = offset
                frames.append(frame)
            frame, data_lines, raw_lines = {}, [], []
            return
        raw_lines.append(text)
        if text.startswith(":"):
            frame.setdefault("comments", []).append(text[1:].removeprefix(" "))
            return
        field, separator, value = text.partition(":")
        value = value.removeprefix(" ") if separator else ""
        if field == "data":
            data_lines.append(value)
        elif field in ("event", "id", "retry"):
            frame[field] = value

    for offset, chunk in chunks:
        buffer.extend(chunk)
        while buffer:
            positions = [position for token in (b"\r", b"\n") if (position := buffer.find(token)) >= 0]
            if not positions:
                break
            position = min(positions)
            if buffer[position] == 13 and position + 1 == len(buffer):
                break
            width = 2 if buffer[position:position + 2] == b"\r\n" else 1
            consume(bytes(buffer[:position]), offset)
            del buffer[:position + width]
    if buffer.endswith(b"\r"):
        consume(bytes(buffer[:-1]), chunks[-1][0])
        buffer.clear()
    return frames, bool(buffer or raw_lines)


def validate_document(document):
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise ValueError(f'文件需要 "format": "{FORMAT}"。')
    request = document.get("request", {})
    if not isinstance(request, dict):
        raise ValueError("request 必须是对象。")
    url = urlsplit(request.get("url", ""))
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
        raise ValueError("request.url 必须是完整的 HTTP(S) URL，且不包含用户名或密码。")
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError("URL 端口无效。")
    if not isinstance(request.get("headers", {}), dict):
        raise ValueError("request.headers 必须是对象。")
    method = request.get("method", "GET").upper()
    if not method.isascii() or not method.isalpha():
        raise ValueError("request.method 无效。")
    response = document.get("response", {})
    if not isinstance(response, dict):
        raise ValueError("response 必须是对象。")
    status = response.get("status", 200)
    if type(status) is not int or not 200 <= status <= 599:
        raise ValueError("response.status 必须介于 200 和 599。")
    headers = response.get("headers", {"content-type": "text/event-stream; charset=utf-8"})
    if not isinstance(headers, dict):
        raise ValueError("response.headers 必须是对象。")
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("响应头名称和值必须是字符串。")
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) or any(character in value for character in "\r\n\0"):
            raise ValueError("响应头包含非法字符。")
        try:
            value.encode("latin-1")
        except UnicodeEncodeError as error:
            raise ValueError("响应头值必须能用 Latin-1 编码。") from error
    if ("frames" in document) == ("chunks" in document):
        raise ValueError("frames 和 chunks 必须且只能提供一个。")
    entries = document.get("frames", document.get("chunks"))
    if not isinstance(entries, list):
        raise ValueError("frames/chunks 必须是数组。")
    chunks = []
    previous = finite_time(document.get("response_offset", 0))
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("事件必须是对象。")
        offset = finite_time(entry.get("offset", previous))
        if offset < previous:
            raise ValueError("事件时间必须递增，且不能早于 response_offset。")
        if "frames" in document:
            content = event_bytes(entry)
        else:
            try:
                content = base64.b64decode(entry["base64"], validate=True)
            except (KeyError, ValueError, TypeError, binascii.Error) as error:
                raise ValueError("chunks[].base64 无效。") from error
        chunks.append((offset, content))
        previous = offset
    duration = finite_time(document.get("duration", previous))
    if duration < previous:
        raise ValueError("duration 不能早于最后一帧。")
    outcome = document.get("outcome", "completed")
    if outcome not in ("completed", "error", "interrupted"):
        raise ValueError("outcome 需为 completed、error 或 interrupted。")
    headers = clean_headers(headers)
    if "frames" in document:
        headers.pop("content-encoding", None)
    normalized = {
        "name": str(document.get("name", url.path or "/"))[:200],
        "request": {
            "method": method, "url": request["url"],
            "headers": clean_headers(request.get("headers", {})),
            "body": str(request.get("body", "")),
        },
        "response": {"status": status, "headers": headers},
        "response_offset": finite_time(document.get("response_offset", 0)),
        "duration": duration, "outcome": outcome,
        "error": str(document.get("error", "")),
    }
    return normalized, chunks


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        with self.connection:
            self.connection.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS recordings (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
                    request TEXT NOT NULL, response TEXT NOT NULL, response_offset REAL NOT NULL,
                    duration REAL NOT NULL DEFAULT 0, outcome TEXT NOT NULL, error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    sequence INTEGER PRIMARY KEY, recording_id TEXT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
                    offset REAL NOT NULL, content BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS recording_chunks ON chunks(recording_id, sequence);
                UPDATE recordings SET outcome='interrupted', error='代理退出前未完成'
                WHERE outcome='recording';
            """)

    def begin(self, request, response, offset=0, name=None):
        identifier = str(uuid.uuid4())
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO recordings VALUES (?, ?, ?, ?, ?, ?, 0, 'recording', '')",
                (identifier, name or urlsplit(request["url"]).path or "/",
                 datetime.now(timezone.utc).isoformat(), json.dumps(request, ensure_ascii=False),
                 json.dumps(response, ensure_ascii=False), offset),
            )
        return identifier

    def append(self, identifier, offset, content):
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO chunks(recording_id, offset, content) VALUES (?, ?, ?)",
                (identifier, offset, content),
            )
            self.connection.execute("UPDATE recordings SET duration=? WHERE id=?", (offset, identifier))

    def finish(self, identifier, duration, outcome="completed", error=""):
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE recordings SET duration=?, outcome=?, error=? WHERE id=?",
                (duration, outcome, error, identifier),
            )

    def list(self):
        with self.lock:
            rows = self.connection.execute("""
                SELECT recordings.*, COUNT(chunks.sequence) AS chunk_count,
                    COALESCE(SUM(LENGTH(chunks.content)), 0) AS byte_count
                FROM recordings LEFT JOIN chunks ON recordings.id=chunks.recording_id
                GROUP BY recordings.id ORDER BY created_at DESC LIMIT 500
            """).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            request = json.loads(item.pop("request"))
            response = json.loads(item.pop("response"))
            item.update(method=request["method"], url=request["url"], status=response["status"])
            result.append(item)
        return result

    def read(self, identifier):
        with self.lock:
            row = self.connection.execute("SELECT * FROM recordings WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise KeyError("录制不存在。")
            chunks = self.connection.execute(
                "SELECT offset, content FROM chunks WHERE recording_id=? ORDER BY sequence", (identifier,),
            ).fetchall()
        item = dict(row)
        item["request"] = json.loads(item["request"])
        item["response"] = json.loads(item["response"])
        item["format"] = FORMAT
        return item, [(chunk["offset"], bytes(chunk["content"])) for chunk in chunks]

    def document(self, identifier, editable=False):
        item, chunks = self.read(identifier)
        if editable:
            if item["response"]["headers"].get("content-encoding", "identity") != "identity":
                raise ValueError("压缩录制请使用原始 JSON；不能直接转成事件。")
            frames, unfinished = parse_events(chunks)
            if unfinished:
                raise ValueError("录制包含未完成的 SSE 帧，请使用原始 JSON 保留全部字节。")
            item["frames"] = frames
        else:
            item["chunks"] = [
                {"offset": offset, "base64": base64.b64encode(content).decode("ascii")}
                for offset, content in chunks
            ]
        return item

    def import_document(self, document):
        normalized, chunks = validate_document(document)
        identifier = self.begin(
            normalized["request"], normalized["response"],
            normalized["response_offset"], normalized["name"],
        )
        try:
            with self.lock, self.connection:
                self.connection.executemany(
                    "INSERT INTO chunks(recording_id, offset, content) VALUES (?, ?, ?)",
                    [(identifier, offset, content) for offset, content in chunks],
                )
                self.connection.execute(
                    "UPDATE recordings SET duration=?, outcome=?, error=? WHERE id=?",
                    (normalized["duration"], normalized["outcome"], normalized["error"], identifier),
                )
        except Exception:
            self.delete(identifier)
            raise
        return identifier

    def delete(self, identifier):
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM recordings WHERE id=?", (identifier,))

    def close(self):
        with self.lock:
            self.connection.close()
