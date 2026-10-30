"""Tests for POST /user, GET /user/{id}/watchlists and the user store."""

import json

import pytest

from user_store import DuplicateUser, InMemoryUserStore, UserNotFound


def _register(client, user_id):
    resp = client.post("/user", json={"userId": user_id})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_watchlist(client, user, name, symbols=()):
    resp = client.post("/watchlist", json={"userId": user, "watchListName": name, "symbols": list(symbols)})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ==========================================================================
# POST /user
# ==========================================================================
def test_create_user_returns_the_id_and_costs_nothing_upstream(client, fake_yfinance):
    resp = client.post("/user", json={"userId": " alice "})

    assert resp.status_code == 201
    assert json.loads(resp.text) == {"userId": "alice"}  # trimmed
    assert fake_yfinance.calls == []


@pytest.mark.parametrize("again", ["alice", "Alice", "ALICE ", " alice"])
def test_create_user_twice_is_409_case_insensitive(client, again):
    _register(client, "alice")

    resp = client.post("/user", json={"userId": again})

    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ({}, "userId"),
        ({"userId": ""}, "blank"),
        ({"userId": "   "}, "blank"),
        ({"userId": "a b"}, "whitespace"),
        ({"userId": "a/b"}, "whitespace or '/'"),
        ({"userId": "x" * 101}, "longer than 100"),
        ({"userId": 42}, "userId"),
        ({"userId": None}, "userId"),
    ],
)
def test_create_user_rejects_bad_ids_with_422(client, payload, fragment):
    resp = client.post("/user", json=payload)

    assert resp.status_code == 422, resp.text
    assert fragment in resp.text


# ==========================================================================
# GET /user/{id}/watchlists
# ==========================================================================
def test_list_watchlists_of_a_new_user_is_empty(client):
    _register(client, "alice")

    resp = client.get("/user/alice/watchlists")

    assert resp.status_code == 200
    assert json.loads(resp.text) == {"userId": "alice", "count": 0, "watchlists": []}


def test_list_watchlists_shows_ids_and_names_oldest_first_without_upstream_calls(client, fake_yfinance):
    _register(client, "alice")
    tech = _create_watchlist(client, "alice", "Tech", ["AAPL", "MSFT"])
    etfs = _create_watchlist(client, "alice", "ETFs", ["VTI"])
    _create_watchlist(client, "u1", "Other user's")

    resp = client.get("/user/alice/watchlists")

    assert resp.status_code == 200
    body = resp.json()
    assert body["userId"] == "alice" and body["count"] == 2
    assert body["watchlists"] == [
        {"watchListId": tech["watchListId"], "watchListName": "Tech"},
        {"watchListId": etfs["watchListId"], "watchListName": "ETFs"},
    ]
    assert fake_yfinance.calls == []  # no snapshots in the listing


def test_list_watchlists_matches_the_user_case_insensitively_and_echoes_the_registered_id(client):
    _register(client, "Alice")
    _create_watchlist(client, "ALICE", "Tech")

    for path in ("/user/alice/watchlists", "/user/ALICE/watchlists", "/user/Alice/watchlists"):
        body = client.get(path).json()
        assert body["userId"] == "Alice" and body["count"] == 1


def test_list_watchlists_follows_rename_and_delete(client):
    _register(client, "alice")
    a = _create_watchlist(client, "alice", "A")["watchListId"]
    b = _create_watchlist(client, "alice", "B")["watchListId"]

    client.put("/watchlist", json={"watchListId": a, "watchListName": "A renamed"})
    client.delete(f"/watchlist/{b}")

    assert client.get("/user/alice/watchlists").json()["watchlists"] == [
        {"watchListId": a, "watchListName": "A renamed"}
    ]


def test_list_watchlists_of_unknown_user_is_404(client):
    resp = client.get("/user/ghost/watchlists")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "User ghost not found."


def test_openapi_lists_the_user_routes():
    from main import app

    paths = app.openapi()["paths"]
    assert set(paths["/user"]) == {"post"}
    assert set(paths["/user/{user_id}/watchlists"]) == {"get"}


# ==========================================================================
# The store on its own
# ==========================================================================
def test_user_store_roundtrip_and_identity():
    store = InMemoryUserStore()

    alice = store.create(" Alice ")
    assert alice.id == "Alice"
    assert store.get("alice") == alice and store.get("ALICE ") == alice
    with pytest.raises(DuplicateUser):
        store.create("alice")
    with pytest.raises(UserNotFound):
        store.get("bob")
    with pytest.raises(ValueError):
        store.create("   ")

    bob = store.create("bob")
    assert [u.id for u in store.list()] == ["Alice", "bob"]
    assert len(store) == 2
    assert store.clear() == 2 and len(store) == 0
    assert bob.created_at >= alice.created_at
