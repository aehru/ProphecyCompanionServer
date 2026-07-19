"""WebSocket endpoint + in-memory room manager.

`hello` -> `welcome`; the GM gets the persisted `roster` and then a live
`update`/`remove`/`presence` stream; a player pushes its latest projection with
`share` (latest-only UPSERT — no history) and withdraws it with `unshare`.
`ping`/`pong` keeps the socket warm.

Rooms and presence are in-memory only (they die with the process); the durable
truth is the `projections` table, so a GM reconnect replays a whole roster.
"""

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, cast

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.codes import verify_token
from app.config import settings
from app.logging_setup import event, mask_char_id
from app.models import Campaign, Projection, now_ms
from app.schemas import (
    CampaignInfo,
    ErrorMsg,
    Hello,
    Pong,
    Presence,
    Remove,
    Roster,
    RosterEntry,
    Share,
    Unshare,
    Update,
    Welcome,
)

router = APIRouter()
log = logging.getLogger(__name__)


@dataclass(eq=False)
class Member:
    ws: WebSocket
    role: str
    char_id: str | None
    # Carried on the member so room-level logging never has to name the join
    # code (which is the campaign's join capability).
    campaign_id: int
    connected_at: float = field(default_factory=time.monotonic)


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

    async def notify_gms(self, code: str, message: BaseModel) -> None:
        """Broadcast to every live GM. A failed send means a dead socket (app
        backgrounded, network drop mid-write): evict it instead of letting the
        exception propagate into the SENDING player's handler and kill their
        connection. The GM's own endpoint finishes cleanup on its next receive."""
        payload = message.model_dump(by_alias=True)
        for gm in self.gms(code):  # gms() returns a copy — safe to evict while iterating
            try:
                await gm.ws.send_json(payload)
            except Exception:
                # Was silent before: a GM vanishing mid-broadcast is exactly the
                # kind of thing an incident report needs to show.
                log.warning(
                    event(
                        "gm_send_failed",
                        campaign_id=gm.campaign_id,
                        message_type=type(message).__name__,
                    ),
                    exc_info=True,
                )
                self.remove(code, gm)


manager = RoomManager()


async def _send(ws: WebSocket, model: BaseModel) -> None:
    await ws.send_json(model.model_dump(by_alias=True))


async def _error(ws: WebSocket, code: str, message: str) -> None:
    await _send(ws, ErrorMsg(code=code, message=message))


class FrameError(Exception):
    """A frame that can't be handled: too big or not JSON."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def _receive(ws: WebSocket) -> dict:
    """Read one frame, enforcing the size cap BEFORE parsing."""
    raw = await ws.receive_text()
    if len(raw.encode()) > settings.max_message_bytes:
        raise FrameError("too_big", f"Frame exceeds {settings.max_message_bytes} bytes.")
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FrameError("bad_json", "Frame is not valid JSON.") from exc
    if not isinstance(msg, dict):
        raise FrameError("bad_json", "Frame must be a JSON object.")
    return msg


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    code: str | None = None
    member: Member | None = None
    ip = ws.client.host if ws.client else "unknown"
    # Overwritten when the peer hangs up; anything else means we closed it.
    closed_by = "server"
    maker = ws.app.state.sessionmaker
    try:
        # First frame must be a valid `hello`.
        try:
            hello = Hello.model_validate(await _receive(ws))
        except FrameError as fe:
            log.warning(event("bad_frame", stage="hello", reason=fe.code, ip=ip))
            await _error(ws, fe.code, fe.message)
            await ws.close()
            return
        except ValidationError:
            log.warning(event("bad_frame", stage="hello", reason="bad_hello", ip=ip))
            await _error(ws, "bad_hello", "First frame must be a valid hello.")
            await ws.close()
            return

        async with maker() as session:
            campaign = await session.scalar(select(Campaign).where(Campaign.code == hello.code))
            if campaign is None:
                log.warning(event("unknown_campaign", ip=ip, role=hello.role))
                await _error(ws, "no_campaign", "Unknown campaign code.")
                await ws.close()
                return

            if hello.role == "gm":
                if not hello.gm_token or not verify_token(hello.gm_token, campaign.gm_token_hash):
                    log.warning(event("bad_gm_token", campaign_id=campaign.id, ip=ip, via="ws"))
                    await _error(ws, "forbidden", "Bad GM token.")
                    await ws.close()
                    return
            elif not hello.char_id:
                log.warning(event("bad_frame", stage="hello", reason="missing_char_id", ip=ip))
                await _error(ws, "bad_hello", "A player must send charId.")
                await ws.close()
                return

            code = hello.code
            campaign_id = campaign.id
            member = Member(ws=ws, role=hello.role, char_id=hello.char_id, campaign_id=campaign_id)
            manager.add(code, member)
            log.info(
                event(
                    "ws_hello",
                    campaign_id=campaign_id,
                    role=hello.role,
                    char=mask_char_id(hello.char_id),
                    ip=ip,
                )
            )
            info = CampaignInfo(code=campaign.code, name=campaign.name)
            await _send(ws, Welcome(campaign=info, role=hello.role))

            if hello.role == "gm":
                await _send(ws, await _build_roster(session, campaign.id, code))
            else:
                await manager.notify_gms(code, Presence(char_id=hello.char_id, online=True))

        # Main loop.
        while True:
            try:
                msg = await _receive(ws)
            except FrameError as fe:
                log.warning(
                    event("bad_frame", stage="loop", reason=fe.code, campaign_id=campaign_id)
                )
                await _error(ws, fe.code, fe.message)
                continue
            match msg.get("type"):
                case "ping":
                    await _send(ws, Pong())
                case "share":
                    await _handle_share(ws, maker, member, campaign_id, code, msg)
                case "unshare":
                    await _handle_unshare(ws, maker, member, campaign_id, code, msg)
                case _:
                    # The type itself is client-controlled text: keep it out of
                    # the log line and off the `key=value` tail.
                    log.warning(
                        event(
                            "bad_frame",
                            stage="loop",
                            reason="unknown_type",
                            campaign_id=campaign_id,
                        )
                    )
                    await _error(ws, "unknown_type", f"Unknown message type: {msg.get('type')!r}.")
    except WebSocketDisconnect:
        closed_by = "client"
    finally:
        if code and member:
            manager.remove(code, member)
            log.info(
                event(
                    "ws_closed",
                    campaign_id=member.campaign_id,
                    role=member.role,
                    char=mask_char_id(member.char_id),
                    closed_by=closed_by,
                    duration_s=round(time.monotonic() - member.connected_at, 1),
                )
            )
            if member.role == "player" and member.char_id is not None:
                await manager.notify_gms(code, Presence(char_id=member.char_id, online=False))


async def _handle_share(
    ws: WebSocket,
    maker: async_sessionmaker[AsyncSession],
    member: Member,
    campaign_id: int,
    code: str,
    msg: dict,
) -> None:
    """UPSERT the character's latest projection and stream it to the GM."""
    try:
        share = Share.model_validate(msg)
    except ValidationError:
        log.warning(event("bad_frame", stage="share", reason="bad_share", campaign_id=campaign_id))
        await _error(ws, "bad_share", "Invalid share message.")
        return
    # The hello bound this socket to ONE roster slot (charUuid = the write
    # capability, docs §3/§8). A GM, or a player naming another slot, is refused.
    if member.role != "player" or share.char_id != member.char_id:
        log.warning(
            event(
                "share_forbidden",
                campaign_id=campaign_id,
                role=member.role,
                joined_as=mask_char_id(member.char_id),
                targeted=mask_char_id(share.char_id),
            )
        )
        await _error(ws, "forbidden", "You can only share the character you joined with.")
        return

    ts = now_ms()
    payload = json.dumps(share.character, ensure_ascii=False, separators=(",", ":"))
    async with maker() as session:
        # A NEW slot is refused once the campaign is at capacity; an update of an
        # existing slot always passes (the cap bounds rows, not writes).
        exists = await session.scalar(
            select(Projection.char_id).where(
                Projection.campaign_id == campaign_id, Projection.char_id == share.char_id
            )
        )
        if exists is None:
            count = await session.scalar(
                select(func.count())
                .select_from(Projection)
                .where(Projection.campaign_id == campaign_id)
            )
            if (count or 0) >= settings.max_projections_per_campaign:
                log.warning(
                    event(
                        "campaign_full",
                        campaign_id=campaign_id,
                        slots=count or 0,
                        char=mask_char_id(share.char_id),
                    )
                )
                await _error(ws, "campaign_full", "This campaign's roster is full.")
                return
        # SQLite-dialect upsert — matches the shipped engine. A postgres deploy
        # (see config.database_url) needs the pg dialect's insert here.
        stmt = (
            sqlite_insert(Projection)
            .values(campaign_id=campaign_id, char_id=share.char_id, payload=payload, updated_at=ts)
            .on_conflict_do_update(
                index_elements=[Projection.campaign_id, Projection.char_id],
                set_={"payload": payload, "updated_at": ts},
            )
        )
        await session.execute(stmt)
        await session.commit()

    # Payload size only — the character sheet itself never reaches a log.
    log.info(
        event(
            "share",
            campaign_id=campaign_id,
            char=mask_char_id(share.char_id),
            new_slot=exists is None,
            bytes=len(payload),
        )
    )
    await manager.notify_gms(
        code, Update(char_id=share.char_id, character=share.character, updated_at=ts)
    )


async def _handle_unshare(
    ws: WebSocket,
    maker: async_sessionmaker[AsyncSession],
    member: Member,
    campaign_id: int,
    code: str,
    msg: dict,
) -> None:
    """Purge the character's projection (right-to-erasure) and tell the GM."""
    try:
        unshare = Unshare.model_validate(msg)
    except ValidationError:
        log.warning(
            event("bad_frame", stage="unshare", reason="bad_unshare", campaign_id=campaign_id)
        )
        await _error(ws, "bad_unshare", "Invalid unshare message.")
        return
    if member.role != "player" or unshare.char_id != member.char_id:
        log.warning(
            event(
                "unshare_forbidden",
                campaign_id=campaign_id,
                role=member.role,
                joined_as=mask_char_id(member.char_id),
                targeted=mask_char_id(unshare.char_id),
            )
        )
        await _error(ws, "forbidden", "You can only unshare the character you joined with.")
        return

    async with maker() as session:
        result = await session.execute(
            delete(Projection).where(
                Projection.campaign_id == campaign_id, Projection.char_id == unshare.char_id
            )
        )
        await session.commit()

    # DML always yields a CursorResult (which owns rowcount); the async execute()
    # stub is just typed too widely as Result.
    deleted = cast(CursorResult[Any], result).rowcount
    log.info(
        event(
            "unshare",
            campaign_id=campaign_id,
            char=mask_char_id(unshare.char_id),
            deleted=deleted,
        )
    )
    # Idempotent: unsharing a slot that holds nothing is a no-op, not an error.
    if deleted:
        await manager.notify_gms(code, Remove(char_id=unshare.char_id))


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
