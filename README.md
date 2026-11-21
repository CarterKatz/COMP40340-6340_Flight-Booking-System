# Flight Booking System

Airline reservation backend for COMP 4340/6340 Project 2, built to the
Flight Control contract.

## Start the airline

Requires Docker Desktop. From the repository's top folder:

```
docker compose up --build
```

- URL: `http://localhost:8080`
- API key: `dev-key`

Check that it is running:

```
curl.exe -H "Authorization: Bearer dev-key" http://localhost:8080/health
```

## Stop the airline

```
docker compose down
```

Bookings are kept in a Docker volume and survive restarts. To wipe all
data as well, run `docker compose down -v`.

## Search implementation and verification

The search endpoint uses PostgreSQL for route discovery, seat availability,
ranking, and result selection. Read [the search design](docs/SEARCH_DESIGN.md)
for the implementation walkthrough, ownership, and measured results.

For isolated PostgreSQL tests and benchmark commands, see
[search verification](tests/SEARCH.md). The API requirements are in
[CONTRACT.md](CONTRACT.md). The test commands require `uv sync` first.
