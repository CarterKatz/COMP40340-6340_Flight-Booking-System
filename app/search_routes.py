"""SQL route discovery, availability and result selection.

The HTTP search endpoint calls selected_routes. Route discovery
returns all candidates, available_routes filters by party size, and
selected_routes ranks and selects the final list.
"""

from datetime import date

MAX_RESULTS = 20
HOUR_VALUE_CENTS = 3300
CONNECTION_COST_CENTS = 8700


ROUTES_SQL = """
WITH first_legs AS (
    SELECT flight_id, origin, destination, scheduled_departure,
           scheduled_arrival, base_fare_cents
    FROM flights
    WHERE origin = %(origin)s AND origin_local_date = %(day)s
)
SELECT first.flight_id AS itinerary_id,
       ARRAY[first.flight_id] AS flight_ids,
       first.scheduled_departure AS departure,
       first.scheduled_arrival AS arrival,
       0 AS stops,
       first.base_fare_cents AS price_cents
FROM first_legs first
WHERE first.destination = %(destination)s

UNION ALL

SELECT first.flight_id || '+' || second.flight_id AS itinerary_id,
       ARRAY[first.flight_id, second.flight_id] AS flight_ids,
       first.scheduled_departure AS departure,
       second.scheduled_arrival AS arrival,
       1 AS stops,
       first.base_fare_cents + second.base_fare_cents AS price_cents
FROM first_legs first
JOIN LATERAL (
    SELECT flight_id, scheduled_departure, scheduled_arrival, base_fare_cents,
           destination
    FROM flights
    WHERE origin = first.destination
      AND destination = %(destination)s
      AND scheduled_departure
          BETWEEN first.scheduled_arrival + INTERVAL '45 minutes'
              AND first.scheduled_arrival + INTERVAL '240 minutes'
    -- Keep this a per-first-leg lookup. Pulling it into a global join can
    -- choose a year-wide scan when connection cardinality is overestimated.
    OFFSET 0
) second ON true
WHERE second.destination = %(destination)s
  AND first.destination <> %(destination)s
  AND first.destination <> first.origin
  AND second.destination <> first.origin
"""


def discover_routes(conn, origin: str, destination: str, day: date) -> list[dict]:
    """Return all scheduled nonstop/one-stop candidates, with UTC datetimes.

    The caller supplies a psycopg connection using dict_row. This is a read-only
    query; it does not validate airport codes, reserve seats, rank or limit.
    """
    return conn.execute(
        ROUTES_SQL, {"origin": origin, "destination": destination, "day": day},
    ).fetchall()


# Keep the route definition shared: availability must consider every candidate
# before filtering by party size or selecting the best twenty results.
INVENTORY_CTES = "WITH routes AS (" + ROUTES_SQL + """
), candidate_flights AS (
    SELECT DISTINCT unnest(flight_ids) AS flight_id FROM routes
), held AS (
    SELECT hl.flight_id, SUM(h.passengers)::int AS seats_held
    FROM candidate_flights c
    JOIN hold_legs hl ON hl.flight_id = c.flight_id
    JOIN holds h ON h.hold_id = hl.hold_id
    WHERE h.status = 'active' AND h.expires_at > statement_timestamp()
    GROUP BY hl.flight_id
), inventory AS (
    SELECT f.flight_id, f.capacity, f.seats_sold,
           COALESCE(h.seats_held, 0) AS seats_held,
           f.capacity - f.seats_sold - COALESCE(h.seats_held, 0) AS seats_available
    FROM flights f
    LEFT JOIN held h ON h.flight_id = f.flight_id
    -- A scalar ID array exposes a small key lookup instead of a join whose
    -- overestimated cardinality can cause a full-catalog hash/sequence scan.
    WHERE f.flight_id = ANY(ARRAY(SELECT flight_id FROM candidate_flights))
)
"""

AVAILABLE_ROUTES_SQL = INVENTORY_CTES + """
SELECT r.*, LEAST(first.seats_available, second.seats_available) AS seats_available
FROM routes r
JOIN inventory first ON first.flight_id = r.flight_ids[1]
LEFT JOIN inventory second ON second.flight_id = r.flight_ids[2]
WHERE LEAST(first.seats_available, second.seats_available) >= %(passengers)s
"""
# PostgreSQL LEAST ignores NULL: a nonstop uses its first leg's availability.


def candidate_inventory(conn, origin: str, destination: str, day: date) -> list[dict]:
    """Inspect each relevant flight once, including inconsistent negative counts."""
    return conn.execute(
        INVENTORY_CTES + "SELECT * FROM inventory",
        {"origin": origin, "destination": destination, "day": day},
    ).fetchall()


def available_routes(conn, origin: str, destination: str, day: date,
                     passengers: int) -> list[dict]:
    """Return all routes with room, using one snapshot and expiration cutoff.

    Airport validation, ranking and result limits belong to the caller. Holds
    still arbitrate exact inventory under concurrency; search acquires no locks.
    """
    if type(passengers) is not int or not 1 <= passengers <= 9:
        raise ValueError("passengers must be an integer between 1 and 9")
    return conn.execute(
        AVAILABLE_ROUTES_SQL,
        {"origin": origin, "destination": destination, "day": day,
         "passengers": passengers},
    ).fetchall()


SELECTED_ROUTES_SQL = "WITH eligible AS (" + AVAILABLE_ROUTES_SQL + """
), scored AS (
    SELECT eligible.*,
           price_cents
           + %(hour_value)s * EXTRACT(EPOCH FROM (arrival - departure)) / 3600
           + %(connection_cost)s * stops AS score
    FROM eligible
), selected AS (
    -- Inclusion priority differs from display order: keep all nonstops first.
    SELECT * FROM scored
    ORDER BY stops, score, itinerary_id COLLATE "C"
    LIMIT %(max_results)s
)
SELECT itinerary_id, flight_ids, departure, arrival, stops, price_cents, seats_available
FROM selected
ORDER BY score, itinerary_id COLLATE "C"
"""


def selected_routes(conn, origin: str, destination: str, day: date,
                    passengers: int) -> list[dict]:
    """Select up to twenty bookable routes, preserving eligible nonstops.

    Ranking adds duration and connection preferences without changing fares.
    Ties use bytewise itinerary ID order, independent of database locale.
    The catalog must have at most twenty nonstops for each origin/destination/day.
    Airport validation and UTC JSON formatting remain the endpoint's job.
    """
    if type(passengers) is not int or not 1 <= passengers <= 9:
        raise ValueError("passengers must be an integer between 1 and 9")
    return conn.execute(
        SELECTED_ROUTES_SQL,
        {"origin": origin, "destination": destination, "day": day,
         "passengers": passengers, "hour_value": HOUR_VALUE_CENTS,
         "connection_cost": CONNECTION_COST_CENTS, "max_results": MAX_RESULTS},
    ).fetchall()
