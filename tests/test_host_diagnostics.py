"""Exact timings, phases and failure details reach authenticated hosts only."""

import asyncio

from logic.llm.errors import LLMResolutionError
from test_engine import build_started_game, payload


def test_host_only_failure_and_usage_projection(tmp_path, monkeypatch):
    async def run():
        engine, sender, resolver = await build_started_game(tmp_path)

        async def fail(*args, **kwargs):
            raise LLMResolutionError("Private recovery diagnosis")

        monkeypatch.setattr(resolver, "plan_dice", fail)
        await engine.process_payload("host", payload("action", action="Wait"))
        await engine.process_payload("player", payload("action", action="Walk"))
        await engine.wait_for_inference()
        public = [event for owner, event in sender.events if owner is None]
        assert "Private recovery diagnosis" not in str(public)
        assert all("phase" not in event.payload for event in public)
        host = [event for owner, event in sender.events if owner == "host"]
        assert any(event.payload.get("diagnosis") == "Private recovery diagnosis" for event in host)
        monkeypatch.setattr(
            resolver,
            "usage_snapshot",
            lambda: {
                "round": {"total_tokens": 100, "latency_seconds": 1.234},
                "game": {"total_tokens": 100, "latency_seconds": 1.234},
                "last_request": {"kind": "dice_audit"},
                "round_work_seconds": 2.5,
            },
        )
        await engine._publish_usage()
        usage = [
            event for owner, event in sender.events if owner is None and event.type == "token_usage"
        ][-1]
        assert usage.payload["round"] == {"total_tokens": 100}
        assert "last_request" not in usage.payload and "round_work_seconds" not in usage.payload
        await engine.shutdown()

    asyncio.run(run())
