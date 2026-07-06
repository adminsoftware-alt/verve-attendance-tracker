import React, { useState, useCallback, useEffect, useRef } from 'react';
import useZoomSdk from '../hooks/useZoomSdk';
import axios from 'axios';

const POLL_INTERVAL_MS = 30000; // 30 seconds
const START_RETRY_MS = 10000;
const WATCHDOG_INTERVAL_MS = 15000;
const STALE_AFTER_MS = POLL_INTERVAL_MS * 3;
// Cache is a FALLBACK only (used when the live room fetch fails). It is
// never the primary source: serving cached rooms (which embed participants)
// as primary data recorded people in rooms they'd left minutes ago.
const ROOM_CACHE_TTL_MS = 300000; // 5 minutes
const MAX_PENDING_SNAPSHOTS = 20;  // ~10 min of queued failed uploads
const MAX_SDK_FAILS_BEFORE_RELOAD = 4; // consecutive all-SDK-call failures
const RELOAD_COOLDOWN_MS = 5 * 60 * 1000;
const PENDING_STORAGE_KEY = 'monitor_pending_snapshots';
const LAST_RELOAD_KEY = 'monitor_last_reload';

const getBackendUrl = () => {
  if (process.env.REACT_APP_BACKEND_URL) return process.env.REACT_APP_BACKEND_URL;
  if (process.env.NODE_ENV === 'production') return '';
  return 'http://localhost:8080';
};

const api = axios.create({
  baseURL: getBackendUrl(),
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' }
});

function getParticipantName(p) {
  return p.screenName || p.displayName || p.participantName || p.name || p.userName || p.user_name || '';
}

function getParticipantEmail(p) {
  return p.email || p.participantEmail || p.user_email || '';
}

// Normalize UUID - remove curly braces and lowercase for consistent matching
function normalizeUUID(uuid) {
  if (!uuid) return '';
  return String(uuid).replace(/[{}]/g, '').toLowerCase().trim();
}

// Extract meeting ID from context (handles different SDK versions/formats)
function extractMeetingId(context) {
  if (!context) return '';
  // Try various field names Zoom SDK might use
  return String(context.meetingID || context.meetingId || context.mid || context.meeting_id || '');
}

// EXACT bot-name match. The old substring/startsWith check ("scout ...")
// silently excluded any real employee whose name starts with "Scout".
const BOT_NAME = (process.env.REACT_APP_SCOUT_BOT_NAME || 'Scout Bot').trim().toLowerCase();
function isScoutBot(name) {
  const n = (name || '').trim().toLowerCase();
  return n === BOT_NAME || n === 'scout bot' || n === 'scoutbot';
}

function loadPendingSnapshots() {
  try {
    const raw = sessionStorage.getItem(PENDING_STORAGE_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr.slice(-MAX_PENDING_SNAPSHOTS) : [];
  } catch { return []; }
}

function savePendingSnapshots(arr) {
  try { sessionStorage.setItem(PENDING_STORAGE_KEY, JSON.stringify(arr.slice(-MAX_PENDING_SNAPSHOTS))); } catch { /* ignore */ }
}

function MonitorPanel() {
  const {
    isConfigured,
    error: sdkError,
    meetingContext,
    getBreakoutRooms,
    getParticipants,
    getMeetingUUID,
    refreshUserRole
  } = useZoomSdk();

  const [isMonitoring, setIsMonitoring] = useState(false);
  const [autoStarted, setAutoStarted] = useState(false);
  const [lastPoll, setLastPoll] = useState(null);
  const [pollCount, setPollCount] = useState(0);
  const [roomCount, setRoomCount] = useState(0);
  const [participantCount, setParticipantCount] = useState(0);
  const [errors, setErrors] = useState([]);
  const [logs, setLogs] = useState([]);
  const [roomSummary, setRoomSummary] = useState([]);

  const intervalRef = useRef(null);
  const pollWorkerRef = useRef(null);
  const watchdogRef = useRef(null);
  const watchdogRestartRef = useRef(null);
  const meetingIdRef = useRef(null);
  const isMonitoringRef = useRef(false);
  const isStartingRef = useRef(false);
  const lastSuccessRef = useRef(null);
  const userStoppedRef = useRef(false);       // Stop button = stay stopped
  const pollInFlightRef = useRef(false);      // prevent overlapping polls
  const sdkFailCountRef = useRef(0);          // consecutive all-SDK failures
  const pendingSnapshotsRef = useRef(loadPendingSnapshots()); // failed uploads

  // Room cache — FALLBACK ONLY when the live fetch fails (see constant note)
  const roomCacheRef = useRef({ rooms: [], timestamp: 0 });

  const addLog = useCallback((msg) => {
    const time = new Date().toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata' });
    setLogs(prev => [...prev.slice(-50), `[${time}] ${msg}`]);
  }, []);

  // Retry helper for SDK calls that may timeout
  const withRetry = useCallback(async (fn, name, maxRetries = 2) => {
    for (let attempt = 1; attempt <= maxRetries; attempt++) {
      try {
        return await fn();
      } catch (err) {
        const isTimeout = err.message?.includes('10000ms') || err.message?.includes('timeout');
        if (attempt < maxRetries && isTimeout) {
          addLog(`${name} timeout, retrying (${attempt}/${maxRetries})...`);
          await new Promise(r => setTimeout(r, 1000)); // Wait 1s before retry
        } else {
          throw err;
        }
      }
    }
  }, [addLog]);

  // POST helper: throws on transport error AND on backend-reported failure,
  // so callers treat both the same (queue for resend).
  const postSnapshot = useCallback(async (payload) => {
    const response = await api.post('/monitor/snapshot', payload);
    if (!response.data?.success) {
      throw new Error(response.data?.error || 'backend rejected snapshot');
    }
    return response;
  }, []);

  // After too many consecutive ALL-SDK-call failures the Zoom bridge is
  // probably dead (client reconnected / session dropped) — a reload re-runs
  // zoomSdk.config and recovers. Cooldown prevents a reload loop; queued
  // snapshots survive in sessionStorage.
  const maybeRecoverSdk = useCallback(() => {
    if (sdkFailCountRef.current < MAX_SDK_FAILS_BEFORE_RELOAD) return;
    let last = 0;
    try { last = Number(sessionStorage.getItem(LAST_RELOAD_KEY) || 0); } catch { /* ignore */ }
    if (Date.now() - last < RELOAD_COOLDOWN_MS) return;
    try { sessionStorage.setItem(LAST_RELOAD_KEY, String(Date.now())); } catch { /* ignore */ }
    savePendingSnapshots(pendingSnapshotsRef.current);
    addLog('Zoom SDK unresponsive - reloading app to re-initialize');
    setErrors(prev => [...prev.slice(-10), 'Zoom SDK unresponsive - auto-recovering (reload)']);
    setTimeout(() => window.location.reload(), 1000);
  }, [addLog]);

  // Single poll: get all rooms + participants, send to backend
  const doPoll = useCallback(async () => {
    if (pollInFlightRef.current) return; // never overlap slow polls
    pollInFlightRef.current = true;
    try {
      const now = Date.now();

      // ALWAYS fetch the room list fresh — it embeds each room's live
      // participants, which is the primary attendance source. The cache is
      // only a fallback when the live call fails.
      let rooms = [];
      let roomsFetchFailed = false;
      try {
        rooms = await withRetry(getBreakoutRooms, 'getBreakoutRoomList', 2) || [];
        roomCacheRef.current = { rooms, timestamp: now };
      } catch (roomErr) {
        roomsFetchFailed = true;
        addLog(`getBreakoutRoomList failed: ${roomErr.message}`);
        const cache = roomCacheRef.current;
        if (cache.rooms.length > 0 && now - cache.timestamp < ROOM_CACHE_TTL_MS) {
          rooms = cache.rooms;
          addLog(`Using stale room cache (${rooms.length} rooms, ${Math.round((now - cache.timestamp) / 1000)}s old)`);
        }
      }

      // Get all participants (for Main Room detection) with retry
      let allParticipants = [];
      let participantsFetchFailed = false;
      try {
        allParticipants = await withRetry(getParticipants, 'getMeetingParticipants', 2) || [];
        addLog(`Got ${allParticipants.length} participants from SDK`);
      } catch (pErr) {
        participantsFetchFailed = true;
        addLog(`getMeetingParticipants failed: ${pErr.message}`);
      }

      // If the room fetch THREW and no usable cache exists, skip the cycle:
      // proceeding would misfile every breakout participant under Main Room.
      if (roomsFetchFailed && rooms.length === 0) {
        if (participantsFetchFailed) sdkFailCountRef.current += 1; else sdkFailCountRef.current += 1;
        addLog('Room list unavailable (no fallback cache) - skipping this poll');
        maybeRecoverSdk();
        return;
      }
      if (rooms.length === 0 && allParticipants.length === 0) {
        if (participantsFetchFailed) {
          sdkFailCountRef.current += 1;
          addLog('Both SDK calls failed - skipping this poll');
          maybeRecoverSdk();
        } else {
          addLog('No rooms and no participants - nothing to record');
        }
        return;
      }
      // At least one SDK call worked with data — bridge is alive.
      sdkFailCountRef.current = 0;

      if (rooms.length === 0) {
        addLog('No breakout rooms - capturing Main Room only');
      }

      // Build snapshot data using BOTH approaches and merge them.
      // Dedup is keyed on participant UUID when available (falling back to
      // name) so two different people who share a display name are BOTH
      // recorded instead of one silently dropping the other.
      const roomData = [];
      const seenUuids = new Set();
      const seenNames = new Set();

      // APPROACH 1 (PRIMARY): room.participants from the FRESH getBreakoutRoomList()
      rooms.forEach(room => {
        const roomName = room.breakoutRoomName || room.name || 'Unknown';
        const roomParticipants = room.participants || room.members || room.attendees || [];

        const validParticipants = roomParticipants.map(p => {
          const pName = getParticipantName(p);
          const pEmail = getParticipantEmail(p);
          return {
            name: pName,
            email: pEmail,
            uuid: p.participantUUID || p.uuid || p.id || ''
          };
        }).filter(p => {
          if (!p.name) return false;
          if (isScoutBot(p.name)) return false;
          const u = normalizeUUID(p.uuid);
          if (u) seenUuids.add(u);
          seenNames.add(p.name.toLowerCase());
          return true;
        });

        if (validParticipants.length > 0) {
          roomData.push({ room_name: roomName, participants: validParticipants });
        }
      });

      // APPROACH 2: getMeetingParticipants() minus breakout occupants = Main Room
      if (allParticipants.length > 0) {
        const mainRoomParticipants = [];

        allParticipants.forEach(p => {
          const pName = getParticipantName(p);
          const pEmail = getParticipantEmail(p);

          if (!pName) return;
          if (isScoutBot(pName)) return;

          const u = normalizeUUID(p.participantUUID || p.uuid || p.id || '');
          // Same UUID already seen in a breakout -> same person, skip.
          // No UUID available -> fall back to name matching (old behavior).
          const alreadySeen = u ? seenUuids.has(u) : seenNames.has(pName.toLowerCase());
          if (!alreadySeen) {
            mainRoomParticipants.push({
              name: pName,
              email: pEmail,
              uuid: p.participantUUID || p.uuid || p.id || ''
            });
            if (u) seenUuids.add(u);
            seenNames.add(pName.toLowerCase());
          }
        });

        if (mainRoomParticipants.length > 0) {
          roomData.push({ room_name: 'Main Room', participants: mainRoomParticipants });
        }
      }

      const totalParticipants = roomData.reduce((sum, r) => sum + r.participants.length, 0);
      if (totalParticipants === 0) {
        addLog('No participants captured this cycle');
        return;
      }

      // Refresh meeting id if it was unresolved when monitoring started
      if (!meetingIdRef.current) {
        const m = extractMeetingId(meetingContext);
        if (m) meetingIdRef.current = m;
      }
      const meetingId = meetingIdRef.current || extractMeetingId(meetingContext);

      // captured_at lets the backend keep RESENT snapshots in their true
      // 30s bucket instead of stamping them at arrival time.
      const payload = {
        meeting_id: meetingId,
        rooms: roomData,
        captured_at: new Date().toISOString()
      };

      try {
        // Drain any queued failed uploads first (oldest first), then send
        // the current snapshot. A failure re-queues and stops the drain.
        while (pendingSnapshotsRef.current.length > 0) {
          const queued = pendingSnapshotsRef.current[0];
          await postSnapshot(queued);
          pendingSnapshotsRef.current.shift();
          savePendingSnapshots(pendingSnapshotsRef.current);
          addLog(`Resent queued snapshot (${queued.captured_at})`);
        }
        await postSnapshot(payload);

        const nowDate = new Date();
        lastSuccessRef.current = nowDate;
        setPollCount(prev => prev + 1);
        setRoomCount(rooms.length);
        setParticipantCount(totalParticipants);
        setLastPoll(nowDate);
        setRoomSummary(roomData.map(r => ({
          name: r.room_name,
          count: r.participants.length
        })));
        addLog(`OK: ${roomData.length} rooms, ${totalParticipants} participants`);
      } catch (postErr) {
        // Queue this snapshot for resend on a later cycle (capped)
        pendingSnapshotsRef.current.push(payload);
        if (pendingSnapshotsRef.current.length > MAX_PENDING_SNAPSHOTS) {
          pendingSnapshotsRef.current = pendingSnapshotsRef.current.slice(-MAX_PENDING_SNAPSHOTS);
        }
        savePendingSnapshots(pendingSnapshotsRef.current);
        addLog(`UPLOAD FAILED (queued for resend, ${pendingSnapshotsRef.current.length} pending): ${postErr.message}`);
        setErrors(prev => [...prev.slice(-10), postErr.message]);
      }
    } catch (err) {
      addLog(`POLL FAILED: ${err.message}`);
      setErrors(prev => [...prev.slice(-10), err.message]);
    } finally {
      pollInFlightRef.current = false;
    }
  }, [getBreakoutRooms, getParticipants, meetingContext, addLog, withRetry, postSnapshot, maybeRecoverSdk]);

  // Stop the poll timer (worker or interval), whichever is active
  const stopPollTimer = useCallback(() => {
    if (pollWorkerRef.current) {
      try { pollWorkerRef.current.terminate(); } catch { /* ignore */ }
      pollWorkerRef.current = null;
    }
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, []);

  // Start the poll timer. Prefer a Web Worker tick: worker timers are NOT
  // throttled when the panel is hidden/minimized, unlike page setInterval
  // (Chromium throttles hidden pages to >=1/min, which both slowed polling
  // to a crawl and made the watchdog constantly tear down the monitor).
  const startPollTimer = useCallback(() => {
    stopPollTimer();
    try {
      const blob = new Blob(
        [`setInterval(function(){ postMessage('tick'); }, ${POLL_INTERVAL_MS});`],
        { type: 'application/javascript' }
      );
      const url = URL.createObjectURL(blob);
      const worker = new Worker(url);
      URL.revokeObjectURL(url);
      worker.onmessage = () => { doPoll(); };
      pollWorkerRef.current = worker;
    } catch (e) {
      // CSP may block blob workers in some webviews — fall back to interval
      intervalRef.current = setInterval(doPoll, POLL_INTERVAL_MS);
    }
  }, [doPoll, stopPollTimer]);

  // Start monitoring
  const startMonitoring = useCallback(async () => {
    if (isMonitoringRef.current || isStartingRef.current) return;

    try {
      isStartingRef.current = true;
      const uuid = await getMeetingUUID();

      // Try multiple sources for meeting ID
      let mid = extractMeetingId(meetingContext);

      // Fallback: numeric meeting ID prefix inside the UUID (some SDK versions)
      if (!mid && uuid) {
        const numericMatch = uuid.match(/^(\d{9,11})/);
        if (numericMatch) {
          mid = numericMatch[1];
          addLog(`Extracted meeting ID from UUID: ${mid}`);
        }
      }

      // Env override, then the meeting UUID itself. NEVER a hardcoded ID:
      // that silently filed a whole day's attendance under the wrong meeting.
      // The UUID is unique and truthful; reports key on event_date anyway.
      if (!mid) mid = process.env.REACT_APP_MEETING_ID || '';
      if (!mid && uuid) {
        mid = uuid;
        addLog('No numeric meeting ID available - using meeting UUID as ID');
      }

      meetingIdRef.current = mid;

      if (!meetingIdRef.current) {
        addLog('ERROR: Could not get meeting ID from any source - will retry');
        setErrors(prev => [...prev.slice(-10), 'Meeting ID not available yet - retrying']);
        return; // watchdog retries; doPoll also re-resolves the ID later
      }

      addLog(`Starting monitor for meeting ${meetingIdRef.current}`);
      addLog(`Meeting UUID: ${uuid}`);

      stopPollTimer();

      isMonitoringRef.current = true;
      setIsMonitoring(true);
      setErrors([]);

      // Clear room cache to get fresh data on start
      roomCacheRef.current = { rooms: [], timestamp: 0 };

      // First poll immediately
      await doPoll();

      // Then poll every 30 seconds
      startPollTimer();
      addLog(`Polling every ${POLL_INTERVAL_MS / 1000}s`);
    } catch (err) {
      addLog(`Failed to start: ${err.message}`);
      sdkFailCountRef.current += 1;
      maybeRecoverSdk();
      isMonitoringRef.current = false;
      setIsMonitoring(false);
    } finally {
      isStartingRef.current = false;
    }
  }, [getMeetingUUID, meetingContext, doPoll, addLog, startPollTimer, stopPollTimer, maybeRecoverSdk]);

  // Stop monitoring — a USER stop stays stopped (the watchdog must not
  // silently restart 25s later, which made this button a lie).
  const stopMonitoring = useCallback(() => {
    userStoppedRef.current = true;
    stopPollTimer();
    isMonitoringRef.current = false;
    isStartingRef.current = false;
    setIsMonitoring(false);
    addLog('Monitoring stopped by user (watchdog paused until Start is clicked)');
  }, [addLog, stopPollTimer]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      stopPollTimer();
      if (watchdogRef.current) clearInterval(watchdogRef.current);
      if (watchdogRestartRef.current) clearTimeout(watchdogRestartRef.current);
      savePendingSnapshots(pendingSnapshotsRef.current);
    };
  }, [stopPollTimer]);

  // Background role refresh for logging only - does NOT gate monitoring.
  // Auto-start fires on SDK config regardless of detected role.
  const [retryCount, setRetryCount] = useState(0);
  useEffect(() => {
    if (isConfigured && retryCount < 3) {
      const timer = setTimeout(async () => {
        await refreshUserRole();
        setRetryCount(prev => prev + 1);
      }, 2000);
      return () => clearTimeout(timer);
    }
  }, [isConfigured, retryCount, refreshUserRole]);

  // AUTO-START: Begin monitoring as soon as SDK is ready. No role gate.
  // SDK calls will surface a permission error if the bot truly isn't host/co-host.
  useEffect(() => {
    const shouldStart = isConfigured && !isMonitoringRef.current && !isStartingRef.current;

    if (shouldStart && !autoStarted) {
      setAutoStarted(true);
      addLog('Auto-starting monitor');
      const timer = setTimeout(() => startMonitoring(), 1000);
      return () => clearTimeout(timer);
    }
  }, [isConfigured, autoStarted, startMonitoring, addLog]);

  // KEEP-ALIVE: warn user before closing the panel while monitoring is active.
  // Won't fully block close in every webview, but adds friction for accidental clicks.
  useEffect(() => {
    const handler = (e) => {
      if (isMonitoringRef.current) {
        e.preventDefault();
        e.returnValue = 'Monitoring is active. Closing this panel will stop attendance tracking.';
        return e.returnValue;
      }
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, []);

  // Poll immediately when the panel becomes visible again — hidden webviews
  // get throttled timers, so the first visible moment catches us up.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState !== 'visible') return;
      if (!isMonitoringRef.current || userStoppedRef.current) return;
      const last = lastSuccessRef.current?.getTime?.() || 0;
      if (Date.now() - last > POLL_INTERVAL_MS) doPoll();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => document.removeEventListener('visibilitychange', onVisible);
  }, [doPoll]);

  // WATCHDOG: keep monitoring alive while the Zoom App panel is loaded.
  useEffect(() => {
    if (!isConfigured) return;

    watchdogRef.current = setInterval(() => {
      if (isStartingRef.current) return;
      if (userStoppedRef.current) return; // user pressed Stop — respect it
      if (watchdogRestartRef.current) return; // restart already scheduled

      const lastSuccess = lastSuccessRef.current?.getTime?.() || 0;
      const stale = isMonitoringRef.current && lastSuccess > 0 && Date.now() - lastSuccess > STALE_AFTER_MS;
      const missingTimer = isMonitoringRef.current && !intervalRef.current && !pollWorkerRef.current;

      if (!isMonitoringRef.current || stale || missingTimer) {
        if (stale) addLog('Watchdog: snapshot stale, restarting monitor');
        if (missingTimer) addLog('Watchdog: polling timer missing, restarting monitor');
        if (!isMonitoringRef.current) addLog('Watchdog: monitor is idle, starting monitor');

        stopPollTimer();
        isMonitoringRef.current = false;
        setIsMonitoring(false);
        setAutoStarted(false);
        // Tracked so unmount/effect-cleanup can cancel it (an untracked
        // timeout kept firing on dead components and leaked orphan pollers).
        watchdogRestartRef.current = setTimeout(() => {
          watchdogRestartRef.current = null;
          if (!userStoppedRef.current) startMonitoring();
        }, START_RETRY_MS);
      }
    }, WATCHDOG_INTERVAL_MS);

    return () => {
      if (watchdogRef.current) {
        clearInterval(watchdogRef.current);
        watchdogRef.current = null;
      }
      if (watchdogRestartRef.current) {
        clearTimeout(watchdogRestartRef.current);
        watchdogRestartRef.current = null;
      }
    };
  }, [isConfigured, startMonitoring, addLog, doPoll, stopPollTimer]);

  // Not configured
  if (!isConfigured) {
    return (
      <div style={styles.container}>
        <h2 style={styles.title}>Room Monitor</h2>
        <div style={styles.idleBox}>
          <div style={styles.statusRow}>
            <span style={styles.statusDot('#FFB800')} />
            <span style={styles.statusText}>{sdkError || 'Connecting to Zoom...'}</span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div style={styles.container}>
      <h2 style={styles.title}>Room Monitor</h2>
      {meetingContext && (
        <span style={styles.meetingId}>Meeting: {meetingContext.meetingID}</span>
      )}

      {/* Status */}
      <div style={isMonitoring ? styles.activeBox : styles.idleBox}>
        <div style={styles.statusRow}>
          <span style={styles.statusDot(isMonitoring ? '#00C851' : '#888')} />
          <span style={styles.statusLabel}>
            {isMonitoring ? 'MONITORING' : 'IDLE'}
          </span>
        </div>

        {isMonitoring && (
          <div style={styles.statsGrid}>
            <div style={styles.stat}>
              <span style={styles.statValue}>{pollCount}</span>
              <span style={styles.statLabel}>Polls</span>
            </div>
            <div style={styles.stat}>
              <span style={styles.statValue}>{roomCount}</span>
              <span style={styles.statLabel}>Rooms</span>
            </div>
            <div style={styles.stat}>
              <span style={styles.statValue}>{participantCount}</span>
              <span style={styles.statLabel}>In Rooms</span>
            </div>
          </div>
        )}

        {lastPoll && (
          <p style={styles.lastPoll}>
            Last poll: {lastPoll.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata' })} IST
          </p>
        )}
      </div>

      {/* Controls */}
      <div style={styles.actions}>
        {!isMonitoring ? (
          <button style={styles.startButton} onClick={() => { userStoppedRef.current = false; startMonitoring(); }}>
            Start Monitoring
          </button>
        ) : (
          <button style={styles.stopButton} onClick={stopMonitoring}>
            Stop Monitoring
          </button>
        )}
      </div>

      {/* Occupied Rooms */}
      {roomSummary.length > 0 && (
        <div style={styles.section}>
          <h3 style={styles.sectionTitle}>
            OCCUPIED ROOMS ({roomSummary.length})
          </h3>
          <div style={styles.roomGrid}>
            {roomSummary.sort((a, b) => b.count - a.count).map((room, i) => (
              <div key={i} style={styles.roomItem}>
                <span style={styles.roomName}>{room.name}</span>
                <span style={styles.roomCount}>{room.count}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Errors */}
      {errors.length > 0 && (
        <div style={styles.errorBox}>
          <h3 style={styles.sectionTitle}>ERRORS ({errors.length})</h3>
          {errors.slice(-3).map((e, i) => (
            <p key={i} style={styles.errorText}>{e}</p>
          ))}
        </div>
      )}

      {/* Log */}
      {logs.length > 0 && (
        <div style={styles.section}>
          <h3 style={styles.sectionTitle}>LOG</h3>
          <pre style={styles.logBox}>{logs.slice(-15).join('\n')}</pre>
        </div>
      )}
    </div>
  );
}

const styles = {
  container: { display: 'flex', flexDirection: 'column', gap: '12px', padding: '16px', maxWidth: '500px', margin: '0 auto', minHeight: '100vh', backgroundColor: '#1a1a2e' },
  title: { color: '#fff', fontSize: '18px', fontWeight: '600', margin: 0 },
  meetingId: { color: '#666', fontSize: '11px' },

  activeBox: { backgroundColor: 'rgba(0,200,81,0.08)', border: '1px solid rgba(0,200,81,0.3)', borderRadius: '10px', padding: '16px' },
  idleBox: { backgroundColor: 'rgba(255,255,255,0.05)', border: '1px solid #333', borderRadius: '10px', padding: '16px' },
  statusRow: { display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' },
  statusDot: (color) => ({ display: 'inline-block', width: '10px', height: '10px', borderRadius: '50%', backgroundColor: color, boxShadow: `0 0 6px ${color}` }),
  statusLabel: { color: '#fff', fontSize: '14px', fontWeight: '600', letterSpacing: '1px' },
  statusText: { color: '#ccc', fontSize: '13px' },
  lastPoll: { color: '#888', fontSize: '11px', margin: '8px 0 0 0' },

  statsGrid: { display: 'flex', gap: '16px' },
  stat: { display: 'flex', flexDirection: 'column', alignItems: 'center' },
  statValue: { color: '#fff', fontSize: '24px', fontWeight: '700' },
  statLabel: { color: '#888', fontSize: '10px', textTransform: 'uppercase' },

  actions: { display: 'flex', gap: '8px' },
  startButton: { flex: 1, padding: '12px', backgroundColor: '#00C851', color: '#fff', border: 'none', borderRadius: '6px', fontSize: '14px', fontWeight: '600', cursor: 'pointer' },
  stopButton: { flex: 1, padding: '12px', backgroundColor: '#ff4757', color: '#fff', border: 'none', borderRadius: '6px', fontSize: '14px', fontWeight: '600', cursor: 'pointer' },

  section: { display: 'flex', flexDirection: 'column', gap: '6px' },
  sectionTitle: { color: '#888', fontSize: '11px', fontWeight: '600', textTransform: 'uppercase', margin: 0 },
  roomGrid: { display: 'flex', flexDirection: 'column', gap: '2px' },
  roomItem: { display: 'flex', justifyContent: 'space-between', padding: '6px 10px', backgroundColor: 'rgba(255,255,255,0.03)', borderRadius: '4px' },
  roomName: { color: '#2D8CFF', fontSize: '12px' },
  roomCount: { color: '#fff', fontSize: '12px', fontWeight: '600' },

  errorBox: { backgroundColor: 'rgba(255,71,87,0.1)', border: '1px solid rgba(255,71,87,0.3)', borderRadius: '8px', padding: '10px' },
  errorText: { color: '#ff6b6b', fontSize: '11px', margin: '4px 0' },

  logBox: { backgroundColor: 'rgba(0,0,0,0.4)', padding: '10px', borderRadius: '6px', fontSize: '10px', color: '#00C851', overflow: 'auto', maxHeight: '150px', fontFamily: 'Monaco, monospace', margin: 0, whiteSpace: 'pre-wrap' }
};

export default MonitorPanel;
