"""In-process abuse bounds, origin policy and isolated socket writers."""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.server import ConnectionManager, create_app
from core.config import settings
from core.schemas import RoundResolution, ServerEvent
from logic.llm.errors import LLMResolutionError
from logic.llm.validation import check_public_output
from support import FakeResolver
from test_priority_one_transport import authenticate


def test_origin_policy_requires_explicit_missing_origin_setting():
    settings.server.allow_missing_origin = False
    with TestClient(create_app(FakeResolver)) as client:
        for headers in ({}, {"origin": "https://evil.test"}, {"origin": "https://[invalid"}):
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(f"/ws/{uuid4()}", headers=headers):
                    pass
        with client.websocket_connect(f"/ws/{uuid4()}", headers={"origin": "http://testserver"}):
            pass


def test_new_sockets_do_not_reset_authentication_budget():
    settings.server.failed_logins_per_source = 2
    with TestClient(create_app(FakeResolver)) as client:
        for _ in range(2):
            identity = str(uuid4())
            with client.websocket_connect(f"/ws/{identity}") as socket:
                authenticate(socket, identity, password="wrong")
                assert socket.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/ws/{uuid4()}"):
                pass


def test_large_and_deep_messages_are_rejected_before_domain_processing():
    with TestClient(create_app(FakeResolver)) as client:
        with client.websocket_connect(f"/ws/{uuid4()}") as socket:
            socket.send_text("x" * 65537)
            with pytest.raises(WebSocketDisconnect) as exc:
                socket.receive_json()
            assert exc.value.code == 1009
        with client.websocket_connect(f"/ws/{uuid4()}") as socket:
            socket.send_text("[" * 9 + "0" + "]" * 9)
            assert "nesting" in socket.receive_json()["payload"]["msg"]


def test_escaped_unpaired_unicode_is_rejected_cleanly():
    with TestClient(create_app(FakeResolver)) as client:
        with client.websocket_connect(f"/ws/{uuid4()}") as socket:
            socket.send_text('{"event_type":"auth","data":{"name":"\\ud800"}}')
            assert "invalid Unicode" in socket.receive_json()["payload"]["msg"]


def test_stalled_writer_does_not_delay_healthy_socket():
    class Socket:
        def __init__(self, blocked=False):
            self.blocked, self.messages = blocked, []

        async def accept(self):
            pass

        async def send_text(self, text):
            if self.blocked:
                await asyncio.Event().wait()
            self.messages.append(text)

        async def close(self, code=1000):
            pass

    async def run():
        settings.server.send_queue_messages = 2
        manager = ConnectionManager()
        slow, fast = Socket(True), Socket()
        for identity, socket in (("slow", slow), ("fast", fast)):
            await manager.connect(identity, socket)
            manager.promote(identity, socket)
        for _ in range(4):
            await asyncio.wait_for(
                manager.broadcast_global(ServerEvent(type="system_msg", payload={"msg": "Hi"})), 0.2
            )
        assert len(fast.messages) == 4
        assert "slow" not in manager.active_connections
        assert manager.owns("fast", fast)
        await manager.close()
        assert not manager._writers and not manager._retired_writers

    asyncio.run(run())


@pytest.mark.parametrize("secret", ["XYZZY", "Salainen tunnus", "隠された合言葉"])
def test_literal_short_and_multilingual_secret_echoes_are_rejected(secret):
    with pytest.raises(LLMResolutionError):
        check_public_output(
            RoundResolution(global_narrative=secret, player_resolutions={}), {}, secret
        )


def test_equal_public_and_hidden_values_do_not_reject_public_subject():
    check_public_output(
        RoundResolution(global_narrative="Bob rolled 17/100.", player_resolutions={}),
        {"Alice": 17},
        "",
        {"Bob": 17},
    )
    with pytest.raises(LLMResolutionError):
        check_public_output(
            RoundResolution(
                global_narrative="Alice secretly rolled 17/100.", player_resolutions={}
            ),
            {"Alice": 17},
            "",
            {"Bob": 17},
        )
