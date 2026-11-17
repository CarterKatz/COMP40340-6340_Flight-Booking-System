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
curl http://localhost:8080/health
```

## Stop the airline

```
docker compose down
```

Bookings are kept in a Docker volume and survive restarts. To wipe all
data as well, run `docker compose down -v`.