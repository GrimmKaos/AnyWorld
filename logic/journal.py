"""Player-safe, append-only event history, independent of private transcripts."""

import asyncio
import json
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
    },
    "action_echo": {"round_number", "player_name", "player_color_index", "action", "action_id"},
    "round_start": {"round_number"},
    "game_ended": {"msg"},
}


class PublicJournal:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.path = Path(".public_games") / f"{session_id}.jsonl"
        self.cursor = 0
        self.lock = asyncio.Lock()

    async def record(self, event: ServerEvent) -> ServerEvent:
        fields = PUBLIC_FIELDS.get(event.type)
        if fields is None:
            return event
        async with self.lock:
            self.cursor += 1
            payload = {key: value for key, value in event.payload.items() if key in fields}
            payload.update(session_id=self.session_id, event_id=self.cursor)
            public = ServerEvent(type=event.type, payload=payload)
            work = asyncio.create_task(asyncio.to_thread(self._append, public))
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                await work
                raise
            return public

    def _append(self, event: ServerEvent) -> None:
        self.path.parent.mkdir(exist_ok=True)
        with self.path.open("a", encoding="utf-8") as archive:
            archive.write(event.model_dump_json() + "\n")

    async def page(self, after: int, limit: int, search: str) -> dict[str, object]:
        async with self.lock:
            events, cursor, more = await asyncio.to_thread(self._page, after, limit, search)
            return {
                "session_id": self.session_id,
                "events": events,
                "cursor": cursor,
                "has_more": more,
                "latest_event_id": self.cursor,
            }

    def _page(self, after: int, limit: int, search: str) -> tuple[list[dict], int, bool]:
        events = []
        cursor = after
        if not self.path.exists():
            return events, cursor, False
        with self.path.open(encoding="utf-8") as archive:
            for line in archive:
                event = json.loads(line)
                event_id = event["payload"]["event_id"]
                if event_id <= after:
                    continue
                matches = not search or search.casefold() in line.casefold()
                if matches and len(events) == limit:
                    return events, cursor, True
                cursor = event_id
                if matches:
                    events.append(event)
        return events, cursor, False
