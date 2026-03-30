/**
 * API Service
 * Handles communication with your backend server
 *
 * In Cloud Run: React app served from same domain, uses relative URLs
 * In Development: Uses localhost:8080
 */

import axios from 'axios';

// Get the backend URL
// In Cloud Run: Same origin (relative URL works)
// In development: Use explicit localhost
const getBackendUrl = () => {
  // If REACT_APP_BACKEND_URL is set, use it
  if (process.env.REACT_APP_BACKEND_URL) {
    return process.env.REACT_APP_BACKEND_URL;
  }

  // In production (Cloud Run), use relative URL (same origin)
  if (process.env.NODE_ENV === 'production') {
    return '';  // Empty string = same origin
  }

  // In development, use localhost
  return 'http://localhost:8080';
};

const BACKEND_URL = getBackendUrl();

const api = axios.create({
  baseURL: BACKEND_URL,
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json'
  }
});

/**
 * Notify backend that calibration is starting
 * @param {string} meetingId - Meeting ID
 * @param {string} meetingUUID - Meeting UUID
 * @param {object} calibrationParticipant - Info about who is doing calibration (optional)
 * @param {string} calibrationParticipant.name - Participant name
 * @param {string} calibrationParticipant.participantUUID - Participant UUID
 * @param {string} calibrationParticipant.mode - 'scout_bot' or 'self'
 * @param {Array} roomSequence - SEQUENCE-BASED MATCHING: Ordered array of rooms [{room_name, room_uuid}, ...]
 */
export async function notifyCalibrationStart(meetingId, meetingUUID, calibrationParticipant = null, roomSequence = []) {
  try {
    const payload = {
      meeting_id: meetingId,
      meeting_uuid: meetingUUID,
      started_at: new Date().toISOString()
    };

    // Add calibration participant info if provided
    if (calibrationParticipant) {
      payload.calibration_participant_name = calibrationParticipant.name || '';
      payload.calibration_participant_uuid = calibrationParticipant.participantUUID || '';
      payload.calibration_mode = calibrationParticipant.mode || 'scout_bot';
    }

    // SEQUENCE-BASED MATCHING: Send the room sequence
    // Backend will use this to match webhooks by position, not timing
    if (roomSequence && roomSequence.length > 0) {
      payload.room_sequence = roomSequence.map(room => ({
        room_name: room.breakoutRoomName || room.name || room.roomName,
        room_uuid: room.breakoutRoomId || room.uuid || room.roomUUID
      }));
      console.log(`[API] Sending room sequence for sequence-based matching: ${payload.room_sequence.length} rooms`);
    }

    const response = await api.post('/calibration/start', payload);
    return response.data;
  } catch (err) {
    console.error('Failed to notify calibration start:', err);
    // Don't throw - calibration can continue without backend notification
    return null;
  }
}

/**
 * Send room mapping data to backend
 */
export async function sendRoomMapping(meetingId, meetingUUID, roomMapping) {
  try {
    const response = await api.post('/calibration/mapping', {
      meeting_id: meetingId,
      meeting_uuid: meetingUUID,
      room_mapping: roomMapping.map(room => ({
        room_uuid: room.roomUUID,
        room_name: room.roomName,
        room_index: room.roomIndex,
        mapped_at: room.timestamp
      })),
      completed_at: new Date().toISOString()
    });
    return response.data;
  } catch (err) {
    console.error('Failed to send room mapping:', err);
    throw err;
  }
}

/**
 * Notify backend that calibration is complete
 */
export async function notifyCalibrationComplete(meetingId, meetingUUID, result) {
  try {
    const response = await api.post('/calibration/complete', {
      meeting_id: meetingId,
      meeting_uuid: meetingUUID,
      success: result.success,
      total_rooms: result.totalRooms,
      mapped_rooms: result.mappedRooms,
      completed_at: new Date().toISOString()
    });
    return response.data;
  } catch (err) {
    console.error('Failed to notify calibration complete:', err);
    return null;
  }
}

/**
 * Get existing room mappings for current meeting (from in-memory state)
 * Note: Backend uses global state, meeting_id is for future filtering
 */
export async function getExistingMappings(meetingId) {
  try {
    const response = await api.get('/mappings');
    // Backend returns: {meeting_id, calibration_complete, mappings: [{room_name, room_uuid}], total}
    return response.data.mappings || [];
  } catch (err) {
    console.error('Failed to get existing mappings:', err);
    return [];
  }
}

/**
 * Health check for backend connection
 */
export async function checkBackendHealth() {
  try {
    const response = await api.get('/health');
    return response.data.status === 'healthy';
  } catch (err) {
    return false;
  }
}

/**
 * Check if a specific room's webhook has been received
 * Used to confirm webhook before moving to next room
 */
export async function checkRoomWebhookReceived(roomName) {
  try {
    const response = await api.get(`/calibration/pending?room_name=${encodeURIComponent(roomName)}`);
    return response.data;
  } catch (err) {
    console.error('Failed to check room webhook:', err);
    return { matched: false };
  }
}

/**
 * Poll until webhook is received for a room (with timeout)
 * @param {string} roomName - Room name to check
 * @param {number} timeoutMs - Maximum time to wait (default 15 seconds)
 * @param {number} pollIntervalMs - Poll interval (default 1 second)
 */
export async function waitForWebhookConfirmation(roomName, timeoutMs = 15000, pollIntervalMs = 1000) {
  const startTime = Date.now();

  while (Date.now() - startTime < timeoutMs) {
    const result = await checkRoomWebhookReceived(roomName);
    if (result.matched) {
      console.log(`Webhook confirmed for room: ${roomName}`);
      return { confirmed: true, ...result };
    }
    // Wait before next poll
    await new Promise(resolve => setTimeout(resolve, pollIntervalMs));
  }

  console.warn(`Webhook timeout for room: ${roomName}`);
  return { confirmed: false, timeout: true };
}

/**
 * Notify backend that a room mapping has been VERIFIED by SDK
 * This triggers BigQuery save (only verified mappings are saved)
 */
export async function verifyRoomMapping(meetingId, roomName) {
  try {
    const response = await api.post('/calibration/verify', {
      meeting_id: meetingId,
      room_name: roomName
    });
    console.log(`[API] Verified mapping for room: ${roomName}`);
    return response.data;
  } catch (err) {
    console.error(`[API] Failed to verify mapping for ${roomName}:`, err);
    // Don't throw - verification can continue without backend confirmation
    return { success: false, error: err.message };
  }
}

/**
 * Abort calibration and delete ALL mappings from this session.
 * Call this when calibration fails midway to prevent duplicate records.
 */
export async function abortCalibration(meetingId) {
  try {
    const response = await api.post('/calibration/abort', {
      meeting_id: meetingId
    });
    console.log(`[API] Calibration aborted - ${response.data.deleted_mappings} mappings deleted`);
    return response.data;
  } catch (err) {
    console.error('[API] Failed to abort calibration:', err);
    return { success: false, error: err.message };
  }
}

/**
 * Reset calibration state completely
 * @param {string} meetingId - Meeting ID
 * @param {boolean} clearBigQuery - Whether to clear BigQuery mappings too
 */
export async function resetCalibration(meetingId, clearBigQuery = false) {
  try {
    const response = await api.post('/calibration/reset', {
      meeting_id: meetingId,
      clear_bigquery: clearBigQuery
    });
    console.log(`[API] Calibration reset complete`);
    return response.data;
  } catch (err) {
    console.error('[API] Failed to reset calibration:', err);
    throw err;
  }
}

/**
 * Get live room participant data for manual verification
 * @param {string} meetingId - Meeting ID
 */
export async function getLiveRooms(meetingId) {
  try {
    const response = await api.get(`/calibration/live-rooms?meeting_id=${meetingId}`);
    return response.data;
  } catch (err) {
    console.error('[API] Failed to get live rooms:', err);
    return { success: false, rooms: [], error: err.message };
  }
}

/**
 * Get mapping summary comparing expected vs actual mappings
 * @param {string} meetingId - Meeting ID
 */
export async function getMappingSummary(meetingId) {
  try {
    const response = await api.get(`/calibration/mapping-summary?meeting_id=${meetingId}`);
    return response.data;
  } catch (err) {
    console.error('[API] Failed to get mapping summary:', err);
    return { success: false, rooms: [], error: err.message };
  }
}

/**
 * Prepare a specific room for re-calibration
 * @param {string} meetingId - Meeting ID
 * @param {string} roomName - Room name to recalibrate
 * @param {string} roomUUID - SDK room UUID (optional)
 */
export async function prepareRoomRecalibration(meetingId, roomName, roomUUID = null) {
  try {
    const response = await api.post('/calibration/recalibrate-room', {
      meeting_id: meetingId,
      room_name: roomName,
      room_uuid: roomUUID
    });
    console.log(`[API] Room prepared for recalibration: ${roomName}`);
    return response.data;
  } catch (err) {
    console.error(`[API] Failed to prepare room recalibration:`, err);
    throw err;
  }
}

/**
 * Complete a single room re-calibration
 * @param {string} meetingId - Meeting ID
 * @param {string} roomName - Room name that was recalibrated
 */
export async function completeRoomRecalibration(meetingId, roomName) {
  try {
    const response = await api.post('/calibration/single-room-complete', {
      meeting_id: meetingId,
      room_name: roomName
    });
    console.log(`[API] Room recalibration complete: ${roomName}`);
    return response.data;
  } catch (err) {
    console.error(`[API] Failed to complete room recalibration:`, err);
    return { success: false, error: err.message };
  }
}

/**
 * Check if a room already has a webhook UUID mapping (can be skipped)
 * @param {string} roomName - Room name to check
 * @param {string} meetingId - Meeting ID (optional)
 * @returns {Promise<{mapped: boolean, can_skip: boolean, source: string|null}>}
 */
export async function checkRoomAlreadyMapped(roomName, meetingId = null) {
  try {
    let url = `/calibration/check-room-mapped?room_name=${encodeURIComponent(roomName)}`;
    if (meetingId) {
      url += `&meeting_id=${encodeURIComponent(meetingId)}`;
    }
    const response = await api.get(url);
    return response.data;
  } catch (err) {
    console.error(`[API] Failed to check room mapping for ${roomName}:`, err);
    // On error, return not mapped (will calibrate to be safe)
    return { mapped: false, can_skip: false, error: err.message };
  }
}

export default {
  notifyCalibrationStart,
  sendRoomMapping,
  notifyCalibrationComplete,
  getExistingMappings,
  checkBackendHealth,
  checkRoomWebhookReceived,
  waitForWebhookConfirmation,
  verifyRoomMapping,
  abortCalibration,
  resetCalibration,
  getLiveRooms,
  getMappingSummary,
  prepareRoomRecalibration,
  completeRoomRecalibration,
  checkRoomAlreadyMapped
};
