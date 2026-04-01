/**
 * Zoom Calibration Service
 * Pure position-based calibration with SDK cross-validation.
 *
 * CRITICAL RULE: You CANNOT skip rooms in position-based matching.
 * If room N fails → STOP. Continuing would make webhook N+1 = room N on backend.
 *
 * Flow per room:
 *   1. Return bot to main room (required by Zoom before joining breakout)
 *   2. Move bot to room[i]
 *   3. Wait for webhook (BLOCKING)
 *   4. Backend assigns webhook UUID to room[i] by position
 *   5. Frontend verifies bot is ACTUALLY in room[i] via SDK
 *   6. If mismatch → STOP (wrong mapping detected)
 *   7. Only then move to room[i+1]
 *
 * NO SKIPPING. Failure at any room = calibration stops = abort all mappings.
 */

import { waitForWebhookConfirmation } from './apiService';

const BOT_NAME = process.env.REACT_APP_BOT_NAME || 'Scout Bot';
const BOT_EMAIL = process.env.REACT_APP_BOT_EMAIL || '';

// ============================================================================
// TIMING CONSTANTS
// ============================================================================
const DEFAULT_MOVE_DELAY_MS = 5000; // Wait for Scout Bot to click Join
const WEBHOOK_TIMEOUT_MS = 60000; // 60 seconds max wait for webhook (BLOCKING)
const MAX_MOVE_RETRIES = 1; // Max retries for SDK move call
const POST_WEBHOOK_DELAY_MS = 2000; // Delay after webhook before next room
const VERIFY_POLL_INTERVAL_MS = 2000; // Poll interval for SDK location check
const VERIFY_POLL_TIMEOUT_MS = 10000; // Max time to wait for SDK verification

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

  if (normalizedBotEmail) {
    const byEmail = participants.find(p => getParticipantEmail(p).toLowerCase() === normalizedBotEmail);
    if (byEmail) {
      console.log('Found scout bot by email match');
      return byEmail;
    }
  }

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

// =============================================================================
// SDK CROSS-VALIDATION
// =============================================================================

/**
 * Verify bot is in the expected room by querying SDK breakout room list.
 */
async function verifyBotInRoom(getBreakoutRooms, expectedRoomName, botName = BOT_NAME) {
  try {
    const rooms = await getBreakoutRooms();

    for (const room of rooms) {
      const roomName = room.breakoutRoomName || room.name || 'Unknown';
      const participants = room.participants || room.members || room.attendees || [];

      for (const participant of participants) {
        const pName = getParticipantName(participant);
        if (isBotNameMatch(pName, botName)) {
          if (roomName === expectedRoomName) {
            return { verified: true, actualRoom: roomName };
          } else {
            return { verified: false, actualRoom: roomName, mismatch: true };
          }
        }
      }
    }

    return { verified: false, actualRoom: null, notFound: true };
  } catch (err) {
    console.error('[Verify] SDK query failed:', err);
    return { verified: false, actualRoom: null, error: err.message };
  }
}

/**
 * Poll SDK until bot is found in expected room or timeout.
 */
async function verifyBotInRoomWithPolling(getBreakoutRooms, expectedRoomName, timeoutMs = VERIFY_POLL_TIMEOUT_MS) {
  const startTime = Date.now();

  while (Date.now() - startTime < timeoutMs) {
    const result = await verifyBotInRoom(getBreakoutRooms, expectedRoomName);

    if (result.verified) return result;
    if (result.mismatch) return result;

    console.log(`[Verify] Bot not found yet, retrying in ${VERIFY_POLL_INTERVAL_MS}ms...`);
    await sleep(VERIFY_POLL_INTERVAL_MS);
  }

  return { verified: false, actualRoom: null, timeout: true };
}

/**
 * Sort SDK rooms to match a preferred order.
 *
 * Sorts by prefix number (e.g., "1.1" < "1.2" < "2.0" < "3.1").
 * This gives us a deterministic order regardless of SDK's random ordering.
 * The backend will use the SAME room list (sent at calibration start).
 */
function sortRoomsByPrefix(rooms) {
  return [...rooms].sort((a, b) => {
    const nameA = a.breakoutRoomName || a.name || '';
    const nameB = b.breakoutRoomName || b.name || '';

    // Extract prefix like "1.1", "2.0", "3.10"
    const matchA = nameA.match(/^(\d+)\.(\d+)/);
    const matchB = nameB.match(/^(\d+)\.(\d+)/);

    if (matchA && matchB) {
      const majorA = parseInt(matchA[1], 10);
      const majorB = parseInt(matchB[1], 10);
      if (majorA !== majorB) return majorA - majorB;
      const minorA = parseInt(matchA[2], 10);
      const minorB = parseInt(matchB[2], 10);
      return minorA - minorB;
    }

    // Rooms with prefix come before rooms without
    if (matchA && !matchB) return -1;
    if (!matchA && matchB) return 1;

    // Fallback: alphabetical
    return nameA.localeCompare(nameB);
  });
}

/**
 * Run the calibration sequence.
 *
 * SINGLE SOURCE OF TRUTH: SDK room list, sorted by prefix.
 * - Frontend sorts SDK rooms → sends sorted list to backend at /calibration/start
 * - Backend stores this as its calibration_sequence
 * - Frontend iterates same sorted list
 * - Webhook #N = room #N in this list (both sides agree)
 *
 * NO SKIPPING ALLOWED. Any failure = STOP = abort all mappings.
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

  // Step 1: Get all breakout rooms from SDK
  onProgress?.({ step: 'fetching_rooms', message: 'Fetching breakout rooms...' });
  const rawRooms = await getBreakoutRooms();

  if (!rawRooms || rawRooms.length === 0) {
    throw new Error('No breakout rooms found. Make sure breakout rooms are created and open.');
  }

  // Sort rooms by prefix for deterministic order
  const rooms = sortRoomsByPrefix(rawRooms);

  console.log('[Calibration] Sorted room order:');
  rooms.forEach((r, i) => {
    const name = r.breakoutRoomName || r.name || '';
    console.log(`  ${i + 1}. ${name}`);
  });

  onProgress?.({
    step: 'rooms_found',
    message: `Found ${rooms.length} breakout rooms (sorted by prefix)`,
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

  // Step 4: Sequential room calibration - NO SKIPPING
  const totalRooms = rooms.length;
  const startIndex = startFromRoom > 0 ? startFromRoom : 0;

  if (startIndex > 0) {
    onProgress?.({
      step: 'resuming',
      message: `Resuming from room ${startIndex + 1}/${totalRooms}`,
      currentRoom: startIndex,
      totalRooms
    });
  }

  for (let i = startIndex; i < rooms.length; i++) {
    const room = rooms[i];
    const roomName = room.breakoutRoomName || room.name || `Room ${i + 1}`;
    const roomUUID = room.breakoutRoomId || room.breakoutRoomUUID || room.breakoutroomid || room.uuid || room.id;

    // Return to main room before each move (except first - already done in Step 3)
    if (i > startIndex) {
      onProgress?.({
        step: 'returning_to_main',
        message: `[${i + 1}/${totalRooms}] Returning to main room...`,
        currentRoom: i + 1,
        totalRooms
      });
      try {
        await moveToMainRoom(botUUID);
        await sleep(2000);
      } catch (err) {
        console.warn(`Could not return to main before room ${i + 1}:`, err.message);
      }
    }

    // --- PHASE 1: Move bot to room ---
    onProgress?.({
      step: 'moving_to_room',
      message: `[${i + 1}/${totalRooms}] Moving to: ${roomName}`,
      currentRoom: i + 1,
      totalRooms,
      roomName,
      roomUUID,
      botUUID
    });

    const moveResult = await moveWithRetry(moveParticipantToRoom, botUUID, roomUUID, roomName);

    if (!moveResult.success) {
      // STOP - can't skip in position-based matching
      errors.push({ roomName, roomUUID, error: `Move failed: ${moveResult.error?.message}` });
      onProgress?.({
        step: 'room_error',
        message: `FAILED to move to ${roomName} - STOPPING`,
        currentRoom: i + 1, totalRooms, roomName,
        error: 'Move failed'
      });
      break;
    }

    // Wait for bot to click Join
    onProgress?.({
      step: 'waiting_join',
      message: `[${i + 1}/${totalRooms}] Waiting for bot to join ${roomName}...`,
      currentRoom: i + 1,
      totalRooms
    });
    await sleep(delayMs);

    // --- PHASE 2: Wait for webhook (BLOCKING) ---
    onProgress?.({
      step: 'waiting_webhook',
      message: `[${i + 1}/${totalRooms}] Waiting for webhook: ${roomName}...`,
      currentRoom: i + 1,
      totalRooms
    });

    const webhookResult = await waitForWebhookConfirmation(roomName, WEBHOOK_TIMEOUT_MS, 1000);

    if (!webhookResult.confirmed) {
      // STOP - can't skip in position-based matching
      errors.push({ roomName, roomUUID, error: 'Webhook timeout - bot may not have joined' });
      onProgress?.({
        step: 'room_error',
        message: `TIMEOUT waiting for webhook: ${roomName} - STOPPING`,
        currentRoom: i + 1, totalRooms, roomName,
        error: 'Webhook timeout'
      });
      break;
    }

    // --- PHASE 3: SDK CROSS-VALIDATION ---
    onProgress?.({
      step: 'verifying',
      message: `[${i + 1}/${totalRooms}] Verifying: ${roomName}...`,
      currentRoom: i + 1,
      totalRooms
    });

    const verifyResult = await verifyBotInRoomWithPolling(getBreakoutRooms, roomName);

    if (verifyResult.mismatch) {
      // STOP - wrong mapping detected
      errors.push({ roomName, roomUUID, error: `Mismatch: bot in "${verifyResult.actualRoom}"` });
      onProgress?.({
        step: 'room_error',
        message: `MISMATCH: Bot in "${verifyResult.actualRoom}", not "${roomName}" - STOPPING`,
        currentRoom: i + 1, totalRooms, roomName,
        error: `Mismatch: bot in "${verifyResult.actualRoom}"`
      });
      break;
    }

    if (!verifyResult.verified) {
      // Bot not found via SDK - webhook was received so likely OK, just log warning
      console.warn(`[Verify] Bot not found in "${roomName}" via SDK - webhook OK, continuing`);
    }

    // --- PHASE 4: Record mapping ---
    const mapping = {
      roomUUID,
      roomName,
      roomIndex: i,
      timestamp: new Date().toISOString(),
      webhookConfirmed: true,
      sdkVerified: verifyResult.verified
    };
    roomMapping.push(mapping);
    onRoomMapped?.(mapping);

    onProgress?.({
      step: 'room_mapped',
      message: `[${i + 1}/${totalRooms}] ${verifyResult.verified ? 'VERIFIED' : 'CONFIRMED'}: ${roomName}`,
      currentRoom: i + 1,
      totalRooms,
      mapping,
      verified: verifyResult.verified,
      webhookConfirmed: true
    });

    // Brief delay before next room
    await sleep(POST_WEBHOOK_DELAY_MS);
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
      ? `Calibration STOPPED at room ${roomMapping.length + 1}/${totalRooms}. ${errors[0]?.error}`
      : `Calibration complete! All ${roomMapping.length} rooms mapped.`,
    totalMapped: roomMapping.length,
    errors: errors.length
  });

  return {
    success: !hasErrors,
    roomMapping,
    totalRooms,
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
