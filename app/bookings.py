import secrets

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from app.db import pool
from app.errors import ApiError
from app.holds import iso_utc

router = APIRouter()


class Passenger(BaseModel):
    model_config = ConfigDict(strict=True)

    name: str


class BookingRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    hold_id: str
    passengers: list[Passenger]
    payment_token: str
    idempotency_key: str


def load_booking(conn, booking_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT b.booking_id, b.status, b.created_at,
               h.itinerary_id, h.total_cents,
               ARRAY(SELECT hl.flight_id FROM hold_legs hl
                     WHERE hl.hold_id = b.hold_id ORDER BY hl.leg) AS flight_ids,
               ARRAY(SELECT bp.name FROM booking_passengers bp
                     WHERE bp.booking_id = b.booking_id ORDER BY bp.seq) AS names
        FROM bookings b
        JOIN holds h ON h.hold_id = b.hold_id
        WHERE b.booking_id = %s
        """,
        (booking_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "booking_id": row["booking_id"],
        "status": row["status"],
        "itinerary_id": row["itinerary_id"],
        "flight_ids": row["flight_ids"],
        "passengers": [{"name": name} for name in row["names"]],
        "total_cents": row["total_cents"],
        "created_at": iso_utc(row["created_at"]),
    }


@router.post("/bookings", status_code=201)
def create_booking(body: BookingRequest):
    declined = False
    with pool.connection() as conn:
        flight_ids = [
            row["flight_id"]
            for row in conn.execute(
                "SELECT flight_id FROM hold_legs WHERE hold_id = %s ORDER BY leg",
                (body.hold_id,),
            )
        ]
        if not flight_ids:
            raise ApiError(410, "hold_gone", f"no hold {body.hold_id!r}")

        conn.execute(
            "SELECT flight_id FROM flights WHERE flight_id = ANY(%s) "
            "ORDER BY flight_id FOR UPDATE",
            (flight_ids,),
        )

        hold = conn.execute(
            """
            SELECT passengers, status, expires_at > clock_timestamp() AS live
            FROM holds
            WHERE hold_id = %s
            FOR UPDATE
            """,
            (body.hold_id,),
        ).fetchone()
        if hold["status"] != "active" or not hold["live"]:
            raise ApiError(410, "hold_gone", f"hold {body.hold_id!r} is expired or already used")

        if len(body.passengers) != hold["passengers"]:
            raise ApiError(422, "passenger_count",
                           f"hold is for {hold['passengers']} passengers, "
                           f"got {len(body.passengers)} names")

        if not body.payment_token.startswith("tok_ok_"):
            conn.execute(
                "UPDATE holds SET status = 'released' WHERE hold_id = %s",
                (body.hold_id,),
            )
            declined = True
        else:
            conn.execute(
                "UPDATE holds SET status = 'booked' WHERE hold_id = %s",
                (body.hold_id,),
            )
            conn.execute(
                "UPDATE flights SET seats_sold = seats_sold + %s WHERE flight_id = ANY(%s)",
                (hold["passengers"], flight_ids),
            )
            booking_id = "b_" + secrets.token_hex(8)
            conn.execute(
                "INSERT INTO bookings (booking_id, hold_id) VALUES (%s, %s)",
                (booking_id, body.hold_id),
            )
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO booking_passengers (booking_id, seq, name) VALUES (%s, %s, %s)",
                    [(booking_id, seq, passenger.name)
                     for seq, passenger in enumerate(body.passengers, start=1)],
                )
            booking = load_booking(conn, booking_id)

    if declined:
        raise ApiError(402, "payment_declined", "the card was declined and the hold released")
    return booking