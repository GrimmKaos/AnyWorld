"""Raw model diagnostics use mocked HTTP and never contact a backend."""

import asyncio
import json
import re

import httpx
import pytest

from core.config import settings
from core.schemas import RoundResolution
from logic import llm_manager
from logic.debug_log import (
    MAX_DEBUG_LINE_LENGTH,
    RawResponseLogger,
    format_debug_body,
    request_type_context,
    set_debug_round_number,
)
from logic.llm_manager import LLMContextManager, LLMResolutionError


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("response_kind", ["valid", "invalid_json", "http_error"])
def test_raw_logging_precedes_sdk_parsing_and_does_not_change_requests(
    tmp_path, monkeypatch, enabled, response_kind, caplog
):
    """Capture exact response text, including parse failures, only when opted in."""
    monkeypatch.chdir(tmp_path)
    settings.llm.provider = "compatible"
    settings.llm.endpoint = "http://model.invalid/v1"
    settings.llm.debug_raw_responses = enabled
    narrative = "Arxs steps cautiouslyจาก beside PRIVATE_DIAGNOSTIC."
    content = json.dumps(
        {
            "global_narrative": narrative,
            "player_resolutions": {"Arxs": "Arxs waits."},
        },
        ensure_ascii=False,
    )
    if response_kind == "invalid_json":
        content = "PRIVATE_DIAGNOSTIC invalid model JSON"
    body = json.dumps(
        {
            "id": "probe",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "reasoning_content": "A private chain of thought.",
                    },
                }
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
    if response_kind == "http_error":
        body = '{"error":{"message":"PRIVATE_DIAGNOSTIC","type":"server_error"}}'

    sent_requests = []

    def handler(request):
        sent_requests.append(request.content.decode("utf-8"))
        request_json = json.loads(request.content)
        assert "repeat_penalty" not in request_json
        assert "presence_penalty" not in request_json
        return httpx.Response(
            503 if response_kind == "http_error" else 200,
            text=body,
            headers={"content-type": "application/json"},
        )

    # Both normal SDK construction and diagnostic SDK construction use the mock.
    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(llm_manager, "DefaultAsyncHttpxClient", client_factory)

    async def run():
        manager = LLMContextManager()
        if not enabled:
            from openai import AsyncOpenAI

            manager.client = AsyncOpenAI(
                api_key="test",
                base_url=settings.llm.endpoint,
                max_retries=0,
                http_client=client_factory(),
            )
        try:
            if response_kind == "valid":
                result, _ = await manager._parse_attempt([], RoundResolution, "round", 100, 0)
                assert result.global_narrative == narrative
            else:
                with pytest.raises(LLMResolutionError):
                    await manager._parse_attempt([], RoundResolution, "round", 100, 0)
        finally:
            await manager.close()

    asyncio.run(run())
    files = list((tmp_path / ".debug" / "llm").glob("*.log"))
    assert len(files) == int(enabled)
    if enabled:
        logged = files[0].read_text(encoding="utf-8")
        assert "-round-" in files[0].name
        assert "Request type: round" in logged
        assert "POST /v1/chat/completions" in logged
        assert "messages" in logged
        if response_kind != "http_error":
            assert "A private chain of thought." in logged
        assert "PRIVATE_DIAGNOSTIC" in logged
        assert '"body":' not in logged
        assert max(map(len, logged.splitlines())) <= MAX_DEBUG_LINE_LENGTH
    else:
        assert not (tmp_path / ".debug").exists()
    assert "PRIVATE_DIAGNOSTIC" not in caplog.text


def test_debug_disk_failure_does_not_break_response(tmp_path):
    """Optional diagnostics must not pause the game when the disk is unwritable."""
    target = tmp_path / "not_a_directory"
    target.write_text("existing file", encoding="utf-8")
    logger = RawResponseLogger(target)

    async def run():
        response = httpx.Response(
            200,
            text="raw",
            request=httpx.Request("POST", "http://model.invalid/v1/chat/completions"),
        )
        await logger.capture(response)
        assert response.text == "raw"

    asyncio.run(run())


def test_request_is_logged_even_when_no_response_arrives(tmp_path):
    """Keep a sent request diagnostic when the backend times out or disconnects."""
    logger = RawResponseLogger(tmp_path / ".debug" / "llm")
    request = httpx.Request(
        "POST",
        "http://model.invalid/v1/chat/completions",
        content=b'{"messages":[{"role":"user","content":"PRIVATE"}]}',
    )

    async def run():
        await logger.capture_request(request)

    asyncio.run(run())
    files = list((tmp_path / ".debug" / "llm").glob("*.log"))
    assert len(files) == 1
    assert "-unknown-" in files[0].name
    logged = files[0].read_text(encoding="utf-8")
    assert "HTTP status: pending (no response captured)" in logged
    assert "Request type: unknown" in logged
    assert "messages: [" in logged
    assert "content: PRIVATE" in logged
    assert "(not received)" in logged
    assert '"body":' not in logged


def test_debug_body_renders_nested_response_newlines():
    """Nested structured model output is readable instead of one escaped line."""
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({"global_narrative": "First line.\nSecond line."})
                    }
                }
            ]
        }
    )
    body_with_escaped_text = json.dumps(
        {"content": r"Current round actions: [{\"event\": \"Bosco attack\"}]"}
    )

    rendered = format_debug_body(body)
    escaped_text_rendered = format_debug_body(body_with_escaped_text)

    assert "First line.\n" in rendered
    assert r"\n" not in rendered
    assert '[{"event": "Bosco attack"}]' in escaped_text_rendered
    assert r"[{\"event\"" not in escaped_text_rendered
    assert "content: Current round actions:" in escaped_text_rendered

    invalid_body_rendered = format_debug_body(
        r"Current round actions: [{\"event\": \"Bosco attack\"}]"
    )
    assert '[{"event": "Bosco attack"}]' in invalid_body_rendered


def test_debug_body_forces_a_newline_at_the_display_limit():
    """Long single-line values cannot create unbounded log rows."""
    body = json.dumps({"content": "x" * (MAX_DEBUG_LINE_LENGTH * 2 + 7)})

    rendered = format_debug_body(body)

    assert max(map(len, rendered.splitlines())) <= MAX_DEBUG_LINE_LENGTH


def test_round_requests_are_combined_after_success(tmp_path):
    """Successful rounds have one readable archive and no leftover temporary files."""
    logger = RawResponseLogger(tmp_path / ".debug" / "llm")
    set_debug_round_number(3)
    request = httpx.Request(
        "POST",
        "http://model.invalid/v1/chat/completions",
        content=b'{"messages":[{"role":"user","content":"line 1\\nline 2"}]}',
    )
    response_body = json.dumps(
        {"choices": [{"message": {"content": json.dumps({"global_narrative": "Done.\nNext."})}}]}
    )

    async def run():
        with request_type_context("dice"):
            await logger.capture_request(request)
        await logger.capture(httpx.Response(200, text=response_body, request=request))
        pending_request = httpx.Request(
            "POST",
            "http://model.invalid/v1/chat/completions",
            content=b'{"messages":[{"role":"user","content":"pending"}]}',
        )
        with request_type_context("round"):
            await logger.capture_request(pending_request)
        assert len(list(logger.directory.glob("*.tmp"))) == 2
        await logger.finish_round(3, {"engine": {"inventory": "brass key"}})

    try:
        asyncio.run(run())
    finally:
        set_debug_round_number(None)

    files = list((tmp_path / ".debug" / "llm").iterdir())
    assert [path.suffix for path in files] == [".log"]
    assert re.fullmatch(
        r"\d{8}T\d{6}Z-round-0003-debug(?:-\d+)?\.log",
        files[0].name,
    )
    content = files[0].read_text(encoding="utf-8")
    assert "ROUND 3 DEBUG LOG" in content
    assert "FULL ROUND SUMMARY" in content
    assert "inventory: brass key" in content
    assert content.count("REQUEST ") == 2
    assert "Temporary record:" not in content
    assert "line 1\n" in content
    assert "Done.\n" in content
    assert max(map(len, content.splitlines())) <= MAX_DEBUG_LINE_LENGTH
    assert not list((tmp_path / ".debug" / "llm").glob("*.tmp"))


def test_manager_round_lifecycle_groups_provider_hooks(tmp_path, monkeypatch):
    """The manager's round usage lifecycle reaches the HTTP debug hooks."""
    monkeypatch.chdir(tmp_path)
    settings.llm.provider = "compatible"
    settings.llm.endpoint = "http://model.invalid/v1"
    settings.llm.debug_raw_responses = True
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "global_narrative": "A line.\nAnother line.",
                                "player_resolutions": {},
                            }
                        )
                    }
                }
            ]
        }
    )

    def handler(request):
        return httpx.Response(200, text=body, headers={"content-type": "application/json"})

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(llm_manager, "DefaultAsyncHttpxClient", client_factory)

    async def run():
        manager = LLMContextManager()
        manager.begin_round_usage(4)
        await manager._parse_attempt([], RoundResolution, "round", 100, 0)
        await manager.complete_round_debug(4, {"private_state": {"injury": "healed"}})
        await manager.close()

    asyncio.run(run())
    files = list((tmp_path / ".debug" / "llm").glob("*-round-0004-debug*.log"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "A line.\n" in content
    assert "injury: healed" in content
