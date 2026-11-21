"""Search contract checks against real PostgreSQL; see tests/SEARCH.md."""

import importlib
import os
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.fixture
def airline(monkeypatch, search_database):
    dsn = search_database

    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    from fastapi.testclient import TestClient

    schema = "search_test_" + uuid4().hex
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root))
    # Use the production DDL, excluding only its Docker-specific catalog COPY.
    ddl = (root / "db/01_schema.sql").read_text()
    copy_start = ddl.index("COPY flights")
    ddl = ddl[:copy_start] + ddl[ddl.index(";", copy_start) + 1:]
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            # Search checks are sequential. Use one real connection, avoiding
            # background pool workers while preserving transaction boundaries.
            with psycopg.connect(
                dsn, row_factory=dict_row, connect_timeout=5,
                options=f"-c search_path={schema} -c statement_timeout=5000 -c lock_timeout=5000",
            ) as conn:
                pool = TestConnections(conn)
                with pool.connection() as setup:
                    setup.execute(ddl)
                monkeypatch.setenv("DATABASE_URL", dsn)
                monkeypatch.setenv("API_KEY", "search-test-key")
                main = importlib.import_module("app.main")
                for name in ("db", "main", "search", "holds", "bookings", "admin"):
                    monkeypatch.setattr(importlib.import_module(f"app.{name}"), "pool", pool)
                monkeypatch.setattr(main, "EXPECTED_AUTH", b"Bearer search-test-key")
                # The fixture owns the connection lifetime; HTTP middleware runs.
                client = TestClient(main.app, headers={"Authorization": "Bearer search-test-key"})
                try:
                    yield Fixture(pool, client)
                finally:
                    client.close()
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture(scope="session")
def search_database():
    """Check connectivity once and stop the run if its prerequisite is missing."""
    dsn = os.environ.get("SEARCH_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set SEARCH_TEST_DATABASE_URL; see tests/SEARCH.md")

    import psycopg
    try:
        with psycopg.connect(dsn, connect_timeout=5, options="-c statement_timeout=5000") as conn:
            conn.execute("SELECT 1")
    except psycopg.Error:
        pytest.exit(
            "Search test database unavailable. Check compose.test.yaml and "
            "SEARCH_TEST_DATABASE_URL. Stopping instead of retrying every test.",
            returncode=2,
        )
    return dsn


class TestConnections:
    """Real PostgreSQL transactions for sequential tests; no pool worker threads."""

    __test__ = False

    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def connection(self):
        with self.conn.transaction():
            yield self.conn


class Fixture:
    def __init__(self, pool, client):
        self.pool, self.client = pool, client

    def flight(self, fid, origin, destination, departure, arrival, *, local="2027-03-15",
               fare=10000, capacity=10, sold=0):
        with self.pool.connection() as conn:
            conn.execute(
                """INSERT INTO flights VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, %s, 100, %s)""",
                (fid, fid, origin, destination, departure, arrival, local, capacity, fare, sold),
            )

    def hold(self, ids, passengers, *, status="active", expired=False):
        hid = uuid4().hex
        expiry = datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 1)
        with self.pool.connection() as conn:
            conn.execute(
                """INSERT INTO holds
                (hold_id, itinerary_id, passengers, price_cents, total_cents, status, expires_at)
                VALUES (%s, %s, %s, 10000, %s, %s, %s)""",
                (hid, "+".join(ids), passengers, passengers * 10000, status, expiry),
            )
            for i, fid in enumerate(ids):
                conn.execute("INSERT INTO hold_legs VALUES (%s, %s, %s)", (hid, i, fid))

    def search(self, **overrides):
        params = dict(origin="BOI", destination="JFK", date="2027-03-15", passengers=1)
        params.update(overrides)
        response = self.client.get("/search", params=params)
        assert response.status_code == 200, response.text
        assert "Server-Timing" in response.headers
        return response.json()["itineraries"]


def schedule(a):
    a.flight("A", "BOI", "JFK", "2027-03-15T14:00Z", "2027-03-15T19:00Z", fare=25000)
    a.flight("B", "BOI", "DEN", "2027-03-15T15:00Z", "2027-03-15T17:00Z", sold=3)
    # Destination must also exist as an origin, as it does in the real catalog.
    a.flight("RETURN", "JFK", "BOI", "2027-03-16T12:00Z", "2027-03-16T18:00Z", local="2027-03-16")


@pytest.mark.parametrize("minutes,eligible", [(44, False), (45, True), (240, True), (241, False)])
def test_connection_boundaries(airline, minutes, eligible):
    schedule(airline)
    departure = datetime(2027, 3, 15, 17, tzinfo=timezone.utc) + timedelta(minutes=minutes)
    airline.flight("C", "DEN", "JFK", departure, departure + timedelta(hours=3), fare=15000)
    results = {r["itinerary_id"]: r for r in airline.search()}
    assert set(results) == ({"A", "B+C"} if eligible else {"A"})
    if eligible:
        assert results["B+C"] == dict(
            itinerary_id="B+C", flight_ids=["B", "C"], departure="2027-03-15T15:00:00Z",
            arrival=(departure + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            stops=1, price_cents=25000, seats_available=7,
        )


def test_active_holds_and_party_size(airline):
    schedule(airline)
    airline.flight("C", "DEN", "JFK", "2027-03-15T18:00Z", "2027-03-15T21:00Z", sold=4, fare=15000)
    airline.hold(["B", "C"], 1)
    airline.hold(["B"], 1)
    airline.hold(["C"], 2)
    for status, expired in [("active", True), ("released", False), ("booked", False)]:
        airline.hold(["B", "C"], 2, status=status, expired=expired)
    results = {r["itinerary_id"]: r for r in airline.search(passengers=3)}
    assert results["B+C"]["seats_available"] == 3
    assert results["B+C"]["price_cents"] == 25000
    assert {r["itinerary_id"] for r in airline.search(passengers=4)} == {"A"}


def test_local_date_and_overnight_connection(airline):
    schedule(airline)
    airline.flight("LATE", "BOI", "DEN", "2027-03-16T04:00Z", "2027-03-16T06:00Z")
    airline.flight("NEXT", "DEN", "JFK", "2027-03-16T07:00Z", "2027-03-16T10:00Z", local="2027-03-16")
    assert {r["itinerary_id"] for r in airline.search()} == {"A", "LATE+NEXT"}
    assert airline.search(date="2027-03-16") == []


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_returns_all_when_few_exist(airline, count):
    schedule(airline)
    for i in range(count):
        airline.flight(f"D{i}", "BOI", "JFK", "2027-03-17T14:00Z", "2027-03-17T19:00Z", local="2027-03-17")
    assert len(airline.search(date="2027-03-17")) == count


def test_limit_preserves_nonstops_and_best_connections(airline):
    schedule(airline)
    airline.flight("EXPENSIVE", "BOI", "JFK", "2027-03-15T14:00Z", "2027-03-15T19:00Z", fare=1000000)
    for i in range(25):
        airline.flight(f"C{i:02}", "DEN", "JFK", "2027-03-15T18:00Z", "2027-03-15T21:00Z", fare=10000 + i * 100)
    results = airline.search()
    assert len(results) == 20
    assert {r["itinerary_id"] for r in results} == {"A", "EXPENSIVE"} | {f"B+C{i:02}" for i in range(18)}


@pytest.mark.parametrize("field,value", [
    ("origin", "XXX"), ("destination", "XXX"), ("date", "2027-02-30"),
    ("passengers", 0), ("passengers", 10), ("passengers", "abc"),
])
def test_invalid_search(airline, field, value):
    schedule(airline)
    params = dict(origin="BOI", destination="JFK", date="2027-03-15", passengers=1)
    params[field] = value
    response = airline.client.get("/search", params=params)
    assert response.status_code == 422
    assert set(response.json()) == {"error", "detail"}
    assert "Server-Timing" in response.headers


@pytest.mark.parametrize("field", ["origin", "destination", "date", "passengers"])
def test_missing_parameter(airline, field):
    params = dict(origin="BOI", destination="JFK", date="2027-03-15", passengers=1)
    del params[field]
    response = airline.client.get("/search", params=params)
    assert response.status_code == 422
    assert set(response.json()) == {"error", "detail"}


@pytest.mark.parametrize("authorization", ["", "Bearer wrong"])
def test_authentication(airline, authorization):
    response = airline.client.get("/search", headers={"Authorization": authorization})
    assert response.status_code == 401
    assert set(response.json()) == {"error", "detail"}
    assert "Server-Timing" in response.headers


def test_restart_endpoint_schedules_process_exit(airline, monkeypatch):
    import app.admin

    exit_codes = []
    monkeypatch.setattr(app.admin.os, "_exit", exit_codes.append)
    response = airline.client.post("/admin/restart")
    assert response.status_code == 202
    assert response.json() == {"status": "restarting"}


def discovered(a, origin="BOI", destination="JFK", day=date(2027, 3, 15)):
    from app.search_routes import discover_routes
    with a.pool.connection() as conn:
        rows = discover_routes(conn, origin, destination, day)
    assert len(rows) == len({r["itinerary_id"] for r in rows})
    return {r["itinerary_id"]: r for r in rows}


@pytest.mark.parametrize("minutes,eligible", [(44, False), (45, True), (240, True), (241, False)])
def test_sql_route_boundaries(airline, minutes, eligible):
    schedule(airline)
    departure = datetime(2027, 3, 15, 17, tzinfo=timezone.utc) + timedelta(minutes=minutes)
    airline.flight("C", "DEN", "JFK", departure, departure + timedelta(hours=3), fare=15000)
    rows = discovered(airline)
    assert set(rows) == ({"A", "B+C"} if eligible else {"A"})
    if eligible:
        assert rows["B+C"] == dict(
            itinerary_id="B+C", flight_ids=["B", "C"],
            departure=datetime(2027, 3, 15, 15, tzinfo=timezone.utc),
            arrival=departure + timedelta(hours=3), stops=1, price_cents=25000,
        )


def test_sql_route_local_date_and_overnight(airline):
    schedule(airline)
    airline.flight("LATE", "BOI", "DEN", "2027-03-16T04:00Z", "2027-03-16T06:00Z")
    airline.flight("NEXT", "DEN", "JFK", "2027-03-16T07:00Z", "2027-03-16T10:00Z", local="2027-03-16")
    assert set(discovered(airline)) == {"A", "LATE+NEXT"}
    assert discovered(airline, day=date(2027, 3, 16)) == {}


def test_sql_routes_reject_wrong_airports_and_return_to_origin(airline):
    schedule(airline)
    airline.flight("WRONG", "SLC", "JFK", "2027-03-15T18:00Z", "2027-03-15T21:00Z")
    airline.flight("BACK", "DEN", "BOI", "2027-03-15T18:00Z", "2027-03-15T20:00Z")
    assert set(discovered(airline)) == {"A"}
    assert discovered(airline, destination="BOI") == {}
    assert discovered(airline, origin="XXX") == {}


def test_sql_routes_are_not_limited_or_filtered_by_seats(airline):
    schedule(airline)
    for i in range(25):
        airline.flight(f"C{i}", "DEN", "JFK", "2027-03-15T18:00Z", "2027-03-15T21:00Z", sold=10)
    assert len(discovered(airline)) == 26


def test_sql_routes_match_real_catalog_oracle(airline):
    """Load all 175k flights once, then compare complete route sets and fields."""
    import random
    from flightcontrol import catalog, oracle

    cat = catalog.get()
    from collections import Counter
    from fractions import Fraction
    from app.search_routes import selected_routes

    # Check the inclusion/limit requirements are compatible for the whole year.
    departures = Counter((f.origin, f.destination, f.origin_local_date) for f in cat.flights)
    assert max(departures.values()) <= 20
    with airline.pool.connection() as conn:
        # Bulk fixture setup gets a bounded maintenance allowance; route queries
        # still use the normal five-second limit after this transaction commits.
        conn.execute("SET LOCAL statement_timeout = '30s'")
        with conn.cursor().copy(
            "COPY flights (" + ", ".join(catalog.FIELDS) + ") FROM STDIN WITH (FORMAT CSV, HEADER true)"
        ) as copy:
            copy.write(cat.to_csv())
        conn.execute("ANALYZE flights")

    codes = sorted(catalog.AIRPORT_BY_CODE)
    days = [date(2027, 1, 1), date(2027, 3, 14), date(2027, 11, 7), date(2027, 12, 31)]
    searches = [(o, d, day) for day in days for o, d in
                [("BOI", "JFK"), ("LAX", "SFO"), ("SEA", "ATL"), ("DEN", "ORD")]]
    rng = random.Random(4340)
    for _ in range(40):
        origin, destination = rng.sample(codes, 2)
        searches.append((origin, destination, date(2027, 1, 1) + timedelta(days=rng.randrange(365))))

    for origin, destination, day in searches:
        expected = {i.itinerary_id: dict(
            itinerary_id=i.itinerary_id, flight_ids=[f.flight_id for f in i.legs],
            departure=i.departure, arrival=i.arrival, stops=i.stops,
            price_cents=i.base_fare_cents,
        ) for i in oracle.itineraries(cat, origin, destination, day)}
        assert discovered(airline, origin, destination, day) == expected, (origin, destination, day)
        for party in (1, 9):
            candidates = [dict(r, seats_available=min(cat.by_id[f].capacity for f in r["flight_ids"]))
                          for r in expected.values()]
            candidates = [r for r in candidates if r["seats_available"] >= party]

            def score(r):
                seconds = int((r["arrival"] - r["departure"]).total_seconds())
                return r["price_cents"] + Fraction(3300 * seconds, 3600) + 8700 * r["stops"]

            chosen = sorted(candidates, key=lambda r: (r["stops"], score(r), r["itinerary_id"]))[:20]
            chosen.sort(key=lambda r: (score(r), r["itinerary_id"]))
            with airline.pool.connection() as conn:
                actual = selected_routes(conn, origin, destination, day, party)
            assert actual == chosen, (origin, destination, day, party)
            response = airline.client.get("/search", params=dict(
                origin=origin, destination=destination, date=day.isoformat(), passengers=party,
            ))
            assert response.status_code == 200, response.text
            assert "Server-Timing" in response.headers
            assert response.json() == {"itineraries": [
                {**r, "departure": catalog.iso(r["departure"]), "arrival": catalog.iso(r["arrival"])}
                for r in chosen
            ]}

    # Diagnostic evidence, not a timing assertion: full catalog, empty inventory.
    from app.search_routes import SELECTED_ROUTES_SQL
    for origin, destination in [("BOI", "JFK"), ("DEN", "ORD"), ("LAX", "SFO")]:
        with airline.pool.connection() as conn:
            plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + SELECTED_ROUTES_SQL,
                                dict(origin=origin, destination=destination, day=date(2027, 3, 15),
                                     passengers=2, hour_value=3300, connection_cost=8700,
                                     max_results=20)).fetchone()["QUERY PLAN"][0]

        def indexes(node):
            return ({node["Index Name"]} if "Index Name" in node else set()).union(
                *(indexes(child) for child in node.get("Plans", [])))

        print(f"\nSQL plan {origin}-{destination}: execution={plan['Execution Time']:.3f} ms; "
              f"planning={plan['Planning Time']:.3f} ms; indexes={sorted(indexes(plan['Plan']))}")


def inventory(a):
    from app.search_routes import candidate_inventory
    with a.pool.connection() as conn:
        rows = candidate_inventory(conn, "BOI", "JFK", date(2027, 3, 15))
    assert len(rows) == len({r["flight_id"] for r in rows})
    return {r["flight_id"]: r for r in rows}


def available(a, passengers=1):
    from app.search_routes import available_routes
    with a.pool.connection() as conn:
        rows = available_routes(conn, "BOI", "JFK", date(2027, 3, 15), passengers)
    assert len(rows) == len({r["itinerary_id"] for r in rows})
    return {r["itinerary_id"]: r for r in rows}


def connection_schedule(a):
    schedule(a)
    a.flight("C", "DEN", "JFK", "2027-03-15T18:00Z", "2027-03-15T21:00Z", sold=4)


def test_sql_inventory_aggregates_each_candidate_once(airline):
    from app.holds import seat_counts
    connection_schedule(airline)
    # B appears in two routes but its holds must only be counted once.
    airline.flight("D", "DEN", "JFK", "2027-03-15T19:00Z", "2027-03-15T22:00Z")
    airline.hold(["B", "C"], 1)
    airline.hold(["B"], 1)
    airline.hold(["C"], 2)
    for status, expired in [("active", True), ("released", False), ("booked", False)]:
        airline.hold(["B", "C"], 2, status=status, expired=expired)
    rows = inventory(airline)
    assert set(rows) == {"A", "B", "C", "D"}
    assert rows["B"]["seats_available"] == 5
    assert rows["C"]["seats_available"] == 3
    assert rows["A"]["seats_held"] == rows["D"]["seats_held"] == 0
    with airline.pool.connection() as conn:
        expected = seat_counts(conn, list(rows))
    for r in expected:
        assert rows[r["flight_id"]] == dict(
            **r, seats_available=r["capacity"] - r["seats_sold"] - r["seats_held"],
        )


@pytest.mark.parametrize("leg", ["B", "C"])
def test_sql_availability_filters_either_sold_out_leg(airline, leg):
    connection_schedule(airline)
    with airline.pool.connection() as conn:
        conn.execute("UPDATE flights SET seats_sold = capacity WHERE flight_id = %s", (leg,))
    assert set(available(airline)) == {"A"}


def test_sql_availability_exact_party_size_and_fields(airline):
    connection_schedule(airline)
    airline.hold(["B"], 2)
    airline.hold(["C"], 3)
    rows = available(airline, 3)
    assert rows["B+C"]["seats_available"] == 3
    assert {k: v for k, v in rows["B+C"].items() if k != "seats_available"} == discovered(airline)["B+C"]
    assert set(available(airline, 4)) == {"A"}
    assert available(airline, 9)["A"]["seats_available"] == 10


def test_sql_inventory_exposes_negative_counts(airline):
    connection_schedule(airline)
    airline.hold(["C"], 9)
    assert inventory(airline)["C"]["seats_available"] == -3
    assert set(available(airline)) == {"A"}


def test_sql_availability_has_no_result_limit(airline):
    schedule(airline)
    for i in range(25):
        airline.flight(f"C{i}", "DEN", "JFK", "2027-03-15T18:00Z", "2027-03-15T21:00Z")
    assert len(available(airline)) == 26


def test_sql_availability_empty_search(airline):
    schedule(airline)
    with airline.pool.connection() as conn:
        conn.execute("DELETE FROM flights")
    assert available(airline) == {}
    assert inventory(airline) == {}


@pytest.mark.parametrize("outcome", ["confirmed", "declined", "expired"])
def test_sql_availability_inventory_lifecycle(airline, outcome):
    connection_schedule(airline)
    assert available(airline)["B+C"]["seats_available"] == 6
    response = airline.client.post("/holds", json={"itinerary_id": "B+C", "passengers": 2})
    assert response.status_code == 201, response.text
    hid = response.json()["hold_id"]
    assert available(airline)["B+C"]["seats_available"] == 4
    if outcome == "expired":
        with airline.pool.connection() as conn:
            conn.execute("UPDATE holds SET expires_at = statement_timestamp() - interval '1 hour' WHERE hold_id = %s", (hid,))
        assert available(airline)["B+C"]["seats_available"] == 6

        return
    body = dict(hold_id=hid, passengers=[{"name": "Ada"}, {"name": "Lee"}],
                payment_token="tok_ok_test" if outcome == "confirmed" else "tok_decline_test",
                idempotency_key=uuid4().hex)
    booked = airline.client.post("/bookings", json=body)
    assert booked.status_code == (201 if outcome == "confirmed" else 402), booked.text
    rows = inventory(airline)
    assert rows["B"]["seats_held"] == rows["C"]["seats_held"] == 0
    if outcome == "declined":
        assert available(airline)["B+C"]["seats_available"] == 6
        return
    assert rows["B"]["seats_sold"] == 5
    assert rows["C"]["seats_sold"] == 6
    assert available(airline)["B+C"]["seats_available"] == 4
    # A duplicate booking or cancellation must not move seats twice.
    assert airline.client.post("/bookings", json=body).json() == booked.json()
    bid = booked.json()["booking_id"]
    for _ in range(2):
        cancelled = airline.client.delete(f"/bookings/{bid}")
        assert cancelled.status_code == 200, cancelled.text
        assert available(airline)["B+C"]["seats_available"] == 6


def selected(a, passengers=1):
    from app.search_routes import selected_routes
    with a.pool.connection() as conn:
        return selected_routes(conn, "BOI", "JFK", date(2027, 3, 15), passengers)


@pytest.mark.parametrize("connection_fare,first", [(1000, "B+C"), (15000, "A")])
def test_sql_selection_ranking_tradeoff(airline, connection_fare, first):
    schedule(airline)
    airline.flight("C", "DEN", "JFK", "2027-03-15T18:00Z", "2027-03-15T21:00Z", fare=connection_fare)
    rows = selected(airline)
    assert rows[0]["itinerary_id"] == first
    # Selection does not expose score or alter any route/inventory fields.
    assert {r["itinerary_id"]: r for r in rows} == available(airline)


def test_sql_selection_shorter_and_cheaper_first(airline):
    schedule(airline)
    airline.flight("FAST", "BOI", "JFK", "2027-03-15T14:00Z", "2027-03-15T18:00Z", fare=25000)
    airline.flight("CHEAP", "BOI", "JFK", "2027-03-15T14:00Z", "2027-03-15T18:00Z", fare=24000)
    assert [r["itinerary_id"] for r in selected(airline)] == ["CHEAP", "FAST", "A"]


def test_sql_selection_nonstops_ties_and_sold_out_replacement(airline):
    schedule(airline)
    with airline.pool.connection() as conn:
        conn.execute("UPDATE flights SET base_fare_cents = 1000000 WHERE flight_id = 'A'")
    # Reverse insertion order ensures ties cannot rely on the physical row order.
    for i in reversed(range(25)):
        airline.flight(f"C{i:02}", "DEN", "JFK", "2027-03-15T18:00Z", "2027-03-15T21:00Z", fare=1000)
    airline.hold(["C00"], 9)
    rows = selected(airline, 2)
    assert [r["itinerary_id"] for r in rows] == [f"B+C{i:02}" for i in range(1, 20)] + ["A"]
    # Smaller parties can use C00, shifting the cutoff without dropping A.
    assert [r["itinerary_id"] for r in selected(airline, 1)] == [f"B+C{i:02}" for i in range(19)] + ["A"]


@pytest.mark.parametrize("count", [0, 1, 2, 3, 20])
def test_sql_selection_small_sets_and_exact_limit(airline, count):
    for i in range(count):
        airline.flight(f"A{i:02}", "BOI", "JFK", "2027-03-15T14:00Z", "2027-03-15T19:00Z")
    assert [r["itinerary_id"] for r in selected(airline)] == [f"A{i:02}" for i in range(count)]
