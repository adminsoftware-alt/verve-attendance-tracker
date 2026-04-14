import { useState, useEffect, useCallback } from 'react';
import {
  fetchTeams, fetchTeamHolidays, addTeamHoliday, deleteTeamHoliday, updateTeamHoliday,
  fetchAllEmployeeLeave, addEmployeeLeave, deleteEmployeeLeave, addBulkEmployeeLeave, updateEmployeeLeave,
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

  // Edit state for holidays
  const [editingHolidayId, setEditingHolidayId] = useState(null);
  const [editHolidayDate, setEditHolidayDate] = useState('');
  const [editHolidayDesc, setEditHolidayDesc] = useState('');

  // Edit state for leave
  const [editingLeaveId, setEditingLeaveId] = useState(null);
  const [editLeaveDate, setEditLeaveDate] = useState('');
  const [editLeaveType, setEditLeaveType] = useState('leave');
  const [editLeaveDesc, setEditLeaveDesc] = useState('');

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
    if (!window.confirm('Delete this holiday?')) return;
    try {
      await deleteTeamHoliday(selectedTeamId, holidayId);
      await loadHolidays();
    } catch (e) {
      setErr(e.message);
    }
  };

  // Start editing a holiday
  const startEditHoliday = (h) => {
    setEditingHolidayId(h.holiday_id);
    setEditHolidayDate(h.date);
    setEditHolidayDesc(h.description || '');
  };

  // Cancel editing holiday
  const cancelEditHoliday = () => {
    setEditingHolidayId(null);
    setEditHolidayDate('');
    setEditHolidayDesc('');
  };

  // Save edited holiday
  const handleSaveHoliday = async (holidayId) => {
    if (!selectedTeamId) return;
    setSaving(true);
    setErr(null);
    try {
      await updateTeamHoliday(selectedTeamId, holidayId, editHolidayDate, editHolidayDesc);
      setEditingHolidayId(null);
      await loadHolidays();
    } catch (e) {
      setErr(e.message);
    }
    setSaving(false);
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
    if (!window.confirm('Delete this leave record?')) return;
    try {
      await deleteEmployeeLeave(employeeId, leaveId);
      await loadLeave();
    } catch (e) {
      setErr(e.message);
    }
  };

  // Start editing leave
  const startEditLeave = (l) => {
    setEditingLeaveId(l.leave_id);
    setEditLeaveDate(l.date);
    setEditLeaveType(l.leave_type || 'leave');
    setEditLeaveDesc(l.description || '');
  };

  // Cancel editing leave
  const cancelEditLeave = () => {
    setEditingLeaveId(null);
    setEditLeaveDate('');
    setEditLeaveType('leave');
    setEditLeaveDesc('');
  };

  // Save edited leave
  const handleSaveLeave = async (employeeId, leaveId) => {
    setSaving(true);
    setErr(null);
    try {
      await updateEmployeeLeave(employeeId, leaveId, editLeaveDate, editLeaveType, editLeaveDesc);
      setEditingLeaveId(null);
      await loadLeave();
    } catch (e) {
      setErr(e.message);
    }
    setSaving(false);
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

  const refresh = () => {
    if (tab === 'team' && selectedTeamId) loadHolidays();
    else if (tab === 'individual') { loadLeave(); loadEmployees(); }
  };

  return (
    <div style={s.container}>
      {/* Header — title left, team selector right (matches Team Attendance) */}
      <div style={s.header}>
        <div>
          <h2 style={s.title}>Holidays & Leave</h2>
        </div>
        <div style={s.headerControls}>
          <select
            value={selectedTeamId}
            onChange={e => setSelectedTeamId(e.target.value)}
            style={s.select}
          >
            <option value="">Select team</option>
            {teams.map(t => (
              <option key={t.team_id} value={t.team_id}>{t.team_name}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Date bar — year / month + add-holiday CTA + refresh on right */}
      <div style={s.dateBar}>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <select value={year} onChange={e => setYear(Number(e.target.value))} style={s.select}>
            {[2024, 2025, 2026, 2027].map(y => <option key={y} value={y}>{y}</option>)}
          </select>
          <select value={month} onChange={e => setMonth(Number(e.target.value))} style={s.select}>
            {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'].map((m, i) => (
              <option key={i} value={i + 1}>{m}</option>
            ))}
          </select>
        </div>
        <button onClick={refresh} disabled={loading} style={s.refreshBtn}>
          {loading ? 'Loading…' : 'Refresh'}
        </button>
      </div>

      {/* Subtitle — team + month range (matches "Accurest Client — 2026-04-01 to 2026-04-30") */}
      {selectedTeamId && (
        <div style={s.subtitle}>
          <strong style={{ color: '#1e293b' }}>{selectedTeam?.team_name || 'Team'}</strong>
          {` — ${monthNames[month - 1]} ${year}`}
        </div>
      )}

      {err && <div style={s.error}>{err}</div>}

      {/* Pill tabs — mirrors Hours Pivot / Isolation / Leaves */}
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

      {/* Legend bar — matches the Hours Pivot legend strip */}
      <div style={s.legendBar}>
        <span style={s.legendItem}>
          <span style={{ ...s.legendSwatch, background: '#fff7ed', borderColor: '#fed7aa' }} />
          Team Holiday
        </span>
        <span style={s.legendItem}>
          <span style={{ ...s.legendSwatch, background: '#eff6ff', borderColor: '#bfdbfe' }} />
          Individual Leave
        </span>
        <span style={s.legendMeta}>
          {tab === 'team'
            ? <>Holidays configured: <strong>{holidays.length}</strong></>
            : <>Leave records: <strong>{leaveList.length}</strong></>}
        </span>
      </div>

      <div style={s.mainContent}>
        {/* Calendar View */}
        <div style={s.calendarSection}>
          <div style={s.calendarHeader}>
            <h3 style={s.calendarTitle}>{monthNames[month - 1]} {year}</h3>
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

        {/* Content panel — tabs now live in the page header */}
        <div style={s.tabsSection}>
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
                              <th style={s.th}>Actions</th>
                            </tr>
                          </thead>
                          <tbody>
                            {holidays.map((h, i) => {
                              const isEditing = editingHolidayId === h.holiday_id;
                              const [y, m, d] = (isEditing ? editHolidayDate : h.date).split('-').map(Number);
                              const dow = new Date(y, m - 1, d).getDay();
                              const dayName = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][dow];
                              return (
                                <tr key={h.holiday_id} style={i % 2 === 0 ? s.trEven : {}}>
                                  <td style={s.td}>
                                    {isEditing ? (
                                      <input
                                        type="date"
                                        value={editHolidayDate}
                                        onChange={e => setEditHolidayDate(e.target.value)}
                                        style={s.editInput}
                                      />
                                    ) : (
                                      <strong>{h.date}</strong>
                                    )}
                                  </td>
                                  <td style={s.td}>{dayName}</td>
                                  <td style={s.td}>
                                    {isEditing ? (
                                      <input
                                        type="text"
                                        value={editHolidayDesc}
                                        onChange={e => setEditHolidayDesc(e.target.value)}
                                        placeholder="Description"
                                        style={{ ...s.editInput, width: '100%' }}
                                      />
                                    ) : (
                                      h.description || <span style={{ color: '#94a3b8' }}>—</span>
                                    )}
                                  </td>
                                  <td style={s.td}>
                                    {isEditing ? (
                                      <div style={s.actionBtns}>
                                        <button onClick={() => handleSaveHoliday(h.holiday_id)} disabled={saving} style={s.saveBtn}>
                                          {saving ? '...' : 'Save'}
                                        </button>
                                        <button onClick={cancelEditHoliday} style={s.cancelBtn}>Cancel</button>
                                      </div>
                                    ) : (
                                      <div style={s.actionBtns}>
                                        <button onClick={() => startEditHoliday(h)} style={s.editBtn}>Edit</button>
                                        <button onClick={() => handleDeleteHoliday(h.holiday_id)} style={s.deleteBtn}>Remove</button>
                                      </div>
                                    )}
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
                          <th style={s.th}>Actions</th>
                        </tr>
                      </thead>
                      <tbody>
                        {leaveList.map((l, i) => {
                          const isEditing = editingLeaveId === l.leave_id;
                          return (
                            <tr key={l.leave_id} style={i % 2 === 0 ? s.trEven : {}}>
                              <td style={s.td}>
                                {isEditing ? (
                                  <input
                                    type="date"
                                    value={editLeaveDate}
                                    onChange={e => setEditLeaveDate(e.target.value)}
                                    style={s.editInput}
                                  />
                                ) : (
                                  <strong>{l.date}</strong>
                                )}
                              </td>
                              <td style={s.td}>{l.employee_name}</td>
                              <td style={s.td}>
                                {isEditing ? (
                                  <select value={editLeaveType} onChange={e => setEditLeaveType(e.target.value)} style={s.editInput}>
                                    <option value="leave">Leave</option>
                                    <option value="sick">Sick</option>
                                    <option value="personal">Personal</option>
                                    <option value="wfh">WFH</option>
                                  </select>
                                ) : (
                                  <span style={{ ...s.badge, ...getLeaveTypeStyle(l.leave_type) }}>
                                    {l.leave_type || 'leave'}
                                  </span>
                                )}
                              </td>
                              <td style={s.td}>
                                {isEditing ? (
                                  <input
                                    type="text"
                                    value={editLeaveDesc}
                                    onChange={e => setEditLeaveDesc(e.target.value)}
                                    placeholder="Note"
                                    style={{ ...s.editInput, width: '100%' }}
                                  />
                                ) : (
                                  l.description || <span style={{ color: '#94a3b8' }}>—</span>
                                )}
                              </td>
                              <td style={s.td}>
                                {isEditing ? (
                                  <div style={s.actionBtns}>
                                    <button onClick={() => handleSaveLeave(l.employee_id, l.leave_id)} disabled={saving} style={s.saveBtn}>
                                      {saving ? '...' : 'Save'}
                                    </button>
                                    <button onClick={cancelEditLeave} style={s.cancelBtn}>Cancel</button>
                                  </div>
                                ) : (
                                  <div style={s.actionBtns}>
                                    <button onClick={() => startEditLeave(l)} style={s.editBtn}>Edit</button>
                                    <button onClick={() => handleDeleteLeave(l.employee_id, l.leave_id)} style={s.deleteBtn}>Remove</button>
                                  </div>
                                )}
                              </td>
                            </tr>
                          );
                        })}
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
  container: { maxWidth: 1400, margin: '0 auto' },

  // Header row — title left, team selector right (Team Attendance layout)
  header: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 16, flexWrap: 'wrap', gap: 12 },
  title: { fontSize: 22, fontWeight: 800, color: '#0f172a', margin: 0 },
  headerControls: { display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' },

  // Date bar — year / month + actions (left) + refresh (right)
  dateBar: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12, flexWrap: 'wrap', gap: 10 },
  subtitle: { marginBottom: 14, fontSize: 13, color: '#64748b' },
  refreshBtn: { padding: '8px 16px', background: '#f1f5f9', color: '#475569', border: '1px solid #e5e7eb', borderRadius: 8, fontSize: 12, cursor: 'pointer' },

  // Legacy leftover fields kept for compat (unused now but harmless)
  controls: { display: 'flex', gap: 10, alignItems: 'flex-end', marginBottom: 20, flexWrap: 'wrap' },
  controlGroup: { display: 'flex', flexDirection: 'column', gap: 4 },
  controlLabel: { fontSize: 10, fontWeight: 600, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.05em' },

  select: { padding: '8px 12px', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 13, background: '#fff', cursor: 'pointer', minWidth: 160 },

  error: { padding: '10px 14px', background: '#fef2f2', color: '#dc2626', border: '1px solid #fecaca', borderRadius: 10, fontSize: 13, marginBottom: 16 },

  // Legend strip under tabs — mirrors Hours Pivot legend
  legendBar: { display: 'flex', gap: 16, alignItems: 'center', flexWrap: 'wrap', marginBottom: 12, padding: '8px 12px', background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 8 },
  legendSwatch: { width: 14, height: 14, border: '1px solid', borderRadius: 3, display: 'inline-block', marginRight: 6, verticalAlign: 'middle' },
  legendMeta: { fontSize: 11, color: '#64748b', marginLeft: 'auto' },

  mainContent: { display: 'grid', gridTemplateColumns: '340px 1fr', gap: 20, alignItems: 'start' },

  // Calendar panel — card shell identical to TeamView tableWrap
  calendarSection: { background: '#fff', borderRadius: 12, border: '1px solid #e5e7eb', padding: 16 },
  calendarHeader: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 },
  calendarTitle: { margin: 0, fontSize: 14, fontWeight: 700, color: '#1e293b' },
  legend: { display: 'flex', gap: 12, fontSize: 10, color: '#64748b' },
  legendItem: { display: 'flex', alignItems: 'center', gap: 4 },
  legendDot: { width: 8, height: 8, borderRadius: '50%' },
  calendar: { display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: 2 },
  calDayHeader: { textAlign: 'center', fontSize: 10, fontWeight: 600, color: '#94a3b8', padding: '6px 0', textTransform: 'uppercase', letterSpacing: '0.05em' },
  calEmpty: { aspectRatio: '1' },
  calDay: { aspectRatio: '1', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', borderRadius: 8, background: '#fafbfc', cursor: 'pointer', position: 'relative', transition: 'all 0.15s' },
  calDayNum: { fontSize: 12, fontWeight: 500, color: '#475569' },
  calDayToday: { border: '2px solid #0f172a' },
  calDayWeekend: { background: '#f1f5f9' },
  calDayHoliday: { background: '#fff7ed' },
  calDayLeave: { background: '#eff6ff' },
  calDot: { width: 5, height: 5, borderRadius: '50%', background: '#f97316', marginTop: 2 },
  tooltip: { position: 'absolute', bottom: '100%', left: '50%', transform: 'translateX(-50%)', background: '#0f172a', color: '#fff', padding: '8px 10px', borderRadius: 8, fontSize: 11, minWidth: 160, zIndex: 100, marginBottom: 6, boxShadow: '0 4px 12px rgba(0,0,0,0.2)' },
  tooltipItem: { marginBottom: 6, lineHeight: 1.4 },
  tooltipBadge: { display: 'inline-block', padding: '2px 8px', borderRadius: 10, fontSize: 9, fontWeight: 600, textTransform: 'uppercase', marginRight: 6 },
  tooltipSub: { fontSize: 10, color: '#94a3b8', marginTop: 2 },

  // Tabs — pill bar matching Hours Pivot / Isolation / Leaves
  tabs: { display: 'flex', gap: 4, background: '#f1f5f9', padding: 3, borderRadius: 8, marginBottom: 12, width: 'fit-content' },
  tab: { padding: '8px 18px', background: 'transparent', border: 'none', borderRadius: 6, fontSize: 12, fontWeight: 500, color: '#64748b', cursor: 'pointer', transition: 'all 0.15s' },
  tabActive: { background: '#0f172a', color: '#fff', fontWeight: 600 },
  tabsSection: {},
  tabContent: {},

  // Add-holiday / add-leave form
  addForm: { display: 'flex', gap: 10, alignItems: 'flex-end', marginBottom: 16, padding: 14, background: '#fafbfc', border: '1px solid #e5e7eb', borderRadius: 12, flexWrap: 'wrap' },
  formGroup: { display: 'flex', flexDirection: 'column', gap: 4 },
  label: { fontSize: 10, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 600 },
  input: { padding: '8px 12px', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 13, background: '#fff' },

  // Buttons — match TeamView CTAs
  addBtn: { padding: '8px 18px', background: '#f97316', color: '#fff', border: 'none', borderRadius: 8, fontSize: 13, fontWeight: 700, cursor: 'pointer', whiteSpace: 'nowrap', boxShadow: '0 2px 4px rgba(249,115,22,0.35)' },
  deleteBtn: { padding: '4px 10px', background: '#fef2f2', color: '#b91c1c', border: '1px solid #fecaca', borderRadius: 6, fontSize: 11, fontWeight: 600, cursor: 'pointer' },
  editBtn: { padding: '4px 10px', background: '#eff6ff', color: '#2563eb', border: '1px solid #bfdbfe', borderRadius: 6, fontSize: 11, fontWeight: 600, cursor: 'pointer' },
  saveBtn: { padding: '4px 10px', background: '#f0fdf4', color: '#16a34a', border: '1px solid #bbf7d0', borderRadius: 6, fontSize: 11, fontWeight: 600, cursor: 'pointer' },
  cancelBtn: { padding: '4px 10px', background: '#f1f5f9', color: '#64748b', border: '1px solid #e2e8f0', borderRadius: 6, fontSize: 11, fontWeight: 600, cursor: 'pointer' },
  actionBtns: { display: 'flex', gap: 6, flexWrap: 'nowrap' },
  editInput: { padding: '6px 10px', border: '1px solid #d1d5db', borderRadius: 6, fontSize: 13, background: '#fff' },
  selectAllBtn: { padding: '6px 12px', background: '#eff6ff', color: '#2563eb', border: '1px solid #bfdbfe', borderRadius: 6, fontSize: 11, fontWeight: 600, cursor: 'pointer' },

  // Section headings — match TeamView sectionTitle
  sectionTitle: { fontSize: 14, fontWeight: 700, color: '#1e293b', margin: '4px 0 10px' },

  // Tables — match TeamView table treatment (padding, radius, fonts)
  tableWrap: { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 12, overflow: 'auto', marginBottom: 14 },
  table: { width: '100%', borderCollapse: 'collapse' },
  th: { padding: '10px 14px', textAlign: 'left', fontSize: 11, fontWeight: 600, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.05em', borderBottom: '1px solid #e5e7eb', background: '#f8fafc', whiteSpace: 'nowrap' },
  td: { padding: '10px 14px', fontSize: 13, color: '#1e293b', borderBottom: '1px solid #f1f5f9' },
  trEven: { background: '#fafbfc' },
  badge: { display: 'inline-block', padding: '3px 10px', borderRadius: 12, fontSize: 11, fontWeight: 600, textTransform: 'capitalize' },

  // Employee picker
  empSelectWrap: { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 12, marginBottom: 14, overflow: 'hidden' },
  empToolbar: { display: 'flex', gap: 8, padding: 10, background: '#f8fafc', borderBottom: '1px solid #e5e7eb' },
  empGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 6, padding: 12, maxHeight: 180, overflowY: 'auto' },
  empItem: { display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px', borderRadius: 8, cursor: 'pointer', fontSize: 13, background: '#fff', border: '1px solid #e5e7eb', transition: 'all 0.1s' },
  empItemSelected: { background: '#eff6ff', borderColor: '#bfdbfe' },
  empName: { color: '#1e293b' },

  // Empty / hint states
  empty: { textAlign: 'center', padding: '40px 20px', color: '#94a3b8', fontSize: 14, marginBottom: 14 },
  hint: { fontSize: 12, color: '#92400e', padding: '10px 14px', background: '#fff7ed', border: '1px solid #fed7aa', borderRadius: 10 },
};
