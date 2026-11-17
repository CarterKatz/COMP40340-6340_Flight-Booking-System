CREATE TABLE flights (
    flight_id            TEXT PRIMARY KEY,
    flight_number        TEXT NOT NULL,
    origin               CHAR(3) NOT NULL,
    destination          CHAR(3) NOT NULL,
    scheduled_departure  TIMESTAMPTZ NOT NULL,
    scheduled_arrival    TIMESTAMPTZ NOT NULL,
    origin_local_date    DATE NOT NULL,
    capacity             INTEGER NOT NULL,
    base_fare_cents      INTEGER NOT NULL,
    distance_mi          INTEGER NOT NULL,
    seats_sold           INTEGER NOT NULL DEFAULT 0,
    CHECK (seats_sold BETWEEN 0 AND capacity)
);

COPY flights (flight_id, flight_number, origin, destination,
              scheduled_departure, scheduled_arrival, origin_local_date,
              capacity, base_fare_cents, distance_mi)
FROM '/docker-entrypoint-initdb.d/flights.csv'
WITH (FORMAT csv, HEADER true);

CREATE TABLE holds (
    hold_id       TEXT PRIMARY KEY,
    itinerary_id  TEXT NOT NULL,
    passengers    INTEGER NOT NULL CHECK (passengers BETWEEN 1 AND 9),
    price_cents   INTEGER NOT NULL,
    total_cents   INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'booked', 'released')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL
);

CREATE TABLE hold_legs (
    hold_id    TEXT NOT NULL REFERENCES holds (hold_id) ON DELETE CASCADE,
    leg        SMALLINT NOT NULL,
    flight_id  TEXT NOT NULL REFERENCES flights (flight_id),
    PRIMARY KEY (hold_id, leg)
);

CREATE INDEX hold_legs_by_flight ON hold_legs (flight_id);

CREATE TABLE bookings (
    booking_id    TEXT PRIMARY KEY,
    hold_id       TEXT NOT NULL UNIQUE REFERENCES holds (hold_id),
    status        TEXT NOT NULL DEFAULT 'confirmed'
                  CHECK (status IN ('confirmed', 'cancelled')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_at  TIMESTAMPTZ
);

CREATE TABLE booking_passengers (
    booking_id  TEXT NOT NULL REFERENCES bookings (booking_id) ON DELETE CASCADE,
    seq         SMALLINT NOT NULL,
    name        TEXT NOT NULL,
    PRIMARY KEY (booking_id, seq)
);

CREATE TABLE idempotency_keys (
    idempotency_key  TEXT PRIMARY KEY,
    request_hash     TEXT NOT NULL,
    status_code      INTEGER,
    response_body    JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);