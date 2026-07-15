"""WebSocket endpoint + in-memory room manager.

Phase 1 scope: `hello` -> `welcome`, GM gets the persisted `roster`, players
announce `presence`, and `ping`/`pong` keeps the socket warm. `share`/`unshare`
land in Phase 2 and currently return an explicit `error`.

Rooms and presence are in-memory only (they die with the process); the durable
truth is the `projections` table, so a GM reconnect replays a whole roster.
"""

import json
from collections import defaultdict
from dataclasses import dataclass

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.codes import hash_token
from app.models import Campaign, Projection
from app.schemas import (
    CampaignInfo,
    ErrorMsg,
    Hello,
    Pong,
    Presence,
    Roster,
    RosterEntry,
    Welcome,
)

router = APIRouter()


@dataclass(eq=False)
class Member:
    ws: WebSocket
    role: str
    char_id: str | None


class RoomManager:
    """Live members per campaign code."""

    def __init__(self) -> None:
        self._rooms: dict[str, set[Member]] = defaultdict(set)

    def add(self, code: str, member: Member) -> None:
        self._rooms[code].add(member)

    def remove(self, code: str, member: Member) -> None:
        room = self._rooms.get(code)
        if not room:
            return
        room.discard(member)
        if not room:
            self._rooms.pop(code, None)

    def gms(self, code: str) -> list[Member]:
        return [m for m in self._rooms.get(code, set()) if m.role == "gm"]

    def online_char_ids(self, code: str) -> set[str]:
        return {m.char_id for m in self._rooms.get(code, set()) if m.char_id is not None}

    async def notify_gms(self, code: str, message: dict) -> None:
        for gm in self.gms(code):
            await gm.ws.send_json(message)


manager = RoomManager()


async def _send(ws: WebSocket, model: BaseModel) -> None:
    await ws.send_json(model.model_dump(by_alias=True))


async def _error(ws: WebSocket, code: str, message: str) -> None:
    await _send(ws, ErrorMsg(code=code, message=message))


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    code: str | None = None
    member: Member | None = None
    maker = ws.app.state.sessionmaker
    try:
        # First frame must be a valid `hello`.
        try:
            hello = Hello.model_validate(await ws.receive_json())
        except ValidationError:
            await _error(ws, "bad_hello", "First frame must be a valid hello.")
            await ws.close()
            return

        async with maker() as session:
            campaign = await session.scalar(select(Campaign).where(Campaign.code == hello.code))
            if campaign is None:
                await _error(ws, "no_campaign", "Unknown campaign code.")
                await ws.close()
                return

            if hello.role == "gm":
                if not hello.gm_token or hash_token(hello.gm_token) != campaign.gm_token_hash:
                    await _error(ws, "forbidden", "Bad GM token.")
                    await ws.close()
                    return
            elif not hello.char_id:
                await _error(ws, "bad_hello", "A player must send charId.")
                await ws.close()
                return

            code = hello.code
            member = Member(ws=ws, role=hello.role, char_id=hello.char_id)
            manager.add(code, member)
            info = CampaignInfo(code=campaign.code, name=campaign.name)
            await _send(ws, Welcome(campaign=info, role=hello.role))

            if hello.role == "gm":
                await _send(ws, await _build_roster(session, campaign.id, code))
            else:
                await manager.notify_gms(
                    code, Presence(char_id=hello.char_id, online=True).model_dump(by_alias=True)
                )

        # Main loop.
        while True:
            msg = await ws.receive_json()
            match msg.get("type"):
                case "ping":
                    await _send(ws, Pong())
                case "share" | "unshare":
                    await _error(ws, "unsupported", "share/unshare arrive in Phase 2.")
                case other:
                    await _error(ws, "unknown_type", f"Unknown message type: {other!r}.")
    except WebSocketDisconnect:
        pass
    finally:
        if code and member:
            manager.remove(code, member)
            if member.role == "player" and member.char_id is not None:
                await manager.notify_gms(
                    code, Presence(char_id=member.char_id, online=False).model_dump(by_alias=True)
                )


async def _build_roster(session: AsyncSession, campaign_id: int, code: str) -> Roster:
    rows = (
        await session.scalars(select(Projection).where(Projection.campaign_id == campaign_id))
    ).all()
    online = manager.online_char_ids(code)
    entries = [
        RosterEntry(
            char_id=p.char_id,
            character=json.loads(p.payload),
            online=p.char_id in online,
            updated_at=p.updated_at,
        )
        for p in rows
    ]
    return Roster(characters=entries)
