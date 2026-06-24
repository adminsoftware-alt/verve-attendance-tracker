import React, { useState, useCallback, useEffect, useRef } from 'react';
import useZoomSdk from '../hooks/useZoomSdk';
import axios from 'axios';

const POLL_INTERVAL_MS = 30000; // 30 seconds
const START_RETRY_MS = 10000;
const WATCHDOG_INTERVAL_MS = 15000;
const STALE_AFTER_MS = POLL_INTERVAL_MS * 3;
const ROOM_CACHE_TTL_MS = 300000; // 5 minutes - room names rarely change

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
  const watchdogRef = useRef(null);
  const meetingIdRef = useRef(null);
  const isMonitoringRef = useRef(false);
  const isStartingRef = useRef(false);
  const lastSuccessRef = useRef(null);

  // Room cache - room names rarely change during a meeting
  const roomCacheRef = useRef({ rooms: [], roomMap: {}, timestamp: 0 });

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

  // Single poll: get all rooms + participants, send to backend
  const doPoll = useCallback(async () => {
    try {
      const now = Date.now();
      const cache = roomCacheRef.current;
      const cacheAge = now - cache.timestamp;
      const cacheValid = cache.rooms.length > 0 && cacheAge < ROOM_CACHE_TTL_MS;

      let rooms = [];
      let roomMap = {};

      // Use cached room list if still valid (room names rarely change)
      if (cacheValid) {
        rooms = cache.rooms;
        roomMap = cache.roomMap;
        // Only log occasionally to avoid spam
        if (cacheAge < 60000) {
          addLog(`Using cached room list (${rooms.length} rooms)`);
        }
      } else {
        // Cache expired or empty - fetch fresh room list
        try {
          rooms = await withRetry(getBreakoutRooms, 'getBreakoutRoomList', 2);

          // Build room UUID -> name mapping (normalized for consistent matching)
          rooms.forEach(room => {
            const rawUuid = room.breakoutRoomUUID || room.uuid || room.id || '';
            const uuid = normalizeUUID(rawUuid);
            const name = room.breakoutRoomName || room.name || 'Unknown';
            if (uuid) roomMap[uuid] = name;
          });

          // Debug: log first few room UUIDs for troubleshooting
          const sampleRooms = rooms.slice(0, 3).map(r => ({
            raw: r.breakoutRoomUUID || r.uuid || r.id || '',
            name: r.breakoutRoomName || r.name || ''
          }));
          console.log('Sample room UUIDs:', JSON.stringify(sampleRooms));

          // Update cache
          roomCacheRef.current = { rooms, roomMap, timestamp: now };
          addLog(`Refreshed room list: ${rooms.length} rooms`);
        } catch (roomErr) {
          addLog(`getBreakoutRoomList failed: ${roomErr.message}`);
          // Use stale cache if available
          if (cache.rooms.length > 0) {
            rooms = cache.rooms;
            roomMap = cache.roomMap;
            addLog(`Using stale cache (${rooms.length} rooms)`);
          }
        }
      }

      if (!rooms || rooms.length === 0) {
        addLog('No breakout rooms - capturing Main Room only');
      }

      // Get all participants (includes their current room) with retry
      let allParticipants = [];
      try {
        allParticipants = await withRetry(getParticipants, 'getMeetingParticipants', 2);
        addLog(`Got ${allParticipants.length} participants from SDK`);

        // Debug: log first participant's fields to see what SDK returns
        if (allParticipants.length > 0) {
          const sample = allParticipants[0];
          console.log('=== PARTICIPANT FIELDS DEBUG ===');
          console.log('Full participant object:', JSON.stringify(sample, null, 2));
          console.log('Available fields:', Object.keys(sample).join(', '));
        }
      } catch (pErr) {
        addLog(`getMeetingParticipants failed: ${pErr.message}`);
        // Fall back to room.participants if available
      }

      // If both SDK calls failed, skip this poll cycle
      if (rooms.length === 0 && allParticipants.length === 0) {
        addLog('Both SDK calls failed - skipping this poll');
        return;
      }

      // Build snapshot data using BOTH approaches and merge them
      // PRIMARY: getBreakoutRoomList() returns rooms WITH embedded participants (most reliable)
      // SECONDARY: getMeetingParticipants() for Main Room participants (no breakout room)
      const roomData = [];
      const seenParticipants = new Set(); // Track who we've seen in breakout rooms

      // Helper to check if name is Scout Bot
      const isScoutBot = (name) => {
        const pLower = name.toLowerCase();
        return pLower.includes('scout bot') || pLower.includes('scoutbot') ||
               pLower === 'scout s' || pLower.startsWith('scout ');
      };

      // APPROACH 1 (PRIMARY): Use room.participants from getBreakoutRoomList()
      // This is the RELIABLE source - SDK embeds participants directly in each room
      rooms.forEach(room => {
        const roomName = room.breakoutRoomName || room.name || 'Unknown';
        const roomParticipants = room.participants || room.members || room.attendees || [];

        console.log(`Room "${roomName}" has ${roomParticipants.length} embedded participants`);

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
          seenParticipants.add(p.name.toLowerCase()); // Track seen participants
          return true;
        });

        if (validParticipants.length > 0) {
          roomData.push({ room_name: roomName, participants: validParticipants });
        }
      });

      // APPROACH 2: Use getMeetingParticipants() for Main Room
      // Anyone in allParticipants but NOT in a breakout room = Main Room
      if (allParticipants.length > 0) {
        const mainRoomParticipants = [];

        allParticipants.forEach(p => {
          const pName = getParticipantName(p);
          const pEmail = getParticipantEmail(p);

          if (!pName) return;
          if (isScoutBot(pName)) return;

          // If NOT already seen in a breakout room, they're in Main Room
          if (!seenParticipants.has(pName.toLowerCase())) {
            mainRoomParticipants.push({
              name: pName,
              email: pEmail,
              uuid: p.participantUUID || p.uuid || p.id || ''
            });
            seenParticipants.add(pName.toLowerCase());
          }
        });

        if (mainRoomParticipants.length > 0) {
          roomData.push({ room_name: 'Main Room', participants: mainRoomParticipants });
        }
      }

      // Debug logging
      console.log('=== ROOM DATA SUMMARY ===');
      roomData.forEach(r => {
        console.log(`${r.room_name}: ${r.participants.map(p => p.name).join(', ')}`);
      });

      const totalParticipants = roomData.reduce((sum, r) => sum + r.participants.length, 0);

      // Send to backend
      const meetingId = meetingIdRef.current || extractMeetingId(meetingContext);
      const response = await api.post('/monitor/snapshot', {
        meeting_id: meetingId,
        rooms: roomData
      });

      if (response.data.success) {
        const now = new Date();
        lastSuccessRef.current = now;
        setPollCount(prev => prev + 1);
        setRoomCount(rooms.length);
        setParticipantCount(totalParticipants);
        setLastPoll(now);
        setRoomSummary(roomData.map(r => ({
          name: r.room_name,
          count: r.participants.length
        })));
        addLog(`OK: ${roomData.length} rooms, ${totalParticipants} participants`);
      } else {
        addLog(`ERROR: ${response.data.error}`);
        setErrors(prev => [...prev.slice(-10), response.data.error]);
      }
    } catch (err) {
      addLog(`POLL FAILED: ${err.message}`);
      setErrors(prev => [...prev.slice(-10), err.message]);
    }
  }, [getBreakoutRooms, getParticipants, meetingContext, addLog]);

  // Start monitoring
  const startMonitoring = useCallback(async () => {
    if (isMonitoringRef.current || isStartingRef.current) return;

    try {
      isStartingRef.current = true;
      const uuid = await getMeetingUUID();

      // Try multiple sources for meeting ID
      let mid = extractMeetingId(meetingContext);

      // Debug: log the full context to see what fields are available
      addLog(`Meeting context: ${JSON.stringify(meetingContext)}`);

      // Fallback: try to get meeting ID from getMeetingUUID response (some SDK versions include it)
      if (!mid && uuid) {
        // Some SDK versions return meeting ID as part of UUID or as separate field
        // Also try extracting numeric part if UUID contains meeting ID
        const numericMatch = uuid.match(/^(\d{9,11})/);
        if (numericMatch) {
          mid = numericMatch[1];
          addLog(`Extracted meeting ID from UUID: ${mid}`);
        }
      }

      // Last resort: Use the configured meeting ID (from env or hardcoded)
      if (!mid) {
        mid = process.env.REACT_APP_MEETING_ID || '9034027764';  // Fallback to known meeting ID
        addLog(`Using fallback meeting ID: ${mid}`);
      }

      meetingIdRef.current = mid;

      if (!meetingIdRef.current) {
        addLog('ERROR: Could not get meeting ID from any source');
        setErrors(prev => [...prev, 'Meeting ID not available - check SDK permissions']);
        return;
      }

      addLog(`Starting monitor for meeting ${meetingIdRef.current}`);
      addLog(`Meeting UUID: ${uuid}`);

      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }

      isMonitoringRef.current = true;
      setIsMonitoring(true);
      setErrors([]);

      // Clear room cache to get fresh data on start
      roomCacheRef.current = { rooms: [], roomMap: {}, timestamp: 0 };

      // First poll immediately
      await doPoll();

      // Then poll every 30 seconds
      intervalRef.current = setInterval(doPoll, POLL_INTERVAL_MS);
      addLog(`Polling every ${POLL_INTERVAL_MS / 1000}s`);
    } catch (err) {
      addLog(`Failed to start: ${err.message}`);
      isMonitoringRef.current = false;
      setIsMonitoring(false);
    } finally {
      isStartingRef.current = false;
    }
  }, [getMeetingUUID, meetingContext, doPoll, addLog]);

  // Stop monitoring
  const stopMonitoring = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    isMonitoringRef.current = false;
    isStartingRef.current = false;
    setIsMonitoring(false);
    addLog('Monitoring stopped');
  }, [addLog]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
      if (watchdogRef.current) clearInterval(watchdogRef.current);
    };
  }, []);

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

  // WATCHDOG: keep monitoring alive while the Zoom App panel is loaded.
  useEffect(() => {
    if (!isConfigured) return;

    watchdogRef.current = setInterval(() => {
      if (isStartingRef.current) return;

      const lastSuccess = lastSuccessRef.current?.getTime?.() || 0;
      const stale = isMonitoringRef.current && lastSuccess > 0 && Date.now() - lastSuccess > STALE_AFTER_MS;
      const missingTimer = isMonitoringRef.current && !intervalRef.current;

      if (!isMonitoringRef.current || stale || missingTimer) {
        if (stale) addLog('Watchdog: snapshot stale, restarting monitor');
        if (missingTimer) addLog('Watchdog: polling timer missing, restarting monitor');
        if (!isMonitoringRef.current) addLog('Watchdog: monitor is idle, starting monitor');

        if (intervalRef.current) {
          clearInterval(intervalRef.current);
          intervalRef.current = null;
        }
        isMonitoringRef.current = false;
        setIsMonitoring(false);
        setAutoStarted(false);
        setTimeout(() => startMonitoring(), START_RETRY_MS);
      }
    }, WATCHDOG_INTERVAL_MS);

    return () => {
      if (watchdogRef.current) {
        clearInterval(watchdogRef.current);
        watchdogRef.current = null;
      }
    };
  }, [isConfigured, startMonitoring, addLog]);

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
          <button style={styles.startButton} onClick={startMonitoring}>
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
