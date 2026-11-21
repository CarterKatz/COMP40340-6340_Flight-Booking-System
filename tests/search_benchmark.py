"""Isolated search investigation: python tests/search_benchmark.py.

Uses the search fixtures, full catalog and a fixed set of warmed searches.
No production tables or planner settings are changed. Prints plans and timings;
this is a sequential diagnostic, not a concurrent traffic benchmark.
"""

import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
import types
from datetime import date

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_search


def main():
    from flightcontrol import catalog

    dsn = os.environ["SEARCH_TEST_DATABASE_URL"]
    patch = pytest.MonkeyPatch()
    fixture = test_search.airline.__wrapped__(patch, dsn)
    try:
        a = next(fixture)
        from app import search, search_routes as queries

        old = types.ModuleType("baseline_search")
        source = subprocess.check_output(
            ["git", "show", "9960185:app/search.py"], text=True,
        )
        exec(compile(source, "baseline_search.py", "exec"), old.__dict__)
        old.pool = a.pool
        with a.pool.connection() as conn:
            conn.execute("SET LOCAL statement_timeout = '30s'")
            with conn.cursor().copy(
                "COPY flights (" + ", ".join(catalog.FIELDS) + ") FROM STDIN WITH (FORMAT CSV, HEADER true)"
            ) as copy:
                copy.write(catalog.get().to_csv())
            conn.execute("ANALYZE flights")

        if "--with-holds" in sys.argv:
            ids = set()
            with a.pool.connection() as conn:
                for origin, destination in [("BOI", "JFK"), ("DEN", "ORD"), ("LAX", "SFO")]:
                    for route in queries.discover_routes(conn, origin, destination, date(2027, 3, 15)):
                        ids.update(route["flight_ids"])
                # Fifty historical holds per candidate flight, one still live.
                conn.execute("""
                    INSERT INTO holds
                    (hold_id, itinerary_id, passengers, price_cents, total_cents, expires_at)
                    SELECT fid || '-history-' || n, fid, 1, 10000, 10000,
                           statement_timestamp() + CASE WHEN n = 50
                               THEN interval '1 hour' ELSE interval '-1 hour' END
                    FROM unnest(%s::text[]) fid CROSS JOIN generate_series(1, 50) n
                """, (sorted(ids),))
                conn.execute("INSERT INTO hold_legs SELECT hold_id, 1, itinerary_id FROM holds")
                conn.execute("ANALYZE holds")
                conn.execute("ANALYZE hold_legs")
            print(f"Populated history: {len(ids) * 50} holds, {len(ids)} active", flush=True)

        def tree(node, indent=""):
            print(indent + node["Node Type"] + " " + node.get("Relation Name", "")
                  + " " + node.get("Index Name", "")
                  + f" est={node['Plan Rows']} actual={node['Actual Rows']}"
                  + f" loops={node['Actual Loops']} ms={node['Actual Total Time']:.3f}"
                  + f" buffers={node.get('Shared Hit Blocks', 0)}")
            for child in node.get("Plans", []):
                tree(child, indent + "  ")

        for origin, destination in [("BOI", "JFK"), ("DEN", "ORD"), ("LAX", "SFO")]:
            params = dict(origin=origin, destination=destination, date=date(2027, 3, 15), passengers=2)
            times = {"old": [], "new": []}
            # Alternate order to reduce warm-cache/order bias; discard warmups.
            for repeat in range(12):
                for label in (("old", "new") if repeat % 2 else ("new", "old")):
                    started = time.perf_counter()
                    (old.search if label == "old" else search.search)(**params)
                    elapsed = (time.perf_counter() - started) * 1000
                    if repeat >= 2:
                        times[label].append(elapsed)
            print(f"\n{origin}-{destination} handler ms: " + "; ".join(
                f"{label} median={statistics.median(values):.3f} max={max(values):.3f}"
                for label, values in times.items()))
            with a.pool.connection() as conn:
                plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + queries.SELECTED_ROUTES_SQL,
                                    dict(origin=origin, destination=destination, day=params["date"],
                                         passengers=2, hour_value=3300, connection_cost=8700,
                                         max_results=20)).fetchone()["QUERY PLAN"][0]
            print(f"SQL execution={plan['Execution Time']:.3f} ms")
            tree(plan["Plan"])
    finally:
        fixture.close()
        patch.undo()


if __name__ == "__main__":
    main()
