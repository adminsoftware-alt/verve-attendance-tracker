# Presence Intervals SQL Builder - Documentation

## Overview

This document tracks the development of a SQL-only query to calculate `presence_intervals` from raw BigQuery tables (`participant_events_p` + `room_mappings`), replacing the Python-based calculation.

**Goal**: Match Zoom's daily attendance report exactly - trust webhook data completely, no artificial caps.

---

## Query Evolution

### v2 (`build_presence_intervals_v2.sql`)
- **Initial version**: Basic state machine approach
- **Issue**: Used `meeting_last_event` as fallback → phantom hours when someone left early but meeting continued

### v3 (`build_presence_intervals_v3.sql`)
- **Fix**: Use `person_last_event` instead of `meeting_last_event`
- **Issue**: If person rejoins next day, their "last event" was from next day → still phantom hours

### v4 (`build_presence_intervals_v4.sql`)
- **Fix**: STRICT MODE - only count time between actual events
- **Issue**: LEAD computed before filtering fake lefts → breakout intervals corrupted
- **Issue**: Same-day rejoins (e.g., 02:56→16:00) still counted as continuation

### v5 (`build_presence_intervals_v5.sql`) ✅ FINAL
- **Fix**: Filter fake lefts AND fake joins BEFORE computing LEAD
- **Fix**: Real `participant_joined` = new session boundary (ends previous interval)
- **Result**: No phantom hours, correct breakout intervals, matches Zoom data

---

## Key Concepts

### IST Time Handling
```sql
DECLARE day_start_utc TIMESTAMP DEFAULT TIMESTAMP_SUB(TIMESTAMP(target_date), INTERVAL 330 MINUTE);
DECLARE day_end_utc TIMESTAMP DEFAULT TIMESTAMP_ADD(day_start_utc, INTERVAL 24 HOUR);
```
- IST = UTC + 5:30 (330 minutes)
- July 22 00:00 IST = July 21 18:30 UTC

### Login-Date Rule
- Session belongs to the IST day it STARTED
- Read partitions `d` and `d+1` for midnight-crossing meetings
- Only count intervals where `start_ts >= day_start_utc AND start_ts < day_end_utc`

### Fake Left Detection
A `participant_left` is FAKE (room transition, not real exit) if:
1. There's a `breakout_room_joined` within 5 seconds, AND
2. There's a `participant_joined` within 30 seconds after

### Fake Join Detection (v5)
A `participant_joined` is FAKE (part of room transition) if:
1. There's a `breakout_room_joined` within 5 seconds, AND
2. There's a `participant_left` within 30 seconds BEFORE

### Session Boundary Rule (v5)
- Real `participant_joined` = NEW SESSION
- Previous interval ends at its start (0 duration, filtered out)
- This prevents phantom hours when someone leaves at 02:56 and rejoins at 16:00

### Room Mappings
Room names come from TWO sources (newest wins):
1. `room_mappings` table (from Zoom App panel)
2. Raw event `room_name` field (from webhooks)

---

## Problems Fixed

| Issue | Example | Fix |
|-------|---------|-----|
| Phantom hours (next-day rejoin) | Anjali 17:01→03:00 (600 mins) | STRICT MODE: rejoin = 0 duration |
| Phantom hours (same-day gap) | Chinmay 02:56→16:00 (785 mins) | participant_joined = new session |
| Previous-day carryover | Harish 00:00→14:59 (899 mins) | participant_joined = new session |
| Missing first interval | Fiza missing 10:42→10:56 | Stricter fake_join detection |
| Corrupted breakout intervals | Breakout ending at fake_left | LEAD computed after filtering |

---

## Output Comparison with Zoom

### Verified Cases (July 22, 2026)

| Person | Query v5 | Zoom | Status |
|--------|----------|------|--------|
| Fiza Rizvi | 10:42→19:11, 13 intervals | 10:42→19:11, 13 intervals | ✅ Match |
| Harshita Gupta | 10:53→20:29, 577 mins | 10:53→20:29, 577 mins | ✅ Exact |
| Chinmay Deshpande | Gap 02:56→16:00 | Gap 02:56→16:00 | ✅ Fixed |
| Harish Kandi | Starts 14:59 | Starts 14:59 | ✅ Fixed |
| Aarti Vridam | 09:40→13:05 | 09:40→13:05 | ✅ Match |

### Expected Differences
- Query may show more granular intervals (each room transition)
- Zoom consolidates some micro-intervals
- Totals should be within 1-5 minutes due to rounding (CEIL vs Zoom's method)

---

## Files

| File | Purpose |
|------|---------|
| `build_presence_intervals_v5.sql` | **Final query** - for comparison output (IST times, minutes) |
| `build_presence_intervals_v4.sql` | Previous version (has same-day phantom bug) |
| `build_presence_intervals_v3.sql` | Earlier version (has next-day phantom bug) |
| `build_presence_intervals_v2.sql` | Initial version |
| `build_presence_intervals_explained.sql` | Original JS UDF version (reference) |

---

## How to Use

### 1. Compare with Zoom (Current v5)
```sql
-- Change date and run in BigQuery console
DECLARE target_date DATE DEFAULT DATE '2026-07-22';
-- ... rest of v5 query
```

Output columns:
- `start_ist`, `end_ist` - IST formatted times (HH:MM)
- `duration_mins` - rounded minutes
- `room_category` - main/breakout/break

### 2. Insert to presence_intervals Table
Use `build_presence_intervals_v5_insert.sql` (to be created) with:
```sql
BEGIN TRANSACTION;
  DELETE FROM presence_intervals WHERE event_date = target_date;
  INSERT INTO presence_intervals (...) SELECT ...;
COMMIT TRANSACTION;
```

---

## Room Categories

| Category | Matching Rule | Counted in Working Hours? |
|----------|---------------|---------------------------|
| `main` | Room name contains "main" or is "0.Main Room" | Yes |
| `breakout` | All other rooms | Yes |
| `break` | Room name contains "break time" | **No** |

---

## Query Parameters

```sql
DECLARE target_date DATE DEFAULT DATE '2026-07-22';  -- Change this
```

To rebuild a different date, change `target_date` and run.

---

## Validation Checklist

Before using in production:

- [ ] Compare 5-10 people's totals against Zoom sheet
- [ ] Check midnight-crossing sessions (people working past 00:00 IST)
- [ ] Verify no phantom hours (intervals > 10 hours should be rare)
- [ ] Confirm break time is excluded from working hours
- [ ] Test with INSERT to presence_intervals table

---

## Contact

Built: July 27, 2026
Dataset: `verve-attendance-tracker.breakout_room_calibrator`
Tables: `participant_events_p`, `room_mappings`, `presence_intervals`
