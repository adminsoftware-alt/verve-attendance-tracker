# Archived builder SQL — do NOT run these

Superseded versions of `sp_build_presence_intervals`, kept for history only.

The ONE live file is `docs/11_build_presence_intervals_v15_1.sql` (installed
2026-09-30). The exact production text before it is
`docs/09b_deployed_production_2026-09-30.sql` (the rollback).

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
