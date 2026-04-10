import { useState, useEffect } from 'react';
import { addAttendanceOverride, addEmployeeLeave } from '../utils/zoomApi';

/**
 * Modal for editing attendance data for a single employee on a single date.
 * Can override: first_seen, last_seen, status, active_mins, break_mins, isolation_mins
 * Can also mark the day as leave directly.
 */
export default function AttendanceEditModal({ member, date, onClose, onSave }) {
  const [mode, setMode] = useState('edit'); // 'edit' | 'leave'
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  // Edit fields
  const [firstSeen, setFirstSeen] = useState(member?.first_seen_ist || '');
  const [lastSeen, setLastSeen] = useState(member?.last_seen_ist || '');
  const [status, setStatus] = useState(member?.status || 'present');
  const [activeMins, setActiveMins] = useState(member?.total_duration_mins || member?.active_minutes || 0);
  const [breakMins, setBreakMins] = useState(member?.break_minutes || 0);
  const [isolationMins, setIsolationMins] = useState(member?.isolation_minutes || 0);
  const [notes, setNotes] = useState('');

  // Leave fields
  const [leaveType, setLeaveType] = useState('leave');
  const [leaveDesc, setLeaveDesc] = useState('');

  useEffect(() => {
    if (member) {
      setFirstSeen(member.first_seen_ist || '');
      setLastSeen(member.last_seen_ist || '');
      setStatus(member.status || 'present');
      setActiveMins(member.total_duration_mins || member.active_minutes || 0);
      setBreakMins(member.break_minutes || 0);
      setIsolationMins(member.isolation_minutes || 0);
    }
  }, [member]);

  const handleSaveEdit = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await addAttendanceOverride({
        employee_name: member.name,
        employee_id: member.employee_id || '',
        event_date: date,
        first_seen_ist: firstSeen || null,
        last_seen_ist: lastSeen || null,
        status: status,
        active_mins: parseInt(activeMins) || null,
        break_mins: parseInt(breakMins) || null,
        isolation_mins: parseInt(isolationMins) || null,
        notes: notes || '',
      });
      onSave && onSave();
      onClose();
    } catch (ex) {
      setError(ex.message);
    }
    setSaving(false);
  };

  const handleSaveLeave = async (e) => {
    e.preventDefault();
    if (!member.employee_id) {
      setError('Cannot mark leave: Employee ID not found. Employee may not be in registry.');
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await addEmployeeLeave(member.employee_id, date, leaveType, leaveDesc);
      onSave && onSave();
      onClose();
    } catch (ex) {
      setError(ex.message);
    }
    setSaving(false);
  };

  return (
    <div style={s.overlay} onClick={onClose}>
      <div style={s.modal} onClick={e => e.stopPropagation()}>
        <div style={s.header}>
          <div>
            <h3 style={{ margin: 0, fontSize: 16 }}>Edit Attendance</h3>
            <div style={{ fontSize: 12, color: '#64748b', marginTop: 2 }}>
              <strong>{member?.name}</strong> — {date}
            </div>
          </div>
          <button onClick={onClose} style={s.close}>×</button>
        </div>

        {/* Mode Tabs */}
        <div style={s.tabs}>
          <button
            style={{ ...s.tab, ...(mode === 'edit' ? s.tabActive : {}) }}
            onClick={() => setMode('edit')}
          >
            Edit Metrics
          </button>
          <button
            style={{ ...s.tab, ...(mode === 'leave' ? s.tabActive : {}) }}
            onClick={() => setMode('leave')}
          >
            Mark as Leave
          </button>
        </div>

        <div style={s.body}>
          {error && <div style={s.error}>{error}</div>}

          {/* ═══ EDIT MODE ═══ */}
          {mode === 'edit' && (
            <form onSubmit={handleSaveEdit}>
              <div style={s.row}>
                <div style={s.formGroup}>
                  <label style={s.label}>First Seen (IST)</label>
                  <input
                    type="time"
                    value={firstSeen}
                    onChange={e => setFirstSeen(e.target.value)}
                    style={s.input}
                  />
                </div>
                <div style={s.formGroup}>
                  <label style={s.label}>Last Seen (IST)</label>
                  <input
                    type="time"
                    value={lastSeen}
                    onChange={e => setLastSeen(e.target.value)}
                    style={s.input}
                  />
                </div>
              </div>

              <div style={s.row}>
                <div style={s.formGroup}>
                  <label style={s.label}>Status</label>
                  <select value={status} onChange={e => setStatus(e.target.value)} style={s.input}>
                    <option value="present">Present</option>
                    <option value="half_day">Half Day</option>
                    <option value="absent">Absent</option>
                  </select>
                </div>
                <div style={s.formGroup}>
                  <label style={s.label}>Active Minutes</label>
                  <input
                    type="number"
                    value={activeMins}
                    onChange={e => setActiveMins(e.target.value)}
                    style={s.input}
                    min="0"
                  />
                </div>
              </div>

              <div style={s.row}>
                <div style={s.formGroup}>
                  <label style={s.label}>Break Minutes</label>
                  <input
                    type="number"
                    value={breakMins}
                    onChange={e => setBreakMins(e.target.value)}
                    style={s.input}
                    min="0"
                  />
                </div>
                <div style={s.formGroup}>
                  <label style={s.label}>Isolation Minutes</label>
                  <input
                    type="number"
                    value={isolationMins}
                    onChange={e => setIsolationMins(e.target.value)}
                    style={s.input}
                    min="0"
                  />
                </div>
              </div>

              <div style={s.formGroup}>
                <label style={s.label}>Notes</label>
                <input
                  type="text"
                  value={notes}
                  onChange={e => setNotes(e.target.value)}
                  placeholder="Reason for override..."
                  style={s.input}
                />
              </div>

              <div style={s.hint}>
                Overrides are stored separately and will be used instead of
                auto-detected values in reports.
              </div>

              <div style={s.actions}>
                <button type="button" onClick={onClose} style={s.cancelBtn}>Cancel</button>
                <button type="submit" disabled={saving} style={s.saveBtn}>
                  {saving ? 'Saving...' : 'Save Override'}
                </button>
              </div>
            </form>
          )}

          {/* ═══ LEAVE MODE ═══ */}
          {mode === 'leave' && (
            <form onSubmit={handleSaveLeave}>
              <div style={s.row}>
                <div style={s.formGroup}>
                  <label style={s.label}>Leave Type</label>
                  <select value={leaveType} onChange={e => setLeaveType(e.target.value)} style={s.input}>
                    <option value="leave">Leave</option>
                    <option value="sick">Sick</option>
                    <option value="personal">Personal</option>
                    <option value="wfh">WFH</option>
                  </select>
                </div>
              </div>

              <div style={s.formGroup}>
                <label style={s.label}>Description</label>
                <input
                  type="text"
                  value={leaveDesc}
                  onChange={e => setLeaveDesc(e.target.value)}
                  placeholder="Optional note..."
                  style={s.input}
                />
              </div>

              <div style={s.hint}>
                Marking as leave will record this date as a non-working day for
                <strong> {member?.name}</strong>. It won&apos;t count as an absence.
              </div>

              <div style={s.actions}>
                <button type="button" onClick={onClose} style={s.cancelBtn}>Cancel</button>
                <button type="submit" disabled={saving} style={s.saveBtn}>
                  {saving ? 'Saving...' : 'Mark as Leave'}
                </button>
              </div>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}

const s = {
  overlay: {
    position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
    background: 'rgba(15,23,42,0.45)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    zIndex: 1000,
  },
  modal: {
    background: '#fff', borderRadius: 14,
    width: 'min(480px, 94vw)', maxHeight: '90vh',
    boxShadow: '0 20px 40px rgba(0,0,0,0.2)',
    display: 'flex', flexDirection: 'column',
  },
  header: {
    padding: '16px 20px', borderBottom: '1px solid #e5e7eb',
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
  },
  close: { background: 'none', border: 'none', fontSize: 26, cursor: 'pointer', color: '#94a3b8', padding: 0, lineHeight: 1 },
  tabs: {
    display: 'flex', borderBottom: '1px solid #e5e7eb', padding: '0 20px',
  },
  tab: {
    padding: '12px 20px', background: 'none', border: 'none',
    fontSize: 13, fontWeight: 600, color: '#64748b',
    cursor: 'pointer', borderBottom: '2px solid transparent', marginBottom: -1,
  },
  tabActive: {
    color: '#2563eb', borderBottomColor: '#2563eb',
  },
  body: { padding: '18px 20px', overflow: 'auto', flex: 1 },

  row: {
    display: 'flex', gap: 12, marginBottom: 12,
  },
  formGroup: { display: 'flex', flexDirection: 'column', gap: 4, flex: 1 },
  label: { fontSize: 11, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.03em', fontWeight: 600 },
  input: {
    padding: '10px 12px', border: '1px solid #d1d5db', borderRadius: 8,
    fontSize: 14, background: '#fff', width: '100%', boxSizing: 'border-box',
  },

  hint: {
    fontSize: 11, color: '#64748b', padding: '10px 12px',
    background: '#f8fafc', border: '1px solid #e5e7eb', borderRadius: 6,
    marginTop: 12, marginBottom: 16,
  },
  error: {
    padding: '10px 14px', background: '#fef2f2', color: '#dc2626',
    border: '1px solid #fecaca', borderRadius: 6, fontSize: 12, marginBottom: 12,
  },

  actions: {
    display: 'flex', gap: 10, justifyContent: 'flex-end',
  },
  cancelBtn: {
    padding: '10px 20px', background: '#f1f5f9', color: '#475569',
    border: 'none', borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: 'pointer',
  },
  saveBtn: {
    padding: '10px 20px', background: '#2563eb', color: '#fff',
    border: 'none', borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: 'pointer',
  },
};
