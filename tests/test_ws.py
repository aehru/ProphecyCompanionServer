import json
import sqlite3
import time
from typing import Any

from starlette.testclient import TestClient

from tests.conftest import make_campaign

TOKEN = "gm-secret-token"


def _hello_gm(code: str, token: str = TOKEN) -> dict[str, Any]:
    return {"v": 2, "type": "hello", "role": "gm", "code": code, "gmToken": token}


def _hello_player(code: str) -> dict[str, Any]:
    # v2: the hello identifies the session — characters arrive via `share`.
    return {"v": 2, "type": "hello", "role": "player", "code": code}


def test_gm_hello_gets_welcome_then_empty_roster(client: TestClient) -> None:
    code = make_campaign(client, name="Table A")["code"]
    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        welcome = gm.receive_json()
        roster = gm.receive_json()
    assert welcome["type"] == "welcome"
    assert welcome["campaign"] == {"code": code, "name": "Table A"}
    assert roster["type"] == "roster"
    assert roster["characters"] == []


def test_gm_bad_token_rejected(client: TestClient) -> None:
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as ws:
        ws.send_json(_hello_gm(code, token="nope"))
        err = ws.receive_json()
    assert err["type"] == "error"
    assert err["code"] == "forbidden"


def test_unknown_campaign_rejected(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json(_hello_gm("ZZZZZZZZ"))
        err = ws.receive_json()
    assert err["type"] == "error"
    assert err["code"] == "no_campaign"


def test_v1_hello_rejected_unsupported_version(client: TestClient) -> None:
    """The v2 break is loud: a stale app gets a dedicated error code, not a
    confusing bad_hello or silently wrong semantics."""
    code = make_campaign(client)["code"]
    for hello in (
        {"v": 1, "type": "hello", "role": "player", "code": code, "charId": "char-uuid-1"},
        {"type": "hello", "role": "gm", "code": code, "gmToken": TOKEN},  # missing v -> 1
    ):
        with client.websocket_connect("/ws") as ws:
            ws.send_json(hello)
            err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "unsupported_version"


def test_share_marks_online_and_disconnect_reports_offline(client: TestClient) -> None:
    """v2 presence: a player hello is silent (no character yet); the first
    `share` doubles as presence (the GM folds an update into online)."""
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        gm.receive_json()  # welcome
        gm.receive_json()  # empty roster

        with client.websocket_connect("/ws") as player:
            player.send_json(_hello_player(code))
            assert player.receive_json()["type"] == "welcome"

            # Nothing was queued for the GM by the hello itself.
            gm.send_json({"v": 2, "type": "ping"})
            assert gm.receive_json() == {"v": 2, "type": "pong"}

            player.send_json(_share("char-uuid-1", {"nom": "Kael"}))
            update = gm.receive_json()
            assert update["type"] == "update"
            assert update["charId"] == "char-uuid-1"

        offline = gm.receive_json()
        assert offline == {"v": 2, "type": "presence", "charId": "char-uuid-1", "online": False}


def test_reconnect_does_not_report_the_slot_offline(client: TestClient) -> None:
    """A second socket re-sharing the same charId takes over the slot: when the
    FIRST one closes afterwards, the GM must not be told the character went
    offline — someone is still holding it. This is the reconnect race a mobile
    client hits on screen lock / network blip."""
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        gm.receive_json()  # welcome
        gm.receive_json()  # empty roster

        old = client.websocket_connect("/ws")
        old.__enter__()
        old.send_json(_hello_player(code))
        old.receive_json()  # welcome
        old.send_json(_share("char-uuid-1", {"nom": "Kael"}))
        assert gm.receive_json()["type"] == "update"

        # The reconnect lands (and re-shares) while the old socket is still
        # registered.
        with client.websocket_connect("/ws") as new:
            new.send_json(_hello_player(code))
            new.receive_json()  # welcome
            new.send_json(_share("char-uuid-1", {"nom": "Kael"}))
            assert gm.receive_json()["type"] == "update"

            # Now the stale socket finally closes — this must stay silent.
            old.__exit__(None, None, None)

            # Prove nothing was queued for the GM: a fresh ping round-trips first.
            new.send_json({"v": 2, "type": "ping"})
            assert new.receive_json()["type"] == "pong"
            gm.send_json({"v": 2, "type": "ping"})
            assert gm.receive_json() == {"v": 2, "type": "pong"}

        # Only the LAST holder leaving reports the character offline.
        assert gm.receive_json() == {
            "v": 2,
            "type": "presence",
            "charId": "char-uuid-1",
            "online": False,
        }


def test_roster_reflects_a_stored_projection(client: TestClient, db_file: str) -> None:
    body = make_campaign(client)
    # Seed the projection row directly (owner falls back to its server default).
    con = sqlite3.connect(db_file)
    con.execute(
        "INSERT INTO projections (campaign_id, char_id, payload, updated_at) VALUES (?, ?, ?, ?)",
        (body["campaignId"], "char-uuid-1", json.dumps({"nom": "Kael"}), int(time.time() * 1000)),
    )
    con.commit()
    con.close()

    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(body["code"]))
        gm.receive_json()  # welcome
        roster = gm.receive_json()

    assert len(roster["characters"]) == 1
    entry = roster["characters"][0]
    assert entry["charId"] == "char-uuid-1"
    assert entry["character"] == {"nom": "Kael"}
    assert entry["online"] is False
    assert entry["owner"] == "player"


def test_ping_pong(client: TestClient) -> None:
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        gm.receive_json()
        gm.receive_json()
        gm.send_json({"v": 2, "type": "ping"})
        assert gm.receive_json()["type"] == "pong"


def _share(char_id: str, character: dict[str, Any]) -> dict[str, Any]:
    return {"v": 2, "type": "share", "charId": char_id, "character": character}


def test_share_streams_update_to_gm_and_persists(client: TestClient) -> None:
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        gm.receive_json()  # welcome
        gm.receive_json()  # empty roster

        with client.websocket_connect("/ws") as player:
            player.send_json(_hello_player(code))
            player.receive_json()  # welcome

            player.send_json(_share("char-uuid-1", {"nom": "Kael", "conditions": ""}))
            update = gm.receive_json()

        gm.receive_json()  # presence offline

    assert update["type"] == "update"
    assert update["charId"] == "char-uuid-1"
    assert update["character"] == {"nom": "Kael", "conditions": ""}
    assert update["owner"] == "player"
    assert isinstance(update["updatedAt"], int)

    # Persisted: a fresh GM connection replays it in the roster (disconnect
    # does NOT purge — only unshare does).
    with client.websocket_connect("/ws") as gm2:
        gm2.send_json(_hello_gm(code))
        gm2.receive_json()
        roster = gm2.receive_json()
    assert [e["charId"] for e in roster["characters"]] == ["char-uuid-1"]
    assert roster["characters"][0]["character"] == {"nom": "Kael", "conditions": ""}


def test_share_is_latest_only_upsert(client: TestClient) -> None:
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        gm.receive_json()
        gm.receive_json()

        with client.websocket_connect("/ws") as player:
            player.send_json(_hello_player(code))
            player.receive_json()

            player.send_json(_share("char-uuid-1", {"nom": "Kael", "pv": 10}))
            gm.receive_json()
            player.send_json(_share("char-uuid-1", {"nom": "Kael", "pv": 7}))
            second = gm.receive_json()

    assert second["character"] == {"nom": "Kael", "pv": 7}
    with client.websocket_connect("/ws") as gm2:
        gm2.send_json(_hello_gm(code))
        gm2.receive_json()
        roster = gm2.receive_json()
    assert len(roster["characters"]) == 1
    assert roster["characters"][0]["character"] == {"nom": "Kael", "pv": 7}


def test_player_shares_two_chars_one_socket(client: TestClient) -> None:
    """v2 core: one socket may hold N roster slots; ALL of them flip offline
    when that socket goes away."""
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        gm.receive_json()
        gm.receive_json()

        with client.websocket_connect("/ws") as player:
            player.send_json(_hello_player(code))
            player.receive_json()

            player.send_json(_share("char-A", {"nom": "Kael"}))
            assert gm.receive_json()["charId"] == "char-A"
            player.send_json(_share("char-B", {"nom": "Garde PNJ"}))
            assert gm.receive_json()["charId"] == "char-B"

        # Both characters go offline (set iteration — order unspecified).
        offline = {gm.receive_json()["charId"], gm.receive_json()["charId"]}
        assert offline == {"char-A", "char-B"}

    with client.websocket_connect("/ws") as gm2:
        gm2.send_json(_hello_gm(code))
        gm2.receive_json()
        roster = gm2.receive_json()
    assert {e["charId"] for e in roster["characters"]} == {"char-A", "char-B"}


def test_gm_shares_pnj(client: TestClient) -> None:
    """v2: the GM shares NPCs over the same authenticated socket; entries are
    stamped owner=gm on the update and in the persisted roster."""
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        gm.receive_json()
        gm.receive_json()
        gm.send_json(_share("pnj-uuid-1", {"nom": "Bandit"}))
        update = gm.receive_json()  # echoed to every GM socket, including self

    assert update["type"] == "update"
    assert update["charId"] == "pnj-uuid-1"
    assert update["owner"] == "gm"

    with client.websocket_connect("/ws") as gm2:
        gm2.send_json(_hello_gm(code))
        gm2.receive_json()
        roster = gm2.receive_json()
    assert len(roster["characters"]) == 1
    assert roster["characters"][0]["owner"] == "gm"


def test_gm_kick_removes_any_entry_and_reshare_readds(client: TestClient) -> None:
    """v2: unshare is unrestricted — the GM's kick. Purge only: the player's
    next share re-adds the entry (no ban)."""
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        gm.receive_json()
        gm.receive_json()

        with client.websocket_connect("/ws") as player:
            player.send_json(_hello_player(code))
            player.receive_json()
            player.send_json(_share("char-uuid-1", {"nom": "Kael"}))
            gm.receive_json()  # update

            gm.send_json({"v": 2, "type": "unshare", "charId": "char-uuid-1"})
            removed = gm.receive_json()
            assert removed == {"v": 2, "type": "remove", "charId": "char-uuid-1"}

            # Re-share re-adds, back under player ownership.
            player.send_json(_share("char-uuid-1", {"nom": "Kael"}))
            readd = gm.receive_json()
            assert readd["type"] == "update"
            assert readd["owner"] == "player"

        gm.receive_json()  # presence offline


def test_owner_flips_on_reshare_by_other_role(client: TestClient) -> None:
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        gm.receive_json()
        gm.receive_json()

        with client.websocket_connect("/ws") as player:
            player.send_json(_hello_player(code))
            player.receive_json()
            player.send_json(_share("char-uuid-1", {"nom": "Kael"}))
            assert gm.receive_json()["owner"] == "player"

            # The GM takes the slot over (e.g. an NPC handed back to the table).
            gm.send_json(_share("char-uuid-1", {"nom": "Kael"}))
            assert gm.receive_json()["owner"] == "gm"

        # No presence-offline on the player's exit: the GM's share claimed the
        # charId too, so the union still holds it. Ping proves nothing queued.
        gm.send_json({"v": 2, "type": "ping"})
        assert gm.receive_json() == {"v": 2, "type": "pong"}

    with client.websocket_connect("/ws") as gm2:
        gm2.send_json(_hello_gm(code))
        gm2.receive_json()
        roster = gm2.receive_json()
    assert roster["characters"][0]["owner"] == "gm"


def test_unshare_purges_and_notifies_gm(client: TestClient) -> None:
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        gm.receive_json()
        gm.receive_json()

        with client.websocket_connect("/ws") as player:
            player.send_json(_hello_player(code))
            player.receive_json()

            player.send_json(_share("char-uuid-1", {"nom": "Kael"}))
            gm.receive_json()  # update
            player.send_json({"v": 2, "type": "unshare", "charId": "char-uuid-1"})
            removed = gm.receive_json()

    assert removed == {"v": 2, "type": "remove", "charId": "char-uuid-1"}
    with client.websocket_connect("/ws") as gm2:
        gm2.send_json(_hello_gm(code))
        gm2.receive_json()
        roster = gm2.receive_json()
    assert roster["characters"] == []


def test_oversized_frame_rejected(client: TestClient) -> None:
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as player:
        player.send_json(_hello_player(code))
        player.receive_json()
        big = "x" * (64 * 1024 + 1)
        player.send_json(_share("char-uuid-1", {"nom": big}))
        err = player.receive_json()
    assert err["type"] == "error"
    assert err["code"] == "too_big"


def test_projection_cap_blocks_new_slots_not_updates(client: TestClient) -> None:
    # PCS_MAX_PROJECTIONS_PER_CAMPAIGN=3 (conftest).
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as player:
        player.send_json(_hello_player(code))
        player.receive_json()
        for i in range(3):
            player.send_json(_share(f"char-{i}", {"nom": f"P{i}"}))
        # Barrier: frames are handled in order, so pong proves the shares were
        # processed before we move on (no fire-and-forget race).
        player.send_json({"v": 2, "type": "ping"})
        assert player.receive_json()["type"] == "pong"

        # Slot 4 is refused…
        player.send_json(_share("char-overflow", {"nom": "Trop"}))
        err = player.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "campaign_full"

        # …but updating an existing slot at capacity still passes.
        player.send_json(_share("char-0", {"nom": "P0", "pv": 5}))
        player.send_json({"v": 2, "type": "ping"})
        assert player.receive_json()["type"] == "pong"  # no error frame came first

    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        gm.receive_json()
        roster = gm.receive_json()
    assert len(roster["characters"]) == 3
    by_id = {e["charId"]: e["character"] for e in roster["characters"]}
    assert by_id["char-0"] == {"nom": "P0", "pv": 5}


def test_invalid_share_payload_rejected(client: TestClient) -> None:
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as player:
        player.send_json(_hello_player(code))
        player.receive_json()
        # `character` must be a JSON object.
        player.send_json({"v": 2, "type": "share", "charId": "char-uuid-1", "character": 42})
        err = player.receive_json()
    assert err["type"] == "error"
    assert err["code"] == "bad_share"
