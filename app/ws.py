"""WebSocket endpoint + in-memory room manager.

`hello` (v2: identifies the session, not a character) -> `welcome`; the GM gets
the persisted `roster` and then a live `update`/`remove`/`presence` stream; any
member pushes projections with `share` (latest-only UPSERT — no history, one
socket may hold N characters) and withdraws them with `unshare`. A GM sharing =
a GM-run PNJ (`owner="gm"` on the wire); a GM unsharing another member's entry =
kick (purge only — the player's next share re-adds it). `ping`/`pong` keeps the
socket warm.

Rooms and presence are in-memory only (they die with the process); the durable
truth is the `projections` table, so a GM reconnect replays a whole roster.
"""

import asyncio
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
    # Carried on the member so room-level logging never has to name the join
    # code (which is the campaign's join capability).
    campaign_id: int
    # v2: the characters this socket currently shares — filled by `share`,
    # drained by `unshare`. Presence derives from the union across members.
    char_ids: set[str] = field(default_factory=set)
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
        return set().union(*(m.char_ids for m in self._rooms.get(code, set())))

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

# Strong refs to detached teardown broadcasts (asyncio only keeps weak ones).
_pending_tasks: set[asyncio.Task[None]] = set()


async def _broadcast_offline(code: str, char_ids: list[str]) -> None:
    for cid in char_ids:
        await manager.notify_gms(code, Presence(char_id=cid, online=False))


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

        # Hard v2 gate — v1 clients (charId-bound hello) get a clear error, not
        # subtly broken multi-share semantics. Checked on the hello only.
        if hello.v != 2:
            log.warning(event("bad_frame", stage="hello", reason="unsupported_version", ip=ip))
            await _error(
                ws, "unsupported_version", "This server speaks protocol v2; update the app."
            )
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

            code = hello.code
            campaign_id = campaign.id
            member = Member(ws=ws, role=hello.role, campaign_id=campaign_id)
            manager.add(code, member)
            log.info(event("ws_hello", campaign_id=campaign_id, role=hello.role, ip=ip))
            info = CampaignInfo(code=campaign.code, name=campaign.name)
            await _send(ws, Welcome(campaign=info, role=hello.role))

            if hello.role == "gm":
                await _send(ws, await _build_roster(session, campaign.id, code))
            # No presence on a player hello — the session owns no character yet;
            # "online" is implied by the first `share` (the GM folds an update
            # into presence).

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
                    chars=len(member.char_ids),
                    closed_by=closed_by,
                    duration_s=round(time.monotonic() - member.connected_at, 1),
                )
            )
            # Only report a character offline if NO other live socket still holds
            # it. A mobile client that reconnects (screen lock, network blip)
            # opens a new socket before the server notices the old half-open one
            # died; the stale socket's cleanup then lands AFTER the new hello and
            # its re-shares. Broadcasting unconditionally would mark a connected,
            # actively-sharing character offline — and nothing would flip it back.
            # No role guard: a GM broadcaster socket's PNJs go offline too.
            still_online = manager.online_char_ids(code)
            offline = sorted(cid for cid in member.char_ids if cid not in still_online)
            if offline:
                # Detached task, NOT awaited: this socket's task is being torn
                # down and may be cancelled at its next suspension point — an
                # await here would silently drop all but the first presence.
                task = asyncio.get_running_loop().create_task(_broadcast_offline(code, offline))
                _pending_tasks.add(task)
                task.add_done_callback(_pending_tasks.discard)


async def _handle_share(
    ws: WebSocket,
    maker: async_sessionmaker[AsyncSession],
    member: Member,
    campaign_id: int,
    code: str,
    msg: dict,
) -> None:
    """UPSERT the character's latest projection and stream it to the GM.

    v2: any authenticated member may share any charId — the join code is the
    room capability, and within a room all members share one write domain (docs
    §security). A GM share is a GM-run PNJ (`owner="gm"`)."""
    try:
        share = Share.model_validate(msg)
    except ValidationError:
        log.warning(event("bad_frame", stage="share", reason="bad_share", campaign_id=campaign_id))
        await _error(ws, "bad_share", "Invalid share message.")
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
            .values(
                campaign_id=campaign_id,
                char_id=share.char_id,
                payload=payload,
                updated_at=ts,
                owner=member.role,
            )
            .on_conflict_do_update(
                index_elements=[Projection.campaign_id, Projection.char_id],
                # owner in set_ too: a re-share by the other role flips it.
                set_={"payload": payload, "updated_at": ts, "owner": member.role},
            )
        )
        await session.execute(stmt)
        await session.commit()

    # This socket now holds the character — presence derives from it.
    member.char_ids.add(share.char_id)

    # Payload size only — the character sheet itself never reaches a log.
    log.info(
        event(
            "share",
            campaign_id=campaign_id,
            char=mask_char_id(share.char_id),
            role=member.role,
            new_slot=exists is None,
            bytes=len(payload),
        )
    )
    await manager.notify_gms(
        code,
        Update(char_id=share.char_id, character=share.character, updated_at=ts, owner=member.role),
    )


async def _handle_unshare(
    ws: WebSocket,
    maker: async_sessionmaker[AsyncSession],
    member: Member,
    campaign_id: int,
    code: str,
    msg: dict,
) -> None:
    """Purge the character's projection (right-to-erasure) and tell the GM.

    v2: any authenticated member may unshare any charId — this is also the GM's
    kick (purge only, no ban: the owner's next share re-adds the entry)."""
    try:
        unshare = Unshare.model_validate(msg)
    except ValidationError:
        log.warning(
            event("bad_frame", stage="unshare", reason="bad_unshare", campaign_id=campaign_id)
        )
        await _error(ws, "bad_unshare", "Invalid unshare message.")
        return

    async with maker() as session:
        result = await session.execute(
            delete(Projection).where(
                Projection.campaign_id == campaign_id, Projection.char_id == unshare.char_id
            )
        )
        await session.commit()

    # Sender no longer holds it (no-op when kicking someone else's entry — the
    # kicked owner's socket keeps its claim, and its eventual disconnect emits a
    # presence for an entry that is already gone: the GM folds it as a no-op).
    member.char_ids.discard(unshare.char_id)

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
            owner="gm" if p.owner == "gm" else "player",
        )
        for p in rows
    ]
    return Roster(characters=entries)
