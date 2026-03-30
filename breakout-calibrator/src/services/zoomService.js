/**
 * Zoom Calibration Service
 * Pure position-based calibration: move bot to room N, wait for webhook, that's room N.
 * Frontend BLOCKS on each room until webhook confirmed before moving to next.
 */

import { waitForWebhookConfirmation } from './apiService';

const BOT_NAME = process.env.REACT_APP_BOT_NAME || 'Scout Bot';
const BOT_EMAIL = process.env.REACT_APP_BOT_EMAIL || '';

// ============================================================================
// TIMING CONSTANTS
// ============================================================================
const DEFAULT_MOVE_DELAY_MS = 5000; // Wait for Scout Bot to click Join
const WEBHOOK_TIMEOUT_MS = 60000; // 60 seconds max wait for webhook (blocking)
const MAX_MOVE_RETRIES = 1; // Max retries for SDK move call
const POST_WEBHOOK_DELAY_MS = 2000; // Delay after webhook before next room

// DELAY OPTIONS (user selectable in UI)
export const DELAY_OPTIONS = {
  FAST: { label: 'Fast (10s)', value: 10000, description: 'Quick but may miss webhooks' },
  NORMAL: { label: 'Normal (30s)', value: 30000, description: 'Recommended for most meetings' },
  SLOW: { label: 'Slow (60s)', value: 60000, description: 'Best accuracy, guaranteed webhook order' },
  VERY_SLOW: { label: 'Very Slow (90s)', value: 90000, description: 'Maximum reliability' }
};

/**
 * Helper to get participant name from various possible SDK fields
 */
function getParticipantName(p) {
  return p.screenName || p.displayName || p.participantName || p.name || p.userName || p.user_name || '';
}

/**
 * Helper to get participant email
 */
function getParticipantEmail(p) {
  return p.email || p.participantEmail || p.user_email || '';
}

/**
 * Shared bot name matching logic
 */
export function isBotNameMatch(participantName, botName = BOT_NAME) {
  const pName = participantName.toLowerCase();
  const normalizedBotName = botName.toLowerCase();

  const isExactMatch = pName === normalizedBotName;
  const containsBotName = pName.includes(normalizedBotName);
  const isScoutBot = pName.includes('scout bot') || pName.includes('scoutbot');
  const isScoutPattern = pName.startsWith('scout') && pName.includes('bot');

  return isExactMatch || containsBotName || isScoutBot || isScoutPattern;
}

/**
 * Find the scout bot in the participant list
 */
export function findScoutBot(participants, botName = BOT_NAME, botEmail = BOT_EMAIL) {
  const normalizedBotEmail = botEmail.toLowerCase();

  console.log('Looking for bot:', botName);
  console.log('Participant names:', participants.map(p => getParticipantName(p)));

  // Strategy 1: Exact email match
  if (normalizedBotEmail) {
    const byEmail = participants.find(p => getParticipantEmail(p).toLowerCase() === normalizedBotEmail);
    if (byEmail) {
      console.log('Found scout bot by email match');
      return byEmail;
    }
  }

  // Strategy 2: Name match
  const byName = participants.find(p => isBotNameMatch(getParticipantName(p), botName));
  if (byName) {
    console.log('Found scout bot by name match');
    return byName;
  }

  console.log('Scout bot not found in', participants.length, 'participants');
  return null;
}

/**
 * Sleep utility
 */
export function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * Move participant with retry logic
 */
async function moveWithRetry(moveParticipantToRoom, botUUID, roomUUID, roomName, maxRetries = MAX_MOVE_RETRIES) {
  let lastError;

  for (let attempt = 1; attempt <= maxRetries + 1; attempt++) {
    try {
      const response = await moveParticipantToRoom(botUUID, roomUUID);
      console.log(`>>> MOVE RESPONSE for ${roomName}:`, JSON.stringify(response));
      return { success: true, attempts: attempt };
    } catch (err) {
      lastError = err;
      console.warn(`Move attempt ${attempt} failed for ${roomName}:`, err.message);
      if (attempt <= maxRetries) {
        await sleep(1000 * attempt);
      }
    }
  }

  return { success: false, error: lastError, attempts: maxRetries + 1 };
}

/**
 * Run the calibration sequence - PURE POSITION-BASED
 *
 * Flow for each room:
 * 1. Move bot to room[i]
 * 2. Wait for webhook (BLOCKING - do NOT move to next room until confirmed)
 * 3. Backend assigns webhook UUID to room[i] by position
 * 4. Only then move to room[i+1]
 *
 * If ANY room fails webhook confirmation, calibration stops and reports failure.
 * Caller should call abortCalibration() to clean up partial mappings.
 */
export async function runCalibration({
  getBreakoutRooms,
  getParticipants,
  moveParticipantToRoom,
  moveToMainRoom,
  onProgress,
  onRoomMapped,
  delayMs = DEFAULT_MOVE_DELAY_MS,
  startFromRoom = 0
}) {
  const roomMapping = [];
  const errors = [];

  // Step 1: Get all breakout rooms
  onProgress?.({ step: 'fetching_rooms', message: 'Fetching breakout rooms...' });
  const rooms = await getBreakoutRooms();

  if (!rooms || rooms.length === 0) {
    throw new Error('No breakout rooms found. Make sure breakout rooms are created and open.');
  }

  onProgress?.({
    step: 'rooms_found',
    message: `Found ${rooms.length} breakout rooms`,
    totalRooms: rooms.length
  });

  // Step 2: Find the scout bot
  onProgress?.({ step: 'finding_bot', message: 'Looking for scout bot...' });
  const participants = await getParticipants();

  const participantNames = participants.map(p => getParticipantName(p)).join(', ');
  onProgress?.({ step: 'participants_found', message: `Found ${participants.length}: ${participantNames}` });

  const scoutBot = findScoutBot(participants);
  if (!scoutBot) {
    throw new Error(`Bot "${BOT_NAME}" not found. Found: ${participantNames}`);
  }

  const botUUID = scoutBot.participantUUID || scoutBot.uuid || scoutBot.participantId || scoutBot.id;
  const botName = scoutBot.name || scoutBot.participantName || scoutBot.screenName || scoutBot.displayName || scoutBot.userName;

  console.log('Using botUUID:', botUUID);

  onProgress?.({
    step: 'bot_found',
    message: `Found scout bot: ${botName} (UUID: ${botUUID})`,
    botId: botUUID
  });

  // Step 3: Return bot to Main Room BEFORE starting
  onProgress?.({ step: 'resetting_bot', message: 'Returning Scout Bot to Main Room first...' });
  try {
    await moveToMainRoom(botUUID);
    await sleep(3000);
  } catch (err) {
    console.warn('Could not return bot to main room (may already be there):', err.message);
  }

  // Step 4: Sequential room calibration - BLOCKING on each webhook
  const startIndex = startFromRoom > 0 ? startFromRoom : 0;
  if (startIndex > 0) {
    onProgress?.({
      step: 'resuming',
      message: `Resuming from room ${startIndex + 1}/${rooms.length}`,
      currentRoom: startIndex,
      totalRooms: rooms.length
    });
  }

  for (let i = startIndex; i < rooms.length; i++) {
    const room = rooms[i];
    const roomName = room.breakoutRoomName || room.name || `Room ${i + 1}`;
    const roomUUID = room.breakoutRoomId || room.breakoutRoomUUID || room.breakoutroomid || room.uuid || room.id;

    // Notify UI: moving to room
    onProgress?.({
      step: 'moving_to_room',
      message: `[${i + 1}/${rooms.length}] Moving to: ${roomName}`,
      currentRoom: i + 1,
      totalRooms: rooms.length,
      roomName,
      roomUUID,
      botUUID
    });

    // Move bot to room
    const moveResult = await moveWithRetry(moveParticipantToRoom, botUUID, roomUUID, roomName);

    if (!moveResult.success) {
      console.error(`Move FAILED for ${roomName}:`, moveResult.error);
      errors.push({ roomName, roomUUID, error: `Move failed: ${moveResult.error?.message}` });

      onProgress?.({
        step: 'room_error',
        message: `FAILED to move to ${roomName} - STOPPING calibration`,
        currentRoom: i + 1,
        totalRooms: rooms.length,
        error: 'Move failed'
      });

      // STOP calibration on move failure - don't continue with wrong sequence
      break;
    }

    // Wait for bot to click Join
    onProgress?.({
      step: 'waiting_join',
      message: `[${i + 1}/${rooms.length}] Waiting for bot to join ${roomName}...`,
      currentRoom: i + 1,
      totalRooms: rooms.length
    });
    await sleep(delayMs);

    // BLOCKING WAIT: Wait for webhook confirmation
    // This is the KEY part - do NOT move to next room until this webhook is confirmed
    onProgress?.({
      step: 'waiting_webhook',
      message: `[${i + 1}/${rooms.length}] Waiting for webhook: ${roomName}...`,
      currentRoom: i + 1,
      totalRooms: rooms.length
    });

    const webhookResult = await waitForWebhookConfirmation(roomName, WEBHOOK_TIMEOUT_MS, 1000);

    if (webhookResult.confirmed) {
      console.log(`Webhook CONFIRMED for ${roomName}`);

      const mapping = {
        roomUUID,
        roomName,
        roomIndex: i,
        timestamp: new Date().toISOString(),
        webhookConfirmed: true
      };
      roomMapping.push(mapping);
      onRoomMapped?.(mapping);

      onProgress?.({
        step: 'room_mapped',
        message: `[${i + 1}/${rooms.length}] CONFIRMED: ${roomName}`,
        currentRoom: i + 1,
        totalRooms: rooms.length,
        mapping,
        verified: true,
        webhookConfirmed: true
      });

      // Brief delay before moving to next room
      await sleep(POST_WEBHOOK_DELAY_MS);
    } else {
      console.error(`Webhook TIMEOUT for ${roomName} - STOPPING calibration`);
      errors.push({ roomName, roomUUID, error: 'Webhook timeout' });

      onProgress?.({
        step: 'room_error',
        message: `TIMEOUT waiting for webhook: ${roomName} - STOPPING`,
        currentRoom: i + 1,
        totalRooms: rooms.length,
        error: 'Webhook timeout'
      });

      // STOP calibration on webhook timeout - continuing would corrupt the sequence
      break;
    }
  }

  // Step 5: Return bot to main room
  onProgress?.({ step: 'returning', message: 'Returning scout bot to main room...' });
  try {
    await moveToMainRoom(botUUID);
    await sleep(1000);
  } catch (err) {
    console.warn('Failed to return bot to main room:', err);
  }

  const hasErrors = errors.length > 0;

  onProgress?.({
    step: 'complete',
    message: hasErrors
      ? `Calibration STOPPED at room ${roomMapping.length + 1}/${rooms.length}. ${errors[0]?.error}`
      : `Calibration complete! All ${roomMapping.length} rooms mapped.`,
    totalMapped: roomMapping.length,
    errors: errors.length
  });

  return {
    success: !hasErrors,
    roomMapping,
    totalRooms: rooms.length,
    mappedRooms: roomMapping.length,
    errors
  };
}

export default {
  findScoutBot,
  runCalibration,
  sleep,
  DELAY_OPTIONS
};
