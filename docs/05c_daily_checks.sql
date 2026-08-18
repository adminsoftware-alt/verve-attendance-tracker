-- Part 3 of 3 — paste this WHOLE file into BigQuery and click Run.
-- Creates sp_health_daily() - the once-a-morning checks.
-- Run parts in order: 1, then 2, then 3.

-- ############################################################################
-- ##  DAILY CHECKS — schedule 06:30 IST (AFTER the 06:00 yesterday rebuild) ##
-- ##  CALL `...breakout_room_calibrator.sp_health_daily`();                  ##
-- ############################################################################
CREATE OR REPLACE PROCEDURE
`verve-attendance-tracker.breakout_room_calibrator.sp_health_daily`()
BEGIN

  DECLARE run_ts    TIMESTAMP DEFAULT CURRENT_TIMESTAMP();
  DECLARE run_ist   STRING    DEFAULT FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', CURRENT_TIMESTAMP(), 'Asia/Kolkata');
  DECLARE biz_date  DATE      DEFAULT DATE(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 5 HOUR), 'Asia/Kolkata');
  DECLARE prev_date DATE      DEFAULT DATE_SUB(DATE(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 5 HOUR), 'Asia/Kolkata'), INTERVAL 1 DAY);
  DECLARE ist_hour  INT64     DEFAULT EXTRACT(HOUR FROM CURRENT_TIMESTAMP() AT TIME ZONE 'Asia/Kolkata');

  -- Yesterday's business window in UTC: 05:00 IST -> 05:00 IST next day.
  -- (-330 min puts midnight IST at the right instant; +5h / +29h are the
  -- business-day edges.) Used by check 23.
  DECLARE day_start TIMESTAMP DEFAULT
    TIMESTAMP_ADD(TIMESTAMP_SUB(TIMESTAMP(prev_date), INTERVAL 330 MINUTE), INTERVAL 5 HOUR);
  DECLARE day_end   TIMESTAMP DEFAULT
    TIMESTAMP_ADD(TIMESTAMP_SUB(TIMESTAMP(prev_date), INTERVAL 330 MINUTE), INTERVAL 29 HOUR);

  ---------------------------------------------------------------------------
  -- 02  YESTERDAY REBUILD RAN — the 06:00 job applies the past-day 10-minute
  --     cap. Without it, yesterday keeps today's inflated open segments and
  --     every hours check below is measuring the wrong thing. GATEKEEPER —
  --     read this one first.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, prev_date, '02', 'Yesterday rebuild ran', 'yesterday',
    CASE WHEN ist_hour < 7      THEN 'SKIPPED'   -- 06:00 job may not have run
         WHEN built IS NULL     THEN 'ALARM'
         WHEN built < expected  THEN 'ALARM'
         ELSE 'OK' END,
    CAST(TIMESTAMP_DIFF(run_ts, built, MINUTE) AS FLOAT64),
    'yesterday rebuilt after 06:00 IST today',
    IFNULL(CONCAT('yesterday last built ',
                  FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', built, 'Asia/Kolkata'), ' IST'),
           'NO ROWS for yesterday'),
    'Run the presence-intervals-yesterday scheduled query, or CALL sp_build_presence_intervals(<yesterday>).'
  FROM (
    SELECT MAX(built_at) AS built,
           TIMESTAMP_ADD(TIMESTAMP_SUB(TIMESTAMP(biz_date), INTERVAL 330 MINUTE),
                         INTERVAL 6 HOUR) AS expected
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date = prev_date
  );

  ---------------------------------------------------------------------------
  -- 07  IMPOSSIBLE HOURS — would have caught the v9 phantom 240-minute tail.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, prev_date, '07', 'Impossible hours', 'yesterday',
    CASE WHEN n = 0 THEN 'OK' ELSE 'ALARM' END,
    CAST(n AS FLOAT64), '0 people above 14h after the 06:00 rebuild',
    IFNULL((SELECT STRING_AGG(txt, '; ') FROM (
              SELECT CONCAT(ANY_VALUE(participant_name), ' ',
                     CAST(ROUND(SUM(duration_seconds)/3600, 1) AS STRING), 'h') AS txt
              FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
              WHERE event_date = prev_date
              GROUP BY participant_key
              HAVING SUM(duration_seconds)/3600 > 14
              LIMIT 5)), 'none'),
    'A phantom open segment or a missed reconnect pair. Inspect their raw rows in participant_events_p for the day.'
  FROM (
    SELECT COUNT(*) AS n FROM (
      SELECT participant_key
      FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
      WHERE event_date = prev_date
      GROUP BY participant_key
      HAVING SUM(duration_seconds)/3600 > 14
    )
  );

  ---------------------------------------------------------------------------
  -- 09  GUESSED ENDINGS % — confidence 0.35 means "no leave webhook arrived,
  --     the ending was invented". A rising share = webhook delivery degrading,
  --     which corrupts hours silently.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, prev_date, '09', 'Guessed endings %', 'yesterday',
    CASE WHEN total = 0 THEN 'SKIPPED'
         WHEN pct > 20  THEN 'ALARM'
         WHEN pct > 10  THEN 'WARN'
         ELSE 'OK' END,
    pct, '<= 10% of intervals at confidence 0.35',
    CONCAT(CAST(guessed AS STRING), ' of ', CAST(total AS STRING),
           ' intervals had no closing webhook'),
    'Zoom is dropping participant_left events. Check the Marketplace subscription and Cloud Run error logs.'
  FROM (
    SELECT COUNT(*) AS total,
           COUNTIF(confidence <= 0.35) AS guessed,
           ROUND(100 * SAFE_DIVIDE(COUNTIF(confidence <= 0.35), COUNT(*)), 1) AS pct
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date = prev_date
  );

  ---------------------------------------------------------------------------
  -- 10  SAME PERSON IN TWO ROOMS AT ONCE — the invariant the v8 reconnect bug
  --     violated. Catches any future regression in the pairing logic.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, prev_date, '10', 'Overlapping rooms', 'yesterday',
    CASE WHEN n = 0 THEN 'OK' WHEN n <= 3 THEN 'WARN' ELSE 'ALARM' END,
    CAST(n AS FLOAT64), '0 overlaps longer than 60s',
    IFNULL((SELECT STRING_AGG(who, ', ') FROM (
              SELECT DISTINCT a.participant_name AS who
              FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals` a
              JOIN `verve-attendance-tracker.breakout_room_calibrator.presence_intervals` b
                ON  a.event_date      = b.event_date
                AND a.participant_key = b.participant_key
                AND a.interval_id    <  b.interval_id
                AND a.start_ts < b.end_ts AND b.start_ts < a.end_ts
              WHERE a.event_date = prev_date
                AND a.room_name != b.room_name
                AND TIMESTAMP_DIFF(LEAST(a.end_ts, b.end_ts),
                                   GREATEST(a.start_ts, b.start_ts), SECOND) > 60
              LIMIT 5)), 'none'),
    'One person cannot be in two rooms at once — the event pairing in sp_build_presence_intervals has regressed and their hours are double-counted.'
  FROM (
    SELECT COUNT(*) AS n
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals` a
    JOIN `verve-attendance-tracker.breakout_room_calibrator.presence_intervals` b
      ON  a.event_date      = b.event_date
      AND a.participant_key = b.participant_key
      AND a.interval_id    <  b.interval_id      -- count each pair once
      AND a.start_ts < b.end_ts AND b.start_ts < a.end_ts
    WHERE a.event_date = prev_date
      AND a.room_name != b.room_name
      AND TIMESTAMP_DIFF(LEAST(a.end_ts, b.end_ts),
                         GREATEST(a.start_ts, b.start_ts), SECOND) > 60
  );

  ---------------------------------------------------------------------------
  -- 11  STRUCTURAL SANITY — cheap, catches a half-failed build. Duplicates
  --     matter most: if the DELETE+INSERT ever half-commits and re-runs,
  --     every hour doubles with no other visible symptom.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  SELECT run_ts, run_ist, prev_date, '11', 'Structural sanity', 'yesterday',
    CASE WHEN bad_range + bad_dur + dupes = 0 THEN 'OK' ELSE 'ALARM' END,
    CAST(bad_range + bad_dur + dupes AS FLOAT64), '0 malformed or duplicate rows',
    CONCAT('end<=start: ', CAST(bad_range AS STRING),
           ' | duration<=0: ', CAST(bad_dur AS STRING),
           ' | duplicate (person,room,start): ', CAST(dupes AS STRING)),
    'Rebuild the day: CALL sp_build_presence_intervals(<date>). Duplicates mean an interrupted build — verify row counts afterwards.'
  FROM (
    SELECT
      COUNTIF(end_ts <= start_ts)                 AS bad_range,
      COUNTIF(COALESCE(duration_seconds, 0) <= 0) AS bad_dur,
      (SELECT COUNT(*) FROM (
         SELECT participant_key, room_name, start_ts
         FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
         WHERE event_date = prev_date
         GROUP BY 1, 2, 3
         HAVING COUNT(*) > 1
       ))                                         AS dupes
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date = prev_date
  );

  ---------------------------------------------------------------------------
  -- 14  UNRECOGNIZED PEOPLE — anyone with 10+ minutes yesterday who is in
  --     no registry row at all. They are on nobody's team, so their time
  --     appears in NO report.
  --
  --     THRESHOLD 10 MINUTES (2026-08-10, was 2h). The intent is that every
  --     human who enters the meeting is accounted for, including short
  --     visits. Interview candidates and guests are meant to be registered
  --     with category='interview' / 'visitor' — this check accepts ANY
  --     active registry row regardless of category, so registering them
  --     silences it permanently while keeping them out of employee reports.
  --
  --     Expect noise until that habit exists: at 10 minutes every drop-in
  --     counts. Bands are wide (<=10 people = WARN) for exactly that reason;
  --     tighten them once the registry has caught up.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  WITH per AS (
    SELECT participant_key,
           ANY_VALUE(participant_name) AS nm,
           LOWER(TRIM(COALESCE(ANY_VALUE(participant_email), ''))) AS em,
           LOWER(TRIM(REGEXP_REPLACE(ANY_VALUE(participant_name), r'[-_]\d+$', ''))) AS name_key,
           SUM(duration_seconds) / 60 AS mins
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date = prev_date
    GROUP BY participant_key
  ),
  reg_keys AS (
    -- ONE COLUMN of acceptable match keys — names, display names and emails
    -- in the same list. Flattening this way keeps the join below a plain
    -- equality; BigQuery rejects a correlated EXISTS whose condition is an
    -- OR of two different equalities ("LEFT SEMI JOIN cannot be used without
    -- ... an equality of fields from both sides").
    -- ANY category counts as recognized: 'employee', 'visitor', 'interview'.
    SELECT DISTINCT LOWER(TRIM(REGEXP_REPLACE(participant_name, r'[-_]\d+$', ''))) AS mk
    FROM `verve-attendance-tracker.breakout_room_calibrator.employee_registry`
    WHERE COALESCE(status, 'active') = 'active'
      AND participant_name IS NOT NULL AND TRIM(participant_name) != ''
    UNION DISTINCT
    SELECT DISTINCT LOWER(TRIM(REGEXP_REPLACE(display_name, r'[-_]\d+$', '')))
    FROM `verve-attendance-tracker.breakout_room_calibrator.employee_registry`
    WHERE COALESCE(status, 'active') = 'active'
      AND display_name IS NOT NULL AND TRIM(display_name) != ''
    UNION DISTINCT
    SELECT DISTINCT LOWER(TRIM(participant_email))
    FROM `verve-attendance-tracker.breakout_room_calibrator.employee_registry`
    WHERE COALESCE(status, 'active') = 'active'
      AND participant_email IS NOT NULL AND TRIM(participant_email) != ''
  ),
  per_keys AS (
    -- One row per (person, candidate key). The DISPLAY NAME row is the one
    -- that matters in practice: every employee_registry email is blank, so
    -- the registry can only ever be matched by name. When v11 keys a person
    -- on their EMAIL, participant_key is an email address and comparing it
    -- against a name-only registry never matches — which is why registered
    -- staff like Fiza Rizvi and Daksha Dhamal were reported as strangers.
    SELECT participant_key, nm, mins, participant_key AS mk FROM per
    UNION ALL
    SELECT participant_key, nm, mins, name_key        FROM per WHERE name_key != ''
    UNION ALL
    SELECT participant_key, nm, mins, em              FROM per WHERE em != ''
  ),
  matched AS (
    SELECT pk.participant_key,
           ANY_VALUE(pk.nm)   AS nm,
           ANY_VALUE(pk.mins) AS mins,
           LOGICAL_OR(rk.mk IS NOT NULL) AS in_registry
    FROM per_keys pk
    LEFT JOIN reg_keys rk ON rk.mk = pk.mk
    GROUP BY pk.participant_key
  ),
  unknown AS (
    SELECT nm, mins FROM matched
    WHERE mins >= 10 AND NOT in_registry
  )
  SELECT run_ts, run_ist, prev_date, '14', 'Unrecognized people', 'yesterday',
    CASE WHEN (SELECT COUNT(*) FROM unknown) = 0  THEN 'OK'
         WHEN (SELECT COUNT(*) FROM unknown) <= 10 THEN 'WARN'
         ELSE 'ALARM' END,
    CAST((SELECT COUNT(*) FROM unknown) AS FLOAT64),
    '0 unregistered people with 10min+',
    IFNULL((SELECT STRING_AGG(CONCAT(nm, ' (', CAST(CAST(ROUND(mins) AS INT64) AS STRING), 'm)'), ', ')
            FROM (SELECT nm, mins FROM unknown ORDER BY mins DESC LIMIT 10)), 'none'),
    'Register each of these in Employees. Staff -> category "employee"; interview candidates -> "interview"; guests -> "visitor". Any category silences this check; only "employee" appears in team reports.';

  ---------------------------------------------------------------------------
  -- 16  ROOM UUID -> MULTIPLE NAMES.  *** THE ASSUMPTION v11 RESTS ON ***
  --     v11 tier-3 name resolution takes a room name from room_mappings on
  --     ANY date, justified by "room_uuid -> room_name is provably 1:1 (176
  --     uuids, ZERO mapping to more than one name)". That was measured once.
  --     If a UUID is ever reused for a renamed/different room, cross-day
  --     resolution starts stamping the WRONG name on historical rooms —
  --     silently, with no error anywhere.
  --
  --     The 60-day window must match what the builder actually searches;
  --     narrowing it to save cost would stop verifying the real assumption.
  --     If this scan proves expensive, measure it (bq query --dry_run)
  --     before changing anything.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  WITH ev AS (
    SELECT DISTINCT room_uuid, room_name
    FROM `verve-attendance-tracker.breakout_room_calibrator.participant_events_p`
    WHERE event_date BETWEEN DATE_SUB(prev_date, INTERVAL 60 DAY) AND biz_date
      AND room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''
      AND room_name NOT LIKE 'Room-%' AND room_name != 'Unknown Room'
    UNION DISTINCT
    SELECT DISTINCT room_uuid, room_name
    FROM `verve-attendance-tracker.breakout_room_calibrator.room_mappings`
    WHERE room_uuid IS NOT NULL AND room_uuid != ''
      AND room_name IS NOT NULL AND room_name != ''
      AND room_name NOT LIKE 'Room-%'
  ),
  conflicts AS (
    SELECT room_uuid, COUNT(DISTINCT room_name) AS names,
           STRING_AGG(room_name, ' | ' ORDER BY room_name) AS name_list
    FROM ev GROUP BY room_uuid HAVING COUNT(DISTINCT room_name) > 1
  )
  SELECT run_ts, run_ist, biz_date, '16', 'Room UUID maps to one name', 'identity',
    CASE WHEN (SELECT COUNT(*) FROM conflicts) = 0 THEN 'OK' ELSE 'ALARM' END,
    CAST((SELECT COUNT(*) FROM conflicts) AS FLOAT64),
    '0 room UUIDs with more than one name (v11 cross-day resolution assumes 1:1)',
    -- FULL uuid, not SUBSTR: a truncated key cannot be looked up, which made
    -- the first real trip of this check impossible to investigate.
    IFNULL((SELECT STRING_AGG(CONCAT(room_uuid, ' = ', name_list), '; ')
            FROM (SELECT room_uuid, name_list FROM conflicts ORDER BY names DESC LIMIT 3)), 'none'),
    'A UUID with two names breaks the premise of v11 tier-3 (any-day) name resolution — rooms may be getting the WRONG name on days the mapper did not run. Restrict resolution to same-day mappings for these UUIDs, or re-map.';

  ---------------------------------------------------------------------------
  -- 20  ONE EMAIL, TWO PERSON-RECORDS — the highest-value check here.
  --
  --     v11 keys a person on their email only when their normalized name
  --     maps to exactly one email. If some of a person's events arrive with
  --     NO email attached, that variant keys on the NAME instead — one human,
  --     two participant_keys, and the day splits between them. Their team
  --     report shows a half day and nothing reports an error.
  --
  --     Grouping by EMAIL (not name) is what makes this work: the old
  --     name-based check only caught the split when both halves shared a
  --     display name, so it missed anyone who had also renamed themselves.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  WITH per AS (
    SELECT LOWER(TRIM(participant_email)) AS em,
           participant_key,
           ANY_VALUE(participant_name) AS nm,
           SUM(duration_seconds) / 3600 AS h
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date = prev_date
      AND participant_email IS NOT NULL AND TRIM(participant_email) != ''
    GROUP BY 1, 2
  ),
  split AS (
    SELECT em,
           COUNT(*) AS keys,
           STRING_AGG(CONCAT(nm, ' ', CAST(ROUND(h, 1) AS STRING), 'h'), ' + ' ORDER BY h DESC) AS parts
    FROM per
    GROUP BY em
    HAVING COUNT(*) > 1
  )
  SELECT run_ts, run_ist, prev_date, '20', 'One email split across records', 'identity',
    CASE WHEN (SELECT COUNT(*) FROM split) = 0 THEN 'OK' ELSE 'ALARM' END,
    CAST((SELECT COUNT(*) FROM split) AS FLOAT64),
    '0 email addresses producing more than one participant_key',
    IFNULL((SELECT STRING_AGG(CONCAT(em, ' = ', parts), '; ')
            FROM (SELECT em, parts FROM split ORDER BY keys DESC LIMIT 3)), 'none'),
    'One human is being counted as two people and their day is split between the rows — each looks like a partial day. Usually events arriving without an email attached. Add a participant_alias linking the variants, then rebuild the day.';

  ---------------------------------------------------------------------------
  -- 23  LATE-ARRIVING EVENTS — Zoom events for yesterday that landed AFTER
  --     the 06:00 rebuild closed the books. Yesterday's numbers changed and
  --     nobody rebuilt it, so the report that went out is already wrong.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  WITH built AS (
    SELECT MAX(built_at) AS built_at
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date = prev_date
  ),
  late AS (
    SELECT COUNT(*) AS n, MAX(e.inserted_at) AS latest
    FROM `verve-attendance-tracker.breakout_room_calibrator.participant_events_p` e
    CROSS JOIN built b
    WHERE e.event_date BETWEEN prev_date AND DATE_ADD(prev_date, INTERVAL 1 DAY)
      AND e.event_timestamp >= day_start
      AND e.event_timestamp <  day_end
      AND b.built_at IS NOT NULL
      AND e.inserted_at > b.built_at
  )
  SELECT run_ts, run_ist, prev_date, '23', 'Late-arriving events', 'yesterday',
    CASE WHEN (SELECT n FROM late) = 0  THEN 'OK'
         WHEN (SELECT n FROM late) <= 10 THEN 'WARN'
         ELSE 'ALARM' END,
    CAST((SELECT n FROM late) AS FLOAT64),
    '0 events arriving after yesterday was finalised',
    IFNULL((SELECT CONCAT(CAST(n AS STRING), ' events landed after the rebuild, latest ',
                          FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', latest, 'Asia/Kolkata'), ' IST')
            FROM late WHERE n > 0), 'none'),
    'Yesterday was computed without these events, so the numbers already sent out are stale. Rebuild the day: CALL sp_build_presence_intervals(<yesterday>).';

  ---------------------------------------------------------------------------
  -- 29  ONE EMAIL, MANY DISPLAY NAMES — name drift. The hours usually still
  --     merge correctly (email wins), but everything that matches by NAME —
  --     team membership, registry lookup, the alias table — quietly misses
  --     the variants, so the person can fall out of their team's report.
  --
  --     30-day window over presence_intervals rather than the raw events
  --     table: same answer, far smaller scan, and it is the table whose
  --     names the reports actually read.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  WITH pairs AS (
    SELECT LOWER(TRIM(participant_email)) AS em,
           LOWER(TRIM(REGEXP_REPLACE(participant_name, r'[-_]\d+$', ''))) AS name_key
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date BETWEEN DATE_SUB(prev_date, INTERVAL 30 DAY) AND prev_date
      AND participant_email IS NOT NULL AND TRIM(participant_email) != ''
      AND participant_name  IS NOT NULL AND TRIM(participant_name)  != ''
    GROUP BY 1, 2
  ),
  drift AS (
    SELECT em, COUNT(*) AS names,
           STRING_AGG(name_key, ' | ' ORDER BY name_key) AS name_list
    FROM pairs GROUP BY em HAVING COUNT(*) >= 3
  )
  SELECT run_ts, run_ist, biz_date, '29', 'Display-name drift', 'identity',
    CASE WHEN (SELECT COUNT(*) FROM drift) = 0 THEN 'OK' ELSE 'WARN' END,
    CAST((SELECT COUNT(*) FROM drift) AS FLOAT64),
    '0 people using 3+ different display names in 30 days',
    IFNULL((SELECT STRING_AGG(CONCAT(em, ' = ', name_list), '; ')
            FROM (SELECT em, name_list FROM drift ORDER BY names DESC LIMIT 3)), 'none'),
    'These people keep changing their Zoom display name. Hours still merge on email, but name-based matching (team roster, registry) misses the variants. Add participant_alias rows, or ask them to settle on one name.';

  ---------------------------------------------------------------------------
  -- 31  EMPLOYEES IN ZERO TEAMS, OR TWO — check 14 catches people missing
  --     from the REGISTRY. This catches people who are registered but on no
  --     TEAM: equally invisible in team reports, and easier to miss because
  --     their name looks correct everywhere else. Two teams is the opposite
  --     failure — their hours are counted twice in any org-level total.
  ---------------------------------------------------------------------------
  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.health_checks`
    (run_ts, run_ist, business_date, check_id, check_name, scope, severity,
     metric, threshold, detail, action)
  WITH worked AS (
    SELECT participant_key,
           ANY_VALUE(participant_name) AS nm,
           LOWER(TRIM(COALESCE(ANY_VALUE(participant_email), ''))) AS em,
           LOWER(TRIM(REGEXP_REPLACE(ANY_VALUE(participant_name), r'[-_]\d+$', ''))) AS name_key
    FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    WHERE event_date = prev_date
    GROUP BY participant_key
  ),
  reg AS (
    -- Match key -> does this registry row carry a team?
    -- TWO SOURCES OF TEAM MEMBERSHIP, and either counts: employee_registry
    -- has its own team_id column (the one HR actually fills in), while
    -- team_members is the table Team View joins on. Looking only at
    -- team_members reported people as team-less who were plainly assigned
    -- in the registry.
    -- Flattened to ONE key column for the same reason as check 14: BigQuery
    -- rejects a semi-join whose condition ORs two different equalities.
    -- Only real staff here: a visitor with no team is expected, not a fault.
    SELECT LOWER(TRIM(REGEXP_REPLACE(participant_name, r'[-_]\d+$', ''))) AS mk,
           (team_id IS NOT NULL AND TRIM(team_id) != '') AS has_team
    FROM `verve-attendance-tracker.breakout_room_calibrator.employee_registry`
    WHERE COALESCE(status, 'active') = 'active'
      AND LOWER(COALESCE(category, 'employee')) = 'employee'
      AND participant_name IS NOT NULL AND TRIM(participant_name) != ''
    UNION ALL
    SELECT LOWER(TRIM(REGEXP_REPLACE(display_name, r'[-_]\d+$', ''))),
           (team_id IS NOT NULL AND TRIM(team_id) != '')
    FROM `verve-attendance-tracker.breakout_room_calibrator.employee_registry`
    WHERE COALESCE(status, 'active') = 'active'
      AND LOWER(COALESCE(category, 'employee')) = 'employee'
      AND display_name IS NOT NULL AND TRIM(display_name) != ''
    UNION ALL
    SELECT LOWER(TRIM(participant_email)),
           (team_id IS NOT NULL AND TRIM(team_id) != '')
    FROM `verve-attendance-tracker.breakout_room_calibrator.employee_registry`
    WHERE COALESCE(status, 'active') = 'active'
      AND LOWER(COALESCE(category, 'employee')) = 'employee'
      AND participant_email IS NOT NULL AND TRIM(participant_email) != ''
  ),
  tm AS (
    SELECT LOWER(TRIM(REGEXP_REPLACE(participant_name, r'[-_]\d+$', ''))) AS k,
           LOWER(TRIM(COALESCE(participant_email, ''))) AS em,
           team_id
    FROM `verve-attendance-tracker.breakout_room_calibrator.team_members`
  ),
  team_keys AS (
    SELECT DISTINCT k AS mk FROM tm WHERE k != ''
    UNION DISTINCT
    SELECT DISTINCT em     FROM tm WHERE em != ''
  ),
  worked_keys AS (
    SELECT participant_key, nm, participant_key AS mk FROM worked
    UNION ALL
    SELECT participant_key, nm, name_key        FROM worked WHERE name_key != ''
    UNION ALL
    SELECT participant_key, nm, em              FROM worked WHERE em != ''
  ),
  flags AS (
    SELECT wk.participant_key,
           ANY_VALUE(wk.nm) AS nm,
           LOGICAL_OR(r.mk IS NOT NULL) AS in_registry,
           LOGICAL_OR(COALESCE(r.has_team, FALSE)) OR LOGICAL_OR(tk.mk IS NOT NULL) AS in_team
    FROM worked_keys wk
    LEFT JOIN reg       r  ON r.mk  = wk.mk
    LEFT JOIN team_keys tk ON tk.mk = wk.mk
    GROUP BY wk.participant_key
  ),
  no_team AS (
    SELECT nm FROM flags WHERE in_registry AND NOT in_team
  ),
  multi_team AS (
    SELECT k, COUNT(DISTINCT team_id) AS teams
    FROM tm WHERE k != '' GROUP BY k HAVING COUNT(DISTINCT team_id) > 1
  )
  SELECT run_ts, run_ist, prev_date, '31', 'Team assignment gaps', 'registry',
    CASE WHEN (SELECT COUNT(*) FROM no_team) + (SELECT COUNT(*) FROM multi_team) = 0 THEN 'OK'
         ELSE 'WARN' END,
    CAST((SELECT COUNT(*) FROM no_team) + (SELECT COUNT(*) FROM multi_team) AS FLOAT64),
    '0 employees on zero teams, 0 on more than one',
    CONCAT(
      'no team: ', IFNULL((SELECT STRING_AGG(nm, ', ')
                           FROM (SELECT nm FROM no_team ORDER BY nm LIMIT 5)), 'none'),
      ' | two teams: ', IFNULL((SELECT STRING_AGG(k, ', ')
                                FROM (SELECT k FROM multi_team ORDER BY teams DESC LIMIT 5)), 'none')),
    'People on no team are in NO team report despite being registered staff. People on two teams are counted twice in org totals. Fix in Teams > Members.';

  ---------------------------------------------------------------------------
  -- retention
  ---------------------------------------------------------------------------
  DELETE FROM `verve-attendance-tracker.breakout_room_calibrator.health_checks`
  WHERE DATE(run_ts) < DATE_SUB(CURRENT_DATE('Asia/Kolkata'), INTERVAL 90 DAY);

END;
