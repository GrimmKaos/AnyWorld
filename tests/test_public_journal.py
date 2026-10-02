"""Public replay excludes private data and action IDs are idempotent."""

import asyncio

from core.schemas import ServerEvent
from logic.journal import PublicJournal
from test_engine import build_started_game, payload


def test_journal_projection_pagination_and_search():
    async def run():
        journal = PublicJournal("test-session")
        for number in range(3):
            await journal.record(
                ServerEvent(
                    type="state_update",
                    payload={
                        "round_number": number + 1,
                        "global_narrative": f"Gate {number}",
                        "private_guidance": "secret",
                        "hidden_rolls": {"Alice": 42},
                        "dice_results": {"Bob": 42},
                    },
                )
            )
        first = await journal.page(0, 2, "")
        assert first["has_more"] and first["cursor"] == 2
        second = await journal.page(first["cursor"], 2, "")
        assert len(second["events"]) == 1 and not second["has_more"]
        assert len((await journal.page(0, 100, "Gate 1"))["events"]) == 1
        assert "secret" not in journal.path.read_text()
        assert "hidden_rolls" not in journal.path.read_text()

    asyncio.run(run())


def test_duplicate_action_acknowledgment_and_complete_snapshot(tmp_path):
    async def run():
        engine, sender, _ = await build_started_game(tmp_path)
        action = payload(
            "action",
            action="Wait",
            action_id="attempt-1",
            session_id=engine.session_id,
            round_number=1,
        )
        await engine.process_payload("host", action)
        before = dict(engine.round_buffer)
        await engine.process_payload("host", action)
        assert engine.round_buffer == before
        await engine.process_payload("player", payload("action", action="Walk"))
        await engine.wait_for_inference()
        snapshot = engine._snapshot_locked(engine.players["host"])
        assert snapshot["latest_round"]["round_number"] == 1
        assert "dice_results" in snapshot["latest_round"]
        await engine.process_payload("host", action)
        assert engine.round_buffer == {}
        assert any(event.type == "action_accepted" for _, event in sender.events)
        await engine.shutdown()

    asyncio.run(run())


def test_journal_disk_failure_replays_bounded_pending_events_once(monkeypatch):
    async def run():
        journal = PublicJournal("session-test")
        append = journal._append_batch

        def fail(events):
            raise OSError("injected failure")

        monkeypatch.setattr(journal, "_append_batch", fail)
        first = await journal.record(
            ServerEvent(type="chat_echo", payload={"name": "Alice", "chat": "Hello"})
        )
        assert first.payload["event_id"] == 1
        assert len((await journal.page(0, 100, ""))["events"]) == 1
        monkeypatch.setattr(journal, "_append_batch", append)
        await journal.record(ServerEvent(type="round_start", payload={"round_number": 1}))
        page = await journal.page(0, 100, "")
        assert [event["payload"]["event_id"] for event in page["events"]] == [1, 2]
        assert not journal._pending
        assert len(journal.path.read_text().splitlines()) == 2

    asyncio.run(run())
