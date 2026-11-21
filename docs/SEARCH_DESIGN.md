# SQL flight search: design and handoff

## Contribution and scope

Ethan owns test 5. His implementation contribution replaces Tate's temporary
Python flight search with PostgreSQL route discovery, inventory calculation,
ranking and selection, plus focused integration tests and performance diagnostics.
Tate supplied the underlying schema, holds, booking/idempotency, cancellation,
administrative audit endpoints, and initial search. Passing test 5 validates the
combined service; it should not be attributed to search alone.

No schema migration or additional index is required. The API response and
reservation workflow remain compatible with Tate's implementation.

## Request handling

`app/search.py` validates airport codes and calls `selected_routes` using a pooled
database connection. FastAPI validates required inputs, dates, and party sizes
from one through nine. The endpoint formats the returned datetimes as UTC strings
ending in Z. Authentication and Server-Timing are supplied by existing middleware.

`app/search_routes.py` contains the parameterized SQL. Request values are passed
as parameters, never interpolated into query text. Its intermediate functions
support inspection and testing; production search calls `selected_routes`.

## How the SQL works

| Stage | Responsibility | Reason |
| --- | --- | --- |
| `first_legs` | Filter origin and origin-local date | UTC departure date can differ from the requested date |
| `routes` | Combine nonstops and valid one-stop flights | Both produce the same itinerary fields |
| `candidate_flights` | Extract distinct flight IDs | A shared leg needs one inventory calculation |
| `held` | Sum active, unexpired held passengers per flight | Avoid multiplying counts across itinerary joins |
| `inventory` | Subtract sold and held seats from capacity | Include flights with no holds using zero |
| `eligible` | Filter by the minimum availability across legs | Every leg must accommodate the whole party |
| `scored` | Calculate traveler preference | Balance fare, duration, and stops |
| `selected` | Prioritize nonstops and take at most twenty | Satisfy mandatory nonstop inclusion |
| Final output | Sort selected itineraries by score and ID | Stable presentation without exposing score |

Connections depart from the first leg's destination between 45 and 240 minutes
after arrival, inclusive. No second-leg date equality is imposed: overnight
connections are allowed. Connection checks also exclude returning to the origin.
Flight IDs are stored in travel order and joined with `+` for itinerary IDs.

Search availability uses one SQL statement's snapshot and `statement_timestamp()`
as its expiration cutoff. Negative inventory is not clamped, so inconsistencies
remain visible during inspection. PostgreSQL LEAST ignores NULL, allowing a
nonstop with no second leg to use the first leg's count.

Search does not reserve seats or lock inventory. A seat can disappear between a
search and a hold; the existing transactional hold endpoint must make the exact
reservation decision. The contract permits search availability to be briefly stale.

## Ranking and selection

The ranking formula preserves the original preference weights:

```text
score = price_cents + 3300 * trip_hours + 8700 * stops
```

Trip duration includes the layover. Scores are numeric and do not change the fare
charged by a hold. Fare remains the sum of the legs' base fares, per passenger.

Selection orders by stops, score and itinerary ID before limiting to twenty.
The chosen rows are then ordered by score and itinerary ID for display. This
keeps an expensive nonstop in the list while allowing a cheaper connection to
appear ahead of it. C collation makes ties independent of database locale.
Tests verify the catalog never has more than twenty nonstops for a single
origin/destination/local-date group, which would make the contract contradictory.

## Performance investigation

The first SQL version passed the audit but increased search p95 server time from
the initial Python audit's 12.4 ms to 56.3 ms. Controlled handler comparisons and
EXPLAIN ANALYZE reproduced the slowdown. Overestimated candidate counts caused a
full scan of 175,224 flights when only 7-66 inventory rows were relevant. One
sample also scanned the catalog during connection discovery.

The optimized query uses a correlated LATERAL connection lookup with `OFFSET 0`
to prevent flattening into the observed global join. This is a deliberate planner
boundary, not a pagination requirement. Inventory uses a candidate ID array with
ANY, enabling primary-key lookups instead of the broad inventory join.

Existing indexes support the query: `(origin, origin_local_date)`,
`(origin, scheduled_departure)`, the flight primary key, and hold-leg indexes.
No global PostgreSQL planner settings were changed. Recheck plans if the catalog,
PostgreSQL version, or inventory history changes substantially.

## Validation evidence

| Validation | Result | Search p95 server time |
| --- | --- | --- |
| Focused PostgreSQL/HTTP suite, optimized implementation | 50 passed; latest isolated run 7.70 seconds | Not a traffic measurement |
| Steady test before plan optimization | 110/110 correct | 56.6 ms |
| Audit before plan optimization, default seed | 1778/1778 correct, score 1.00 | 56.3 ms |
| Audit after plan optimization, default seed | 1783/1783 correct, score 1.00 | 12.9 ms |
| Audit after plan optimization, seed 4341 | 1743/1743 correct, score 1.00 | 9.9 ms |

Each passing audit reported exact reconciliation and rules kept. These are
observed runs, not proof of every possible schedule or interleaving. The final
post-optimization steady rerun has not been reported. A prior test 3 passed 10/10
on Tate's baseline; it has not been rerun for the final search changes.

Final repository-wide verification passed all 72 tests in 39.56 seconds,
including the search checks and the pre-existing grader tests. Git whitespace
checks passed. Changes remain uncommitted because this execution environment
cannot write Git's index; staging and commit commands are in tests/SEARCH.md.

The focused suite uses controlled schedules and the full 175,224-flight catalog.
It compares route sets for 56 searches and selected SQL/HTTP results for two
party sizes (112 responses) against independently computed expected values.
Other checks cover holds, expiration, sold-out legs, duplicates, cancellations,
validation, authentication, ranking and the result limit. The fixture uses real
PostgreSQL transactions but sequential connections, so it does not replace
production pool, startup or concurrency testing. The existing TestClient
deprecation warning does not fail these checks.

`tests/search_benchmark.py` compares the original search at commit 9960185 against
the current handler in an isolated schema. It alternates implementations, drops
two warmups, and reports ten warmed measurements for each of three routes.
The optional history fixture contains 5,300 holds, including 106 active holds.
Handler medians with history were 16.9/37.9/27.9 ms for the original and
13.7/14.5/11.4 ms for optimized SQL. These are direct sequential handler timings,
not HTTP p95 or sustained throughput.

## Review together

1. Follow the short request handler in `app/search.py`.
2. Trace one BOI-DEN-JFK itinerary through route discovery and inventory.
3. Explain why held passengers are grouped before attaching both legs.
4. Distinguish selecting nonstops first from ordering the final display.
5. Read the plan evidence and explain why indexes alone were insufficient until
   the query shape allowed targeted lookups.

## Handoff boundaries

The intended change set is README.md, app/search.py, app/search_routes.py,
compose.test.yaml, tests/test_search.py, tests/search_benchmark.py,
tests/SEARCH.md, and this design document. Local IDE directories are unrelated.
No changes to the grader, production schema, or booking/locking code are needed.
Use the commands in tests/SEARCH.md to reproduce checks, and rebuild the app
container before running traffic against changed source. Confirm any separate
paper/presentation requirements against the course assignment.
