import { useState, useEffect, useCallback } from 'react';
import {
  fetchTeamHolidays, addTeamHoliday, deleteTeamHoliday,
  fetchAllEmployeeLeave, addEmployeeLeave, deleteEmployeeLeave, addBulkEmployeeLeave,
  fetchEmployees
} from '../utils/zoomApi';

/**
 * Modal for managing holidays and individual leave.
 * Two tabs:
 * 1. Team Holidays - per-team holiday dates
 * 2. Individual Leave - mark specific employees as on leave
 */
export default function HolidayManager({ teamId, teamName, year, month, onClose, onChange }) {
  const [tab, setTab] = useState('team');  // 'team' | 'individual'

  // Team holidays state
  const [holidays, setHolidays] = useState([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);
  const [newDate, setNewDate] = useState(`${year}-${String(month).padStart(2, '0')}-01`);
  const [newDesc, setNewDesc] = useState('');
  const [saving, setSaving] = useState(false);

  // Individual leave state
  const [leaveList, setLeaveList] = useState([]);
  const [employees, setEmployees] = useState([]);
  const [selectedEmployees, setSelectedEmployees] = useState([]);
  const [leaveDate, setLeaveDate] = useState(`${year}-${String(month).padStart(2, '0')}-01`);
  const [leaveDesc, setLeaveDesc] = useState('');
  const [leaveType, setLeaveType] = useState('leave');
  const [empSearch, setEmpSearch] = useState('');

  const monthStr = String(month).padStart(2, '0');
  const daysInMonth = new Date(year, month, 0).getDate();
  const minDate = `${year}-${monthStr}-01`;
  const maxDate = `${year}-${monthStr}-${String(daysInMonth).padStart(2, '0')}`;

  // Load team holidays
  const loadHolidays = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const res = await fetchTeamHolidays(teamId, year, month);
      setHolidays(res.holidays || []);
    } catch (e) {
      setErr(e.message);
    }
    setLoading(false);
  }, [teamId, year, month]);

  // Load individual leave
  const loadLeave = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const res = await fetchAllEmployeeLeave(year, month);
      setLeaveList(res.leave || []);
    } catch (e) {
      setErr(e.message);
    }
    setLoading(false);
  }, [year, month]);

  // Load employees for selection
  const loadEmployees = useCallback(async () => {
    try {
      const res = await fetchEmployees({ status: 'active', team_id: teamId });
      setEmployees(res.employees || []);
    } catch (e) {
      console.error('Failed to load employees:', e);
    }
  }, [teamId]);

  useEffect(() => {
    if (tab === 'team') loadHolidays();
    else { loadLeave(); loadEmployees(); }
  }, [tab, loadHolidays, loadLeave, loadEmployees]);

  // Add team holiday
  const handleAddHoliday = async (e) => {
    e.preventDefault();
    if (!newDate) { setErr('Pick a date'); return; }
    setSaving(true);
    setErr(null);
    try {
      await addTeamHoliday(teamId, newDate, newDesc);
      setNewDesc('');
      await loadHolidays();
      onChange && onChange();
    } catch (ex) {
      setErr(ex.message);
    }
    setSaving(false);
  };

  // Delete team holiday
  const handleDeleteHoliday = async (holidayId) => {
    try {
      await deleteTeamHoliday(teamId, holidayId);
      await loadHolidays();
      onChange && onChange();
    } catch (e) {
      setErr(e.message);
    }
  };

  // Add individual leave
  const handleAddLeave = async (e) => {
    e.preventDefault();
    if (!leaveDate) { setErr('Pick a date'); return; }
    if (selectedEmployees.length === 0) { setErr('Select at least one employee'); return; }
    setSaving(true);
    setErr(null);
    try {
      if (selectedEmployees.length === 1) {
        await addEmployeeLeave(selectedEmployees[0], leaveDate, leaveType, leaveDesc);
      } else {
        await addBulkEmployeeLeave(leaveDate, selectedEmployees, leaveType, leaveDesc);
      }
      setSelectedEmployees([]);
      setLeaveDesc('');
      await loadLeave();
      onChange && onChange();
    } catch (ex) {
      setErr(ex.message);
    }
    setSaving(false);
  };

  // Delete individual leave
  const handleDeleteLeave = async (employeeId, leaveId) => {
    try {
      await deleteEmployeeLeave(employeeId, leaveId);
      await loadLeave();
      onChange && onChange();
    } catch (e) {
      setErr(e.message);
    }
  };

  // Toggle employee selection
  const toggleEmployee = (empId) => {
    setSelectedEmployees(prev =>
      prev.includes(empId) ? prev.filter(id => id !== empId) : [...prev, empId]
    );
  };

  // Select/deselect all filtered employees
  const toggleAll = () => {
    const filtered = filteredEmployees.map(e => e.employee_id);
    const allSelected = filtered.every(id => selectedEmployees.includes(id));
    if (allSelected) {
      setSelectedEmployees(prev => prev.filter(id => !filtered.includes(id)));
    } else {
      setSelectedEmployees(prev => [...new Set([...prev, ...filtered])]);
    }
  };

  const filteredEmployees = employees.filter(e =>
    (e.participant_name || '').toLowerCase().includes(empSearch.toLowerCase())
  );

  return (
    <div style={s.overlay} onClick={onClose}>
      <div style={s.modal} onClick={e => e.stopPropagation()}>
        <div style={s.header}>
          <div>
            <h3 style={{ margin: 0, fontSize: 16 }}>Manage Holidays & Leave</h3>
            <div style={{ fontSize: 11, color: '#64748b', marginTop: 2 }}>
              {teamName} — {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][month - 1]} {year}
            </div>
          </div>
          <button onClick={onClose} style={s.close}>×</button>
        </div>

        {/* Tabs */}
        <div style={s.tabs}>
          <button
            style={{ ...s.tab, ...(tab === 'team' ? s.tabActive : {}) }}
            onClick={() => setTab('team')}
          >
            Team Holidays
          </button>
          <button
            style={{ ...s.tab, ...(tab === 'individual' ? s.tabActive : {}) }}
            onClick={() => setTab('individual')}
          >
            Individual Leave
          </button>
        </div>

        <div style={s.body}>
          {err && <div style={s.error}>{err}</div>}

          {/* ═══ TEAM HOLIDAYS TAB ═══ */}
          {tab === 'team' && (
            <>
              <form onSubmit={handleAddHoliday} style={s.addForm}>
                <div style={s.formGroup}>
                  <label style={s.label}>Date</label>
                  <input
                    type="date"
                    value={newDate}
                    min={minDate}
                    max={maxDate}
                    onChange={e => setNewDate(e.target.value)}
                    style={s.input}
                    required
                  />
                </div>
                <div style={{ ...s.formGroup, flex: 1 }}>
                  <label style={s.label}>Description</label>
                  <input
                    type="text"
                    value={newDesc}
                    onChange={e => setNewDesc(e.target.value)}
                    placeholder="e.g. Diwali, Team off-site"
                    style={s.input}
                  />
                </div>
                <button type="submit" disabled={saving} style={s.addBtn}>
                  {saving ? 'Adding...' : '+ Add'}
                </button>
              </form>

              <div style={s.sectionTitle}>Holidays this month</div>
              {loading && <div style={s.empty}>Loading...</div>}
              {!loading && holidays.length === 0 && (
                <div style={s.empty}>No holidays configured for this month.</div>
              )}
              {!loading && holidays.length > 0 && (
                <div style={s.tableWrap}>
                  <table style={s.table}>
                    <thead>
                      <tr>
                        <th style={s.th}>Date</th>
                        <th style={s.th}>Day</th>
                        <th style={s.th}>Description</th>
                        <th style={s.th}></th>
                      </tr>
                    </thead>
                    <tbody>
                      {holidays.map(h => {
                        const [y, m, d] = h.date.split('-').map(Number);
                        const dow = new Date(y, m - 1, d).getDay();
                        const dayName = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][dow];
                        return (
                          <tr key={h.holiday_id}>
                            <td style={s.td}><strong>{h.date}</strong></td>
                            <td style={s.td}>{dayName}</td>
                            <td style={s.td}>{h.description || <span style={{ color: '#94a3b8' }}>—</span>}</td>
                            <td style={s.td}>
                              <button onClick={() => handleDeleteHoliday(h.holiday_id)} style={s.deleteBtn}>
                                Remove
                              </button>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}

              <div style={s.hint}>
                Team holidays apply to all members of <strong>{teamName}</strong>.
                These dates won&apos;t count as absences in reports.
              </div>
            </>
          )}

          {/* ═══ INDIVIDUAL LEAVE TAB ═══ */}
          {tab === 'individual' && (
            <>
              <form onSubmit={handleAddLeave} style={s.addForm}>
                <div style={s.formGroup}>
                  <label style={s.label}>Date</label>
                  <input
                    type="date"
                    value={leaveDate}
                    min={minDate}
                    max={maxDate}
                    onChange={e => setLeaveDate(e.target.value)}
                    style={s.input}
                    required
                  />
                </div>
                <div style={s.formGroup}>
                  <label style={s.label}>Type</label>
                  <select value={leaveType} onChange={e => setLeaveType(e.target.value)} style={s.input}>
                    <option value="leave">Leave</option>
                    <option value="sick">Sick</option>
                    <option value="personal">Personal</option>
                    <option value="wfh">WFH</option>
                  </select>
                </div>
                <div style={{ ...s.formGroup, flex: 1 }}>
                  <label style={s.label}>Note</label>
                  <input
                    type="text"
                    value={leaveDesc}
                    onChange={e => setLeaveDesc(e.target.value)}
                    placeholder="Optional note"
                    style={s.input}
                  />
                </div>
                <button type="submit" disabled={saving || selectedEmployees.length === 0} style={s.addBtn}>
                  {saving ? 'Adding...' : `+ Add (${selectedEmployees.length})`}
                </button>
              </form>

              {/* Employee Selection */}
              <div style={s.sectionTitle}>Select Employees</div>
              <div style={s.empSelectWrap}>
                <div style={s.empToolbar}>
                  <input
                    type="text"
                    placeholder="Search employees..."
                    value={empSearch}
                    onChange={e => setEmpSearch(e.target.value)}
                    style={{ ...s.input, flex: 1 }}
                  />
                  <button type="button" onClick={toggleAll} style={s.selectAllBtn}>
                    {filteredEmployees.every(e => selectedEmployees.includes(e.employee_id)) ? 'Deselect All' : 'Select All'}
                  </button>
                </div>
                <div style={s.empGrid}>
                  {filteredEmployees.map(e => (
                    <label key={e.employee_id} style={s.empItem}>
                      <input
                        type="checkbox"
                        checked={selectedEmployees.includes(e.employee_id)}
                        onChange={() => toggleEmployee(e.employee_id)}
                      />
                      <span style={s.empName}>{e.display_name || e.participant_name}</span>
                    </label>
                  ))}
                  {filteredEmployees.length === 0 && (
                    <div style={{ padding: 12, color: '#94a3b8', fontSize: 12 }}>No employees found</div>
                  )}
                </div>
              </div>

              {/* Leave List */}
              <div style={s.sectionTitle}>Individual Leave this month</div>
              {loading && <div style={s.empty}>Loading...</div>}
              {!loading && leaveList.length === 0 && (
                <div style={s.empty}>No individual leave records for this month.</div>
              )}
              {!loading && leaveList.length > 0 && (
                <div style={s.tableWrap}>
                  <table style={s.table}>
                    <thead>
                      <tr>
                        <th style={s.th}>Date</th>
                        <th style={s.th}>Employee</th>
                        <th style={s.th}>Type</th>
                        <th style={s.th}>Note</th>
                        <th style={s.th}></th>
                      </tr>
                    </thead>
                    <tbody>
                      {leaveList.map(l => (
                        <tr key={l.leave_id}>
                          <td style={s.td}><strong>{l.date}</strong></td>
                          <td style={s.td}>{l.employee_name}</td>
                          <td style={s.td}>
                            <span style={{ ...s.badge, ...getLeaveTypeStyle(l.leave_type) }}>
                              {l.leave_type || 'leave'}
                            </span>
                          </td>
                          <td style={s.td}>{l.description || <span style={{ color: '#94a3b8' }}>—</span>}</td>
                          <td style={s.td}>
                            <button onClick={() => handleDeleteLeave(l.employee_id, l.leave_id)} style={s.deleteBtn}>
                              Remove
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <div style={s.hint}>
                Individual leave is per-employee. Use this to mark specific people
                as on leave without affecting the entire team.
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function getLeaveTypeStyle(type) {
  switch (type) {
    case 'sick': return { background: '#fef2f2', color: '#dc2626' };
    case 'personal': return { background: '#eff6ff', color: '#2563eb' };
    case 'wfh': return { background: '#f0fdf4', color: '#16a34a' };
    default: return { background: '#fef9c3', color: '#854d0e' };
  }
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
    width: 'min(720px, 94vw)', maxHeight: '90vh',
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

  addForm: {
    display: 'flex', gap: 10, alignItems: 'flex-end', marginBottom: 14,
    padding: 12, background: '#f8fafc',
    border: '1px solid #e5e7eb', borderRadius: 10, flexWrap: 'wrap',
  },
  formGroup: { display: 'flex', flexDirection: 'column', gap: 4 },
  label: { fontSize: 10, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.03em', fontWeight: 600 },
  input: { padding: '7px 10px', border: '1px solid #d1d5db', borderRadius: 6, fontSize: 12, background: '#fff' },

  addBtn: {
    padding: '7px 16px', background: '#f97316', color: '#fff',
    border: 'none', borderRadius: 6, fontSize: 12, fontWeight: 700,
    cursor: 'pointer', whiteSpace: 'nowrap',
  },
  deleteBtn: {
    padding: '4px 10px', background: '#fef2f2', color: '#b91c1c',
    border: '1px solid #fecaca', borderRadius: 6, fontSize: 11,
    fontWeight: 600, cursor: 'pointer',
  },
  selectAllBtn: {
    padding: '6px 12px', background: '#eff6ff', color: '#2563eb',
    border: '1px solid #bfdbfe', borderRadius: 6, fontSize: 11,
    fontWeight: 600, cursor: 'pointer',
  },

  sectionTitle: {
    fontSize: 11, fontWeight: 700, color: '#64748b',
    textTransform: 'uppercase', letterSpacing: '0.04em',
    marginBottom: 8, marginTop: 4,
  },
  tableWrap: { border: '1px solid #e5e7eb', borderRadius: 8, overflow: 'hidden', marginBottom: 14 },
  table: { width: '100%', borderCollapse: 'collapse' },
  th: {
    padding: '8px 12px', textAlign: 'left', fontSize: 10, fontWeight: 600,
    color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.04em',
    borderBottom: '1px solid #e5e7eb', background: '#f8fafc',
  },
  td: { padding: '8px 12px', fontSize: 12, color: '#1e293b', borderBottom: '1px solid #f1f5f9' },

  badge: {
    display: 'inline-block', padding: '2px 8px', borderRadius: 4,
    fontSize: 10, fontWeight: 600, textTransform: 'uppercase',
  },

  empSelectWrap: {
    border: '1px solid #e5e7eb', borderRadius: 8, marginBottom: 14, overflow: 'hidden',
  },
  empToolbar: {
    display: 'flex', gap: 8, padding: 10, background: '#f8fafc', borderBottom: '1px solid #e5e7eb',
  },
  empGrid: {
    display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))',
    gap: 4, padding: 10, maxHeight: 180, overflowY: 'auto',
  },
  empItem: {
    display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px',
    borderRadius: 6, cursor: 'pointer', fontSize: 12,
    background: '#fff', border: '1px solid #e5e7eb',
  },
  empName: { color: '#1e293b' },

  empty: { textAlign: 'center', padding: '24px 12px', color: '#94a3b8', fontSize: 12, background: '#f8fafc', border: '1px dashed #e5e7eb', borderRadius: 8, marginBottom: 14 },
  error: { padding: '8px 12px', background: '#fef2f2', color: '#dc2626', border: '1px solid #fecaca', borderRadius: 6, fontSize: 12, marginBottom: 12 },
  hint: { fontSize: 11, color: '#64748b', padding: '10px 12px', background: '#fff7ed', border: '1px solid #fed7aa', borderRadius: 6 },
};
