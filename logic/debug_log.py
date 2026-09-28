"""Opt-in private diagnostics, captured before SDK parsing or narrative validation."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
from uuid import uuid4

import httpx

LOGGER = logging.getLogger(__name__)
_REQUEST_TYPE: ContextVar[str] = ContextVar("anyworld_debug_request_type", default="unknown")


@contextmanager
def request_type_context(kind: str):
    """Make the current inference request type available to HTTP debug hooks."""
    token = _REQUEST_TYPE.set(kind)
    try:
        yield
    finally:
        _REQUEST_TYPE.reset(token)


class RawResponseLogger:
    """Save completion requests and responses; never headers or credentials."""

    def __init__(self, directory: Path = Path(".debug/llm")) -> None:
        """Select a private directory outside the served static tree."""
        self.directory = directory

    async def capture_request(self, request: httpx.Request) -> None:
        """Persist the sent completion body before waiting for a response."""
        if not request.url.path.endswith("/chat/completions"):
            return
        try:
            body = request.content
        except (httpx.RequestNotRead, RuntimeError):
            body = b""
        timestamp = datetime.now(timezone.utc)
        request_type = re.sub(r"[^a-zA-Z0-9_-]", "_", _REQUEST_TYPE.get()) or "unknown"
        filename = f"{timestamp:%Y%m%dT%H%M%S.%fZ}-{request_type}-{uuid4().hex}.json"
        request_record = {
            "method": request.method,
            "url": request.url.path,
            "body": body.decode("utf-8", errors="replace"),
        }
        record = {
            "timestamp": timestamp.isoformat(),
            "request_type": request_type,
            "status_code": None,
            "request": request_record,
            "body": None,
            "thinking_sequences": [],
        }
        write = asyncio.create_task(asyncio.to_thread(self._write, filename, record))
        request.extensions["anyworld_debug_request"] = request_record
        request.extensions["anyworld_debug_request_type"] = request_type
        request.extensions["anyworld_debug_filename"] = filename
        request.extensions["anyworld_debug_write"] = write
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            await write
            raise

    async def capture(self, response: httpx.Response) -> None:
        """Record even malformed/rejected response bodies without changing SDK input."""
        if not response.request.url.path.endswith("/chat/completions"):
            return
        body = await response.aread()
        write = response.request.extensions.get("anyworld_debug_write")
        if isinstance(write, asyncio.Task):
            await asyncio.shield(write)
        filename = response.request.extensions.get("anyworld_debug_filename")
        timestamp = datetime.now(timezone.utc)
        record = {
            "timestamp": timestamp.isoformat(),
            "request_type": response.request.extensions.get(
                "anyworld_debug_request_type", "unknown"
            ),
            "status_code": response.status_code,
            "request": response.request.extensions.get("anyworld_debug_request"),
            "body": body.decode("utf-8", errors="replace"),
            "thinking_sequences": self._thinking_sequences(body),
        }
        if not isinstance(filename, str):
            request_type = re.sub(r"[^a-zA-Z0-9_-]", "_", str(record["request_type"])) or "unknown"
            record["request_type"] = request_type
            filename = f"{timestamp:%Y%m%dT%H%M%S.%fZ}-{request_type}-{uuid4().hex}.json"
        write = asyncio.create_task(asyncio.to_thread(self._write, filename, record))
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            await write
            raise

    @staticmethod
    def _thinking_sequences(body: bytes) -> list[dict[str, object]]:
        """Extract provider reasoning fields while retaining the complete raw body."""
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict):
            return []
        sequences: list[dict[str, object]] = []
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return sequences
        for index, choice in enumerate(choices):
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            delta = choice.get("delta")
            source = message if isinstance(message, dict) else delta
            if not isinstance(source, dict):
                continue
            thinking = source.get("reasoning_content")
            if thinking is None:
                thinking = source.get("thinking")
            if isinstance(thinking, str) and thinking:
                sequences.append({"choice_index": index, "content": thinking})
        return sequences

    def _write(self, filename: str, record: dict) -> None:
        """Keep disk failures nonfatal and private data out of console logs."""
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / filename).write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            LOGGER.warning("Raw response debug log could not be written: %s", type(exc).__name__)
