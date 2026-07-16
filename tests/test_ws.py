import json
import sqlite3
import time
from typing import Any

from starlette.testclient import TestClient

from tests.conftest import make_campaign

TOKEN = "gm-secret-token"


def _hello_gm(code: str, token: str = TOKEN) -> dict[str, Any]:
    return {"v": 1, "type": "hello", "role": "gm", "code": code, "gmToken": token}


def _hello_player(code: str, char_id: str) -> dict[str, Any]:
    return {"v": 1, "type": "hello", "role": "player", "code": code, "charId": char_id}


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


def test_player_join_and_leave_pings_gm_presence(client: TestClient) -> None:
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        gm.receive_json()  # welcome
        gm.receive_json()  # empty roster

        with client.websocket_connect("/ws") as player:
            player.send_json(_hello_player(code, "char-uuid-1"))
            assert player.receive_json()["type"] == "welcome"
            online = gm.receive_json()
            assert online == {"v": 1, "type": "presence", "charId": "char-uuid-1", "online": True}

        offline = gm.receive_json()
        assert offline == {"v": 1, "type": "presence", "charId": "char-uuid-1", "online": False}


def test_roster_reflects_a_stored_projection(client: TestClient, db_file: str) -> None:
    body = make_campaign(client)
    # No `share` handler in Phase 1 — seed the projection row directly.
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


def test_ping_pong(client: TestClient) -> None:
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        gm.receive_json()
        gm.receive_json()
        gm.send_json({"v": 1, "type": "ping"})
        assert gm.receive_json()["type"] == "pong"


def _share(char_id: str, character: dict[str, Any]) -> dict[str, Any]:
    return {"v": 1, "type": "share", "charId": char_id, "character": character}


def test_share_streams_update_to_gm_and_persists(client: TestClient) -> None:
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        gm.receive_json()  # welcome
        gm.receive_json()  # empty roster

        with client.websocket_connect("/ws") as player:
            player.send_json(_hello_player(code, "char-uuid-1"))
            player.receive_json()  # welcome
            gm.receive_json()  # presence online

            player.send_json(_share("char-uuid-1", {"nom": "Kael", "conditions": ""}))
            update = gm.receive_json()

        gm.receive_json()  # presence offline

    assert update["type"] == "update"
    assert update["charId"] == "char-uuid-1"
    assert update["character"] == {"nom": "Kael", "conditions": ""}
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
            player.send_json(_hello_player(code, "char-uuid-1"))
            player.receive_json()
            gm.receive_json()  # presence

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


def test_unshare_purges_and_notifies_gm(client: TestClient) -> None:
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        gm.receive_json()
        gm.receive_json()

        with client.websocket_connect("/ws") as player:
            player.send_json(_hello_player(code, "char-uuid-1"))
            player.receive_json()
            gm.receive_json()  # presence

            player.send_json(_share("char-uuid-1", {"nom": "Kael"}))
            gm.receive_json()  # update
            player.send_json({"v": 1, "type": "unshare", "charId": "char-uuid-1"})
            removed = gm.receive_json()

    assert removed == {"v": 1, "type": "remove", "charId": "char-uuid-1"}
    with client.websocket_connect("/ws") as gm2:
        gm2.send_json(_hello_gm(code))
        gm2.receive_json()
        roster = gm2.receive_json()
    assert roster["characters"] == []


def test_share_other_slot_forbidden(client: TestClient) -> None:
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as player:
        player.send_json(_hello_player(code, "char-uuid-1"))
        player.receive_json()
        player.send_json(_share("char-uuid-STOLEN", {"nom": "Voleur"}))
        err = player.receive_json()
    assert err["type"] == "error"
    assert err["code"] == "forbidden"


def test_gm_cannot_share(client: TestClient) -> None:
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        gm.receive_json()
        gm.receive_json()
        gm.send_json(_share("char-uuid-1", {"nom": "MJ"}))
        err = gm.receive_json()
    assert err["type"] == "error"
    assert err["code"] == "forbidden"


def test_oversized_frame_rejected(client: TestClient) -> None:
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as player:
        player.send_json(_hello_player(code, "char-uuid-1"))
        player.receive_json()
        big = "x" * (64 * 1024 + 1)
        player.send_json(_share("char-uuid-1", {"nom": big}))
        err = player.receive_json()
    assert err["type"] == "error"
    assert err["code"] == "too_big"


def test_projection_cap_blocks_new_slots_not_updates(client: TestClient) -> None:
    # PCS_MAX_PROJECTIONS_PER_CAMPAIGN=3 (conftest).
    code = make_campaign(client)["code"]
    for i in range(3):
        with client.websocket_connect("/ws") as player:
            player.send_json(_hello_player(code, f"char-{i}"))
            player.receive_json()
            player.send_json(_share(f"char-{i}", {"nom": f"P{i}"}))
            # Barrier: frames are handled in order, so pong proves the share
            # was processed before we close and move on (no fire-and-forget race).
            player.send_json({"v": 1, "type": "ping"})
            assert player.receive_json()["type"] == "pong"

    with client.websocket_connect("/ws") as player:
        # Slot 4 is refused…
        player.send_json(_hello_player(code, "char-overflow"))
        player.receive_json()
        player.send_json(_share("char-overflow", {"nom": "Trop"}))
        err = player.receive_json()
    assert err["type"] == "error"
    assert err["code"] == "campaign_full"

    with client.websocket_connect("/ws") as player:
        # …but updating an existing slot at capacity still passes.
        player.send_json(_hello_player(code, "char-0"))
        player.receive_json()
        player.send_json(_share("char-0", {"nom": "P0", "pv": 5}))
        player.send_json({"v": 1, "type": "ping"})
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
        player.send_json(_hello_player(code, "char-uuid-1"))
        player.receive_json()
        # `character` must be a JSON object.
        player.send_json({"v": 1, "type": "share", "charId": "char-uuid-1", "character": 42})
        err = player.receive_json()
    assert err["type"] == "error"
    assert err["code"] == "bad_share"
