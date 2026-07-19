"""Stdout logging setup + secret redaction helpers.

Plain single-line records to stdout only (12-factor: the container runtime owns
rotation and shipping). Event lines carry a `key=value` tail so a plain grep
still yields fields.

NOTHING that grants access may reach a log line. Two values are capabilities,
not identifiers:
  * the campaign join code — whoever holds it joins the campaign, so log the
    internal `Campaign.id` instead (see `models.Campaign.code`);
  * `char_id` — the charUuid is the WRITE capability for a roster slot
    (see `ws._handle_share`), so it is only ever logged truncated.
Character payloads and GM tokens are never logged at all.
"""

import logging
import sys
from typing import Any

# Enough to tell two slots apart in a log, far too little to forge one.
_CHAR_ID_PREFIX = 6

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
# ISO-8601-ish and sortable; %(asctime)s default appends milliseconds.
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def configure_logging(level: str) -> None:
    """Point the root logger at stdout. `force` replaces any handler uvicorn
    installed first, so app and server records share one format."""
    logging.basicConfig(
        level=level.upper(),
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
        stream=sys.stdout,
        force=True,
    )


def mask_char_id(char_id: str | None) -> str:
    """Truncate a charUuid to a non-forgeable prefix."""
    if not char_id:
        return "-"
    # ASCII only: a Windows console defaults to cp1252 and mangles "…".
    return f"{char_id[:_CHAR_ID_PREFIX]}~"


def event(name: str, **fields: Any) -> str:
    """Build one log message: an event name plus a `key=value` tail.

    Values are rendered bare (no quoting) — every call site passes ints, bools
    or already-masked short strings, so there is no whitespace to escape.
    """
    if not fields:
        return name
    tail = " ".join(f"{key}={value}" for key, value in fields.items())
    return f"{name} {tail}"
