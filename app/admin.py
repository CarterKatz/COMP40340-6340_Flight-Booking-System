import asyncio
import os

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.bookings import BOOKING_SELECT, booking_json
from app.db import pool
from app.errors import ApiError
from app.holds import seat_counts

router = APIRouter(prefix="/admin")


@router.get("/stats")
def stats():
    with pool.connection() as conn:
        sold = conn.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE b.status = 'confirmed') AS bookings_confirmed,
                COUNT(*) FILTER (WHERE b.status = 'cancelled') AS bookings_cancelled,
                COALESCE(SUM(h.passengers) FILTER (WHERE b.status = 'confirmed'), 0)
                    AS seats_sold,
                COALESCE(SUM(h.total_cents) FILTER (WHERE b.status = 'confirmed'), 0)
                    AS revenue_cents
            FROM bookings b
            JOIN holds h ON h.hold_id = b.hold_id
            """
        ).fetchone()
        held = conn.execute(
            """
            SELECT COUNT(*) AS active_holds,
                   COALESCE(SUM(passengers), 0) AS seats_held
            FROM holds
            WHERE status = 'active' AND expires_at > clock_timestamp()
            """
        ).fetchone()
    return {**sold, **held}


@router.get("/flights/{flight_id}")
def flight_seats(flight_id: str):
    with pool.connection() as conn:
        rows = seat_counts(conn, [flight_id])
    if not rows:
        raise ApiError(404, "unknown_flight", f"no flight {flight_id!r}")
    row = rows[0]
    return {
        "flight_id": row["flight_id"],
        "capacity": row["capacity"],
        "seats_sold": row["seats_sold"],
        "seats_held": row["seats_held"],
        "seats_available": row["capacity"] - row["seats_sold"] - row["seats_held"],
    }


@router.get("/bookings")
def export_bookings(offset: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=1000)):
    with pool.connection() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM bookings").fetchone()["n"]
        rows = conn.execute(
            BOOKING_SELECT + " ORDER BY b.created_at, b.booking_id LIMIT %s OFFSET %s",
            (limit, offset),
        ).fetchall()
    return {"total": total, "bookings": [booking_json(row) for row in rows]}


@router.post("/reset")
def reset():
    with pool.connection() as conn:
        conn.execute(
            "TRUNCATE booking_passengers, bookings, hold_legs, holds, idempotency_keys"
        )
        conn.execute("UPDATE flights SET seats_sold = 0 WHERE seats_sold <> 0")
    return {"status": "reset"}


@router.post("/restart")
async def restart():
    asyncio.get_running_loop().call_later(0.3, os._exit, 1)
    return JSONResponse({"status": "restarting"}, status_code=202)