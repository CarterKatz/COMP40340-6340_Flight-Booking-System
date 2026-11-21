import datetime

from fastapi import APIRouter, Query

from app.db import pool
from app.errors import ApiError
from app.holds import iso_utc
from app.search_routes import selected_routes

router = APIRouter()


def airport_exists(conn, code: str) -> bool:
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM flights WHERE origin = %s) AS found",
        (code,),
    ).fetchone()
    return row["found"]


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

        rows = selected_routes(conn, origin, destination, date, passengers)

    return {"itineraries": [
        {**row, "departure": iso_utc(row["departure"]), "arrival": iso_utc(row["arrival"])}
        for row in rows
    ]}
