# Archived builder SQL — do NOT run these

Superseded versions of `sp_build_presence_intervals`, kept for history only.

The ONE live file is `docs/12_build_presence_intervals_v16.sql` (installed
2026-10-05): the owner's tested v11 text, untouched, plus additions only
(room_uuid column, event_id dedup, HR name/category override, same-meeting
mapping). Rollback = `docs/11_build_presence_intervals_v15_1.sql` (live
2026-09-30 to 2026-10-05); before that,
`docs/09b_deployed_production_2026-09-30.sql`.

Known trade-off of v16, measured before install (5 days, side by side with
v15.1): total hours identical on every day; the only difference is v11's
room-name rule (an event's own stamped name wins), which can give one room
two names in a day — 16 people / 13.8 h moved to break on 2026-09-23,
9 people / 6.6 h on 2026-09-30, zero on the other three days. A wrong name is
fixed for the whole day with the pencil on the Live card (HR override wins).

Why this folder exists (review point 7): on 2026-09-30 we found that the
procedure running in BigQuery had drifted from the repo's v14 file (hot-
patched in the console). Installing a version built from the stale repo file
silently reverted those patches. Rule from now on: change the SQL in the
repo first, install from the repo file, never edit the procedure in the
BigQuery console.

| File | Was |
|---|---|
| build_presence_intervals_v2..v7 | Python-era builders (pre-2026-07-21) |
| 02_..._v11 | first all-BigQuery builder |
| 06_..._v12, 07_backfill_v12 | room-name fix + backfill |
| 08_room_overrides_v13 | human overrides |
| 09_break_guard_v14 | break-room guard (the repo's last version before the drift) |
| 10_..._v15 | built from the stale v14 file; rolled back within minutes, never left in production |
