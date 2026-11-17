import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from app.errors import error_response, install_error_handlers
from app.db import pool
from app.holds import router as holds_router
from app.bookings import router as bookings_router

BOOT_ID = uuid.uuid4().hex
EXPECTED_AUTH = f"Bearer {os.environ['API_KEY']}".encode()


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open(wait=True)
    yield
    pool.close()


app = FastAPI(lifespan=lifespan)
install_error_handlers(app)
app.include_router(holds_router)
app.include_router(bookings_router)


@app.middleware("http")
async def auth_and_timing(request: Request, call_next):
    start = time.perf_counter()

    supplied = request.headers.get("authorization", "").encode()
    if secrets.compare_digest(supplied, EXPECTED_AUTH):
        response = await call_next(request)
    else:
        response = error_response(401, "unauthorized", "missing or wrong API key")

    elapsed_ms = (time.perf_counter() - start) * 1000
    response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.1f}"
    return response


@app.get("/health")
def health():
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "boot_id": BOOT_ID, "version": "0.1"}