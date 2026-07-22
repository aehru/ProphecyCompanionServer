import time

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now_ms() -> int:
    """Epoch milliseconds — matches the app's timestamp unit."""
    return int(time.time() * 1000)


class Base(DeclarativeBase):
    pass


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The join capability (external handle). The server's `id` stays internal.
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    # Only the hash of the GM's portable token is stored — never the token.
    gm_token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms)


class Projection(Base):
    """The latest shared view of one character in one campaign. Keyed by the
    character's portable UUID so it survives the player changing devices. Latest
    only — a new share UPSERTs this row (no history)."""

    __tablename__ = "projections"

    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    # = characters.uuid on the app side.
    char_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # SharedCharacter as opaque JSON (validated loosely; stored verbatim).
    payload: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=now_ms, onupdate=now_ms)
    # v2: role of the socket that shared this entry ("gm" = a GM-run PNJ). A
    # re-share by the other role flips it (the UPSERT sets it on every write).
    owner: Mapped[str] = mapped_column(String(8), nullable=False, server_default="player")
