import { useState, useEffect, useCallback } from 'react';
import {
  fetchTeams, fetchTeamHolidays, addTeamHoliday, deleteTeamHoliday,
  fetchAllEmployeeLeave, addEmployeeLeave, deleteEmployeeLeave, addBulkEmployeeLeave,
  fetchEmployees
} from '../utils/zoomApi';

/**
 * Full-page Holidays & Leave Manager
 * - Team selector dropdown
 * - Calendar view showing holidays
 * - Tabs for Team Holidays and Individual Leave
 */
export default function HolidayManager({ user }) {
  // Core state
  const [teams, setTeams] = useState([]);
  const [selectedTeamId, setSelectedTeamId] = useState('');
  const [year, setYear] = useState(new Date().getFullYear());
  const [month, setMonth] = useState(new Date().getMonth() + 1);
  const [tab, setTab] = useState('team'); // 'team' | 'individual'

  // Data state
  const [holidays, setHolidays] = useState([]);
  const [leaveList, setLeaveList] = useState([]);
  const [employees, setEmployees] = useState([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  // Form state - Team holidays
  const [newDate, setNewDate] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [saving, setSaving] = useState(false);

  // Form state - Individual leave
  const [selectedEmployees, setSelectedEmployees] = useState([]);
  const [leaveDate, setLeaveDate] = useState('');
  const [leaveDesc, setLeaveDesc] = useState('');
  const [leaveType, setLeaveType] = useState('leave');
  const [empSearch, setEmpSearch] = useState('');

  // Hover state for calendar
  const [hoverDay, setHoverDay] = useState(null);

  const monthStr = String(month).padStart(2, '0');
  const daysInMonth = new Date(year, month, 0).getDate();
  const firstDayOfWeek = new Date(year, month - 1, 1).getDay();

  // Initialize default dates when month/year changes
  useEffect(() => {
    const defaultDate = `${year}-${monthStr}-01`;
    setNewDate(defaultDate);
    setLeaveDate(defaultDate);
  }, [year, monthStr]);

  // Load teams on mount
  useEffect(() => {
    const loadTeams = async () => {
      try {
        const res = await fetchTeams();
        setTeams(res.teams || []);
        if (res.teams?.length > 0 && !selectedTeamId) {
          setSelectedTeamId(res.teams[0].team_id);
        }
      } catch (e) {
        console.error('Failed to load teams:', e);
      }
    };
    loadTeams();
  }, []);

  // Load team holidays
  const loadHolidays = useCallback(async () => {
    if (!selectedTeamId) return;
    setLoading(true);
    setErr(null);
    try {
      const res = await fetchTeamHolidays(selectedTeamId, year, month);
      setHolidays(res.holidays || []);
    } catch (e) {
      setErr(e.message);
    }
    setLoading(false);
  }, [selectedTeamId, year, month]);

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

  // Load all employees
  const loadEmployees = useCallback(async () => {
    try {
      const res = await fetchEmployees({ status: 'active' });
      setEmployees(res.employees || []);
    } catch (e) {
      console.error('Failed to load employees:', e);
    }
  }, []);

  // Load data when tab or selections change
  useEffect(() => {
    if (tab === 'team' && selectedTeamId) {
      loadHolidays();
    } else if (tab === 'individual') {
      loadLeave();
      loadEmployees();
    }
  }, [tab, selectedTeamId, year, month, loadHolidays, loadLeave, loadEmployees]);

  // Add team holiday
  const handleAddHoliday = async (e) => {
    e.preventDefault();
    if (!selectedTeamId) { setErr('Select a team first'); return; }
    if (!newDate) { setErr('Pick a date'); return; }
    setSaving(true);
    setErr(null);
    try {
      await addTeamHoliday(selectedTeamId, newDate, newDesc);
      setNewDesc('');
      await loadHolidays();
    } catch (ex) {
      setErr(ex.message);
    }
    setSaving(false);
  };

  // Delete team holiday
  const handleDeleteHoliday = async (holidayId) => {
    if (!selectedTeamId) return;
    try {
      await deleteTeamHoliday(selectedTeamId, holidayId);
      await loadHolidays();
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

  // Get holidays/leave for a specific day
  const getHolidaysForDay = (day) => {
    const dateStr = `${year}-${monthStr}-${String(day).padStart(2, '0')}`;
    return holidays.filter(h => h.date === dateStr);
  };

  const getLeaveForDay = (day) => {
    const dateStr = `${year}-${monthStr}-${String(day).padStart(2, '0')}`;
    return leaveList.filter(l => l.date === dateStr);
  };

  // Build calendar grid
  const calendarDays = [];
  // Empty cells before first day
  for (let i = 0; i < firstDayOfWeek; i++) {
    calendarDays.push(null);
  }
  // Days of month
  for (let d = 1; d <= daysInMonth; d++) {
    calendarDays.push(d);
  }

  const selectedTeam = teams.find(t => t.team_id === selectedTeamId);
  const monthNames = ['January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December'];

  return (
    <div style={s.container}>
      <h1 style={s.title}>Holidays & Leave Management</h1>

      {/* Controls Row */}
      <div style={s.controls}>
        <div style={s.controlGroup}>
          <label style={s.controlLabel}>Team</label>
          <select
            value={selectedTeamId}
            onChange={e => setSelectedTeamId(e.target.value)}
            style={s.select}
          >
            <option value="">-- Select Team --</option>
            {teams.map(t => (
              <option key={t.team_id} value={t.team_id}>{t.team_name}</option>
            ))}
          </select>
        </div>
        <div style={s.controlGroup}>
          <label style={s.controlLabel}>Month</label>
          <select value={month} onChange={e => setMonth(Number(e.target.value))} style={s.select}>
            {monthNames.map((m, i) => (
              <option key={i} value={i + 1}>{m}</option>
            ))}
          </select>
        </div>
        <div style={s.controlGroup}>
          <label style={s.controlLabel}>Year</label>
          <select value={year} onChange={e => setYear(Number(e.target.value))} style={s.select}>
            {[2024, 2025, 2026, 2027].map(y => (
              <option key={y} value={y}>{y}</option>
            ))}
          </select>
        </div>
      </div>

      {err && <div style={s.error}>{err}</div>}

      <div style={s.mainContent}>
        {/* Calendar View */}
        <div style={s.calendarSection}>
          <div style={s.calendarHeader}>
            <h3 style={s.calendarTitle}>{monthNames[month - 1]} {year}</h3>
            <div style={s.legend}>
              <span style={s.legendItem}><span style={{ ...s.legendDot, background: '#f97316' }}></span> Team Holiday</span>
              <span style={s.legendItem}><span style={{ ...s.legendDot, background: '#2563eb' }}></span> Individual Leave</span>
            </div>
          </div>
          <div style={s.calendar}>
            {/* Day headers */}
            {['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].map(d => (
              <div key={d} style={s.calDayHeader}>{d}</div>
            ))}
            {/* Calendar cells */}
            {calendarDays.map((day, idx) => {
              if (day === null) return <div key={`empty-${idx}`} style={s.calEmpty}></div>;

              const dayHolidays = getHolidaysForDay(day);
              const dayLeave = getLeaveForDay(day);
              const hasHoliday = dayHolidays.length > 0;
              const hasLeave = dayLeave.length > 0;
              const isToday = new Date().getDate() === day &&
                new Date().getMonth() + 1 === month &&
                new Date().getFullYear() === year;
              const isWeekend = new Date(year, month - 1, day).getDay() % 6 === 0;

              return (
                <div
                  key={day}
                  style={{
                    ...s.calDay,
                    ...(isToday ? s.calDayToday : {}),
                    ...(isWeekend ? s.calDayWeekend : {}),
                    ...(hasHoliday ? s.calDayHoliday : {}),
                    ...(hasLeave && !hasHoliday ? s.calDayLeave : {}),
                  }}
                  onMouseEnter={() => setHoverDay(day)}
                  onMouseLeave={() => setHoverDay(null)}
                >
                  <span style={s.calDayNum}>{day}</span>
                  {hasHoliday && <div style={s.calDot}></div>}
                  {hasLeave && <div style={{ ...s.calDot, background: '#2563eb', marginLeft: hasHoliday ? 4 : 0 }}></div>}

                  {/* Hover tooltip */}
                  {hoverDay === day && (hasHoliday || hasLeave) && (
                    <div style={s.tooltip}>
                      {dayHolidays.map(h => (
                        <div key={h.holiday_id} style={s.tooltipItem}>
                          <span style={{ ...s.tooltipBadge, background: '#fff7ed', color: '#c2410c' }}>Holiday</span>
                          {h.description || 'Team Holiday'}
                          <div style={s.tooltipSub}>All team members</div>
                        </div>
                      ))}
                      {dayLeave.map(l => (
                        <div key={l.leave_id} style={s.tooltipItem}>
                          <span style={{ ...s.tooltipBadge, ...getLeaveTypeStyle(l.leave_type) }}>{l.leave_type}</span>
                          {l.employee_name}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* Tabs Panel */}
        <div style={s.tabsSection}>
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

          <div style={s.tabContent}>
            {/* TEAM HOLIDAYS TAB */}
            {tab === 'team' && (
              <>
                {!selectedTeamId ? (
                  <div style={s.empty}>Select a team above to manage holidays</div>
                ) : (
                  <>
                    <form onSubmit={handleAddHoliday} style={s.addForm}>
                      <div style={s.formGroup}>
                        <label style={s.label}>Date</label>
                        <input
                          type="date"
                          value={newDate}
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
                          placeholder="e.g. Diwali, Christmas"
                          style={s.input}
                        />
                      </div>
                      <button type="submit" disabled={saving} style={s.addBtn}>
                        {saving ? 'Adding...' : '+ Add Holiday'}
                      </button>
                    </form>

                    <div style={s.sectionTitle}>
                      Holidays for {selectedTeam?.team_name || 'Team'}
                    </div>
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
                              const dayName = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][dow];
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
                      Team holidays apply to all members of <strong>{selectedTeam?.team_name}</strong>.
                      These dates won't count as absences in reports.
                    </div>
                  </>
                )}
              </>
            )}

            {/* INDIVIDUAL LEAVE TAB */}
            {tab === 'individual' && (
              <>
                <form onSubmit={handleAddLeave} style={s.addForm}>
                  <div style={s.formGroup}>
                    <label style={s.label}>Date</label>
                    <input
                      type="date"
                      value={leaveDate}
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
                    {saving ? 'Adding...' : `+ Add Leave (${selectedEmployees.length})`}
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
                      {filteredEmployees.length > 0 && filteredEmployees.every(e => selectedEmployees.includes(e.employee_id)) ? 'Deselect All' : 'Select All'}
                    </button>
                  </div>
                  <div style={s.empGrid}>
                    {filteredEmployees.map(e => (
                      <label key={e.employee_id} style={{
                        ...s.empItem,
                        ...(selectedEmployees.includes(e.employee_id) ? s.empItemSelected : {})
                      }}>
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
                <div style={s.sectionTitle}>Individual Leave - {monthNames[month - 1]} {year}</div>
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
  container: {
    maxWidth: 1400,
    margin: '0 auto',
  },
  title: {
    fontSize: 22,
    fontWeight: 700,
    color: '#1e293b',
    margin: '0 0 20px 0',
  },
  controls: {
    display: 'flex',
    gap: 16,
    marginBottom: 20,
    flexWrap: 'wrap',
  },
  controlGroup: {
    display: 'flex',
    flexDirection: 'column',
    gap: 4,
  },
  controlLabel: {
    fontSize: 10,
    fontWeight: 600,
    color: '#64748b',
    textTransform: 'uppercase',
    letterSpacing: '0.04em',
  },
  select: {
    padding: '8px 12px',
    border: '1px solid #d1d5db',
    borderRadius: 8,
    fontSize: 13,
    background: '#fff',
    minWidth: 160,
  },
  error: {
    padding: '10px 14px',
    background: '#fef2f2',
    color: '#dc2626',
    border: '1px solid #fecaca',
    borderRadius: 8,
    fontSize: 13,
    marginBottom: 16,
  },
  mainContent: {
    display: 'grid',
    gridTemplateColumns: '340px 1fr',
    gap: 24,
    alignItems: 'start',
  },
  // Calendar styles
  calendarSection: {
    background: '#fff',
    borderRadius: 12,
    border: '1px solid #e5e7eb',
    padding: 16,
  },
  calendarHeader: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 12,
  },
  calendarTitle: {
    margin: 0,
    fontSize: 15,
    fontWeight: 700,
    color: '#1e293b',
  },
  legend: {
    display: 'flex',
    gap: 12,
    fontSize: 10,
    color: '#64748b',
  },
  legendItem: {
    display: 'flex',
    alignItems: 'center',
    gap: 4,
  },
  legendDot: {
    width: 8,
    height: 8,
    borderRadius: '50%',
  },
  calendar: {
    display: 'grid',
    gridTemplateColumns: 'repeat(7, 1fr)',
    gap: 2,
  },
  calDayHeader: {
    textAlign: 'center',
    fontSize: 10,
    fontWeight: 600,
    color: '#94a3b8',
    padding: '6px 0',
    textTransform: 'uppercase',
  },
  calEmpty: {
    aspectRatio: '1',
  },
  calDay: {
    aspectRatio: '1',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    borderRadius: 6,
    background: '#f8fafc',
    cursor: 'pointer',
    position: 'relative',
    transition: 'all 0.15s',
  },
  calDayNum: {
    fontSize: 12,
    fontWeight: 500,
    color: '#475569',
  },
  calDayToday: {
    border: '2px solid #2563eb',
  },
  calDayWeekend: {
    background: '#f1f5f9',
  },
  calDayHoliday: {
    background: '#fff7ed',
  },
  calDayLeave: {
    background: '#eff6ff',
  },
  calDot: {
    width: 5,
    height: 5,
    borderRadius: '50%',
    background: '#f97316',
    marginTop: 2,
  },
  tooltip: {
    position: 'absolute',
    bottom: '100%',
    left: '50%',
    transform: 'translateX(-50%)',
    background: '#1e293b',
    color: '#fff',
    padding: '8px 10px',
    borderRadius: 8,
    fontSize: 11,
    minWidth: 160,
    zIndex: 100,
    marginBottom: 6,
    boxShadow: '0 4px 12px rgba(0,0,0,0.2)',
  },
  tooltipItem: {
    marginBottom: 6,
    lineHeight: 1.4,
  },
  tooltipBadge: {
    display: 'inline-block',
    padding: '1px 6px',
    borderRadius: 4,
    fontSize: 9,
    fontWeight: 600,
    textTransform: 'uppercase',
    marginRight: 6,
  },
  tooltipSub: {
    fontSize: 10,
    color: '#94a3b8',
    marginTop: 2,
  },
  // Tabs section
  tabsSection: {
    background: '#fff',
    borderRadius: 12,
    border: '1px solid #e5e7eb',
    overflow: 'hidden',
  },
  tabs: {
    display: 'flex',
    borderBottom: '1px solid #e5e7eb',
  },
  tab: {
    flex: 1,
    padding: '14px 20px',
    background: 'none',
    border: 'none',
    fontSize: 13,
    fontWeight: 600,
    color: '#64748b',
    cursor: 'pointer',
    borderBottom: '2px solid transparent',
    marginBottom: -1,
    transition: 'all 0.15s',
  },
  tabActive: {
    color: '#2563eb',
    borderBottomColor: '#2563eb',
    background: '#f8fafc',
  },
  tabContent: {
    padding: 16,
    maxHeight: 'calc(100vh - 300px)',
    overflowY: 'auto',
  },
  addForm: {
    display: 'flex',
    gap: 10,
    alignItems: 'flex-end',
    marginBottom: 16,
    padding: 12,
    background: '#f8fafc',
    border: '1px solid #e5e7eb',
    borderRadius: 10,
    flexWrap: 'wrap',
  },
  formGroup: { display: 'flex', flexDirection: 'column', gap: 4 },
  label: { fontSize: 10, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.03em', fontWeight: 600 },
  input: { padding: '8px 10px', border: '1px solid #d1d5db', borderRadius: 6, fontSize: 12, background: '#fff' },
  addBtn: {
    padding: '8px 16px',
    background: '#f97316',
    color: '#fff',
    border: 'none',
    borderRadius: 6,
    fontSize: 12,
    fontWeight: 700,
    cursor: 'pointer',
    whiteSpace: 'nowrap',
  },
  deleteBtn: {
    padding: '4px 10px',
    background: '#fef2f2',
    color: '#b91c1c',
    border: '1px solid #fecaca',
    borderRadius: 6,
    fontSize: 11,
    fontWeight: 600,
    cursor: 'pointer',
  },
  selectAllBtn: {
    padding: '6px 12px',
    background: '#eff6ff',
    color: '#2563eb',
    border: '1px solid #bfdbfe',
    borderRadius: 6,
    fontSize: 11,
    fontWeight: 600,
    cursor: 'pointer',
  },
  sectionTitle: {
    fontSize: 11,
    fontWeight: 700,
    color: '#64748b',
    textTransform: 'uppercase',
    letterSpacing: '0.04em',
    marginBottom: 8,
    marginTop: 8,
  },
  tableWrap: {
    border: '1px solid #e5e7eb',
    borderRadius: 8,
    overflow: 'hidden',
    marginBottom: 14,
  },
  table: { width: '100%', borderCollapse: 'collapse' },
  th: {
    padding: '8px 12px',
    textAlign: 'left',
    fontSize: 10,
    fontWeight: 600,
    color: '#64748b',
    textTransform: 'uppercase',
    letterSpacing: '0.04em',
    borderBottom: '1px solid #e5e7eb',
    background: '#f8fafc',
  },
  td: {
    padding: '8px 12px',
    fontSize: 12,
    color: '#1e293b',
    borderBottom: '1px solid #f1f5f9',
  },
  badge: {
    display: 'inline-block',
    padding: '2px 8px',
    borderRadius: 4,
    fontSize: 10,
    fontWeight: 600,
    textTransform: 'uppercase',
  },
  empSelectWrap: {
    border: '1px solid #e5e7eb',
    borderRadius: 8,
    marginBottom: 14,
    overflow: 'hidden',
  },
  empToolbar: {
    display: 'flex',
    gap: 8,
    padding: 10,
    background: '#f8fafc',
    borderBottom: '1px solid #e5e7eb',
  },
  empGrid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))',
    gap: 4,
    padding: 10,
    maxHeight: 160,
    overflowY: 'auto',
  },
  empItem: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    padding: '6px 10px',
    borderRadius: 6,
    cursor: 'pointer',
    fontSize: 12,
    background: '#fff',
    border: '1px solid #e5e7eb',
    transition: 'all 0.1s',
  },
  empItemSelected: {
    background: '#eff6ff',
    borderColor: '#bfdbfe',
  },
  empName: { color: '#1e293b' },
  empty: {
    textAlign: 'center',
    padding: '24px 12px',
    color: '#94a3b8',
    fontSize: 12,
    background: '#f8fafc',
    border: '1px dashed #e5e7eb',
    borderRadius: 8,
    marginBottom: 14,
  },
  hint: {
    fontSize: 11,
    color: '#64748b',
    padding: '10px 12px',
    background: '#fff7ed',
    border: '1px solid #fed7aa',
    borderRadius: 6,
  },
};
