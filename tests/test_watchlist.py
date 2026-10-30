"""Tests for the watchlist API (POST/GET/PUT/DELETE /watchlist) and the
in-memory store behind it."""

import json
import re
from datetime import datetime, timezone

import pytest

import stock_router
import watchlist_router
from tests.test_snapshot_endpoint import AAPL_INFO, EXPECTED_KEYS
from watchlist_store import ID_LENGTH, DuplicateWatchlistName, InMemoryWatchlistStore, WatchlistNotFound, new_short_id

SHORT_ID = re.compile(rf"^[A-Za-z0-9]{{{ID_LENGTH}}}$")

MSFT_INFO = dict(
    AAPL_INFO, symbol="MSFT", longName="Microsoft Corporation", shortName="Microsoft", displayName="Microsoft",
    regularMarketPrice=410.5, currentPrice=410.4, previousClose=405.0,
)
VTI_INFO = {
    "symbol": "VTI",
    "regularMarketPrice": 250.0,
    "currency": "USD",
    "language": "en-US",
    "shortName": "Vanguard Total Stock Market ETF",
    "longName": "Vanguard Total Stock Market Index Fund ETF Shares",
    "exchange": "PCX",
    "market": "us_market",
}
KNOWN = {"AAPL": AAPL_INFO, "MSFT": MSFT_INFO, "VTI": VTI_INFO}


@pytest.fixture
def empty_store(empty_stores):
    return empty_stores.watchlists


@pytest.fixture
def known_symbols(fake_yfinance):
    fake_yfinance.info_for = dict(KNOWN)
    return fake_yfinance


def _create(client, name="Tech", symbols=("AAPL", "MSFT"), user="u1"):
    resp = client.post("/watchlist", json={"userId": user, "watchListName": name, "symbols": list(symbols)})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ==========================================================================
# POST /watchlist
# ==========================================================================
def test_create_returns_summary_with_an_id_and_costs_nothing_upstream(client, fake_yfinance):
    before = stock_router._limiter.remaining()

    resp = client.post("/watchlist", json={"userId": "u1", "watchListName": "Tech", "symbols": ["AAPL", "MSFT"]})

    assert resp.status_code == 201
    body = json.loads(resp.text)
    assert set(body) == {"userId", "watchListName", "watchListId"}
    assert body["userId"] == "u1" and body["watchListName"] == "Tech"
    assert SHORT_ID.match(body["watchListId"]), body["watchListId"]  # short, URL-safe, not a UUID
    assert stock_router._limiter.remaining() == before, "symbols are not looked up on Yahoo at create time"
    assert fake_yfinance.calls == []


def test_create_normalises_symbols_and_name(client, known_symbols):
    body = _create(client, name="  My List ", symbols=[" aapl", "Msft "])
    assert body["watchListName"] == "My List"

    view = client.get(f"/watchlist/{body['watchListId']}").json()
    assert [v["ticker"] for v in view["symbols"].values()] == ["AAPL", "MSFT"]


def test_create_with_no_symbols_is_allowed_and_symbols_defaults_to_empty(client):
    resp = client.post("/watchlist", json={"userId": "u1", "watchListName": "Empty"})
    assert resp.status_code == 201
    view = client.get(f"/watchlist/{resp.json()['watchListId']}").json()
    assert view["symbols"] == {} and view["count"] == 0


def test_create_ids_are_short_unique_and_usable_in_urls(client):
    ids = {_create(client, name=f"list {i}")["watchListId"] for i in range(20)}
    assert len(ids) == 20
    assert all(SHORT_ID.match(i) for i in ids)
    for i in ids:
        assert client.get(f"/watchlist/{i}").status_code == 200


def test_create_for_unregistered_user_is_422_and_stores_nothing(client, empty_store):
    resp = client.post("/watchlist", json={"userId": "ghost", "watchListName": "Tech", "symbols": ["AAPL"]})

    assert resp.status_code == 422
    assert resp.json()["detail"] == "userId 'ghost' does not exist; register it with POST /user first."
    assert len(empty_store) == 0


def test_create_echoes_the_registered_spelling_of_the_user_id(client):
    body = _create(client, user=" U1 ")
    assert body["userId"] == "u1"
    assert client.get(f"/watchlist/{body['watchListId']}").json()["userId"] == "u1"
    assert client.get("/user/u1/watchlists").json()["count"] == 1


def test_create_same_name_for_same_user_is_409_but_other_user_is_fine(client):
    _create(client, name="Tech", user="u1")

    dup = client.post("/watchlist", json={"userId": "u1", "watchListName": " tech ", "symbols": []})
    assert dup.status_code == 409
    assert "already has a watchlist named" in dup.json()["detail"]

    other = client.post("/watchlist", json={"userId": "u2", "watchListName": "Tech", "symbols": []})
    assert other.status_code == 201


@pytest.mark.parametrize("user", ["U1", " u1", "u1 ", "U1 "])
def test_create_same_name_is_409_whatever_the_case_or_spacing_of_the_user_id(client, user):
    _create(client, name="Tech", user="u1")

    resp = client.post("/watchlist", json={"userId": user, "watchListName": "TECH", "symbols": []})

    assert resp.status_code == 409, resp.text


def test_uniqueness_survives_rename_and_delete(client):
    """The (user, name) index follows every write: after a rename the old
    name is free and the new one taken; after a delete the name is free."""
    a = _create(client, name="A")["watchListId"]

    client.put("/watchlist", json={"watchListId": a, "watchListName": "B", "symbols": {}})
    assert client.post("/watchlist", json={"userId": "u1", "watchListName": "b", "symbols": []}).status_code == 409
    b2 = client.post("/watchlist", json={"userId": "u1", "watchListName": "A", "symbols": []})
    assert b2.status_code == 201  # the old name is free again

    client.delete(f"/watchlist/{a}")
    assert client.post("/watchlist", json={"userId": "U1", "watchListName": "B", "symbols": []}).status_code == 201
    # and the survivor still guards its own name
    assert client.post("/watchlist", json={"userId": "u1", "watchListName": "a", "symbols": []}).status_code == 409


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ({"userId": "", "watchListName": "x", "symbols": []}, "userId"),
        ({"userId": "   ", "watchListName": "x", "symbols": []}, "userId"),
        ({"userId": "a b", "watchListName": "x", "symbols": []}, "whitespace"),
        ({"watchListName": "x", "symbols": []}, "userId"),
        ({"userId": "ghost", "watchListName": "x", "symbols": []}, "does not exist"),
        ({"userId": "u1", "watchListName": "", "symbols": []}, "watchListName"),
        ({"userId": "u1", "watchListName": "  ", "symbols": []}, "blank"),
        ({"userId": "u1", "watchListName": "x" * 101, "symbols": []}, "longer than 100"),
        ({"userId": "u1", "symbols": []}, "watchListName"),
        ({"userId": "u1", "watchListName": "x", "symbols": ["AAPL", ""]}, "must not be empty"),
        ({"userId": "u1", "watchListName": "x", "symbols": ["AA PL"]}, "whitespace"),
        ({"userId": "u1", "watchListName": "x", "symbols": ["A/B"]}, "whitespace or '/'"),
        ({"userId": "u1", "watchListName": "x", "symbols": ["A" * 21]}, "longer than 20"),
        ({"userId": "u1", "watchListName": "x", "symbols": ["aapl", "AAPL"]}, "more than once"),
        ({"userId": "u1", "watchListName": "x", "symbols": "AAPL"}, "symbols"),
        ({"userId": "u1", "watchListName": "x", "symbols": [1, 2]}, "symbols"),
    ],
)
def test_create_rejects_bad_input_with_422_and_no_upstream_call(client, fake_yfinance, payload, fragment):
    resp = client.post("/watchlist", json=payload)

    assert resp.status_code == 422, resp.text
    assert fragment in resp.text
    assert fake_yfinance.calls == []


def test_create_caps_the_number_of_symbols(client):
    ok = client.post(
        "/watchlist",
        json={"userId": "u1", "watchListName": "big", "symbols": [f"S{i}" for i in range(watchlist_router.MAX_SYMBOLS)]},
    )
    assert ok.status_code == 201

    too_many = client.post(
        "/watchlist",
        json={"userId": "u1", "watchListName": "bigger", "symbols": [f"S{i}" for i in range(watchlist_router.MAX_SYMBOLS + 1)]},
    )
    assert too_many.status_code == 422
    assert f"at most {watchlist_router.MAX_SYMBOLS} symbols" in too_many.text


# ==========================================================================
# GET /watchlist/{id}
# ==========================================================================
def test_get_returns_one_snapshot_per_position(client, known_symbols):
    wid = _create(client, symbols=["MSFT", "AAPL", "VTI"])["watchListId"]

    resp = client.get(f"/watchlist/{wid}")

    assert resp.status_code == 200
    body = json.loads(resp.text)  # strict parser: no NaN tokens
    assert set(body) == {"watchListId", "userId", "watchListName", "count", "symbols"}
    assert body["watchListId"] == wid and body["userId"] == "u1" and body["watchListName"] == "Tech"
    assert body["count"] == 3
    assert list(body["symbols"]) == ["1", "2", "3"]  # positions, in list order
    assert [s["ticker"] for s in body["symbols"].values()] == ["MSFT", "AAPL", "VTI"]
    for snapshot in body["symbols"].values():
        assert set(snapshot) == EXPECTED_KEYS
    # each entry IS the /stock/{symbol} response
    assert body["symbols"]["2"] == client.get("/stock/AAPL").json()
    assert body["symbols"]["1"]["fulldayPrice"] == 410.5
    assert body["symbols"]["3"]["sector"] is None  # ETF


def test_get_charges_two_upstream_calls_per_symbol_every_time(client, known_symbols):
    wid = _create(client, symbols=["AAPL", "MSFT", "VTI"])["watchListId"]
    before = stock_router._limiter.remaining()

    client.get(f"/watchlist/{wid}")
    after_first = stock_router._limiter.remaining()
    client.get(f"/watchlist/{wid}")

    assert before - after_first == 6, "2 per .info x 3 symbols"
    assert after_first - stock_router._limiter.remaining() == 6, "no cache: paid again"


def test_get_unknown_symbol_is_an_error_entry_not_a_failure(client, known_symbols):
    wid = _create(client, symbols=["AAPL", "NOPE123", "MSFT"])["watchListId"]

    resp = client.get(f"/watchlist/{wid}")

    assert resp.status_code == 200
    symbols = resp.json()["symbols"]
    assert symbols["1"]["ticker"] == "AAPL" and "error" not in symbols["1"]
    assert symbols["2"] == {"ticker": "NOPE123", "error": "Ticker NOPE123 not found."}
    assert symbols["3"]["ticker"] == "MSFT"
    assert resp.json()["count"] == 3


def test_get_dotted_us_share_class_falls_back_to_dash_like_the_snapshot(client, fake_yfinance):
    fake_yfinance.info_for = {"BRK-B": dict(AAPL_INFO, symbol="BRK-B", longName="Berkshire Hathaway Inc.")}
    wid = _create(client, symbols=["brk.b"])["watchListId"]
    before = stock_router._limiter.remaining()

    body = client.get(f"/watchlist/{wid}").json()

    assert body["symbols"]["1"]["ticker"] == "BRK.B" and body["symbols"]["1"]["symbol"] == "BRK-B"
    assert before - stock_router._limiter.remaining() == 4  # the retry is charged


def test_get_429_when_budget_runs_out_part_way(client, known_symbols, monkeypatch):
    wid = _create(client, symbols=["AAPL", "MSFT"])["watchListId"]
    monkeypatch.setattr(stock_router._limiter, "max_calls", 3)  # enough for one .info, not two

    resp = client.get(f"/watchlist/{wid}")

    assert resp.status_code == 429
    assert "budget" in resp.json()["detail"]


def test_get_502_when_upstream_misbehaves(client, fake_yfinance):
    class Broken(fake_yfinance):
        @property
        def info(self):
            raise RuntimeError("boom")

    stock_router.yf.Ticker = Broken
    wid = _create(client, symbols=["AAPL"])["watchListId"]

    resp = client.get(f"/watchlist/{wid}")

    assert resp.status_code == 502
    assert "Upstream data error" in resp.json()["detail"]


def test_get_unknown_id_is_404_without_upstream_call(client, fake_yfinance):
    resp = client.get("/watchlist/does-not-exist")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Watchlist does-not-exist not found."
    assert fake_yfinance.calls == []


# ==========================================================================
# PUT /watchlist
# ==========================================================================
def test_put_replaces_name_and_symbols_and_returns_the_get_view(client, known_symbols):
    wid = _create(client, name="Tech", symbols=["AAPL", "MSFT"])["watchListId"]

    resp = client.put(
        "/watchlist",
        json={"watchListId": wid, "watchListName": "Tech & ETFs", "symbols": {"1": "VTI", "2": "AAPL"}},
    )

    assert resp.status_code == 200, resp.text
    body = json.loads(resp.text)
    assert body == client.get(f"/watchlist/{wid}").json()
    assert body["watchListName"] == "Tech & ETFs"
    assert [s["ticker"] for s in body["symbols"].values()] == ["VTI", "AAPL"]  # MSFT removed, VTI added
    assert body["symbols"]["2"] == client.get("/stock/AAPL").json()


def test_put_sorts_by_position_and_renumbers(client, known_symbols):
    wid = _create(client, symbols=["AAPL"])["watchListId"]

    body = client.put(
        "/watchlist",
        json={"watchListId": wid, "watchListName": "Tech", "symbols": {"30": "VTI", "5": "MSFT", "10": "AAPL"}},
    ).json()

    assert list(body["symbols"]) == ["1", "2", "3"]
    assert [s["ticker"] for s in body["symbols"].values()] == ["MSFT", "AAPL", "VTI"]


def test_put_reorder_only(client, known_symbols):
    wid = _create(client, symbols=["AAPL", "MSFT", "VTI"])["watchListId"]

    body = client.put(
        "/watchlist", json={"watchListId": wid, "watchListName": "Tech", "symbols": {"1": "VTI", "2": "MSFT", "3": "AAPL"}}
    ).json()

    assert [s["ticker"] for s in body["symbols"].values()] == ["VTI", "MSFT", "AAPL"]


def test_put_rename_only_keeps_symbols(client, known_symbols):
    wid = _create(client, name="Tech", symbols=["AAPL", "MSFT"])["watchListId"]

    body = client.put(
        "/watchlist", json={"watchListId": wid, "watchListName": "Renamed", "symbols": {"1": "AAPL", "2": "MSFT"}}
    ).json()

    assert body["watchListName"] == "Renamed"
    assert [s["ticker"] for s in body["symbols"].values()] == ["AAPL", "MSFT"]
    # the rename is persisted
    assert client.get(f"/watchlist/{wid}").json()["watchListName"] == "Renamed"


def test_put_accepts_a_plain_list_too(client, known_symbols):
    wid = _create(client, symbols=["AAPL"])["watchListId"]

    body = client.put("/watchlist", json={"watchListId": wid, "watchListName": "Tech", "symbols": ["msft", "vti"]}).json()

    assert [s["ticker"] for s in body["symbols"].values()] == ["MSFT", "VTI"]


def test_put_with_empty_symbols_empties_the_watchlist(client, known_symbols):
    wid = _create(client, symbols=["AAPL"])["watchListId"]

    for empty in ({}, []):
        body = client.put("/watchlist", json={"watchListId": wid, "watchListName": "Tech", "symbols": empty}).json()
        assert body["symbols"] == {} and body["count"] == 0


def test_put_charges_two_upstream_calls_per_symbol(client, known_symbols):
    wid = _create(client, symbols=[])["watchListId"]
    before = stock_router._limiter.remaining()

    client.put("/watchlist", json={"watchListId": wid, "watchListName": "Tech", "symbols": {"1": "AAPL", "2": "MSFT"}})

    assert before - stock_router._limiter.remaining() == 4


def test_put_unknown_id_is_404_and_nothing_is_created(client, fake_yfinance, empty_store):
    resp = client.put("/watchlist", json={"watchListId": "nope", "watchListName": "x", "symbols": {"1": "AAPL"}})

    assert resp.status_code == 404
    assert len(empty_store) == 0
    assert fake_yfinance.calls == []


def test_put_name_collision_with_another_list_of_the_user_is_409_and_changes_nothing(client, known_symbols):
    a = _create(client, name="A", symbols=["AAPL"])["watchListId"]
    _create(client, name="B", symbols=["MSFT"])
    before = stock_router._limiter.remaining()

    resp = client.put("/watchlist", json={"watchListId": a, "watchListName": "b", "symbols": {"1": "VTI"}})

    assert resp.status_code == 409
    assert stock_router._limiter.remaining() == before
    view = client.get(f"/watchlist/{a}").json()
    assert view["watchListName"] == "A" and [s["ticker"] for s in view["symbols"].values()] == ["AAPL"]


def test_put_keeping_its_own_name_is_not_a_collision(client, known_symbols):
    wid = _create(client, name="Tech", symbols=["AAPL"])["watchListId"]

    resp = client.put("/watchlist", json={"watchListId": wid, "watchListName": "TECH", "symbols": {"1": "AAPL"}})

    assert resp.status_code == 200
    assert resp.json()["watchListName"] == "TECH"


@pytest.mark.parametrize(
    "symbols, fragment",
    [
        ({"0": "AAPL"}, "positive integers"),
        ({"-1": "AAPL"}, "positive integers"),
        ({"a": "AAPL"}, "symbols"),
        ({"1": "AAPL", "2": "aapl"}, "more than once"),
        ({"1": ""}, "must not be empty"),
        ({"1": 5}, "symbols"),
        ("AAPL", "symbols"),
    ],
)
def test_put_rejects_bad_symbols_with_422_and_no_upstream_call(client, known_symbols, symbols, fragment):
    wid = _create(client, symbols=["AAPL"])["watchListId"]

    resp = client.put("/watchlist", json={"watchListId": wid, "watchListName": "Tech", "symbols": symbols})

    assert resp.status_code == 422, resp.text
    assert fragment in resp.text
    assert known_symbols.calls == []
    assert [s["ticker"] for s in client.get(f"/watchlist/{wid}").json()["symbols"].values()] == ["AAPL"]


def test_put_name_only_keeps_symbols(client, known_symbols):
    wid = _create(client, name="Tech", symbols=["AAPL", "MSFT"])["watchListId"]

    resp = client.put("/watchlist", json={"watchListId": wid, "watchListName": "Renamed"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["watchListName"] == "Renamed"
    assert [s["ticker"] for s in body["symbols"].values()] == ["AAPL", "MSFT"]
    assert client.get(f"/watchlist/{wid}").json() == body


def test_put_symbols_only_keeps_name(client, known_symbols):
    wid = _create(client, name="Tech", symbols=["AAPL", "MSFT"])["watchListId"]

    for symbols in ({"1": "VTI", "2": "MSFT"}, ["MSFT", "VTI", "AAPL"], []):
        resp = client.put("/watchlist", json={"watchListId": wid, "symbols": symbols})
        assert resp.status_code == 200, resp.text
        assert resp.json()["watchListName"] == "Tech"
    assert resp.json()["symbols"] == {}  # the last PUT emptied the list without touching the name


def test_put_name_only_still_enforces_uniqueness(client):
    a = _create(client, name="A")["watchListId"]
    _create(client, name="B")

    assert client.put("/watchlist", json={"watchListId": a, "watchListName": "b"}).status_code == 409
    assert client.get(f"/watchlist/{a}").json()["watchListName"] == "A"


def test_put_with_nothing_to_update_is_422(client, fake_yfinance):
    wid = _create(client)["watchListId"]

    for payload in ({"watchListId": wid}, {"watchListId": wid, "watchListName": None, "symbols": None}):
        resp = client.put("/watchlist", json=payload)
        assert resp.status_code == 422, resp.text
        assert "nothing to update" in resp.text
    assert fake_yfinance.calls == []


def test_put_field_validation(client):
    wid = _create(client)["watchListId"]
    assert client.put("/watchlist", json={"watchListName": "x", "symbols": {}}).status_code == 422  # no id
    assert client.put("/watchlist", json={"watchListId": wid, "watchListName": "  "}).status_code == 422  # blank name
    assert client.put("/watchlist", json={"watchListId": wid, "watchListName": "x" * 101}).status_code == 422


# ==========================================================================
# DELETE /watchlist/{id}
# ==========================================================================
def test_delete_then_get_put_delete_are_404(client, fake_yfinance):
    wid = _create(client)["watchListId"]

    resp = client.delete(f"/watchlist/{wid}")

    assert resp.status_code == 200
    assert resp.json() == {"watchListId": wid, "deleted": True}
    assert client.get(f"/watchlist/{wid}").status_code == 404
    assert client.put("/watchlist", json={"watchListId": wid, "watchListName": "x", "symbols": {}}).status_code == 404
    assert client.delete(f"/watchlist/{wid}").status_code == 404
    assert fake_yfinance.calls == []


def test_delete_frees_the_name_for_reuse(client):
    wid = _create(client, name="Tech")["watchListId"]
    client.delete(f"/watchlist/{wid}")
    assert client.post("/watchlist", json={"userId": "u1", "watchListName": "Tech", "symbols": []}).status_code == 201


def test_delete_only_removes_that_watchlist(client, known_symbols):
    a = _create(client, name="A", symbols=["AAPL"])["watchListId"]
    b = _create(client, name="B", symbols=["MSFT"])["watchListId"]

    client.delete(f"/watchlist/{a}")

    assert client.get(f"/watchlist/{b}").json()["symbols"]["1"]["ticker"] == "MSFT"


# ==========================================================================
# API surface
# ==========================================================================
def test_openapi_lists_the_watchlist_routes():
    from main import app

    paths = app.openapi()["paths"]
    assert set(paths["/watchlist"]) == {"post", "put"}
    assert set(paths["/watchlist/{watchlist_id}"]) == {"get", "delete"}
    assert paths["/watchlist"]["post"]["responses"]["201"]


def test_watchlist_requests_do_not_touch_stock_budget_reporting(client):
    before = client.get("/status").json()["upstream_budget"]["remaining"]
    _create(client)
    assert client.get("/status").json()["upstream_budget"]["remaining"] == before


# ==========================================================================
# The store on its own
# ==========================================================================
class FrozenClock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def test_store_create_get_update_delete_roundtrip():
    clock = FrozenClock()
    ids = iter(["id-1", "id-2"])
    store = InMemoryWatchlistStore(id_fn=lambda: next(ids), time_fn=clock)

    created = store.create("u1", " Tech ", ["AAPL", "MSFT"])
    assert created.id == "id-1" and created.name == "Tech" and created.symbols == ("AAPL", "MSFT")
    assert created.created_at == created.updated_at == clock.now
    assert store.get("id-1") == created

    clock.now = clock.now.replace(day=2)
    updated = store.update("id-1", "Renamed", ["MSFT"])
    assert updated.name == "Renamed" and updated.symbols == ("MSFT",)
    assert updated.created_at == created.created_at and updated.updated_at == clock.now
    assert store.get("id-1") == updated

    assert store.update("id-1", name="Again").symbols == ("MSFT",)  # None keeps symbols
    assert store.update("id-1", symbols=["VTI"]).name == "Again"  # None keeps name
    assert store.update("id-1").name == "Again"  # a no-op only bumps updated_at
    updated = store.get("id-1")

    assert store.delete("id-1") == updated
    assert len(store) == 0
    with pytest.raises(WatchlistNotFound):
        store.get("id-1")
    with pytest.raises(WatchlistNotFound):
        store.update("id-1", "x", [])
    with pytest.raises(WatchlistNotFound):
        store.delete("id-1")


def test_store_records_are_immutable_and_symbols_are_copied():
    store = InMemoryWatchlistStore()
    symbols = ["AAPL"]
    item = store.create("u1", "Tech", symbols)
    symbols.append("MSFT")  # the caller's list is not shared with the store

    assert store.get(item.id).symbols == ("AAPL",)
    with pytest.raises(AttributeError):
        item.name = "hacked"  # frozen dataclass


def test_store_name_uniqueness_per_user_case_insensitive():
    store = InMemoryWatchlistStore()
    a = store.create("u1", "Tech", [])
    store.create("u2", "Tech", [])  # other user: fine

    with pytest.raises(DuplicateWatchlistName):
        store.create("u1", "TECH", [])
    b = store.create("u1", "Other", [])
    with pytest.raises(DuplicateWatchlistName):
        store.update(b.id, "tech", [])
    store.update(a.id, "TECH", [])  # a list may keep (re-case) its own name

    assert [w.name for w in store.list_for_user("u1")] == ["TECH", "Other"]
    assert [w.name for w in store.list_for_user("U1")] == ["TECH", "Other"]  # same user


def test_store_ids_are_short_and_collisions_are_retried():
    assert all(SHORT_ID.match(new_short_id()) for _ in range(100))
    assert len({new_short_id() for _ in range(1000)}) == 1000

    ids = iter(["same", "same", "same", "other"])  # a colliding id generator
    store = InMemoryWatchlistStore(id_fn=lambda: next(ids))
    assert store.create("u1", "A", []).id == "same"
    assert store.create("u1", "B", []).id == "other"  # skipped the two collisions


def test_store_rejects_invalid_state():
    store = InMemoryWatchlistStore()
    with pytest.raises(ValueError):
        store.create("", "Tech", [])
    with pytest.raises(ValueError):
        store.create("u1", "   ", [])
    with pytest.raises(ValueError):
        store.create("u1", "Tech", ["AAPL", "AAPL"])
    with pytest.raises(ValueError):
        store.create("u1", "Tech", [""])
    assert len(store) == 0


def test_store_is_thread_safe_under_concurrent_creates():
    import threading

    store = InMemoryWatchlistStore()
    errors = []

    def worker(n):
        try:
            for i in range(50):
                store.create(f"user-{n}", f"list-{i}", ["AAPL"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(store) == 400
