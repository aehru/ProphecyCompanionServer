"""Wire contract with the app. Fields are snake_case in Python but serialize to
camelCase on the wire (`gm_token` <-> `gmToken`) to match the TypeScript client.
Mirrors src/lib/character-share.ts + docs/campaign-protocol.md."""

from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# --- SharedCharacter (mirror of the app's zod schema) ------------------------
class Pool(Wire):
    current: int
    max: int


class Initiative(Wire):
    max: int
    values: list[int]


class SharedSkill(Wire):
    name: str
    attribut: str
    value: int
    parent_name: str | None = None
    spec_label: str | None = None


class SharedEffect(Wire):
    label: str
    target: str
    value: int
    duration_unit: str
    duration_remaining: int


class SharedCharacter(Wire):
    nom: str
    caracteristiques: dict[str, int]
    attributs: dict[str, int]
    tendances: dict[str, int]
    wounds: dict[str, Pool]
    resources: dict[str, Pool]
    initiative: Initiative
    conditions: str
    # v2 (SHARED_SCHEMA_VERSION=2): trained skills (value>0) + active (non-expired)
    # bonus/malus effects. Additive — the projection is still stored opaquely.
    skills: list[SharedSkill] = []
    effects: list[SharedEffect] = []


# --- REST --------------------------------------------------------------------
class CreateCampaignIn(Wire):
    name: str
    gm_token: str


class CreateCampaignOut(Wire):
    campaign_id: int
    code: str


class DeleteCampaignIn(Wire):
    gm_token: str


# --- WebSocket: client -> server ---------------------------------------------
class Hello(Wire):
    # v2: the hello identifies the DEVICE/SESSION, not a character. Kept as a
    # plain int (not Literal[2]) so a v1 hello parses and gets a clear
    # `unsupported_version` error from ws.py instead of an opaque `bad_hello`.
    v: int = 1
    type: Literal["hello"]
    role: Literal["gm", "player"]
    code: str
    gm_token: str | None = None


class Share(Wire):
    v: int = 2
    type: Literal["share"]
    char_id: str
    # Tolerant reader (docs/campaign-protocol.md §4): the projection is stored as
    # opaque JSON, not validated against SharedCharacter, so an older server
    # accepts a newer app's additive fields. Only "is a JSON object" is enforced.
    character: dict


class Unshare(Wire):
    v: int = 2
    type: Literal["unshare"]
    char_id: str


# --- WebSocket: server -> client ---------------------------------------------
class CampaignInfo(Wire):
    code: str
    name: str


class Welcome(Wire):
    v: int = 2
    type: Literal["welcome"] = "welcome"
    campaign: CampaignInfo
    role: str


class RosterEntry(Wire):
    char_id: str
    character: dict  # the stored SharedCharacter, passed through verbatim
    online: bool
    updated_at: int
    # v2: who shared this entry — lets the GM UI badge their own PNJs.
    owner: Literal["gm", "player"]


class Roster(Wire):
    v: int = 2
    type: Literal["roster"] = "roster"
    characters: list[RosterEntry]


class Presence(Wire):
    v: int = 2
    type: Literal["presence"] = "presence"
    char_id: str
    online: bool


class Update(Wire):
    v: int = 2
    type: Literal["update"] = "update"
    char_id: str
    character: dict
    updated_at: int
    owner: Literal["gm", "player"]


class Remove(Wire):
    v: int = 2
    type: Literal["remove"] = "remove"
    char_id: str


class Pong(Wire):
    v: int = 2
    type: Literal["pong"] = "pong"


class ErrorMsg(Wire):
    v: int = 2
    type: Literal["error"] = "error"
    code: str
    message: str
