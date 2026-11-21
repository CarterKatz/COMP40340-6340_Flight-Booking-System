# Search verification

See [the design and results](../docs/SEARCH_DESIGN.md) for architecture,
contribution boundaries, performance findings, and the code review guide.

## Isolated PostgreSQL and HTTP checks

From the repository root in PowerShell, with Docker Desktop running:

```powershell
uv sync
docker compose -f compose.test.yaml up -d --wait
$env:SEARCH_TEST_DATABASE_URL = 'postgresql://search_test:search_test@127.0.0.1:55432/search_test'
uv run pytest tests/test_search.py -v -x
```

The latest isolated optimized run passed all 50 checks in 7.70 seconds.
The full repository suite also passed: 72 checks in 39.56 seconds with the same
known deprecation warning. To include the existing grader tests, use
`uv run pytest -q -x` with the test database configured.
The suite includes 56 full-catalog route comparisons and 112 selected-result HTTP
comparisons. Add `-s` to display the three EXPLAIN ANALYZE diagnostic summaries.
These probe timings are not assertions or performance benchmarks.

The separate PostgreSQL container listens only on loopback port 55432 and uses
ephemeral storage. Every test creates and removes a unique schema using the
production DDL without its Docker-specific catalog COPY. Most tests use small
fixtures; the catalog test loads all 175,224 generated flights. The running
production airline database is not reset by these checks.

Without SEARCH_TEST_DATABASE_URL the integration tests explicitly skip. An
unreachable configured database stops the run after a connectivity check with a
five-second connection timeout, rather than retrying every test. Fixture queries
and lock waits have five-second limits; bulk catalog setup has a thirty-second
statement timeout. Sequential tests use real PostgreSQL transactions instead of
background pool workers. They do not test production pool concurrency or startup.
The TestClient/httpx deprecation warning is known and did not fail validation.

## Controlled performance diagnostics

Keep the isolated database running and the environment variable set, then run
these separately from other tests for cleaner timing comparisons:

```powershell
uv run python tests/search_benchmark.py
uv run python tests/search_benchmark.py --with-holds
```

The script compares Tate's original search from commit 9960185 against the current
handler in the same isolated schema. That commit must be available in local Git
history. It alternates old/new call order, discards two warmups, and reports the
median and maximum of ten direct handler calls per implementation and route.
It also prints EXPLAIN ANALYZE plans with actual/estimated rows and buffer hits.
The history option adds fifty holds per candidate flight: 5,300 total, 106 active.
These sequential timings exclude the HTTP transport and are not p95 estimates.
No production data or global planner settings are changed.

Stop the isolated database when finished (its temporary contents are discarded):

```powershell
docker compose -f compose.test.yaml down
```

## Traffic against the running airline

Rebuild first so the container runs current source. Run tests sequentially.
Test 1 is unscored and runs two minutes by default. Each audit resets the running
airline's bookings and holds, generates three minutes of traffic, waits for
background work, then drains holds for seventy seconds before reconciliation.

```powershell
docker compose up -d --build
uv run flightcontrol test --url http://localhost:8080 --key dev-key --test 1
uv run flightcontrol test --url http://localhost:8080 --key dev-key --test 5
uv run flightcontrol test --url http://localhost:8080 --key dev-key --test 5 --seed 4341
```

The optimized default-seed audit passed with 1783/1783 correct travelers, score
1.00, and 12.9 ms search p95 server time. Seed 4341 passed with 1743/1743 correct,
score 1.00, and 9.9 ms search p95 server time. Both reported exact reconciliation.
A final post-optimization steady rerun is not yet recorded; the earlier steady
run before optimization passed 110/110. These results do not establish success
for every hidden seed or heavy-load test.

## Files to include in the team handoff

```text
README.md
app/search.py
app/search_routes.py
compose.test.yaml
docs/SEARCH_DESIGN.md
tests/SEARCH.md
tests/test_search.py
tests/search_benchmark.py
```

Review and stage these explicit paths rather than all untracked files. Local IDE
folders are unrelated. The SQL search needs no migration or new index. The
existing hold, booking, cancellation and administrative endpoints are reused.

After reviewing the intended changes, stage just those files and commit:

```powershell
git add -- README.md app/search.py app/search_routes.py compose.test.yaml docs/SEARCH_DESIGN.md tests/SEARCH.md tests/test_search.py tests/search_benchmark.py
git diff --cached --stat
git commit -m "Implement and verify SQL flight search"
git push origin Ethan
```
