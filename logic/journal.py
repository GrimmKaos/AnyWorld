"""Player-safe, append-only event history, independent of private transcripts."""

import asyncio
import json
import logging
from collections import deque
from contextlib import nullcontext
from itertools import chain
from pathlib import Path

from core.schemas import ServerEvent

PUBLIC_FIELDS = {
    "state_update": {
        "title",
        "scenario_title",
        "global_narrative",
        "player_resolutions",
        "round_number",
        "submitted_actions",
        "player_order",
        "dice_results",
        "original_scenario",
    },
    "action_echo": {"round_number", "player_name", "player_color_index", "action", "action_id"},
    "round_start": {"round_number"},
    "game_ended": {"msg"},
    "chat_echo": {"name", "chat"},
}
LOGGER = logging.getLogger(__name__)


class PublicJournal:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.path = Path(".public_games") / f"{session_id}.jsonl"
        self.cursor = 0
        self.lock = asyncio.Lock()
        self._pending: deque[ServerEvent] = deque()
        self._pending_bytes = 0
        self.incomplete = False

    async def record(self, event: ServerEvent) -> ServerEvent:
        fields = PUBLIC_FIELDS.get(event.type)
        if fields is None:
            return event
        async with self.lock:
            # A cancelled committed delivery may retry this exact event after its
            # write completed. Retain its identity before the first I/O await.
            if event.payload.get("session_id") == self.session_id and isinstance(
                event.payload.get("event_id"), int
            ):
                return event
            self.cursor += 1
            payload = {key: value for key, value in event.payload.items() if key in fields}
            payload.update(session_id=self.session_id, event_id=self.cursor)
            public = ServerEvent(type=event.type, payload=payload)
            event.payload = payload
            self._pending.append(public)
            self._pending_bytes += len(public.model_dump_json().encode("utf-8"))
            while len(self._pending) > 128 or self._pending_bytes > 2097152:
                discarded = self._pending.popleft()
                self._pending_bytes -= len(discarded.model_dump_json().encode("utf-8"))
                self.incomplete = True
            work = asyncio.create_task(asyncio.to_thread(self._append_batch, tuple(self._pending)))
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                try:
                    await work
                except OSError:
                    LOGGER.warning("Public journal write deferred after cancellation")
                else:
                    self._pending.clear()
                    self._pending_bytes = 0
                raise
            except OSError:
                LOGGER.warning("Public journal write deferred; retaining bounded public events")
            else:
                self._pending.clear()
                self._pending_bytes = 0
            return public

    def _append_batch(self, events: tuple[ServerEvent, ...]) -> None:
        self.path.parent.mkdir(exist_ok=True)
        with self.path.open("a+b") as archive:
            offset = archive.tell()
            try:
                archive.write(
                    "".join(event.model_dump_json() + "\n" for event in events).encode("utf-8")
                )
                archive.flush()
            except BaseException:
                archive.seek(offset)
                archive.truncate()
                raise

    async def page(self, after: int, limit: int, search: str) -> dict[str, object]:
        async with self.lock:
            events, cursor, more = await asyncio.to_thread(self._page, after, limit, search)
            return {
                "session_id": self.session_id,
                "events": events,
                "cursor": cursor,
                "has_more": more,
                "latest_event_id": self.cursor,
                "incomplete": self.incomplete,
            }

    def _page(self, after: int, limit: int, search: str) -> tuple[list[dict], int, bool]:
        events = []
        cursor = after
        size = 0
        try:
            archive_source = self.path.open(encoding="utf-8")
        except OSError:
            archive_source = nullcontext(())
        with archive_source as archive:
            previous_id = 0
            for line in chain(archive, (event.model_dump_json() for event in self._pending)):
                try:
                    event = json.loads(line)
                    event_id = event["payload"]["event_id"]
                    if (
                        type(event_id) is not int
                        or event["payload"]["session_id"] != self.session_id
                    ):
                        raise ValueError("Invalid public event identity")
                except (ValueError, KeyError, TypeError):
                    self.incomplete = True
                    continue
                if event_id <= previous_id:
                    continue
                if event_id != previous_id + 1:
                    self.incomplete = True
                previous_id = event_id
                if event_id <= after:
                    continue
                matches = not search or search.casefold() in line.casefold()
                if matches and (
                    len(events) == limit or (events and size + len(line.encode("utf-8")) > 524288)
                ):
                    return events, cursor, True
                cursor = event_id
                if matches:
                    events.append(event)
                    size += len(line.encode("utf-8"))
        if previous_id < self.cursor:
            self.incomplete = True
        return events, cursor, False
