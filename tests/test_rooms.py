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


def _member(role: str = "gm", dead: bool = False) -> Member:
    return Member(ws=cast(WebSocket, FakeWS(dead=dead)), role=role, campaign_id=1)


def test_dead_gm_is_evicted_and_live_gm_still_notified() -> None:
    async def scenario() -> None:
        manager = RoomManager()
        dead, live = _member(dead=True), _member()
        manager.add("ROOM1234", dead)
        manager.add("ROOM1234", live)

        # Must not raise despite the dead socket.
        await manager.notify_gms("ROOM1234", Presence(char_id="c1", online=True))

        assert manager.gms("ROOM1234") == [live]  # dead one evicted
        assert cast(FakeWS, live.ws).sent == [
            {"v": 2, "type": "presence", "charId": "c1", "online": True}
        ]

    asyncio.run(scenario())


def test_notify_empty_room_is_noop() -> None:
    async def scenario() -> None:
        await RoomManager().notify_gms("NOROOM", Presence(char_id="c1", online=False))

    asyncio.run(scenario())


def test_online_char_ids_is_union_over_members() -> None:
    """v2: each socket holds a SET of charIds; presence is the room-wide union."""
    manager = RoomManager()
    a, b, gm = _member("player"), _member("player"), _member()
    a.char_ids.update({"c1", "c2"})
    b.char_ids.add("c2")  # same char held by a reconnecting second socket
    gm.char_ids.add("pnj1")  # GM broadcaster holding an NPC
    for m in (a, b, gm):
        manager.add("ROOM1234", m)

    assert manager.online_char_ids("ROOM1234") == {"c1", "c2", "pnj1"}

    manager.remove("ROOM1234", a)
    # c2 survives via b; c1 is gone.
    assert manager.online_char_ids("ROOM1234") == {"c2", "pnj1"}

    assert RoomManager().online_char_ids("EMPTY") == set()
