/**
 * RoomMapperPanel.jsx
 *
 * NEW WEBHOOK-PRIMARY ARCHITECTURE (2026-07-20)
 *
 * This panel replaces MonitorPanel for the webhook-primary approach:
 * - Webhooks are the PRIMARY data source (exact timestamps, stored in BQ)
 * - SDK is ONLY used to map room UUIDs to room names
 * - Bot sits IDLE in any room, responds to mapping requests
 * - No 30s polling - just on-demand room name lookups
 *
 * How it works:
 * 1. Bot joins meeting (any room, can stay in Main Room or idle breakout)
 * 2. When webhook receives unknown room_uuid, backend requests mapping
 * 3. This panel queries SDK for room list + participant locations
 * 4. Sends mapping back to backend -> stored in BQ room_mappings table
 *
 * Benefits:
 * - Exact timestamps from webhooks (not 30s buckets)
 * - Works even if bot crashes after initial mapping
 * - Less SDK load (on-demand vs continuous polling)
 * - All processing moves to BQ SQL
 */

import React, { useState, useCallback, useEffect, useRef } from 'react';
import useZoomSdk from '../hooks/useZoomSdk';
import axios from 'axios';

// How often to check for pending mapping requests (ms)
const MAPPING_CHECK_INTERVAL_MS = 5000; // 5 seconds

// How often to proactively send full room list (for redundancy)
const FULL_ROOM_SYNC_INTERVAL_MS = 60000; // 1 minute

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

function normalizeUUID(uuid) {
  if (!uuid) return '';
  return String(uuid).replace(/[{}]/g, '').toLowerCase().trim();
}

function extractMeetingId(context) {
  if (!context) return '';
  return String(context.meetingID || context.meetingId || context.mid || context.meeting_id || '');
}

const BOT_NAME = (process.env.REACT_APP_SCOUT_BOT_NAME || 'Scout Bot').trim().toLowerCase();
function isScoutBot(name) {
  const n = (name || '').trim().toLowerCase();
  return n === BOT_NAME || n === 'scout bot' || n === 'scoutbot';
}

// Zoom appends "-1", "-2" rejoin suffixes to display names. Strip them so
// "John Doe-1" (webhook) still matches "John Doe" (SDK room list).
function normalizeName(name) {
  return (name || '').trim().toLowerCase().replace(/-\d+$/, '').trim();
}

function RoomMapperPanel() {
  const {
    isConfigured,
    error: sdkError,
    meetingContext,
    getBreakoutRooms,
    getParticipants,
    getMeetingUUID,
  } = useZoomSdk();

  const [isActive, setIsActive] = useState(false);
  const [lastSync, setLastSync] = useState(null);
  const [syncCount, setSyncCount] = useState(0);
  const [roomCount, setRoomCount] = useState(0);
  const [mappingRequests, setMappingRequests] = useState(0);
  const [logs, setLogs] = useState([]);
  const [errors, setErrors] = useState([]);
  const [roomList, setRoomList] = useState([]);

  const checkIntervalRef = useRef(null);
  const syncIntervalRef = useRef(null);
  const meetingIdRef = useRef(null);
  const isActiveRef = useRef(false);

  const addLog = useCallback((msg) => {
    const time = new Date().toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata' });
    setLogs(prev => [...prev.slice(-50), `[${time}] ${msg}`]);
  }, []);

  /**
   * Get all rooms with their SDK UUIDs and names
   * This is the core function - maps SDK room info
   */
  const fetchRoomList = useCallback(async () => {
    try {
      const rooms = await getBreakoutRooms() || [];
      const roomData = rooms.map(room => {
        // Embed each room's current participants: the backend uses them to
        // resolve pending webhook-uuid requests directly from this payload.
        const participants = (room.participants || room.members || room.attendees || [])
          .map(p => ({ name: getParticipantName(p), email: getParticipantEmail(p) }))
          .filter(p => p.name && !isScoutBot(p.name));
        return {
          room_name: room.breakoutRoomName || room.name || 'Unknown',
          sdk_uuid: normalizeUUID(room.uuid || room.breakoutRoomUUID || ''),
          participant_count: participants.length,
          participants
        };
      });
      setRoomList(roomData);
      setRoomCount(roomData.length);
      return roomData;
    } catch (err) {
      addLog(`Failed to get rooms: ${err.message}`);
      throw err;
    }
  }, [getBreakoutRooms, addLog]);

  /**
   * Find which room a specific participant is currently in
   * Used when webhook arrives with unknown room_uuid
   */
  const findParticipantRoom = useCallback(async (participantName, participantEmail) => {
    try {
      // Get all rooms with participants
      const rooms = await getBreakoutRooms() || [];

      for (const room of rooms) {
        const roomName = room.breakoutRoomName || room.name || 'Unknown';
        const participants = room.participants || room.members || [];

        for (const p of participants) {
          const pName = getParticipantName(p);
          const pEmail = getParticipantEmail(p);

          // Match by email (preferred) or rejoin-suffix-normalized name
          if (participantEmail && pEmail && pEmail.toLowerCase() === participantEmail.toLowerCase()) {
            return { room_name: roomName, sdk_uuid: normalizeUUID(room.uuid || '') };
          }
          if (participantName && pName && normalizeName(pName) === normalizeName(participantName)) {
            return { room_name: roomName, sdk_uuid: normalizeUUID(room.uuid || '') };
          }
        }
      }

      // Not found in any breakout room - might be in Main Room
      return null;
    } catch (err) {
      addLog(`Failed to find participant: ${err.message}`);
      throw err;
    }
  }, [getBreakoutRooms, addLog]);

  /**
   * Send full room mapping to backend
   * Backend will store in room_mappings table
   */
  const syncRoomMappings = useCallback(async () => {
    try {
      const rooms = await fetchRoomList();

      if (rooms.length === 0) {
        addLog('No breakout rooms to sync');
        return;
      }

      const meetingId = meetingIdRef.current || extractMeetingId(meetingContext);
      let meetingUuid = '';
      try {
        meetingUuid = await getMeetingUUID();
      } catch (e) {
        // Meeting UUID is optional
      }

      const payload = {
        meeting_id: meetingId,
        meeting_uuid: meetingUuid,
        rooms: rooms,
        synced_at: new Date().toISOString()
      };

      await api.post('/mapping/sync', payload);

      setLastSync(new Date());
      setSyncCount(prev => prev + 1);
      addLog(`Synced ${rooms.length} room mappings`);
    } catch (err) {
      addLog(`Sync failed: ${err.message}`);
      setErrors(prev => [...prev.slice(-10), err.message]);
    }
  }, [fetchRoomList, meetingContext, getMeetingUUID, addLog]);

  /**
   * Check for pending mapping requests from backend
   * When webhook has unknown room_uuid, it queues a request here
   */
  const checkMappingRequests = useCallback(async () => {
    try {
      const meetingId = meetingIdRef.current || extractMeetingId(meetingContext);
      if (!meetingId) return;

      const response = await api.get(`/mapping/pending?meeting_id=${meetingId}`);
      const pending = response.data?.pending || [];

      if (pending.length === 0) return;

      addLog(`Processing ${pending.length} mapping request(s)`);
      setMappingRequests(prev => prev + pending.length);

      for (const request of pending) {
        const { participant_name, participant_email, webhook_uuid, request_id } = request;

        // Find which room this participant is in
        const roomInfo = await findParticipantRoom(participant_name, participant_email);

        if (roomInfo) {
          // Send mapping back to backend
          await api.post('/mapping/resolve', {
            request_id: request_id,
            webhook_uuid: webhook_uuid,
            room_name: roomInfo.room_name,
            sdk_uuid: roomInfo.sdk_uuid,
            meeting_id: meetingId
          });
          addLog(`Mapped ${webhook_uuid.substring(0, 8)}... -> ${roomInfo.room_name}`);
        } else {
          // Participant not found in any breakout - mark as Main Room or unknown
          await api.post('/mapping/resolve', {
            request_id: request_id,
            webhook_uuid: webhook_uuid,
            room_name: null, // Backend will handle as Main Room
            meeting_id: meetingId
          });
          addLog(`Could not find room for ${participant_name}`);
        }
      }
    } catch (err) {
      // Don't spam errors for 404 (no pending requests)
      if (!err.response || err.response.status !== 404) {
        addLog(`Check pending failed: ${err.message}`);
      }
    }
  }, [meetingContext, findParticipantRoom, addLog]);

  /**
   * Start the room mapper
   */
  const startMapper = useCallback(async () => {
    if (isActiveRef.current) return;

    try {
      const uuid = await getMeetingUUID();
      let mid = extractMeetingId(meetingContext);

      if (!mid && uuid) {
        const numericMatch = uuid.match(/^(\d{9,11})/);
        if (numericMatch) mid = numericMatch[1];
      }
      if (!mid) mid = process.env.REACT_APP_MEETING_ID || '';
      if (!mid && uuid) mid = uuid;

      meetingIdRef.current = mid;

      if (!mid) {
        addLog('ERROR: Could not get meeting ID');
        return;
      }

      addLog(`Starting room mapper for meeting ${mid}`);
      isActiveRef.current = true;
      setIsActive(true);

      // Initial sync
      await syncRoomMappings();

      // Check for mapping requests every 5 seconds
      checkIntervalRef.current = setInterval(checkMappingRequests, MAPPING_CHECK_INTERVAL_MS);

      // Full sync every 1 minute (redundancy)
      syncIntervalRef.current = setInterval(syncRoomMappings, FULL_ROOM_SYNC_INTERVAL_MS);

      addLog('Room mapper active - bot can stay idle');
    } catch (err) {
      addLog(`Failed to start: ${err.message}`);
      setErrors(prev => [...prev.slice(-10), err.message]);
      isActiveRef.current = false;
      setIsActive(false);
    }
  }, [getMeetingUUID, meetingContext, syncRoomMappings, checkMappingRequests, addLog]);

  /**
   * Stop the room mapper
   */
  const stopMapper = useCallback(() => {
    if (checkIntervalRef.current) {
      clearInterval(checkIntervalRef.current);
      checkIntervalRef.current = null;
    }
    if (syncIntervalRef.current) {
      clearInterval(syncIntervalRef.current);
      syncIntervalRef.current = null;
    }
    isActiveRef.current = false;
    setIsActive(false);
    addLog('Room mapper stopped');
  }, [addLog]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (checkIntervalRef.current) clearInterval(checkIntervalRef.current);
      if (syncIntervalRef.current) clearInterval(syncIntervalRef.current);
    };
  }, []);

  // Auto-start when SDK is ready
  useEffect(() => {
    if (isConfigured && !isActiveRef.current) {
      const timer = setTimeout(() => startMapper(), 1000);
      return () => clearTimeout(timer);
    }
  }, [isConfigured, startMapper]);

  // Not configured
  if (!isConfigured) {
    return (
      <div style={styles.container}>
        <h2 style={styles.title}>Room Mapper</h2>
        <p style={styles.subtitle}>Webhook-Primary Mode</p>
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
      <h2 style={styles.title}>Room Mapper</h2>
      <p style={styles.subtitle}>Webhook-Primary Mode - Bot stays idle</p>

      {meetingContext && (
        <span style={styles.meetingId}>Meeting: {meetingContext.meetingID}</span>
      )}

      {/* Status */}
      <div style={isActive ? styles.activeBox : styles.idleBox}>
        <div style={styles.statusRow}>
          <span style={styles.statusDot(isActive ? '#00C851' : '#888')} />
          <span style={styles.statusLabel}>
            {isActive ? 'MAPPING ACTIVE' : 'IDLE'}
          </span>
        </div>

        {isActive && (
          <>
            <div style={styles.statsGrid}>
              <div style={styles.stat}>
                <span style={styles.statValue}>{syncCount}</span>
                <span style={styles.statLabel}>Syncs</span>
              </div>
              <div style={styles.stat}>
                <span style={styles.statValue}>{roomCount}</span>
                <span style={styles.statLabel}>Rooms</span>
              </div>
              <div style={styles.stat}>
                <span style={styles.statValue}>{mappingRequests}</span>
                <span style={styles.statLabel}>Mapped</span>
              </div>
            </div>

            <p style={styles.infoText}>
              Webhooks capture all attendance data with exact timestamps.
              This panel only provides room name mappings.
            </p>
          </>
        )}

        {lastSync && (
          <p style={styles.lastSync}>
            Last sync: {lastSync.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata' })} IST
          </p>
        )}
      </div>

      {/* Controls */}
      <div style={styles.actions}>
        {!isActive ? (
          <button style={styles.startButton} onClick={startMapper}>
            Start Mapper
          </button>
        ) : (
          <button style={styles.stopButton} onClick={stopMapper}>
            Stop Mapper
          </button>
        )}
        <button style={styles.syncButton} onClick={syncRoomMappings} disabled={!isActive}>
          Sync Now
        </button>
      </div>

      {/* Room List */}
      {roomList.length > 0 && (
        <div style={styles.section}>
          <h3 style={styles.sectionTitle}>BREAKOUT ROOMS ({roomList.length})</h3>
          <div style={styles.roomGrid}>
            {roomList.map((room, i) => (
              <div key={i} style={styles.roomItem}>
                <span style={styles.roomName}>{room.room_name}</span>
                <span style={styles.roomUuid}>{room.sdk_uuid.substring(0, 8)}...</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Errors */}
      {errors.length > 0 && (
        <div style={styles.errorBox}>
          <h3 style={styles.sectionTitle}>ERRORS</h3>
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
  subtitle: { color: '#2D8CFF', fontSize: '12px', margin: '-8px 0 0 0' },
  meetingId: { color: '#666', fontSize: '11px' },

  activeBox: { backgroundColor: 'rgba(0,200,81,0.08)', border: '1px solid rgba(0,200,81,0.3)', borderRadius: '10px', padding: '16px' },
  idleBox: { backgroundColor: 'rgba(255,255,255,0.05)', border: '1px solid #333', borderRadius: '10px', padding: '16px' },
  statusRow: { display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' },
  statusDot: (color) => ({ display: 'inline-block', width: '10px', height: '10px', borderRadius: '50%', backgroundColor: color, boxShadow: `0 0 6px ${color}` }),
  statusLabel: { color: '#fff', fontSize: '14px', fontWeight: '600', letterSpacing: '1px' },
  statusText: { color: '#ccc', fontSize: '13px' },
  lastSync: { color: '#888', fontSize: '11px', margin: '8px 0 0 0' },
  infoText: { color: '#888', fontSize: '11px', margin: '8px 0 0 0', lineHeight: '1.4' },

  statsGrid: { display: 'flex', gap: '16px' },
  stat: { display: 'flex', flexDirection: 'column', alignItems: 'center' },
  statValue: { color: '#fff', fontSize: '24px', fontWeight: '700' },
  statLabel: { color: '#888', fontSize: '10px', textTransform: 'uppercase' },

  actions: { display: 'flex', gap: '8px' },
  startButton: { flex: 1, padding: '12px', backgroundColor: '#00C851', color: '#fff', border: 'none', borderRadius: '6px', fontSize: '14px', fontWeight: '600', cursor: 'pointer' },
  stopButton: { flex: 1, padding: '12px', backgroundColor: '#ff4757', color: '#fff', border: 'none', borderRadius: '6px', fontSize: '14px', fontWeight: '600', cursor: 'pointer' },
  syncButton: { padding: '12px 16px', backgroundColor: '#2D8CFF', color: '#fff', border: 'none', borderRadius: '6px', fontSize: '14px', fontWeight: '600', cursor: 'pointer' },

  section: { display: 'flex', flexDirection: 'column', gap: '6px' },
  sectionTitle: { color: '#888', fontSize: '11px', fontWeight: '600', textTransform: 'uppercase', margin: 0 },
  roomGrid: { display: 'flex', flexDirection: 'column', gap: '2px' },
  roomItem: { display: 'flex', justifyContent: 'space-between', padding: '6px 10px', backgroundColor: 'rgba(255,255,255,0.03)', borderRadius: '4px' },
  roomName: { color: '#2D8CFF', fontSize: '12px' },
  roomUuid: { color: '#666', fontSize: '10px', fontFamily: 'monospace' },

  errorBox: { backgroundColor: 'rgba(255,71,87,0.1)', border: '1px solid rgba(255,71,87,0.3)', borderRadius: '8px', padding: '10px' },
  errorText: { color: '#ff6b6b', fontSize: '11px', margin: '4px 0' },

  logBox: { backgroundColor: 'rgba(0,0,0,0.4)', padding: '10px', borderRadius: '6px', fontSize: '10px', color: '#00C851', overflow: 'auto', maxHeight: '150px', fontFamily: 'Monaco, monospace', margin: 0, whiteSpace: 'pre-wrap' }
};

export default RoomMapperPanel;
