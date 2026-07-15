from starlette.testclient import TestClient

from tests.conftest import make_campaign


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_create_returns_code_and_id(client: TestClient) -> None:
    body = make_campaign(client, name="La Nuit des Dragons")
    assert isinstance(body["campaignId"], int)
    assert isinstance(body["code"], str)
    assert len(body["code"]) == 8


def test_codes_are_distinct(client: TestClient) -> None:
    a = make_campaign(client)["code"]
    b = make_campaign(client)["code"]
    assert a != b


def test_delete_with_right_token(client: TestClient) -> None:
    body = make_campaign(client, token="right")
    r = client.request("DELETE", f"/campaigns/{body['code']}", json={"gmToken": "right"})
    assert r.status_code == 204


def test_delete_wrong_token_forbidden(client: TestClient) -> None:
    body = make_campaign(client, token="right")
    r = client.request("DELETE", f"/campaigns/{body['code']}", json={"gmToken": "wrong"})
    assert r.status_code == 403


def test_delete_unknown_code_not_found(client: TestClient) -> None:
    r = client.request("DELETE", "/campaigns/ZZZZZZZZ", json={"gmToken": "whatever"})
    assert r.status_code == 404
