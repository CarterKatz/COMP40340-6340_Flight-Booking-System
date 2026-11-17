import datetime

from fastapi import APIRouter, Query

from app.db import pool
from app.errors import ApiError
from app.holds import MAX_LAYOVER, MIN_LAYOVER, iso_utc, seat_counts

router = APIRouter()

MAX_RESULTS = 20
HOUR_VALUE_CENTS = 3300
CONNECTION_COST_CENTS = 8700

FLIGHT_COLUMNS = """
    SELECT flight_id, origin, destination,
           scheduled_departure, scheduled_arrival, base_fare_cents
    FROM flights
"""


def airport_exists(conn, code: str) -> bool:
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM flights WHERE origin = %s) AS found",
        (code,),
    ).fetchone()
    return row["found"]


def itinerary_json(legs: list[dict], seats: dict[str, int]) -> dict:
    return {
        "itinerary_id": "+".join(leg["flight_id"] for leg in legs),
        "flight_ids": [leg["flight_id"] for leg in legs],
        "departure": iso_utc(legs[0]["scheduled_departure"]),
        "arrival": iso_utc(legs[-1]["scheduled_arrival"]),
        "stops": len(legs) - 1,
        "price_cents": sum(leg["base_fare_cents"] for leg in legs),
        "seats_available": min(seats[leg["flight_id"]] for leg in legs),
    }


def trip_cost(itinerary: dict, legs: list[dict]) -> float:
    hours = (legs[-1]["scheduled_arrival"]
             - legs[0]["scheduled_departure"]).total_seconds() / 3600
    return (itinerary["price_cents"]
            + HOUR_VALUE_CENTS * hours
            + CONNECTION_COST_CENTS * itinerary["stops"])


@router.get("/search")
def search(
    origin: str,
    destination: str,
    date: datetime.date,
    passengers: int = Query(ge=1, le=9),
):
    with pool.connection() as conn:
        for code in (origin, destination):
            if not airport_exists(conn, code):
                raise ApiError(422, "unknown_airport", f"no airport {code!r}")

        first_legs = conn.execute(
            FLIGHT_COLUMNS + " WHERE origin = %s AND origin_local_date = %s",
            (origin, date),
        ).fetchall()

        hubs = [leg for leg in first_legs if leg["destination"] != destination]
        second_legs = []
        if hubs:
            second_legs = conn.execute(
                FLIGHT_COLUMNS + """
                WHERE origin = ANY(%s)
                  AND destination = %s
                  AND scheduled_departure BETWEEN %s AND %s
                """,
                (
                    list({leg["destination"] for leg in hubs}),
                    destination,
                    min(leg["scheduled_arrival"] for leg in hubs) + MIN_LAYOVER,
                    max(leg["scheduled_arrival"] for leg in hubs) + MAX_LAYOVER,
                ),
            ).fetchall()

        all_ids = [leg["flight_id"] for leg in first_legs + second_legs]
        seats = {
            row["flight_id"]: row["capacity"] - row["seats_sold"] - row["seats_held"]
            for row in seat_counts(conn, all_ids)
        }

    nonstops, connections = [], []
    for first in first_legs:
        if first["destination"] == destination:
            nonstops.append([first])
            continue
        for second in second_legs:
            layover = second["scheduled_departure"] - first["scheduled_arrival"]
            if (second["origin"] == first["destination"]
                    and MIN_LAYOVER <= layover <= MAX_LAYOVER
                    and second["destination"] != origin):
                connections.append([first, second])

    def with_room(options: list[list[dict]]) -> list[tuple[float, dict]]:
        found = []
        for legs in options:
            itinerary = itinerary_json(legs, seats)
            if itinerary["seats_available"] >= passengers:
                found.append((trip_cost(itinerary, legs), itinerary))
        return sorted(found, key=lambda pair: pair[0])

    results = with_room(nonstops)
    results += with_room(connections)[: max(0, MAX_RESULTS - len(results))]
    results.sort(key=lambda pair: pair[0])
    return {"itineraries": [itinerary for _, itinerary in results[:MAX_RESULTS]]}