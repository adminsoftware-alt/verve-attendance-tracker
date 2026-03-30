import React from 'react';

const STATUS_STYLES = {
  idle: { color: '#888', icon: '○', bg: 'rgba(255,255,255,0.05)' },
  checking: { color: '#2D8CFF', icon: '◉', bg: 'rgba(45,140,255,0.1)' },
  calibrating: { color: '#FFB800', icon: '◉', bg: 'rgba(255,184,0,0.1)' },
  paused: { color: '#FF9800', icon: '❚❚', bg: 'rgba(255,152,0,0.1)' },
  recalibrating: { color: '#9C27B0', icon: '↻', bg: 'rgba(156,39,176,0.1)' },
  complete: { color: '#00C851', icon: '✓', bg: 'rgba(0,200,81,0.1)' },
  error: { color: '#FF4444', icon: '✕', bg: 'rgba(255,68,68,0.1)' }
};

function StatusMessage({ status, message }) {
  const style = STATUS_STYLES[status] || STATUS_STYLES.idle;

  return (
    <div style={{
      display: 'flex',
      alignItems: 'flex-start',
      gap: '10px',
      padding: '12px 16px',
      backgroundColor: style.bg,
      borderRadius: '8px',
      borderLeft: `3px solid ${style.color}`
    }}>
      <span style={{
        color: style.color,
        fontSize: '16px',
        fontWeight: 'bold',
        lineHeight: '20px'
      }}>
        {style.icon}
      </span>
      <span style={{
        color: status === 'error' ? '#ff6b6b' : '#fff',
        fontSize: '13px',
        lineHeight: '20px',
        wordBreak: 'break-word'
      }}>
        {message || getDefaultMessage(status)}
      </span>
    </div>
  );
}

function getDefaultMessage(status) {
  switch (status) {
    case 'idle':
      return 'Ready to start calibration';
    case 'checking':
      return 'Checking meeting status...';
    case 'calibrating':
      return 'Calibration in progress...';
    case 'paused':
      return 'Calibration paused';
    case 'recalibrating':
      return 'Re-calibrating room...';
    case 'complete':
      return 'Calibration complete!';
    case 'error':
      return 'An error occurred';
    default:
      return '';
  }
}

export default StatusMessage;
