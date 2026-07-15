import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Campaign

# Crockford base32 — no I, L, O, U (avoids ambiguity and accidental words).
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def gen_code(length: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def hash_token(token: str) -> str:
    """SHA-256 hex. A fast hash is correct here: the GM token is a random 128-bit
    value, so a slow password KDF (bcrypt/argon2) would guard entropy it doesn't
    have. Never compare the result yourself — go through `verify_token`."""
    return hashlib.sha256(token.encode()).hexdigest()


def verify_token(token: str, token_hash: str) -> bool:
    """THE one way to check a presented GM token against the stored hash —
    timing-safe, and a single choke point so REST and WS can't drift apart."""
    return secrets.compare_digest(hash_token(token), token_hash)


async def gen_unique_code(session: AsyncSession, length: int) -> str:
    for _ in range(10):
        code = gen_code(length)
        clash = await session.scalar(select(Campaign.id).where(Campaign.code == code))
        if clash is None:
            return code
    raise RuntimeError("could not allocate a unique campaign code")
