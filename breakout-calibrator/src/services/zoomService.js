/**
 * Zoom Calibration Service
 * Handles the core logic for moving scout bot through breakout rooms
 *
 * ENHANCED with:
 * - Retry logic and room caching
 * - SDK-based VERIFICATION after each move (queries getBreakoutRoomList to confirm location)
 * - Automatic retry if Scout Bot ends up in wrong room
 */

import { waitForWebhookConfirmation } from './apiService';

const BOT_NAME = process.env.REACT_APP_BOT_NAME || 'Scout Bot';
const BOT_EMAIL = process.env.REACT_APP_BOT_EMAIL || '';
const MOVE_DELAY_MS = 3000; // 3 seconds initial wait for Scout Bot to click Join
const WEBHOOK_TIMEOUT_MS = 20000; // 20 seconds max wait for webhook confirmation
const MAX_RETRIES = 1; // Max retries for SDK move call (2 attempts total)
const MAX_VERIFY_RETRIES = 2; // Max retries if verification fails (2 attempts total)
const VERIFY_POLL_TIMEOUT_MS = 10000; // Max time to poll for location verification
const VERIFY_POLL_INTERVAL_MS = 2000; // Poll interval for verification

// Room mapping cache (persists across calibration runs)
const roomCache = new Map();

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
 * Shared bot name matching logic - used by both findScoutBot and verifyBotLocation
 * Returns true if the participant name matches the bot name
 */
export function isBotNameMatch(participantName, botName = BOT_NAME) {
  const pName = participantName.toLowerCase();
  const normalizedBotName = botName.toLowerCase();

  // Exact match
  const isExactMatch = pName === normalizedBotName;
  // Contains the configured bot name (e.g., "My Scout Bot" matches "Scout Bot")
  const containsBotName = pName.includes(normalizedBotName);
  // Scout bot patterns
  const isScoutBot = pName.includes('scout bot') || pName.includes('scoutbot');
  const isScoutPattern = pName.startsWith('scout') && pName.includes('bot');

  return isExactMatch || containsBotName || isScoutBot || isScoutPattern;
}

/**
 * Find the scout bot in the participant list
 * Tries multiple matching strategies
 */
export function findScoutBot(participants, botName = BOT_NAME, botEmail = BOT_EMAIL) {
  const normalizedBotEmail = botEmail.toLowerCase();

  // Debug: Log all participants with all their fields
  console.log('Looking for bot:', botName);
  console.log('Raw participants:', JSON.stringify(participants, null, 2));
  console.log('Participant names:', participants.map(p => getParticipantName(p)));

  // Strategy 1: Exact email match (most reliable)
  if (normalizedBotEmail) {
    const byEmail = participants.find(p => {
      const email = getParticipantEmail(p).toLowerCase();
      return email === normalizedBotEmail;
    });
    if (byEmail) {
      console.log('Found scout bot by email match');
      return byEmail;
    }
  }

  // Strategy 2: Name match using shared isBotNameMatch function
  const byName = participants.find(p => isBotNameMatch(getParticipantName(p), botName));
  if (byName) {
    console.log('Found scout bot by name match');
    return byName;
  }

  console.log('Scout bot not found in', participants.length, 'participants');
  return null;
}

/**
 * Sleep utility for delays between room moves
 */
export function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * Get cached room name (if previously mapped)
 */
export function getCachedRoomName(roomUUID) {
  return roomCache.get(roomUUID);
}

/**
 * Cache a room mapping
 */
export function cacheRoomMapping(roomUUID, roomName) {
  roomCache.set(roomUUID, roomName);
}

/**
 * Get all cached mappings
 */
export function getAllCachedMappings() {
  return Array.from(roomCache.entries()).map(([uuid, name]) => ({
    roomUUID: uuid,
    roomName: name
  }));
}

/**
 * Clear the room cache
 */
export function clearRoomCache() {
  roomCache.clear();
}

/**
 * Verify Scout Bot's location by querying breakout rooms
 * Returns the room name where Scout Bot is currently located
 *
 * @param {Function} getBreakoutRooms - SDK method to get rooms with participants
 * @param {string} botName - Name of the bot to find
 * @returns {object} { found: boolean, roomName: string, roomUUID: string }
 */
export async function verifyBotLocation(getBreakoutRooms, botName = BOT_NAME) {
  try {
    const rooms = await getBreakoutRooms();

    console.log(`[Verify] Checking ${rooms.length} rooms for ${botName}`);

    for (const room of rooms) {
      const roomName = room.breakoutRoomName || room.name || 'Unknown';
      const roomUUID = room.breakoutRoomId || room.breakoutRoomUUID || room.uuid || room.id;

      // Handle different SDK response structures for participants
      const participants = room.participants || room.members || room.attendees || [];

      // Log first room's participant structure for debugging
      if (participants.length > 0) {
        console.log(`[Verify] Room "${roomName}" has ${participants.length} participants`);
      }

      // Check if bot is in this room using shared matching logic
      for (const participant of participants) {
        const pName = getParticipantName(participant);

        // Use shared isBotNameMatch function for consistent matching
        if (isBotNameMatch(pName, botName)) {
          console.log(`[Verify] Found ${botName} in room: ${roomName} (matched: ${pName})`);
          return { found: true, roomName, roomUUID, participant };
        }
      }
    }

    console.log(`[Verify] ${botName} not found in any breakout room (may be in main room or SDK not updated yet)`);
    return { found: false, roomName: null, roomUUID: null };
  } catch (err) {
    console.error('[Verify] Failed to verify bot location:', err);
    return { found: false, roomName: null, roomUUID: null, error: err.message };
  }
}

/**
 * Verify bot location with polling (retry until found or timeout)
 * This handles the race condition where SDK may not immediately show the new location
 *
 * @param {Function} getBreakoutRooms - SDK method
 * @param {string} expectedRoomName - Room we expect bot to be in
 * @param {number} timeoutMs - Max time to wait (default 10 seconds)
 * @param {number} pollIntervalMs - Poll interval (default 2 seconds)
 */
export async function verifyBotLocationWithPolling(getBreakoutRooms, expectedRoomName, timeoutMs = 10000, pollIntervalMs = 2000) {
  const startTime = Date.now();

  while (Date.now() - startTime < timeoutMs) {
    const location = await verifyBotLocation(getBreakoutRooms);

    if (location.found) {
      if (location.roomName === expectedRoomName) {
        console.log(`[Verify] Confirmed: Bot is in expected room "${expectedRoomName}"`);
        return { verified: true, roomName: location.roomName, roomUUID: location.roomUUID };
      } else {
        // Found in wrong room - don't keep polling, return mismatch
        console.log(`[Verify] Mismatch: Expected "${expectedRoomName}", found in "${location.roomName}"`);
        return { verified: false, mismatch: true, expectedRoom: expectedRoomName, actualRoom: location.roomName };
      }
    }

    // Not found yet, wait and try again
    console.log(`[Verify] Bot not found yet, retrying in ${pollIntervalMs}ms...`);
    await sleep(pollIntervalMs);
  }

  console.log(`[Verify] Timeout: Bot not found in any room after ${timeoutMs}ms`);
  return { verified: false, timeout: true, expectedRoom: expectedRoomName };
}

/**
 * Move participant with retry logic
 */
async function moveWithRetry(moveParticipantToRoom, botUUID, roomUUID, roomName, maxRetries = MAX_RETRIES) {
  let lastError;

  for (let attempt = 1; attempt <= maxRetries + 1; attempt++) {
    try {
      const response = await moveParticipantToRoom(botUUID, roomUUID);
      console.log(`>>> MOVE RESPONSE for ${roomName}:`, JSON.stringify(response));
      return { success: true, attempts: attempt, response, sdkResponse: response };
    } catch (err) {
      lastError = err;
      console.warn(`Move attempt ${attempt} failed for ${roomName}:`, err.message);

      if (attempt <= maxRetries) {
        // Wait before retry (exponential backoff)
        await sleep(1000 * attempt);
      }
    }
  }

  return { success: false, error: lastError, attempts: maxRetries + 1 };
}

/**
 * Run the calibration sequence
 * Moves scout bot through all breakout rooms and builds the mapping
 *
 * @param {Object} options
 * @param {Function} options.getBreakoutRooms - SDK method to get rooms
 * @param {Function} options.getParticipants - SDK method to get participants
 * @param {Function} options.moveParticipantToRoom - SDK method to move participant
 * @param {Function} options.moveToMainRoom - SDK method to return to main room
 * @param {Function} options.onProgress - Callback for progress updates
 * @param {Function} options.onRoomMapped - Callback when a room is mapped
 * @param {number} options.delayMs - Delay between room moves (default 3000)
 * @param {boolean} options.useCache - Use cached mappings if available (default true)
 */
export async function runCalibration({
  getBreakoutRooms,
  getParticipants,
  moveParticipantToRoom,
  moveToMainRoom,
  onProgress,
  onRoomMapped,
  delayMs = MOVE_DELAY_MS,
  useCache = true
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

  // Check cache for already-mapped rooms
  let cachedCount = 0;
  if (useCache) {
    for (const room of rooms) {
      // Use breakoutRoomId WITH curly braces (don't strip them!)
      const roomUUID = room.breakoutRoomId || room.breakoutRoomUUID || room.breakoutroomid || room.uuid || room.id;
      const cachedName = getCachedRoomName(roomUUID);
      if (cachedName) {
        cachedCount++;
      }
    }
    if (cachedCount > 0) {
      onProgress?.({
        step: 'cache_hit',
        message: `${cachedCount} rooms already cached from previous calibration`
      });
    }
  }

  // Step 2: Find the scout bot
  onProgress?.({ step: 'finding_bot', message: 'Looking for scout bot...' });
  const participants = await getParticipants();

  // Show participants found for debugging - check all possible name fields
  const getParticipantName = (p) => p.screenName || p.displayName || p.participantName || p.name || p.userName || p.user_name || 'NoName';
  const participantNames = participants.map(p => getParticipantName(p)).join(', ');
  const participantKeys = participants.length > 0 ? Object.keys(participants[0]).join(', ') : 'none';

  onProgress?.({ step: 'participants_found', message: `Found ${participants.length}: ${participantNames} [Keys: ${participantKeys}]` });

  const scoutBot = findScoutBot(participants);

  if (!scoutBot) {
    throw new Error(`Bot "${BOT_NAME}" not found. Found: ${participantNames}. Keys: ${participantKeys}`);
  }

  // SDK expects participantUUID - prefer that field
  const botUUID = scoutBot.participantUUID || scoutBot.uuid || scoutBot.participantId || scoutBot.id;
  const botName = scoutBot.name || scoutBot.participantName || scoutBot.screenName || scoutBot.displayName || scoutBot.userName;

  console.log('Bot participant object:', JSON.stringify(scoutBot));
  console.log('Using botUUID:', botUUID);

  // DEBUG: Log full scout bot object to see all available fields
  console.log('=== SCOUT BOT FULL OBJECT ===');
  console.log(JSON.stringify(scoutBot, null, 2));
  console.log('Extracted botUUID:', botUUID);
  console.log('=============================');

  onProgress?.({
    step: 'bot_found',
    message: `Found scout bot: ${botName} (UUID: ${botUUID})`,
    botId: botUUID
  });

  // Step 3: Move bot through each room
  for (let i = 0; i < rooms.length; i++) {
    const room = rooms[i];
    const roomName = room.breakoutRoomName || room.name || `Room ${i + 1}`;
    // Use breakoutRoomId WITH curly braces (don't strip them!)
    const roomUUID = room.breakoutRoomId || room.breakoutRoomUUID || room.breakoutroomid || room.uuid || room.id;

    // Check cache first
    if (useCache && getCachedRoomName(roomUUID)) {
      const mapping = {
        roomUUID,
        roomName,
        roomIndex: i,
        timestamp: new Date().toISOString(),
        fromCache: true
      };
      roomMapping.push(mapping);
      onRoomMapped?.(mapping);

      onProgress?.({
        step: 'room_cached',
        message: `Cached: ${roomName}`,
        currentRoom: i + 1,
        totalRooms: rooms.length
      });
      continue;
    }

    // DEBUG: Log room object for first room
    if (i === 0) {
      console.log('=== FIRST ROOM FULL OBJECT ===');
      console.log(JSON.stringify(room, null, 2));
      console.log('Extracted roomUUID:', roomUUID);
      console.log('==============================');
    }

    // CRITICAL: Await onProgress to ensure mapping is sent to backend BEFORE moving
    // This allows the backend to know which room Scout Bot is about to enter
    const progressResult = onProgress?.({
      step: 'moving_to_room',
      message: `Moving to room ${i + 1}/${rooms.length}: ${roomName}`,
      currentRoom: i + 1,
      totalRooms: rooms.length,
      roomName,
      roomUUID: roomUUID,
      botUUID: botUUID
    });

    // Wait for async onProgress (mapping notification) to complete
    if (progressResult && typeof progressResult.then === 'function') {
      await progressResult;
    }

    console.log(`>>> MOVE CALL: botUUID=${botUUID}, roomUUID=${roomUUID}`);

    // Move bot to this room with retry and VERIFICATION
    let verified = false;
    let verifyAttempts = 0;
    let webhookResult = { confirmed: false };

    while (!verified && verifyAttempts < MAX_VERIFY_RETRIES) {
      verifyAttempts++;

      // If this is a retry (not first attempt), move bot back to main room first
      if (verifyAttempts > 1) {
        console.log(`[Calibration] Retry ${verifyAttempts}: Moving bot back to main room first...`);
        try {
          await moveToMainRoom(botUUID);
          await sleep(2000); // Wait for bot to return to main room
        } catch (err) {
          console.warn(`[Calibration] Failed to return to main room before retry:`, err.message);
        }
      }

      // Move bot to room (moveWithRetry handles SDK-level retries)
      const moveResult = await moveWithRetry(moveParticipantToRoom, botUUID, roomUUID, roomName);

      if (!moveResult.success) {
        console.error(`Move failed for ${roomName} after SDK retries:`, moveResult.error);
        // Don't continue immediately - the outer loop will retry after moving to main room
        continue;
      }

      // Wait for Scout Bot to click Join
      await sleep(delayMs);

      // VERIFICATION: Query SDK with polling to confirm Scout Bot is in the correct room
      onProgress?.({
        step: 'verifying_location',
        message: `Verifying location: ${roomName} (attempt ${verifyAttempts}/${MAX_VERIFY_RETRIES})...`,
        currentRoom: i + 1,
        totalRooms: rooms.length
      });

      // Use polling-based verification (handles SDK update delay)
      const verifyResult = await verifyBotLocationWithPolling(getBreakoutRooms, roomName, VERIFY_POLL_TIMEOUT_MS, VERIFY_POLL_INTERVAL_MS);

      if (verifyResult.verified) {
        console.log(`[Calibration] VERIFIED: Scout Bot is in ${roomName}`);
        verified = true;

        // Also wait for webhook (but we're already verified via SDK)
        webhookResult = await waitForWebhookConfirmation(roomName, WEBHOOK_TIMEOUT_MS, 1000);
        if (webhookResult.confirmed) {
          console.log(`Webhook also confirmed for ${roomName}`);
        }
      } else if (verifyResult.mismatch) {
        console.warn(`[Calibration] MISMATCH: Expected ${roomName}, but Scout Bot is in ${verifyResult.actualRoom}`);
        onProgress?.({
          step: 'verification_mismatch',
          message: `Mismatch: Expected ${roomName}, found in ${verifyResult.actualRoom}. Moving to main room and retrying...`,
          currentRoom: i + 1,
          totalRooms: rooms.length
        });
        // Will retry - outer loop will move to main room first
      } else {
        console.warn(`[Calibration] Scout Bot not found in any breakout room after polling`);
        onProgress?.({
          step: 'verification_failed',
          message: `Bot not found in breakout rooms. Retrying (${verifyAttempts}/${MAX_VERIFY_RETRIES})...`,
          currentRoom: i + 1,
          totalRooms: rooms.length
        });
      }
    }

    if (verified) {
      // Record the mapping
      const mapping = {
        roomUUID,
        roomName,
        roomIndex: i,
        timestamp: new Date().toISOString(),
        attempts: verifyAttempts,
        verified: true,
        webhookConfirmed: webhookResult.confirmed
      };

      roomMapping.push(mapping);
      cacheRoomMapping(roomUUID, roomName); // Add to cache
      onRoomMapped?.(mapping);

      onProgress?.({
        step: 'room_mapped',
        message: `Verified & Mapped: ${roomName} ✓`,
        currentRoom: i + 1,
        totalRooms: rooms.length,
        mapping,
        verified: true,
        webhookConfirmed: webhookResult.confirmed
      });
    } else {
      console.error(`Failed to verify room ${roomName} after ${MAX_VERIFY_RETRIES} attempts`);
      errors.push({
        roomName,
        roomUUID,
        error: `Verification failed after ${MAX_VERIFY_RETRIES} attempts`
      });

      onProgress?.({
        step: 'room_error',
        message: `Failed to verify ${roomName} after ${MAX_VERIFY_RETRIES} attempts`,
        error: 'Verification failed'
      });
      // Continue with next room
    }
  }

  // Step 4: Return bot to main room
  onProgress?.({ step: 'returning', message: 'Returning scout bot to main room...' });

  try {
    await moveToMainRoom(botUUID);
    await sleep(1000);
  } catch (err) {
    console.warn('Failed to return bot to main room:', err);
    // Not critical, continue
  }

  const successCount = roomMapping.filter(m => !m.fromCache).length;
  const cachedUsed = roomMapping.filter(m => m.fromCache).length;

  onProgress?.({
    step: 'complete',
    message: `Calibration complete! Mapped ${successCount} rooms${cachedUsed > 0 ? ` (${cachedUsed} from cache)` : ''}.`,
    totalMapped: roomMapping.length,
    errors: errors.length
  });

  return {
    success: errors.length === 0,
    roomMapping,
    totalRooms: rooms.length,
    mappedRooms: roomMapping.length,
    newlyMapped: successCount,
    fromCache: cachedUsed,
    errors
  };
}

export default {
  findScoutBot,
  runCalibration,
  sleep,
  getCachedRoomName,
  cacheRoomMapping,
  getAllCachedMappings,
  clearRoomCache,
  verifyBotLocation,
  verifyBotLocationWithPolling
};
