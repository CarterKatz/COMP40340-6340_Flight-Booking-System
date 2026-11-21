import secrets
from datetime import timedelta, timezone

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.db import pool
from app.errors import ApiError

router = APIRouter()

MIN_LAYOVER = timedelta(minutes=45)
MAX_LAYOVER = timedelta(minutes=240)


class HoldRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    itinerary_id: str
    passengers: int = Field(ge=1, le=9)


def load_legs(conn, itinerary_id: str) -> list[dict]:
    flight_ids = itinerary_id.split("+")
    if len(flight_ids) > 2:
        raise ApiError(422, "invalid_itinerary", "an itinerary has at most 2 flights")

    rows = conn.execute(
        """
        SELECT flight_id, origin, destination,
               scheduled_departure, scheduled_arrival, base_fare_cents
        FROM flights
        WHERE flight_id = ANY(%s)
        """,
        (flight_ids,),
    ).fetchall()
    by_id = {row["flight_id"]: row for row in rows}

    legs = []
    for flight_id in flight_ids:
        leg = by_id.get(flight_id)
        if leg is None:
            raise ApiError(404, "unknown_flight", f"no flight {flight_id!r}")
        legs.append(leg)

    if len(legs) == 2:
        first, second = legs
        if first["destination"] != second["origin"]:
            raise ApiError(422, "invalid_itinerary",
                           f"{first['flight_id']} lands at {first['destination']} "
                           f"but {second['flight_id']} leaves {second['origin']}")
        layover = second["scheduled_departure"] - first["scheduled_arrival"]
        if not MIN_LAYOVER <= layover <= MAX_LAYOVER:
            minutes = int(layover.total_seconds() // 60)
            raise ApiError(422, "invalid_itinerary",
                           f"layover of {minutes} min, must be 45 to 240")
        if second["destination"] == first["origin"]:
            raise ApiError(422, "invalid_itinerary",
                           "itinerary goes back through its origin")

    return legs


HOLD_SECONDS = 60

def seat_counts(conn, flight_ids: list[str]) -> list[dict]:
    return conn.execute(
        """
        SELECT f.flight_id, f.capacity, f.seats_sold,
               COALESCE(SUM(h.passengers), 0)::int AS seats_held
        FROM flights f
        LEFT JOIN hold_legs hl ON hl.flight_id = f.flight_id
        LEFT JOIN holds h ON h.hold_id = hl.hold_id
                         AND h.status = 'active'
                         AND h.expires_at > clock_timestamp()
        WHERE f.flight_id = ANY(%s)
        GROUP BY f.flight_id
        """,
        (flight_ids,),
    ).fetchall()

def iso_utc(moment) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.post("/holds", status_code=201)
def create_hold(body: HoldRequest):
    with pool.connection() as conn:
        legs = load_legs(conn, body.itinerary_id)
        flight_ids = [leg["flight_id"] for leg in legs]

        conn.execute(
            "SELECT flight_id FROM flights WHERE flight_id = ANY(%s) "
            "ORDER BY flight_id FOR UPDATE",
            (flight_ids,),
        )

        for row in seat_counts(conn, flight_ids):
            available = row["capacity"] - row["seats_sold"] - row["seats_held"]
            if available < body.passengers:
                raise ApiError(409, "sold_out",
                            f"{row['flight_id']} has {available} seats left")

        price_cents = sum(leg["base_fare_cents"] for leg in legs)
        total_cents = price_cents * body.passengers
        hold_id = "h_" + secrets.token_hex(8)

        hold = conn.execute(
            """
            INSERT INTO holds (hold_id, itinerary_id, passengers,
                               price_cents, total_cents, expires_at)
            VALUES (%s, %s, %s, %s, %s,
                    clock_timestamp() + make_interval(secs => %s))
            RETURNING expires_at
            """,
            (hold_id, body.itinerary_id, body.passengers,
             price_cents, total_cents, HOLD_SECONDS),
        ).fetchone()

        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO hold_legs (hold_id, leg, flight_id) VALUES (%s, %s, %s)",
                [(hold_id, number, flight_id)
                 for number, flight_id in enumerate(flight_ids, start=1)],
            )

    return {
        "hold_id": hold_id,
        "itinerary_id": body.itinerary_id,
        "passengers": body.passengers,
        "price_cents": price_cents,
        "total_cents": total_cents,
        "expires_at": iso_utc(hold["expires_at"]),
    }