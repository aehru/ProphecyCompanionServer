"""RoomManager unit tests — no server, fake sockets."""

import asyncio
from typing import Any, cast

from fastapi import WebSocket

from app.schemas import Presence
from app.ws import Member, RoomManager


class FakeWS:
    """Records sends; optionally dies like a dropped socket."""

    def __init__(self, dead: bool = False) -> None:
        self.dead = dead
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        if self.dead:
            raise RuntimeError("Cannot call 'send' once a close message has been sent.")
        self.sent.append(data)


def _gm(dead: bool = False) -> Member:
    return Member(ws=cast(WebSocket, FakeWS(dead=dead)), role="gm", char_id=None)


def test_dead_gm_is_evicted_and_live_gm_still_notified() -> None:
    async def scenario() -> None:
        manager = RoomManager()
        dead, live = _gm(dead=True), _gm()
        manager.add("ROOM1234", dead)
        manager.add("ROOM1234", live)

        # Must not raise despite the dead socket.
        await manager.notify_gms("ROOM1234", Presence(char_id="c1", online=True))

        assert manager.gms("ROOM1234") == [live]  # dead one evicted
        assert cast(FakeWS, live.ws).sent == [
            {"v": 1, "type": "presence", "charId": "c1", "online": True}
        ]

    asyncio.run(scenario())


def test_notify_empty_room_is_noop() -> None:
    async def scenario() -> None:
        await RoomManager().notify_gms("NOROOM", Presence(char_id="c1", online=False))

    asyncio.run(scenario())
