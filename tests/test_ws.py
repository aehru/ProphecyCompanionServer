import json
import sqlite3
import time

from tests.conftest import make_campaign

TOKEN = "gm-secret-token"


def _hello_gm(code, token=TOKEN):
    return {"v": 1, "type": "hello", "role": "gm", "code": code, "gmToken": token}


def _hello_player(code, char_id):
    return {"v": 1, "type": "hello", "role": "player", "code": code, "charId": char_id}


def test_gm_hello_gets_welcome_then_empty_roster(client):
    code = make_campaign(client, name="Table A")["code"]
    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        welcome = gm.receive_json()
        roster = gm.receive_json()
    assert welcome["type"] == "welcome"
    assert welcome["campaign"] == {"code": code, "name": "Table A"}
    assert roster["type"] == "roster"
    assert roster["characters"] == []


def test_gm_bad_token_rejected(client):
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as ws:
        ws.send_json(_hello_gm(code, token="nope"))
        err = ws.receive_json()
    assert err["type"] == "error"
    assert err["code"] == "forbidden"


def test_unknown_campaign_rejected(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json(_hello_gm("ZZZZZZZZ"))
        err = ws.receive_json()
    assert err["type"] == "error"
    assert err["code"] == "no_campaign"


def test_player_join_and_leave_pings_gm_presence(client):
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


def test_roster_reflects_a_stored_projection(client, db_file):
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


def test_ping_pong(client):
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as gm:
        gm.send_json(_hello_gm(code))
        gm.receive_json()
        gm.receive_json()
        gm.send_json({"v": 1, "type": "ping"})
        assert gm.receive_json()["type"] == "pong"


def test_share_is_unsupported_in_phase_1(client):
    code = make_campaign(client)["code"]
    with client.websocket_connect("/ws") as player:
        player.send_json(_hello_player(code, "char-uuid-1"))
        player.receive_json()  # welcome
        player.send_json({"v": 1, "type": "share", "charId": "char-uuid-1", "character": {}})
        err = player.receive_json()
    assert err["type"] == "error"
    assert err["code"] == "unsupported"
