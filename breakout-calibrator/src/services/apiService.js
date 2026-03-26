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
 */
export async function notifyCalibrationStart(meetingId, meetingUUID, calibrationParticipant = null) {
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
 * Get existing room mappings for a meeting
 */
export async function getExistingMappings(meetingId) {
  try {
    const response = await api.get(`/calibration/mappings/${meetingId}`);
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

export default {
  notifyCalibrationStart,
  sendRoomMapping,
  notifyCalibrationComplete,
  getExistingMappings,
  checkBackendHealth,
  checkRoomWebhookReceived,
  waitForWebhookConfirmation,
  verifyRoomMapping
};
