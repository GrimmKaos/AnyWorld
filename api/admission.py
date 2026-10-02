"""Bounded socket admission, message parsing, and source identity."""

import json
from collections import OrderedDict, deque
from ipaddress import ip_address, ip_network
from time import monotonic
from urllib.parse import urlsplit

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from core.config import settings


class WindowBudget:
    """Bound records and reject without extending an attacker's window."""

    def __init__(self, capacity: int, seconds: float, max_keys: int = 4096):
        self.capacity, self.seconds, self.max_keys = capacity, seconds, max_keys
        self.records: OrderedDict[str, deque[float]] = OrderedDict()

    def blocked(self, key: str) -> bool:
        values = self.records.get(key)
        if values is None:
            return False
        now = monotonic()
        while values and values[0] <= now - self.seconds:
            values.popleft()
        return len(values) >= self.capacity

    def record(self, key: str) -> None:
        self.blocked(key)
        values = self.records.setdefault(key, deque())
        self.records.move_to_end(key)
        if len(values) < self.capacity:
            values.append(monotonic())
        while len(self.records) > self.max_keys:
            self.records.popitem(last=False)

    def accept(self, key: str) -> bool:
        if self.blocked(key):
            return False
        self.record(key)
        return True


def source_address(socket: WebSocket) -> str:
    direct = socket.client.host if socket.client else "unknown"
    networks = [ip_network(value, strict=False) for value in settings.server.trusted_proxies]

    def trusted(value):
        try:
            return any(ip_address(value) in network for network in networks)
        except ValueError:
            return False

    if not trusted(direct):
        return direct
    for candidate in reversed(socket.headers.get("x-forwarded-for", "").split(",")):
        candidate = candidate.strip()
        try:
            ip_address(candidate)
        except ValueError:
            continue
        if not trusted(candidate):
            return candidate
    return direct


def origin_allowed(socket: WebSocket) -> bool:
    origin = socket.headers.get("origin")
    if origin is None:
        return settings.server.allow_missing_origin
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
        return False
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        return False
    expected = (
        ("https" if socket.url.scheme == "wss" else "http") + "://" + socket.headers.get("host", "")
    )
    return origin.rstrip("/") in {expected, *settings.server.allowed_origins}


async def receive_payload(socket: WebSocket):
    event = await socket.receive()
    if event["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(event.get("code", 1000))
    text = event.get("text")
    if text is None:
        await socket.close(code=1003)
        raise WebSocketDisconnect(1003)
    if len(text.encode("utf-8")) > settings.server.max_message_bytes:
        await socket.close(code=1009)
        raise WebSocketDisconnect(1009)
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > settings.server.max_message_depth:
                raise ValueError("Message nesting exceeds the configured limit.")
        elif char in "]}":
            depth -= 1
    payload = json.loads(text)

    def valid_unicode(value):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("Message contains invalid Unicode.") from exc
        elif isinstance(value, dict):
            for key, item in value.items():
                valid_unicode(key)
                valid_unicode(item)
        elif isinstance(value, list):
            for item in value:
                valid_unicode(item)

    valid_unicode(payload)
    return payload
