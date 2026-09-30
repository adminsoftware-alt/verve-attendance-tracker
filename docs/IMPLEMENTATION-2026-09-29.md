# Implementation: review points 2, 3, 5, 6, 8, 10, 11 — before vs now

**Date:** 29 Sep 2026
**Status:** All code changes are written and covered by **49 automated tests, all passing** (`pytest tests/`). Nothing is deployed yet, and nothing has run against real Zoom or real BigQuery — the "How to test" column below is your checklist for that.

**Rollout order that keeps everything reversible:**
1. Run the tests locally (point 11) — no cloud access needed.
2. Deploy the code — every behaviour change is either off by default (Pub/Sub), additive (new endpoints, new IDs), or self-contained (mapping fixes).
3. Run `docs/10_build_presence_intervals_v15.sql` in BigQuery when ready (point 6) — rollback is re-running the v14 file.
4. Turn Pub/Sub on last, following `docs/PUBSUB-ROLLOUT.md`.

---

## Point 2 — Pub/Sub buffer (already built earlier, still OFF)

| | |
|---|---|
| **Before** | Webhook saved straight to BigQuery. A failed save was logged and Zoom was still told "OK" → **the event was lost forever**. |
| **Now** | With `WEBHOOK_PUBSUB_ENABLED=true`: webhook verifies the signature, queues the event, replies to Zoom fast. A failed BigQuery save is retried automatically (10s→10min, 10 tries, then a dead-letter queue we can inspect and replay). If Pub/Sub itself is down, the event is processed inline like before — never dropped. |
| **Files** | `zt_pubsub.py`, `/webhook` + `/pubsub/*` routes in `app.py`, `scripts/setup_pubsub.sh`, `docs/PUBSUB-ROLLOUT.md` |
| **How to test** | Follow `docs/PUBSUB-ROLLOUT.md` steps 1–5. Until you set the env var, behaviour is unchanged — verify with `curl .../pubsub/status` → `"enabled": false`. |
| **Impact** | No silent data loss; wrong "guessed leave time" hours from lost events stop happening. |

## Point 3 — Idempotent events (deterministic event_id)

| | |
|---|---|
| **Before** | Every stored event got a **random** ID. Duplicates were only caught by a 60-second in-memory cache — a restart (every deploy!), a late retry, or a second instance let the duplicate through, and nothing downstream could tell it was a duplicate. |
| **Now** | `event_id` is a **hash of the event itself** (meeting + person + type + time + room): the same Zoom event always gets the same ID. Three layers now drop repeats: ① the 60s cache (unchanged), ② BigQuery streaming dedup (`row_ids=` on insert), ③ the v15 SQL keeps one row per `event_id`. Camera events too. Old rows keep their random IDs (unique anyway — history unaffected). |
| **Files** | `deterministic_event_id()` + 5 handler changes + `row_ids` in `app.py`; `QUALIFY` dedup in `docs/10_build_presence_intervals_v15.sql` |
| **How to test** | `pytest tests/test_idempotency.py -v` (7 tests, incl. the "restart between duplicates" case). In production after deploy: new rows in `participant_events_p` have IDs starting `e1-`. Count real duplicates with the query in the v15 file header. |
| **Impact** | Duplicate joins/leaves can no longer distort the hours state machine, and duplicates become **countable** (feeds the health dashboard). Also makes Pub/Sub retries and backfills safe to run. |

## Point 5 — Room mapping: own module + DISPUTED survives restarts + 2 real bug fixes

| | |
|---|---|
| **Before** | All mapping decisions lived inline in the 14k-line `app.py`. **Bug A:** a person with both an email and a name counted as **2 witnesses**, silently bypassing the "2 people must agree" rule (the rule that exists because of the 17–20 Aug incident: 63 hours filed as break). **Bug C:** two no-email people sharing a display name overwrote each other and could produce a wrong room pairing. **DISPUTED freezes lived only in memory** — every deploy forgot them and a frozen room could resume flip-flopping the same day. |
| **Now** | Witness decisions moved to `zt_mapping.py`: one person = one witness; a name shared by 2+ no-email people is excluded as a witness entirely. Every DISPUTED freeze is also written to a new BigQuery table `mapping_disputes` (auto-created on first use) and **re-loaded after a restart**, so frozen stays frozen. Correction/stability/anti-ping-pong logic itself is unchanged. |
| **Files** | `zt_mapping.py` (new), `/mapping/sync` in `app.py` calls into it, `Dockerfile` COPY line |
| **How to test** | `pytest tests/test_mapping.py -v` (9 tests; the one-person-held test *fails* if the old counting is restored — verified). In production: run the Room Mapper panel as usual; logs show `Ignoring ambiguous shared name(s)` when relevant and `Restored N DISPUTED freeze(s)` after a restart on a day with disputes. `SELECT * FROM mapping_disputes` shows the frozen rooms. |
| **Impact** | Fewer wrongly-named rooms (the exact failure class of the August incident closed properly); a deploy can no longer un-freeze a disputed room; which rooms are uncertain is now queryable instead of buried in logs. Note: with the double-count fixed, a genuinely single-occupant room now correctly **waits** for a second witness — that is the rule working, not a regression. |

## Point 6 — meeting_uuid + room_uuid in the hours SQL (v15)

| | |
|---|---|
| **Before** | The BigQuery builder matched room names by `room_uuid` alone. If Zoom ever reused a room ID across meeting instances, a stale name could label the new instance's room. |
| **Now** | `docs/10_build_presence_intervals_v15.sql` (copy of v14 + 3 marked edits, verified by diff): name resolution is now **human override → mapping for THIS meeting_uuid+room_uuid → v14 global chain**. The fallback is kept deliberately so old mapping rows with an empty meeting_uuid still resolve — nothing that named correctly under v14 becomes "Unknown Room" under v15. Plus the point-3 event dedup. |
| **How to test** | **Not run yet — this file must be run by you in BigQuery.** The file header contains: a preflight query (how many mappings are scoped vs legacy), and the after-check (`CALL sp_build_presence_intervals(DATE '...')` on a past date, then compare per-person totals with a day you know is right). Rollback: re-run the v14 file. |
| **Impact** | A meeting restart can no longer let one instance's room name contaminate another when a same-instance mapping exists. Day-to-day hours should come out identical — that's exactly what the past-date comparison verifies. |

## Point 8 — Data-quality metrics + System Health dashboard

| | |
|---|---|
| **Before** | Health-check SQL existed (`docs/05*`) but with no page, no API, and unclear whether it was even installed. Finding a problem meant reading logs or waiting for HR to complain. |
| **Now** | `GET /health/summary` — JSON with: events today, last insert, **duplicate groups**, unknown-room time %, last hours build, disputed rooms today, Pub/Sub counters, webhook liveness, and the latest `v_health_latest` watchdog rows if installed. Each metric is isolated (one failure reports itself, the page still loads) and cached 60s. `GET /health/dashboard` — a server-rendered System Health page (green/amber/red tiles, auto-refresh) that does **not** touch the React app, so it cannot break the frontend build. |
| **Files** | `zt_observability.py` (new), 4 routes in `app.py` |
| **How to test** | `pytest tests/test_health.py -v` (10 tests, incl. "BigQuery down → page still answers"). After deploy: open `https://breakout-room-calibrator-…run.app/health/dashboard` in a browser. If the checks panel says "watchdog not installed", run `docs/05a/05b/05c` in BigQuery once and schedule them. |
| **Impact** | A dead webhook feed, a mapping problem, or a stalled build is visible within a minute on one page, instead of days later in a wrong report. |

## Point 10 — Observability

| | |
|---|---|
| **Before** | `/health`, `/monitor/health`, `/mapping/health` existed; everything else was ~440 unstructured `print()`s that Cloud Monitoring can't alert on. |
| **Now** | `GET /health/webhook` — seconds since the last Zoom event (in-memory, zero cost; returns **503 when stale >30 min**, so Cloud Monitoring's built-in uptime check on this URL becomes the "Zoom stopped talking to us" alert with no code). `GET /health/bigquery` — reachability probe, cached 60s, 503 when down. `zt_observability.log_json()` — structured JSON logging that Cloud Logging parses; used by new code, old prints migrate gradually (all 440 were **not** rewritten — deliberate). |
| **How to test** | Covered in `tests/test_health.py`. After deploy: `curl .../health/webhook` during work hours → HEALTHY; then create a Cloud Monitoring uptime check on `/health/webhook` expecting HTTP 200. |
| **Impact** | The two failure modes that silently cost data (Zoom feed dead, BigQuery unreachable) each have a one-URL check that can page someone. |

## Point 11 — Automated tests

| | |
|---|---|
| **Before** | **Zero tests.** Every refactor of the most delicate code (mapping, webhook) was a blind change. |
| **Now** | **49 tests** in `tests/`: 21 Pub/Sub (webhook path, retries, auth, fallback), 7 idempotency, 11 mapping (witness rules, sync integration, DISPUTED persistence), 10 health/observability — plus **5 realistic Zoom payload fixtures** (`tests/fixtures/`: join, leave, breakout join/leave, reconnect) so future tests replay real event shapes. Two key tests were mutation-checked: deliberately restoring the old buggy behaviour makes them fail. No BigQuery/Zoom access needed — everything is faked. |
| **How to test** | `uv run --no-project --python 3.11 --with-requirements requirements.txt --with pytest python -m pytest tests/ -q` → `49 passed`. |
| **Impact** | The August witness bug, the lost-event bug, and the dedup-swallows-retry bug are now permanent regression tests — they cannot quietly come back. |

---

## Honest list of what is NOT done

- **v15 SQL has not been executed** — I cannot run BigQuery from here. Run its preflight, run it, compare a past date (instructions in the file header).
- **Pub/Sub GCP resources not created / feature off** — `scripts/setup_pubsub.sh` + rollout doc are ready.
- **Cloud Build still deploys without running tests** (point 12, agreed as later). Until then, run `pytest tests/` before pushing.
- **The 440 old print() statements are not converted** to structured logs — gradual, by design.
- **No React "System Health" page** — the served `/health/dashboard` page covers it without risking the frontend build; a frontend port can come later.
- **Full extraction of the sync orchestration** out of app.py (rest of point 4/5) is staged for after these tests have run in production for a while.

## Quick production test checklist (after deploy)

```bash
BASE=https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app
curl $BASE/health/webhook      # HEALTHY during work hours
curl $BASE/health/bigquery     # HEALTHY
curl $BASE/health/summary      # JSON, all sections present
open $BASE/health/dashboard    # the System Health page
curl $BASE/pubsub/status       # "enabled": false until you opt in
# BigQuery: new events have ids starting 'e1-' ; after first dispute:
#   SELECT * FROM breakout_room_calibrator.mapping_disputes
```
