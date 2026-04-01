import React, { useState, useCallback, useEffect } from 'react';
import useZoomSdk from '../hooks/useZoomSdk';
import { runCalibration } from '../services/zoomService';
import {
  notifyCalibrationStart,
  notifyCalibrationComplete,
  abortCalibration,
  waitForWebhookConfirmation,
  resetCalibration,
  getLiveRooms,
  getMappingSummary,
  prepareRoomRecalibration,
  completeRoomRecalibration
} from '../services/apiService';
import StatusMessage from './StatusMessage';
import ProgressIndicator from './ProgressIndicator';
import RoomList from './RoomList';

const UI_STATES = {
  IDLE: 'idle',
  CHECKING: 'checking',
  CALIBRATING: 'calibrating',
  COMPLETE: 'complete',
  ERROR: 'error',
  RECALIBRATING: 'recalibrating'
};

const DELAY_OPTIONS = [
  { label: 'Fast (10s)', value: 10000, desc: 'Quick but may miss webhooks' },
  { label: 'Normal (30s)', value: 30000, desc: 'Recommended for most meetings' },
  { label: 'Slow (60s)', value: 60000, desc: 'Best accuracy' },
  { label: 'Very Slow (90s)', value: 90000, desc: 'Maximum reliability' }
];

function CalibrationPanel() {
  const {
    isConfigured,
    error: sdkError,
    meetingContext,
    isHost,
    getBreakoutRooms,
    getParticipants,
    moveParticipantToRoom,
    moveToMainRoom,
    getMeetingUUID
  } = useZoomSdk();

  // UI State
  const [uiState, setUiState] = useState(UI_STATES.IDLE);
  const [statusMessage, setStatusMessage] = useState('');
  const [rooms, setRooms] = useState([]);
  const [mappedRooms, setMappedRooms] = useState([]);
  const [currentRoom, setCurrentRoom] = useState(-1);
  const [totalRooms, setTotalRooms] = useState(0);
  const [errorMessage, setErrorMessage] = useState('');
  const [debugLogs, setDebugLogs] = useState([]);

  // Failure tracking
  const [failedRoomIndex, setFailedRoomIndex] = useState(-1);
  const [failedRoomName, setFailedRoomName] = useState('');
  const [failedReason, setFailedReason] = useState('');

  // Settings
  const [selectedDelay, setSelectedDelay] = useState(30000);

  // Live View & Summary
  const [liveRooms, setLiveRooms] = useState([]);
  const [mappingSummary, setMappingSummary] = useState(null);
  const [showLiveView, setShowLiveView] = useState(false);

  // Recalibration
  const [recalibratingRoom, setRecalibratingRoom] = useState(null);

  // Resume support
  const [resumeFromRoom, setResumeFromRoom] = useState(null);

  // ETA
  const [estimatedTime, setEstimatedTime] = useState(null);

  // Calculate ETA when settings change
  useEffect(() => {
    if (rooms.length > 0) {
      const totalMs = rooms.length * (selectedDelay + 8000);
      const mins = Math.ceil(totalMs / 60000);
      setEstimatedTime(`~${mins} min for ${rooms.length} rooms`);
    }
  }, [rooms.length, selectedDelay]);

  // Check for existing calibration progress on mount
  useEffect(() => {
    const checkExistingProgress = async () => {
      const meetingId = meetingContext?.meetingID;
      if (!meetingId) return;
      try {
        const response = await fetch(`/calibration/status?meeting_id=${meetingId}`);
        const data = await response.json();
        if (data.calibration_in_progress && data.current_room_index > 0) {
          setResumeFromRoom(data.current_room_index);
          setTotalRooms(data.total_rooms || 66);
        }
      } catch (err) {
        console.error('Failed to check calibration progress:', err);
      }
    };
    checkExistingProgress();
  }, [meetingContext]);

  // Fetch live rooms
  const refreshLiveRooms = useCallback(async () => {
    const meetingId = meetingContext?.meetingID;
    if (!meetingId) return;
    try {
      const data = await getLiveRooms(meetingId);
      if (data.success) setLiveRooms(data.rooms || []);
    } catch (err) {
      console.error('Failed to fetch live rooms:', err);
    }
  }, [meetingContext]);

  // Fetch mapping summary
  const refreshMappingSummary = useCallback(async () => {
    const meetingId = meetingContext?.meetingID;
    if (!meetingId) return;
    try {
      const data = await getMappingSummary(meetingId);
      if (data.success) setMappingSummary(data);
    } catch (err) {
      console.error('Failed to fetch mapping summary:', err);
    }
  }, [meetingContext]);

  // Clear failure state
  const clearFailure = useCallback(() => {
    setFailedRoomIndex(-1);
    setFailedRoomName('');
    setFailedReason('');
  }, []);

  // Main calibration
  const handleStartCalibration = useCallback(async (startFromRoom = 0) => {
    if (!isConfigured || !isHost) {
      setErrorMessage(!isConfigured ? 'Zoom SDK not configured' : 'Only hosts can run calibration');
      setUiState(UI_STATES.ERROR);
      return;
    }

    try {
      setUiState(UI_STATES.CHECKING);
      setStatusMessage(startFromRoom > 0 ? `Resuming from room ${startFromRoom + 1}...` : 'Initializing...');
      setMappedRooms([]);
      setCurrentRoom(startFromRoom > 0 ? startFromRoom - 1 : -1);
      setErrorMessage('');
      setDebugLogs([]);
      clearFailure();
      setMappingSummary(null);

      const meetingUUID = await getMeetingUUID();
      const meetingId = meetingContext?.meetingID;

      const rawRooms = await getBreakoutRooms();
      // Sort by prefix (1.1, 1.2, ..., 2.0, 3.1, ...) - SAME order as zoomService.js
      const breakoutRooms = [...rawRooms].sort((a, b) => {
        const nameA = a.breakoutRoomName || a.name || '';
        const nameB = b.breakoutRoomName || b.name || '';
        const matchA = nameA.match(/^(\d+)\.(\d+)/);
        const matchB = nameB.match(/^(\d+)\.(\d+)/);
        if (matchA && matchB) {
          const diff = parseInt(matchA[1], 10) - parseInt(matchB[1], 10);
          if (diff !== 0) return diff;
          return parseInt(matchA[2], 10) - parseInt(matchB[2], 10);
        }
        if (matchA && !matchB) return -1;
        if (!matchA && matchB) return 1;
        return nameA.localeCompare(nameB);
      });
      setRooms(breakoutRooms);
      setTotalRooms(breakoutRooms.length);

      const remainingRooms = breakoutRooms.length - startFromRoom;
      const etaMins = Math.ceil((remainingRooms * (selectedDelay + 8000)) / 60000);
      setDebugLogs([
        `=== CALIBRATION ${startFromRoom > 0 ? 'RESUMING' : 'STARTING'} ===`,
        `Total Rooms: ${breakoutRooms.length}`,
        startFromRoom > 0 ? `Resuming from: Room ${startFromRoom + 1}` : '',
        `Remaining: ${remainingRooms} rooms`,
        `Delay: ${selectedDelay / 1000}s per room`,
        `ETA: ~${etaMins} minutes`
      ].filter(Boolean));

      await notifyCalibrationStart(meetingId, meetingUUID, {
        mode: 'scout_bot',
        name: 'Scout Bot',
        participantUUID: ''
      }, breakoutRooms);

      setUiState(UI_STATES.CALIBRATING);

      const result = await runCalibration({
        getBreakoutRooms,
        getParticipants,
        moveParticipantToRoom,
        moveToMainRoom,
        delayMs: selectedDelay,
        startFromRoom: startFromRoom,
        onProgress: (progress) => {
          setStatusMessage(progress.message);
          if (progress.currentRoom !== undefined) {
            setCurrentRoom(progress.currentRoom - 1);
          }

          if (progress.step === 'bot_found') {
            setDebugLogs(prev => [...prev, `BOT: ${progress.message}`]);
          }

          if (progress.step === 'moving_to_room') {
            setDebugLogs(prev => [...prev, `[${progress.currentRoom}/${progress.totalRooms}] ${progress.roomName}`]);
          }

          if (progress.step === 'waiting_webhook') {
            setDebugLogs(prev => [...prev, `  Webhook...`]);
          }

          if (progress.step === 'verifying') {
            setDebugLogs(prev => [...prev, `  SDK verify...`]);
          }

          if (progress.step === 'verify_warning') {
            setDebugLogs(prev => [...prev, `  SDK: not confirmed (webhook OK)`]);
          }

          if (progress.step === 'room_mapped') {
            const tag = progress.verified ? 'VERIFIED' : 'OK (webhook only)';
            setDebugLogs(prev => [...prev, `  ${tag}: ${progress.mapping.roomName}`]);
          }

          if (progress.step === 'room_error') {
            const roomIdx = (progress.currentRoom || 1) - 1;
            setFailedRoomIndex(roomIdx);
            setFailedRoomName(progress.roomName || `Room ${roomIdx + 1}`);
            setFailedReason(progress.error || 'Unknown error');
            setDebugLogs(prev => [...prev, `  FAILED: ${progress.message}`]);
          }
        },
        onRoomMapped: (mapping) => {
          setMappedRooms(prev => [...prev, mapping]);
        }
      });

      if (result.success) {
        await notifyCalibrationComplete(meetingId, meetingUUID, result);
        setDebugLogs(prev => [...prev, `=== COMPLETE: ${result.mappedRooms}/${result.totalRooms} ===`]);
        setUiState(UI_STATES.COMPLETE);
        setStatusMessage(`All ${result.mappedRooms} rooms mapped successfully`);
        setTimeout(() => refreshMappingSummary(), 1000);
      } else {
        // Calibration stopped midway - abort and clean up partial mappings
        const failedAt = result.mappedRooms;
        const failedError = result.errors[0]?.error || 'Unknown error';
        const failedName = result.errors[0]?.roomName || `Room ${failedAt + 1}`;

        setDebugLogs(prev => [...prev,
          `=== FAILED at room ${failedAt + 1}/${result.totalRooms} ===`,
          `Room: ${failedName}`,
          `Error: ${failedError}`,
          `Cleaning up ${failedAt} partial mappings...`
        ]);

        await abortCalibration(meetingId);

        setDebugLogs(prev => [...prev, `All partial mappings deleted. Ready to retry.`]);
        setErrorMessage(`Calibration stopped at room ${failedAt + 1}: ${failedError}`);
        setFailedRoomIndex(failedAt);
        setFailedRoomName(failedName);
        setFailedReason(failedError);
        setUiState(UI_STATES.ERROR);
      }
      setCurrentRoom(-1);

    } catch (err) {
      console.error('Calibration failed:', err);
      const meetingId2 = meetingContext?.meetingID;
      if (meetingId2) await abortCalibration(meetingId2);
      setErrorMessage(err.message || 'Calibration failed');
      setDebugLogs(prev => [...prev, `ERROR: ${err.message}`, `Partial mappings cleaned up`]);
      setUiState(UI_STATES.ERROR);
      setCurrentRoom(-1);
    }
  }, [isConfigured, isHost, meetingContext, getMeetingUUID, getBreakoutRooms, getParticipants, moveParticipantToRoom, moveToMainRoom, selectedDelay, refreshMappingSummary, clearFailure]);

  // Full reset (abort + clear all BigQuery mappings)
  const handleFullReset = useCallback(async () => {
    const meetingId = meetingContext?.meetingID;
    try {
      setStatusMessage('Resetting...');
      if (meetingId) {
        await abortCalibration(meetingId);
        await resetCalibration(meetingId, true);
      }
      setUiState(UI_STATES.IDLE);
      setStatusMessage('');
      setRooms([]);
      setMappedRooms([]);
      setCurrentRoom(-1);
      setTotalRooms(0);
      setErrorMessage('');
      setDebugLogs(['=== RESET COMPLETE ===']);
      clearFailure();
      setLiveRooms([]);
      setMappingSummary(null);
    } catch (err) {
      setErrorMessage(`Reset failed: ${err.message}`);
    }
  }, [meetingContext, clearFailure]);

  // Recalibrate specific room
  const handleRecalibrateRoom = useCallback(async (room) => {
    const meetingId = meetingContext?.meetingID;
    if (!meetingId) return;

    const roomName = room.roomName || room.expected_name;
    try {
      setRecalibratingRoom(room);
      setUiState(UI_STATES.RECALIBRATING);
      setStatusMessage(`Preparing: ${roomName}`);
      setDebugLogs(prev => [...prev, `=== RECALIBRATING: ${roomName} ===`]);

      const prepResult = await prepareRoomRecalibration(meetingId, roomName, room.roomUUID);
      if (prepResult.success) {
        setStatusMessage(`Move Scout Bot to "${roomName}" NOW`);
        setDebugLogs(prev => [...prev, `Waiting for Scout Bot to join...`]);

        const webhookResult = await waitForWebhookConfirmation(roomName, 120000, 2000);
        if (webhookResult.confirmed) {
          const completeResult = await completeRoomRecalibration(meetingId, roomName);
          if (completeResult.success) {
            setDebugLogs(prev => [...prev, `RECALIBRATION SUCCESS: ${roomName}`]);
            setStatusMessage(`"${roomName}" recalibrated!`);
            // Remove from failed state if it was the failed room
            if (failedRoomName === roomName) clearFailure();
            await refreshMappingSummary();
          } else {
            setDebugLogs(prev => [...prev, `FAILED: ${completeResult.error}`]);
            setStatusMessage(`Failed: ${completeResult.error}`);
          }
        } else {
          setDebugLogs(prev => [...prev, `TIMEOUT: No webhook received for ${roomName}`]);
          setStatusMessage(`Timeout - Scout Bot didn't join "${roomName}"`);
        }
      }
    } catch (err) {
      setDebugLogs(prev => [...prev, `ERROR: ${err.message}`]);
      setStatusMessage(`Error: ${err.message}`);
    } finally {
      setRecalibratingRoom(null);
      setUiState(mappedRooms.length > 0 ? UI_STATES.COMPLETE : UI_STATES.IDLE);
    }
  }, [meetingContext, refreshMappingSummary, mappedRooms.length, failedRoomName, clearFailure]);

  // Simple reset (UI only)
  const handleReset = useCallback(() => {
    setUiState(UI_STATES.IDLE);
    setStatusMessage('');
    setRooms([]);
    setMappedRooms([]);
    setCurrentRoom(-1);
    setTotalRooms(0);
    setErrorMessage('');
    clearFailure();
    setDebugLogs([]);
    setMappingSummary(null);
    setLiveRooms([]);
  }, [clearFailure]);

  // Render: SDK not ready
  if (!isConfigured) {
    return (
      <div style={styles.container}>
        <h2 style={styles.title}>Breakout Room Calibrator</h2>
        <StatusMessage status="checking" message={sdkError || "Connecting to Zoom..."} />
      </div>
    );
  }

  // Render: Not a host
  if (!isHost) {
    return (
      <div style={styles.container}>
        <h2 style={styles.title}>Breakout Room Calibrator</h2>
        <StatusMessage status="error" message="Only hosts or co-hosts can run calibration" />
      </div>
    );
  }

  return (
    <div style={styles.container}>
      {/* Header */}
      <div style={styles.header}>
        <h2 style={styles.title}>Breakout Room Calibrator</h2>
        {meetingContext && <span style={styles.meetingId}>Meeting: {meetingContext.meetingID}</span>}
      </div>

      {/* Settings Panel */}
      {uiState === UI_STATES.IDLE && (
        <div style={styles.settingsPanel}>
          <div style={styles.settingRow}>
            <label style={styles.label}>Delay per room:</label>
            <select style={styles.select} value={selectedDelay} onChange={(e) => setSelectedDelay(Number(e.target.value))}>
              {DELAY_OPTIONS.map(opt => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>
          <p style={styles.hint}>
            {DELAY_OPTIONS.find(o => o.value === selectedDelay)?.desc}
          </p>
          {estimatedTime && <p style={styles.eta}>{estimatedTime}</p>}
        </div>
      )}

      {/* Status */}
      <StatusMessage status={uiState} message={errorMessage || statusMessage} />

      {/* Progress */}
      {uiState === UI_STATES.CALIBRATING && totalRooms > 0 && (
        <ProgressIndicator current={mappedRooms.length} total={totalRooms} showSpinner={true} />
      )}

      {/* ============================================================ */}
      {/* FAILURE PANEL - Prominent error display when calibration fails */}
      {/* ============================================================ */}
      {uiState === UI_STATES.ERROR && failedRoomIndex >= 0 && (
        <div style={styles.failurePanel}>
          <div style={styles.failureHeader}>
            <span style={styles.failureIcon}>!</span>
            <span style={styles.failureTitle}>Calibration Failed</span>
          </div>

          <div style={styles.failureDetails}>
            <div style={styles.failureRow}>
              <span style={styles.failureLabel}>Stopped at:</span>
              <span style={styles.failureValue}>Room {failedRoomIndex + 1} of {totalRooms}</span>
            </div>
            <div style={styles.failureRow}>
              <span style={styles.failureLabel}>Room name:</span>
              <span style={styles.failureValue}>{failedRoomName}</span>
            </div>
            <div style={styles.failureRow}>
              <span style={styles.failureLabel}>Error:</span>
              <span style={styles.failureValueError}>{failedReason}</span>
            </div>
            <div style={styles.failureRow}>
              <span style={styles.failureLabel}>Mapped before failure:</span>
              <span style={styles.failureValue}>{mappedRooms.length} rooms (deleted)</span>
            </div>
          </div>

          <p style={styles.failureNote}>
            All partial mappings have been cleaned up. You can retry calibration from the beginning.
          </p>

          <div style={styles.failureActions}>
            <button
              style={styles.retryButton}
              onClick={() => handleStartCalibration(0)}
            >
              Retry Calibration
            </button>
            <button
              style={styles.secondaryButton}
              onClick={handleFullReset}
            >
              Full Reset
            </button>
          </div>
        </div>
      )}

      {/* Room List - visible during calibration AND on error */}
      {rooms.length > 0 && (uiState === UI_STATES.CALIBRATING || uiState === UI_STATES.ERROR) && (
        <div style={styles.section}>
          <h3 style={styles.sectionTitle}>
            {uiState === UI_STATES.ERROR ? 'Room Status (failed)' : 'Progress'}
          </h3>
          <RoomList
            rooms={rooms}
            mappedRooms={mappedRooms}
            currentRoom={currentRoom}
            failedRoom={failedRoomIndex}
          />
        </div>
      )}

      {/* Action Buttons */}
      <div style={styles.actions}>
        {uiState === UI_STATES.IDLE && (
          <>
            {resumeFromRoom > 0 && (
              <button style={styles.primaryButton} onClick={() => handleStartCalibration(resumeFromRoom)}>
                Resume from Room {resumeFromRoom + 1}/{totalRooms}
              </button>
            )}
            <button style={resumeFromRoom > 0 ? styles.secondaryButton : styles.primaryButton} onClick={() => handleStartCalibration(0)}>
              {resumeFromRoom > 0 ? 'Start Fresh' : 'Start Calibration'}
            </button>
          </>
        )}

        {uiState === UI_STATES.CALIBRATING && (
          <>
            <button style={styles.disabledButton} disabled>
              Calibrating... {mappedRooms.length}/{totalRooms}
            </button>
            <button style={styles.dangerButton} onClick={handleFullReset}>
              Cancel
            </button>
          </>
        )}

        {uiState === UI_STATES.RECALIBRATING && (
          <button style={styles.disabledButton} disabled>
            Recalibrating: {recalibratingRoom?.roomName || recalibratingRoom?.expected_name}...
          </button>
        )}

        {/* Error buttons (only when no failure panel, e.g. initialization errors) */}
        {uiState === UI_STATES.ERROR && failedRoomIndex < 0 && (
          <>
            <button style={styles.primaryButton} onClick={() => handleStartCalibration(0)}>Try Again</button>
            <button style={styles.dangerButton} onClick={handleFullReset}>Full Reset</button>
          </>
        )}

        {uiState === UI_STATES.COMPLETE && (
          <>
            <button style={styles.primaryButton} onClick={() => handleStartCalibration(0)}>Run Again</button>
            <button style={styles.secondaryButton} onClick={handleReset}>Reset</button>
            <button style={styles.dangerButton} onClick={handleFullReset}>Full Reset</button>
          </>
        )}
      </div>

      {/* Mapping Summary (after successful calibration) */}
      {uiState === UI_STATES.COMPLETE && (
        <div style={styles.section}>
          <div style={styles.sectionHeader}>
            <h3 style={styles.sectionTitle}>MAPPING SUMMARY</h3>
            <button style={styles.smallButton} onClick={refreshMappingSummary}>Refresh</button>
          </div>

          {mappingSummary && (
            <div style={styles.summaryBox}>
              <p style={styles.summaryText}>
                <span style={styles.green}>{mappingSummary.mapped_count} mapped</span>
                {' / '}
                <span style={mappingSummary.missing_count > 0 ? styles.red : styles.gray}>
                  {mappingSummary.missing_count} missing
                </span>
                {' / '}{mappingSummary.total_expected} total
              </p>

              {mappingSummary.rooms?.filter(r => !r.mapped).length > 0 && (
                <div style={styles.missingList}>
                  <h4 style={styles.subTitle}>Missing rooms:</h4>
                  {mappingSummary.rooms.filter(r => !r.mapped).slice(0, 10).map((room, idx) => (
                    <div key={idx} style={styles.missingItem}>
                      <span style={styles.roomIdx}>{room.index + 1}.</span>
                      <span style={styles.missingRoomName}>{room.expected_name}</span>
                      <button
                        style={styles.recalButton}
                        onClick={() => handleRecalibrateRoom(room)}
                        disabled={uiState === UI_STATES.RECALIBRATING}
                      >
                        Fix
                      </button>
                    </div>
                  ))}
                  {mappingSummary.rooms.filter(r => !r.mapped).length > 10 && (
                    <p style={styles.moreText}>+{mappingSummary.rooms.filter(r => !r.mapped).length - 10} more</p>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Live Room View Toggle */}
      {uiState === UI_STATES.COMPLETE && (
        <div style={styles.section}>
          <button
            style={styles.secondaryButton}
            onClick={async () => {
              setShowLiveView(!showLiveView);
              if (!showLiveView) await refreshLiveRooms();
            }}
          >
            {showLiveView ? 'Hide Live View' : 'Show Live Room Participants'}
          </button>

          {showLiveView && liveRooms.length > 0 && (
            <div style={styles.liveBox}>
              <div style={styles.liveHeader}>
                <h4 style={styles.subTitle}>Current Occupancy</h4>
                <button style={styles.smallButton} onClick={refreshLiveRooms}>Refresh</button>
              </div>
              {liveRooms.slice(0, 10).map((room, idx) => (
                <div key={idx} style={styles.liveRoom}>
                  <span style={styles.liveRoomName}>{room.room_name}</span>
                  <span style={styles.liveCount}>{room.participant_count}</span>
                </div>
              ))}
              {liveRooms.length > 10 && <p style={styles.moreText}>+{liveRooms.length - 10} more rooms</p>}
            </div>
          )}
        </div>
      )}

      {/* Debug Log */}
      {debugLogs.length > 0 && (
        <div style={styles.section}>
          <h3 style={styles.sectionTitle}>LOG</h3>
          <pre style={styles.codeBlock}>{debugLogs.slice(-25).join('\n')}</pre>
        </div>
      )}
    </div>
  );
}

const styles = {
  container: { display: 'flex', flexDirection: 'column', gap: '12px', padding: '16px', maxWidth: '500px', margin: '0 auto', minHeight: '100vh', backgroundColor: '#1a1a2e' },
  header: { display: 'flex', flexDirection: 'column', gap: '2px' },
  title: { color: '#fff', fontSize: '18px', fontWeight: '600', margin: 0 },
  meetingId: { color: '#666', fontSize: '11px' },

  // Settings
  settingsPanel: { backgroundColor: 'rgba(45,140,255,0.1)', border: '1px solid rgba(45,140,255,0.3)', borderRadius: '8px', padding: '12px' },
  settingRow: { display: 'flex', alignItems: 'center', gap: '10px' },
  label: { color: '#ccc', fontSize: '13px' },
  select: { flex: 1, padding: '8px', backgroundColor: '#2a2a4a', border: '1px solid #444', borderRadius: '4px', color: '#fff', fontSize: '13px' },
  hint: { color: '#888', fontSize: '11px', margin: '6px 0 0 0', fontStyle: 'italic' },
  eta: { color: '#2D8CFF', fontSize: '12px', margin: '4px 0 0 0', fontWeight: '500' },

  // Layout
  section: { display: 'flex', flexDirection: 'column', gap: '6px' },
  sectionHeader: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' },
  sectionTitle: { color: '#888', fontSize: '11px', fontWeight: '600', textTransform: 'uppercase', margin: 0 },
  subTitle: { color: '#aaa', fontSize: '12px', margin: '6px 0 4px 0' },

  // Buttons
  actions: { display: 'flex', gap: '8px', marginTop: '4px' },
  primaryButton: { flex: 1, padding: '12px', backgroundColor: '#2D8CFF', color: '#fff', border: 'none', borderRadius: '6px', fontSize: '14px', fontWeight: '600', cursor: 'pointer' },
  secondaryButton: { padding: '10px 14px', backgroundColor: 'transparent', color: '#888', border: '1px solid #444', borderRadius: '6px', fontSize: '12px', cursor: 'pointer' },
  dangerButton: { padding: '10px 14px', backgroundColor: '#ff4757', color: '#fff', border: 'none', borderRadius: '6px', fontSize: '12px', cursor: 'pointer' },
  disabledButton: { flex: 1, padding: '12px', backgroundColor: '#333', color: '#666', border: 'none', borderRadius: '6px', fontSize: '14px', cursor: 'not-allowed' },
  smallButton: { padding: '4px 8px', backgroundColor: 'transparent', color: '#2D8CFF', border: '1px solid #2D8CFF', borderRadius: '4px', fontSize: '10px', cursor: 'pointer' },
  recalButton: { padding: '4px 8px', backgroundColor: '#ff6b6b', color: '#fff', border: 'none', borderRadius: '4px', fontSize: '10px', cursor: 'pointer' },

  // Failure Panel
  failurePanel: {
    backgroundColor: 'rgba(255, 71, 87, 0.08)',
    border: '1px solid rgba(255, 71, 87, 0.4)',
    borderRadius: '10px',
    padding: '16px',
    display: 'flex',
    flexDirection: 'column',
    gap: '12px'
  },
  failureHeader: {
    display: 'flex',
    alignItems: 'center',
    gap: '10px'
  },
  failureIcon: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: '28px',
    height: '28px',
    borderRadius: '50%',
    backgroundColor: '#ff4757',
    color: '#fff',
    fontSize: '16px',
    fontWeight: '700'
  },
  failureTitle: {
    color: '#ff6b6b',
    fontSize: '16px',
    fontWeight: '600'
  },
  failureDetails: {
    display: 'flex',
    flexDirection: 'column',
    gap: '6px',
    padding: '10px 12px',
    backgroundColor: 'rgba(0,0,0,0.2)',
    borderRadius: '6px'
  },
  failureRow: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center'
  },
  failureLabel: {
    color: '#888',
    fontSize: '12px'
  },
  failureValue: {
    color: '#fff',
    fontSize: '12px',
    fontWeight: '500'
  },
  failureValueError: {
    color: '#ff6b6b',
    fontSize: '12px',
    fontWeight: '600'
  },
  failureNote: {
    color: '#888',
    fontSize: '11px',
    margin: 0,
    fontStyle: 'italic'
  },
  failureActions: {
    display: 'flex',
    gap: '8px'
  },
  retryButton: {
    flex: 1,
    padding: '10px',
    backgroundColor: '#ff6b6b',
    color: '#fff',
    border: 'none',
    borderRadius: '6px',
    fontSize: '13px',
    fontWeight: '600',
    cursor: 'pointer'
  },

  // Summary
  summaryBox: { backgroundColor: 'rgba(0,0,0,0.2)', borderRadius: '6px', padding: '10px' },
  summaryText: { color: '#fff', fontSize: '14px', margin: 0 },
  green: { color: '#00C851' },
  red: { color: '#ff4757' },
  gray: { color: '#888' },
  missingList: { marginTop: '10px' },
  missingItem: { display: 'flex', alignItems: 'center', gap: '8px', padding: '4px 0', borderBottom: '1px solid rgba(255,255,255,0.05)' },
  roomIdx: { color: '#666', fontSize: '11px', minWidth: '24px' },
  missingRoomName: { color: '#ff6b6b', fontSize: '12px', flex: 1 },
  moreText: { color: '#666', fontSize: '10px', margin: '6px 0 0 0' },

  // Live view
  liveBox: { backgroundColor: 'rgba(0,0,0,0.3)', borderRadius: '6px', padding: '10px', marginTop: '8px' },
  liveHeader: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' },
  liveRoom: { display: 'flex', justifyContent: 'space-between', padding: '4px 0', borderBottom: '1px solid rgba(255,255,255,0.05)' },
  liveRoomName: { color: '#2D8CFF', fontSize: '12px' },
  liveCount: { color: '#888', fontSize: '11px' },

  // Debug log
  codeBlock: { backgroundColor: 'rgba(0,0,0,0.4)', padding: '10px', borderRadius: '6px', fontSize: '10px', color: '#00C851', overflow: 'auto', maxHeight: '180px', fontFamily: 'Monaco, monospace', margin: 0, whiteSpace: 'pre-wrap' }
};

export default CalibrationPanel;
