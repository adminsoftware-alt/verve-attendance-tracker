import { useState, useEffect, useMemo, useCallback, Fragment } from 'react';
import {
  fetchTeams,
  fetchEmployees,
  createEmployee,
  updateEmployee,
  fetchTeamMonthlyReport,
  fetchEmployeeDetail,
  fetchUnrecognized,
  fetchUnrecognizedMonthly,
  fetchClassifiedMonthly,
  addTeamMember,
  splitSharedAttendance,
  assignUnrecognizedAttendance,
} from '../utils/zoomApi';
import { exportRowsCsv } from '../utils/exportCsv';

function istDate() {
  const now = new Date();
  return new Date(now.getTime() + 330 * 60000).toISOString().slice(0, 10);
}

// Categories shown in the Add flow. "employee" is created via the Teams tab
// (team-member add flow), so it's excluded here.
const ADD_CATEGORIES = [
  { value: 'visitor', label: 'Visitor' },
  { value: 'vendor',  label: 'Vendor'  },
  { value: 'interview',   label: 'Interview'   },
  { value: 'other',   label: 'Other'   },
];

const ALL_CATEGORIES = [
  { value: 'all',      label: 'All' },
  { value: 'employee', label: 'Employee' },
  { value: 'visitor',  label: 'Visitor' },
  { value: 'vendor',   label: 'Vendor' },
  { value: 'interview',    label: 'Interview' },
  { value: 'other',    label: 'Other' },
];

function catBadgeStyle(cat) {
  switch (cat) {
    case 'employee': return { background: '#dcfce7', color: '#15803d' };
    case 'visitor':  return { background: '#dbeafe', color: '#1d4ed8' };
    case 'vendor':   return { background: '#fef3c7', color: '#92400e' };
    case 'interview':   return { background: '#e0e7ff', color: '#3730a3' };
    case 'other':    return { background: '#f3e8ff', color: '#7c3aed' };
    default:         return { background: '#f1f5f9', color: '#475569' };
  }
}

function fmtMins(m) {
  if (!m) return '-';
  const h = Math.floor(m / 60);
  const min = m % 60;
  if (h > 0 && min > 0) return `${h}hr ${min}min`;
  if (h > 0) return `${h}hr`;
  return `${min}min`;
}

function fmtHours(m) {
  if (!m) return '0min';
  return fmtMins(Math.round(m));
}

function attendanceStatusStyle(status) {
  switch ((status || '').toLowerCase()) {
    case 'present': return { background: '#dcfce7', color: '#15803d' };
    case 'half day':
    case 'half_day': return { background: '#fef3c7', color: '#92400e' };
    case 'leave': return { background: '#dbeafe', color: '#1d4ed8' };
    case 'manual override': return { background: '#ede9fe', color: '#6d28d9' };
    case 'absent':
    default: return { background: '#fee2e2', color: '#b91c1c' };
  }
}

export default function Employees({ user }) {
  // Top-level view toggle
  const [mainView, setMainView] = useState('members'); // 'members' | 'classified' | 'unrecognized'

  const [teams, setTeams] = useState([]);
  const [selectedTeam, setSelectedTeam] = useState('');
  const [year, setYear] = useState(new Date().getFullYear());
  const [month, setMonth] = useState(new Date().getMonth() + 1);
  const [categoryFilter, setCategoryFilter] = useState('all');
  const [search, setSearch] = useState('');

  const [registryEmployees, setRegistryEmployees] = useState([]);
  const [monthlyData, setMonthlyData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [showAddModal, setShowAddModal] = useState(false);
  const [expandedId, setExpandedId] = useState(null);
  const [detailCache, setDetailCache] = useState({});

  const isManager = user?.role === 'manager';

  // Load teams once; auto-select first
  useEffect(() => {
    fetchTeams().then(d => {
      let list = d.teams || [];
      if (isManager && user?.name) {
        list = list.filter(t =>
          (t.manager_name || '').toLowerCase().trim() === user.name.toLowerCase().trim()
        );
      }
      setTeams(list);
      if (list.length > 0) setSelectedTeam(list[0].team_id);
    }).catch(e => setError(e.message));
  }, [isManager, user?.name]);

  // Load registry + monthly attendance for the selected team/month.
  // When selectedTeam === '__all__' we fetch every team's monthly data in
  // parallel and merge their member_summary arrays so the view shows every
  // employee across every team.
  const loadData = useCallback(async () => {
    if (!selectedTeam) return;
    setLoading(true);
    setError(null);
    setExpandedId(null);
    setDetailCache({});
    try {
      if (selectedTeam === '__all__') {
        if (!teams.length) {
          setRegistryEmployees([]);
          setMonthlyData(null);
          setLoading(false);
          return;
        }
        const [empRes, ...monthlyResults] = await Promise.all([
          fetchEmployees({}),
          ...teams.map(t =>
            fetchTeamMonthlyReport(t.team_id, year, month).catch(e => {
              console.warn(`monthly fetch failed for ${t.team_id}:`, e);
              return { member_summary: [] };
            })
          ),
        ]);
        const mergedByKey = {};
        monthlyResults.forEach(res => {
          (res?.member_summary || []).forEach(m => {
            const key = (m.name || '').toLowerCase().trim();
            if (!key) return;
            if (!mergedByKey[key]) {
              mergedByKey[key] = { ...m };
            } else {
              mergedByKey[key].days_present = Math.max(
                mergedByKey[key].days_present || 0, m.days_present || 0
              );
              mergedByKey[key].total_active_mins =
                (mergedByKey[key].total_active_mins || 0) + (m.total_active_mins || 0);
              mergedByKey[key].total_break_mins =
                (mergedByKey[key].total_break_mins || 0) + (m.total_break_mins || 0);
              mergedByKey[key].total_isolation_mins =
                (mergedByKey[key].total_isolation_mins || 0) + (m.total_isolation_mins || 0);
            }
          });
        });
        setRegistryEmployees(empRes.employees || []);
        setMonthlyData({ member_summary: Object.values(mergedByKey) });
      } else {
        const [empRes, monthlyRes] = await Promise.all([
          fetchEmployees({ team_id: selectedTeam }),
          fetchTeamMonthlyReport(selectedTeam, year, month),
        ]);
        setRegistryEmployees(empRes.employees || []);
        setMonthlyData(monthlyRes);
      }
    } catch (e) {
      setError(e.message);
    }
    setLoading(false);
  }, [selectedTeam, year, month, teams]);

  useEffect(() => { loadData(); }, [loadData]);

  // Merge registry rows with monthly attendance summary by name
  const rows = useMemo(() => {
    if (!registryEmployees.length) return [];

    const summaryByName = {};
    (monthlyData?.member_summary || []).forEach(m => {
      summaryByName[(m.name || '').toLowerCase().trim()] = m;
    });

    return registryEmployees
      .filter(e => {
        if (categoryFilter !== 'all' && e.category !== categoryFilter) return false;
        if (search) {
          const q = search.toLowerCase();
          const hay = `${e.participant_name || ''} ${e.participant_email || ''}`.toLowerCase();
          if (!hay.includes(q)) return false;
        }
        return true;
      })
      .map(e => {
        const key = (e.participant_name || '').toLowerCase().trim();
        const summary = summaryByName[key] || {};
        return {
          ...e,
          days_present: summary.days_present || 0,
          total_active_mins: summary.total_active_mins || 0,
          total_break_mins: summary.total_break_mins || 0,
          total_isolation_mins: summary.total_isolation_mins || 0,
        };
      })
      .sort((a, b) => {
        // Employees first, then by hours descending
        if (a.category !== b.category) {
          if (a.category === 'employee') return -1;
          if (b.category === 'employee') return 1;
        }
        return (b.total_active_mins || 0) - (a.total_active_mins || 0);
      });
  }, [registryEmployees, monthlyData, categoryFilter, search]);

  // Counts for category filter chips
  const counts = useMemo(() => {
    const c = { all: registryEmployees.length, employee: 0, visitor: 0, vendor: 0, other: 0, interview: 0 };
    registryEmployees.forEach(e => {
      if (c[e.category] !== undefined) c[e.category]++;
    });
    return c;
  }, [registryEmployees]);

  const teamTotals = useMemo(() => {
    const activeEmps = rows.filter(r => r.days_present > 0);
    const totalMins = activeEmps.reduce((s, r) => s + (r.total_active_mins || 0), 0);
    return {
      total: rows.length,
      active: activeEmps.length,
      absent: rows.length - activeEmps.length,
      totalHours: totalMins / 60,
    };
  }, [rows]);

  const toggleExpand = async (emp) => {
    if (expandedId === emp.employee_id) {
      setExpandedId(null);
      return;
    }
    setExpandedId(emp.employee_id);
    if (!detailCache[emp.employee_id]) {
      try {
        const yearMonth = `${year}-${String(month).padStart(2, '0')}`;
        const detail = await fetchEmployeeDetail(emp.employee_id, yearMonth);
        setDetailCache(prev => ({ ...prev, [emp.employee_id]: detail }));
      } catch (e) {
        console.error('Failed to load employee detail:', e);
      }
    }
  };

  const handleAdd = async (form) => {
    await createEmployee({
      participant_name: form.name,
      participant_email: form.email || '',
      category: form.category,
      team_id: form.team_id || null,
    });
    setShowAddModal(false);
    await loadData();
  };

  const handleReassign = async (empId, newTeamId) => {
    try {
      await updateEmployee(empId, { team_id: newTeamId || null });
      await loadData();
    } catch (e) {
      setError(e.message);
    }
  };

  const selectedTeamName = selectedTeam === '__all__'
    ? 'All Teams'
    : (teams.find(t => t.team_id === selectedTeam)?.team_name || '');

  return (
    <div>
      {/* Header */}
      <div style={s.header}>
        <div>
          <h2 style={s.title}>Employees</h2>
          <div style={s.subtitle}>Team members and visitors with monthly attendance</div>
        </div>
        <button onClick={() => setShowAddModal(true)} style={s.addBtn}>
          + Add Visitor / Vendor  / Interview
        </button>
      </div>

      {error && <div style={s.error}>{error}</div>}

      {/* Main view toggle: Members / Visitors & Others / Unrecognized */}
      <div style={s.mainToggle}>
        <button
          onClick={() => setMainView('members')}
          style={{ ...s.mainToggleBtn, ...(mainView === 'members' ? s.mainToggleBtnOn : {}) }}
        >
          Team Members
        </button>
        <button
          onClick={() => setMainView('classified')}
          style={{ ...s.mainToggleBtn, ...(mainView === 'classified' ? s.mainToggleBtnOn : {}) }}
        >
          Visitors & Others
        </button>
        <button
          onClick={() => setMainView('unrecognized')}
          style={{ ...s.mainToggleBtn, ...(mainView === 'unrecognized' ? s.mainToggleBtnOn : {}) }}
        >
          Unrecognized Participants
        </button>
      </div>

      {mainView === 'classified' && (
        <ClassifiedPanel teams={teams} />
      )}

      {mainView === 'unrecognized' && (
        <UnrecognizedPanel
          teams={teams}
          onClassified={() => loadData()}
        />
      )}

      {mainView === 'members' && (
      <>
      {/* Control bar */}
      <div style={s.controlBar}>
        <div style={s.controlGroup}>
          <label style={s.label}>Team</label>
          <select value={selectedTeam} onChange={e => setSelectedTeam(e.target.value)} style={s.select}>
            <option value="">Select team</option>
            <option value="__all__">All Teams (every member)</option>
            {teams.map(t => <option key={t.team_id} value={t.team_id}>{t.team_name}</option>)}
          </select>
        </div>
        <div style={s.controlGroup}>
          <label style={s.label}>Year</label>
          <select value={year} onChange={e => setYear(+e.target.value)} style={s.select}>
            {[2024, 2025, 2026].map(y => <option key={y} value={y}>{y}</option>)}
          </select>
        </div>
        <div style={s.controlGroup}>
          <label style={s.label}>Month</label>
          <select value={month} onChange={e => setMonth(+e.target.value)} style={s.select}>
            {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'].map((m, i) => (
              <option key={i} value={i + 1}>{m}</option>
            ))}
          </select>
        </div>
        <div style={{ ...s.controlGroup, flex: 1, minWidth: 180 }}>
          <label style={s.label}>Search</label>
          <input
            type="text"
            placeholder="Name or email"
            value={search}
            onChange={e => setSearch(e.target.value)}
            style={s.input}
          />
        </div>
        <div style={s.controlGroup}>
          <label style={s.label}>&nbsp;</label>
          <button
            onClick={() => {
              if (!rows.length) return;
              const headers = ['Name', 'Email', 'Category', 'Team', 'Days Present', 'Total Active (min)', 'Break (min)', 'Isolation (min)'];
              const csvRows = rows.map(e => [
                e.participant_name || e.display_name || '',
                e.participant_email || '',
                e.category || 'employee',
                teams.find(t => t.team_id === e.team_id)?.team_name || '',
                e.days_present || 0,
                e.total_active_mins || 0,
                e.total_break_mins || 0,
                e.total_isolation_mins || 0,
              ]);
              exportRowsCsv(`employees_${selectedTeamName.replace(/\s+/g,'_')}_${year}-${String(month).padStart(2,'0')}.csv`, headers, csvRows);
            }}
            style={s.refreshBtn}
            disabled={!rows.length}
          >
            Export CSV
          </button>
        </div>
      </div>

      {/* Category chips */}
      <div style={s.chipRow}>
        {ALL_CATEGORIES.map(c => (
          <button
            key={c.value}
            onClick={() => setCategoryFilter(c.value)}
            style={{
              ...s.chip,
              ...(categoryFilter === c.value ? s.chipOn : {}),
            }}
          >
            {c.label}
            <span style={{
              ...s.chipCount,
              background: categoryFilter === c.value ? 'rgba(255,255,255,0.25)' : '#f1f5f9',
              color: categoryFilter === c.value ? '#fff' : '#64748b',
            }}>
              {counts[c.value] ?? 0}
            </span>
          </button>
        ))}
      </div>

      {/* Stats */}
      {selectedTeam && !loading && (
        <div style={s.statsRow}>
          <Stat label="Total" value={teamTotals.total} color="#3b82f6" />
          <Stat label="Active This Month" value={teamTotals.active} color="#10b981" />
          <Stat label="Absent" value={teamTotals.absent} color="#ef4444" />
          <Stat label="Total Hours" value={`${teamTotals.totalHours.toFixed(0)}h`} color="#f97316" />
        </div>
      )}

      {!selectedTeam && !loading && <div style={s.empty}>Select a team to view its members</div>}

      {loading && <div style={s.loader}>Loading employees...</div>}

      {/* Table */}
      {selectedTeam && !loading && rows.length > 0 && (
        <div style={s.tableWrap}>
          <table style={s.table}>
            <thead>
              <tr>
                <th style={s.th}></th>
                <th style={s.th}>Name</th>
                <th style={s.th}>Category</th>
                <th style={s.th}>Days Present</th>
                <th style={s.th}>Total Hours</th>
                <th style={s.th}>Avg / Day</th>
                <th style={s.th}>Break</th>
                <th style={s.th}>Isolation</th>
                <th style={s.th}>Team</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((e) => {
                const isOpen = expandedId === e.employee_id;
                const avgMins = e.days_present > 0 ? Math.round(e.total_active_mins / e.days_present) : 0;
                return (
                  <Fragment key={e.employee_id}>
                    <tr
                      onClick={() => toggleExpand(e)}
                      style={{ cursor: 'pointer', ...(isOpen ? { background: '#eff6ff' } : {}) }}
                    >
                      <td style={s.td}>
                        <span style={{ color: '#64748b', fontSize: 11 }}>{isOpen ? '▼' : '▶'}</span>
                      </td>
                      <td style={s.td}>
                        <div style={{ fontWeight: 600 }}>{e.participant_name || e.display_name}</div>
                        {e.participant_email && (
                          <div style={{ fontSize: 11, color: '#64748b' }}>{e.participant_email}</div>
                        )}
                      </td>
                      <td style={s.td}>
                        <span style={{ ...s.badge, ...catBadgeStyle(e.category) }}>
                          {e.category || 'employee'}
                        </span>
                      </td>
                      <td style={{ ...s.td, fontWeight: 600 }}>{e.days_present || 0}</td>
                      <td style={{ ...s.td, color: '#10b981', fontWeight: 700 }}>
                        {fmtHours(e.total_active_mins)}
                      </td>
                      <td style={s.td}>{fmtMins(avgMins)}</td>
                      <td style={{ ...s.td, color: '#f97316' }}>{fmtMins(e.total_break_mins)}</td>
                      <td style={{ ...s.td, color: (e.total_isolation_mins || 0) > 120 ? '#ef4444' : '#64748b' }}>
                        {fmtMins(e.total_isolation_mins)}
                      </td>
                      <td style={s.td} onClick={(ev) => ev.stopPropagation()}>
                        <select
                          value={e.team_id || ''}
                          onChange={(ev) => handleReassign(e.employee_id, ev.target.value)}
                          style={s.teamSelect}
                          title="Allocate to team"
                        >
                          <option value="">Unassigned</option>
                          {teams.map(t => (
                            <option key={t.team_id} value={t.team_id}>{t.team_name}</option>
                          ))}
                        </select>
                      </td>
                    </tr>
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {selectedTeam && !loading && rows.length === 0 && (
        <div style={s.empty}>
          No {categoryFilter === 'all' ? 'members' : categoryFilter + 's'} in <strong>{selectedTeamName}</strong>.
        </div>
      )}
      </>
      )}

      {showAddModal && (
        <AddModal
          teams={teams}
          defaultTeamId={selectedTeam}
          onClose={() => setShowAddModal(false)}
          onSave={handleAdd}
        />
      )}

      {expandedId && (
        <EmployeeDetailDrawer
          employee={rows.find(r => r.employee_id === expandedId) || registryEmployees.find(r => r.employee_id === expandedId)}
          detail={detailCache[expandedId]}
          year={year}
          month={month}
          onClose={() => setExpandedId(null)}
        />
      )}
    </div>
  );
}

// ─── Employee detail drawer (row click) ──────────────────
// Right-side overlay showing one employee's monthly activity:
//   - summary stat cards
//   - color-coded monthly calendar
//   - full daily breakdown table
function EmployeeDetailDrawer({ employee, detail, year, month, onClose }) {
  if (!employee) return null;

  // Handle ESC to close
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const daily = detail?.daily || [];
  const byDate = {};
  daily.forEach(d => { if (d?.date) byDate[d.date] = d; });

  const daysInMonth = new Date(year, month, 0).getDate();
  const firstDow = new Date(year, month - 1, 1).getDay();
  const cells = [];
  for (let i = 0; i < firstDow; i++) cells.push(null);
  for (let d = 1; d <= daysInMonth; d++) cells.push(d);

  const totals = daily.reduce((acc, d) => {
    acc.active += d.active_minutes || 0;
    acc.break += d.break_minutes || 0;
    acc.iso += d.isolation_minutes || 0;
    if ((d.active_minutes || 0) > 0) acc.days += 1;
    return acc;
  }, { active: 0, break: 0, iso: 0, days: 0 });
  const avg = totals.days > 0 ? Math.round(totals.active / totals.days) : 0;

  const statusColor = (day) => {
    const row = byDate[`${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`];
    if (!row) return { bg: '#f8fafc', fg: '#94a3b8' };
    const s = (row.status || '').toLowerCase();
    if (s === 'present') return { bg: '#dcfce7', fg: '#15803d' };
    if (s === 'half day' || s === 'half_day') return { bg: '#fef3c7', fg: '#92400e' };
    if (s === 'leave') return { bg: '#dbeafe', fg: '#1d4ed8' };
    return { bg: '#fee2e2', fg: '#b91c1c' };
  };

  const monthLabel = ['January','February','March','April','May','June','July','August','September','October','November','December'][month - 1];
  const exportEmployee = () => {
    if (!daily.length) return;
    const headers = ['Date', 'First Seen', 'Last Seen', 'Active (min)', 'Break (min)', 'Isolation (min)', 'Status'];
    const csvRows = daily.map(d => [
      d.date, d.first_seen_ist || '', d.last_seen_ist || '',
      d.active_minutes || 0, d.break_minutes || 0, d.isolation_minutes || 0, d.status || '',
    ]);
    exportRowsCsv(
      `employee_${(employee.participant_name || 'employee').replace(/\s+/g, '_')}_${year}-${String(month).padStart(2,'0')}.csv`,
      headers, csvRows
    );
  };

  return (
    <div style={s.drawerOverlay} onClick={onClose}>
      <div style={s.drawer} onClick={e => e.stopPropagation()}>
        <div style={s.drawerHeader}>
          <div>
            <div style={{ fontSize: 18, fontWeight: 700, color: '#0f172a' }}>
              {employee.participant_name || employee.display_name}
            </div>
            <div style={{ fontSize: 12, color: '#64748b', marginTop: 2 }}>
              {employee.participant_email || '—'} · {monthLabel} {year}
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button onClick={exportEmployee} style={s.refreshBtn} disabled={!daily.length}>Export CSV</button>
            <button onClick={onClose} style={s.modalClose}>×</button>
          </div>
        </div>

        <div style={s.drawerBody}>
          {!detail ? (
            <div style={{ padding: 24, color: '#94a3b8' }}>Loading attendance…</div>
          ) : (
            <>
              {/* Summary cards */}
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))', gap: 10, marginBottom: 16 }}>
                <MiniStat label="Days Present" value={totals.days} color="#10b981" />
                <MiniStat label="Total Hours" value={fmtHours(totals.active)} color="#3b82f6" />
                <MiniStat label="Avg / Day" value={fmtMins(avg)} color="#6366f1" />
                <MiniStat label="Break" value={fmtMins(totals.break)} color="#f97316" />
                <MiniStat label="Isolation" value={fmtMins(totals.iso)} color="#ef4444" />
              </div>

              {/* Monthly calendar */}
              <div style={{ fontSize: 11, fontWeight: 700, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8 }}>
                {monthLabel} {year}
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: 4, marginBottom: 6 }}>
                {['Sun','Mon','Tue','Wed','Thu','Fri','Sat'].map(d => (
                  <div key={d} style={{ fontSize: 10, fontWeight: 600, color: '#94a3b8', textAlign: 'center', padding: '4px 0', textTransform: 'uppercase' }}>{d}</div>
                ))}
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: 4, marginBottom: 16 }}>
                {cells.map((day, i) => {
                  if (day === null) return <div key={`e${i}`} style={{ aspectRatio: '1' }} />;
                  const row = byDate[`${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`];
                  const { bg, fg } = statusColor(day);
                  const hours = row ? ((row.active_minutes || 0) / 60).toFixed(1) : null;
                  const title = row
                    ? `${row.date}: ${row.status || '-'} · ${fmtMins(row.active_minutes)}${row.first_seen_ist ? ` (${row.first_seen_ist}–${row.last_seen_ist})` : ''}`
                    : `${year}-${String(month).padStart(2,'0')}-${String(day).padStart(2,'0')}: no data`;
                  return (
                    <div
                      key={day}
                      title={title}
                      style={{
                        aspectRatio: '1', background: bg, color: fg,
                        borderRadius: 6, display: 'flex', flexDirection: 'column',
                        alignItems: 'center', justifyContent: 'center',
                        fontSize: 12, fontWeight: 600,
                      }}
                    >
                      <div>{day}</div>
                      {hours && row?.active_minutes > 0 && (
                        <div style={{ fontSize: 9, fontWeight: 500, opacity: 0.85 }}>{hours}h</div>
                      )}
                    </div>
                  );
                })}
              </div>

              {/* Legend */}
              <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', fontSize: 10, color: '#64748b', marginBottom: 12 }}>
                {[
                  { bg: '#dcfce7', label: 'Present' },
                  { bg: '#fef3c7', label: 'Half Day' },
                  { bg: '#fee2e2', label: 'Absent' },
                  { bg: '#dbeafe', label: 'Leave' },
                  { bg: '#f8fafc', label: 'No data' },
                ].map(l => (
                  <span key={l.label} style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                    <span style={{ width: 12, height: 12, background: l.bg, borderRadius: 3, border: '1px solid #e2e8f0' }} />
                    {l.label}
                  </span>
                ))}
              </div>

              {/* Daily table */}
              <DailyBreakdown detail={detail} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function MiniStat({ label, value, color }) {
  return (
    <div style={{ background: '#fff', border: '1px solid #e5e7eb', borderRadius: 10, padding: '10px 12px' }}>
      <div style={{ fontSize: 9, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 600, marginBottom: 2 }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 800, color, lineHeight: 1.1 }}>{value}</div>
    </div>
  );
}

// ─── Expanded daily-breakdown table ──────────────────────
function DailyBreakdown({ detail }) {
  const daily = detail?.daily || [];
  if (!daily.length) {
    return <div style={{ padding: 16, color: '#94a3b8', fontSize: 13 }}>No daily data for this month.</div>;
  }
  return (
    <div style={{ padding: '12px 20px', background: '#f8fafc' }}>
      <div style={{ fontSize: 11, color: '#64748b', marginBottom: 8, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
        Daily Breakdown
      </div>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
        <thead>
          <tr>
            <th style={detailTh}>Date</th>
            <th style={detailTh}>First Seen</th>
            <th style={detailTh}>Last Seen</th>
            <th style={detailTh}>Active</th>
            <th style={detailTh}>Break</th>
            <th style={detailTh}>Isolation</th>
            <th style={detailTh}>Status</th>
          </tr>
        </thead>
        <tbody>
          {daily.map((d, i) => (
            <tr key={i}>
              <td style={detailTd}>{d.date}</td>
              <td style={detailTd}>{d.first_seen_ist || '-'}</td>
              <td style={detailTd}>{d.last_seen_ist || '-'}</td>
              <td style={{ ...detailTd, color: '#10b981', fontWeight: 600 }}>{fmtMins(d.active_minutes)}</td>
              <td style={{ ...detailTd, color: '#f97316' }}>{fmtMins(d.break_minutes)}</td>
              <td style={{ ...detailTd, color: '#64748b' }}>{fmtMins(d.isolation_minutes)}</td>
              <td style={detailTd}>
                <span style={{ ...s.badge, ...attendanceStatusStyle(d.status || '-') }}>
                  {d.status || '-'}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── Unrecognized participants panel ─────────────────────
// Monthly view: aggregate attendance per person (days, hours, break,
// isolation) plus an expandable per-day breakdown mirroring the Team Members
// view. Each row can be classified as visitor/vendor/interview/other/employee.
function UnrecognizedPanel({ teams, onClassified }) {
  const [year, setYear] = useState(new Date().getFullYear());
  const [month, setMonth] = useState(new Date().getMonth() + 1);
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);
  const [expandedKey, setExpandedKey] = useState(null);
  // per-row classification form state, keyed by name_key
  const [rowState, setRowState] = useState({});
  const [savingRow, setSavingRow] = useState(null);
  // Bulk classify
  const [selected, setSelected] = useState(() => new Set());
  const [bulkCategory, setBulkCategory] = useState('visitor');
  const [bulkTeam, setBulkTeam] = useState('');
  const [bulkBusy, setBulkBusy] = useState(false);
  // Existing employee names — used as datalist options so HR can pick a known
  // person (avoids duplicate registry entries from typos) OR type a new name.
  const [registryNames, setRegistryNames] = useState([]);

  useEffect(() => {
    fetchEmployees({}).then(res => {
      const list = (res?.employees || [])
        .map(e => e.display_name || e.participant_name)
        .filter(Boolean);
      // Dedupe + sort
      setRegistryNames([...new Set(list)].sort((a, b) => a.localeCompare(b)));
    }).catch(() => {});
  }, []);

  // Helper: check if name contains "&" (shared session between 2+ people)
  const isSharedName = (name) => name && name.includes('&');

  // Helper: split "A & B & C" into N trimmed names (min length 2)
  const splitNames = (name) => {
    if (!name || !name.includes('&')) return [name];
    return name.split('&').map(s => s.trim()).filter(Boolean);
  };

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setExpandedKey(null);
    try {
      const res = await fetchUnrecognizedMonthly(year, month);
      const list = res.unrecognized || [];
      setItems(list);
      const initial = {};
      list.forEach(u => {
        const name = u.display_name || u.participant_name;
        if (isSharedName(name)) {
          // N-way shared session. Seed one split entry per "&"-delimited name.
          const parts = splitNames(name);
          // Ensure at least 2 split slots (some "A & " strings parse to 1 part).
          while (parts.length < 2) parts.push('');
          initial[u.name_key] = {
            isShared: true,
            splits: parts.map(n => ({ name: n, email: '', team_id: '' })),
            apply_attendance: true,
          };
        } else {
          initial[u.name_key] = {
            isShared: false,
            category: 'visitor',
            team_id: '',
            name: name,
            email: u.participant_email || '',
            assign_attendance: true,
          };
        }
      });
      setRowState(initial);
    } catch (e) {
      setErr(e.message);
    }
    setLoading(false);
  }, [year, month]);

  useEffect(() => { load(); }, [load]);

  const updateRow = (key, field, value) => {
    setRowState(prev => ({
      ...prev,
      [key]: { ...prev[key], [field]: value },
    }));
  };

  // Split-UI helpers (N-person shared sessions)
  const updateSplit = (key, idx, field, value) => {
    setRowState(prev => {
      const cur = prev[key] || {};
      const splits = [...(cur.splits || [])];
      splits[idx] = { ...(splits[idx] || {}), [field]: value };
      return { ...prev, [key]: { ...cur, splits } };
    });
  };
  const addSplit = (key) => {
    setRowState(prev => {
      const cur = prev[key] || {};
      const splits = [...(cur.splits || []), { name: '', email: '', team_id: '' }];
      return { ...prev, [key]: { ...cur, splits } };
    });
  };
  const removeSplit = (key, idx) => {
    setRowState(prev => {
      const cur = prev[key] || {};
      const splits = [...(cur.splits || [])];
      splits.splice(idx, 1);
      return { ...prev, [key]: { ...cur, splits } };
    });
  };

  const saveRow = async (item) => {
    const key = item.name_key;
    const state = rowState[key];
    if (!state) return;
    if (state.category === 'employee' && !state.team_id) {
      setErr('Select a team to add this person as an employee.');
      return;
    }
    setSavingRow(key);
    setErr(null);
    try {
      const targetName = state.name.trim() || item.participant_name;
      const targetEmail = state.email.trim();
      if (state.category === 'employee' && state.assign_attendance) {
        await assignUnrecognizedAttendance(
          item.participant_name,
          {
            name: targetName,
            email: targetEmail,
            team_id: state.team_id || '',
          },
          item.daily || [],
          true,
        );
      } else {
        await createEmployee({
          participant_name: targetName,
          participant_email: targetEmail,
          category: state.category,
          team_id: state.team_id || null,
        });
        if (state.category === 'employee' && state.team_id) {
          try {
            await addTeamMember(
              state.team_id,
              targetName,
              targetEmail,
            );
          } catch (e) {
            console.warn('addTeamMember failed:', e);
          }
        }
      }
      setItems(prev => prev.filter(x => x.name_key !== key));
      onClassified && onClassified();
    } catch (e) {
      setErr(e.message);
    }
    setSavingRow(null);
  };

  // Save split attendance for shared sessions of N people
  // (e.g., "A & B" or "A & B & C"). All selected employees get the same
  // daily attendance overrides.
  const saveSplitRow = async (item) => {
    const key = item.name_key;
    const state = rowState[key];
    if (!state) return;
    const splits = (state.splits || []).map(s => ({
      name: (s.name || '').trim(),
      email: (s.email || '').trim(),
      team_id: s.team_id || '',
    })).filter(s => s.name);

    if (splits.length < 2) {
      setErr('Enter at least 2 employee names for a shared session split.');
      return;
    }
    setSavingRow(key);
    setErr(null);
    try {
      await splitSharedAttendance(
        item.participant_name,
        splits,                               // N employees
        item.daily || [],                      // daily rows to copy
        state.apply_attendance !== false,      // apply attendance?
      );
      setItems(prev => prev.filter(x => x.name_key !== key));
      onClassified && onClassified();
    } catch (e) {
      setErr(e.message);
    }
    setSavingRow(null);
  };

  // Bulk classify selected rows with the chosen category/team. Shared-name
  // rows are skipped (they need the split flow, not a single category).
  const bulkApply = async () => {
    const keys = Array.from(selected);
    if (keys.length === 0) return;
    if (bulkCategory === 'employee' && !bulkTeam) {
      setErr('Pick a team before bulk-classifying as employee.');
      return;
    }
    setBulkBusy(true);
    setErr(null);
    const successful = [];
    for (const key of keys) {
      const item = items.find(x => x.name_key === key);
      if (!item) continue;
      const st = rowState[key] || {};
      if (st.isShared) continue;  // shared names skip bulk
      try {
        const targetName = (st.name || item.display_name || item.participant_name || '').trim();
        const targetEmail = (st.email || item.participant_email || '').trim();
        await createEmployee({
          participant_name: targetName,
          participant_email: targetEmail,
          category: bulkCategory,
          team_id: bulkTeam || null,
        });
        if (bulkCategory === 'employee' && bulkTeam) {
          try { await addTeamMember(bulkTeam, targetName, targetEmail); }
          catch (e) { console.warn('bulk addTeamMember failed:', e); }
        }
        successful.push(key);
      } catch (e) {
        console.warn(`bulk classify failed for ${key}:`, e);
      }
    }
    setItems(prev => prev.filter(x => !successful.includes(x.name_key)));
    setSelected(new Set());
    setBulkBusy(false);
    onClassified && onClassified();
  };

  const toggleAll = () => {
    const selectable = items.filter(p => !rowState[p.name_key]?.isShared).map(p => p.name_key);
    if (selected.size === selectable.length) setSelected(new Set());
    else setSelected(new Set(selectable));
  };

  const toggleOne = (key) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  };

  // CSV export of the currently loaded unrecognized rows
  const exportCsv = () => {
    if (!items.length) return;
    const headers = ['Name', 'Email', 'Days Present', 'Total Active (min)', 'Break (min)', 'Isolation (min)'];
    const rows = items.map(p => [
      p.display_name || p.participant_name || '',
      p.participant_email || '',
      p.days_present || 0,
      p.total_active_mins || 0,
      p.total_break_mins || 0,
      p.total_isolation_mins || 0,
    ]);
    exportRowsCsv(`unrecognized_${year}-${String(month).padStart(2,'0')}.csv`, headers, rows);
  };

  // Monthly aggregates shown in the header stat cards
  const totals = useMemo(() => {
    const count = items.length;
    const totalHours = items.reduce((sum, p) => sum + (p.total_active_mins || 0), 0) / 60;
    const totalBreak = items.reduce((sum, p) => sum + (p.total_break_mins || 0), 0);
    const totalIso = items.reduce((sum, p) => sum + (p.total_isolation_mins || 0), 0);
    return { count, totalHours, totalBreak, totalIso };
  }, [items]);

  return (
    <div>
      {/* Controls: year/month, refresh */}
      <div style={s.controlBar}>
        <div style={s.controlGroup}>
          <label style={s.label}>Year</label>
          <select value={year} onChange={e => setYear(+e.target.value)} style={s.select}>
            {[2024, 2025, 2026].map(y => <option key={y} value={y}>{y}</option>)}
          </select>
        </div>
        <div style={s.controlGroup}>
          <label style={s.label}>Month</label>
          <select value={month} onChange={e => setMonth(+e.target.value)} style={s.select}>
            {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'].map((m, i) => (
              <option key={i} value={i + 1}>{m}</option>
            ))}
          </select>
        </div>
        <div style={s.controlGroup}>
          <label style={s.label}>&nbsp;</label>
          <button onClick={load} style={s.refreshBtn} disabled={loading}>
            {loading ? 'Loading...' : 'Refresh'}
          </button>
        </div>
        <div style={s.controlGroup}>
          <label style={s.label}>&nbsp;</label>
          <button onClick={exportCsv} style={s.refreshBtn} disabled={!items.length}>Export CSV</button>
        </div>
        <div style={{ flex: 1, minWidth: 220, alignSelf: 'flex-end', fontSize: 12, color: '#64748b' }}>
          {loading
            ? ''
            : items.length > 0
              ? <>Found <strong>{items.length}</strong> unrecognized participant{items.length === 1 ? '' : 's'} this month.</>
              : 'No unrecognized participants — everyone is already classified.'}
        </div>
      </div>

      {err && <div style={s.error}>{err}</div>}

      {/* Bulk classify bar — appears when rows are selected */}
      {selected.size > 0 && (
        <div style={s.bulkBar}>
          <strong style={{ fontSize: 13 }}>{selected.size} selected</strong>
          <span style={{ fontSize: 11, color: '#94a3b8' }}>→ classify as</span>
          <select
            value={bulkCategory}
            onChange={e => setBulkCategory(e.target.value)}
            style={s.bulkSelect}
          >
            <option value="visitor">Visitor</option>
            <option value="vendor">Vendor</option>
            <option value="interview">Interview</option>
            <option value="other">Other (ignore)</option>
            <option value="employee">Employee</option>
          </select>
          {bulkCategory === 'employee' && (
            <select
              value={bulkTeam}
              onChange={e => setBulkTeam(e.target.value)}
              style={s.bulkSelect}
            >
              <option value="">— Pick team —</option>
              {teams.map(t => <option key={t.team_id} value={t.team_id}>{t.team_name}</option>)}
            </select>
          )}
          <button onClick={bulkApply} disabled={bulkBusy} style={s.bulkApply}>
            {bulkBusy ? 'Applying…' : 'Apply to selected'}
          </button>
          <button onClick={() => setSelected(new Set())} style={{ ...s.bulkSelect, cursor: 'pointer' }}>
            Clear
          </button>
        </div>
      )}

      {!loading && items.length > 0 && (
        <div style={s.statsRow}>
          <Stat label="Unrecognized" value={totals.count} color="#3b82f6" />
          <Stat label="Total Hours" value={`${totals.totalHours.toFixed(0)}h`} color="#10b981" />
          <Stat label="Total Break" value={fmtMins(totals.totalBreak)} color="#f97316" />
          <Stat label="Total Isolation" value={fmtMins(totals.totalIso)} color="#ef4444" />
        </div>
      )}

      {loading && <div style={s.loader}>Loading unrecognized participants...</div>}

      {/* Datalist of registered employee names — referenced by name inputs
          via list="employee-names-datalist" so HR can pick existing or type new. */}
      <datalist id="employee-names-datalist">
        {registryNames.map(n => <option key={n} value={n} />)}
      </datalist>

      {!loading && items.length > 0 && (
        <div style={s.tableWrap}>
          <table style={s.table}>
            <thead>
              <tr>
                <th style={s.th}>
                  <input
                    type="checkbox"
                    checked={
                      items.filter(p => !rowState[p.name_key]?.isShared).length > 0 &&
                      selected.size === items.filter(p => !rowState[p.name_key]?.isShared).length
                    }
                    onChange={toggleAll}
                    title="Select all (non-shared)"
                  />
                </th>
                <th style={s.th}></th>
                <th style={s.th}>Name</th>
                <th style={s.th}>Category</th>
                <th style={s.th}>Days Present</th>
                <th style={s.th}>Total Hours</th>
                <th style={s.th}>Avg / Day</th>
                <th style={s.th}>Break</th>
                <th style={s.th}>Isolation</th>
                <th style={s.th}>Classify As</th>
                <th style={s.th}>Team</th>
                <th style={s.th}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map((p, i) => {
                const key = p.name_key;
                const state = rowState[key] || {};
                const isOpen = expandedKey === key;
                const avgMins = p.days_present > 0 ? Math.round(p.total_active_mins / p.days_present) : 0;
                const isShared = state.isShared;
                return (
                  <Fragment key={key}>
                    <tr
                      onClick={() => setExpandedKey(isOpen ? null : key)}
                      style={{
                        cursor: 'pointer',
                        ...(i % 2 === 0 ? s.trEven : {}),
                        ...(isOpen ? { background: '#eff6ff' } : {}),
                        ...(isShared ? { background: '#fef3c7' } : {}),
                      }}
                    >
                      <td style={s.td} onClick={(ev) => ev.stopPropagation()}>
                        <input
                          type="checkbox"
                          checked={selected.has(key)}
                          onChange={() => toggleOne(key)}
                          disabled={isShared}
                          title={isShared ? 'Shared sessions need the Split flow' : 'Select for bulk classify'}
                        />
                      </td>
                      <td style={s.td}>
                        <span style={{ color: '#64748b', fontSize: 11 }}>{isOpen ? '▼' : '▶'}</span>
                      </td>
                      <td style={s.td}>
                        <div style={{ fontWeight: 600 }}>{p.display_name || p.participant_name}</div>
                        {isShared && (
                          <div style={{ fontSize: 10, color: '#92400e', fontWeight: 600 }}>
                            ⚠ Shared session — split to 2 employees
                          </div>
                        )}
                        {p.participant_name !== p.display_name && !isShared && (
                          <div style={{ fontSize: 10, color: '#94a3b8' }}>
                            zoom name: {p.participant_name}
                          </div>
                        )}
                        {p.participant_email && (
                          <div style={{ fontSize: 11, color: '#64748b' }}>{p.participant_email}</div>
                        )}
                      </td>
                      <td style={s.td}>
                        <span style={{ ...s.badge, background: isShared ? '#fef3c7' : '#f1f5f9', color: isShared ? '#92400e' : '#64748b' }}>
                          {isShared ? 'Shared' : 'Unclassified'}
                        </span>
                      </td>
                      <td style={{ ...s.td, fontWeight: 600 }}>{p.days_present || 0}</td>
                      <td style={{ ...s.td, color: '#10b981', fontWeight: 700 }}>
                        {fmtHours(p.total_active_mins)}
                      </td>
                      <td style={s.td}>{fmtMins(avgMins)}</td>
                      <td style={{ ...s.td, color: '#f97316' }}>{fmtMins(p.total_break_mins)}</td>
                      <td style={{ ...s.td, color: (p.total_isolation_mins || 0) > 120 ? '#ef4444' : '#64748b' }}>
                        {fmtMins(p.total_isolation_mins)}
                      </td>
                      {/* Shared sessions: N-person split UI (one block per "&"-delimited name).
                          Users can add/remove people so any N ≥ 2 is supported. */}
                      {isShared ? (
                        <>
                          <td style={s.td} onClick={(ev) => ev.stopPropagation()} colSpan={2}>
                            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                              {(state.splits || []).map((sp, idx) => (
                                <div key={idx} style={{ display: 'flex', gap: 6, alignItems: 'flex-start' }}>
                                  <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                                    <input
                                      type="text"
                                      placeholder={`Employee ${idx + 1} (pick or type new)`}
                                      value={sp.name || ''}
                                      list="employee-names-datalist"
                                      onChange={e => updateSplit(key, idx, 'name', e.target.value)}
                                      style={{ ...s.teamSelect, fontSize: 11, padding: '4px 6px', width: 140 }}
                                    />
                                    <input
                                      type="email"
                                      placeholder={`Email ${idx + 1} (optional)`}
                                      value={sp.email || ''}
                                      onChange={e => updateSplit(key, idx, 'email', e.target.value)}
                                      style={{ ...s.teamSelect, fontSize: 10, padding: '3px 6px', width: 140, color: '#64748b' }}
                                    />
                                  </div>
                                  <select
                                    value={sp.team_id || ''}
                                    onChange={e => updateSplit(key, idx, 'team_id', e.target.value)}
                                    style={{ ...s.teamSelect, fontSize: 11, padding: '4px 6px', minWidth: 110 }}
                                  >
                                    <option value="">— Team {idx + 1} —</option>
                                    {teams.map(t => (
                                      <option key={t.team_id} value={t.team_id}>{t.team_name}</option>
                                    ))}
                                  </select>
                                  {state.splits.length > 2 && (
                                    <button
                                      type="button"
                                      onClick={() => removeSplit(key, idx)}
                                      title="Remove this employee"
                                      style={{ ...s.deleteBtn, padding: '4px 8px', fontSize: 11 }}
                                    >
                                      ×
                                    </button>
                                  )}
                                </div>
                              ))}
                              <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginTop: 4 }}>
                                <button
                                  type="button"
                                  onClick={() => addSplit(key)}
                                  style={{
                                    padding: '4px 10px', fontSize: 11, fontWeight: 600,
                                    border: '1px dashed #cbd5e1', background: '#f8fafc',
                                    color: '#475569', borderRadius: 6, cursor: 'pointer',
                                  }}
                                >
                                  + Add employee
                                </button>
                                <label style={s.rowCheckbox}>
                                  <input
                                    type="checkbox"
                                    checked={state.apply_attendance !== false}
                                    onChange={e => updateRow(key, 'apply_attendance', e.target.checked)}
                                  />
                                  Copy attendance to all
                                </label>
                              </div>
                            </div>
                          </td>
                          <td
                            style={{ ...s.td, ...(isOpen ? { background: '#eff6ff' } : {}), ...(isShared ? { background: '#fef3c7' } : {}) }}
                            onClick={(ev) => ev.stopPropagation()}
                          >
                            <div style={{ display: 'flex', gap: 6 }}>
                              <button
                                onClick={() => saveSplitRow(p)}
                                disabled={savingRow === key}
                                style={{ ...s.saveRowBtn, background: '#f59e0b', minWidth: 70 }}
                              >
                                {savingRow === key ? '...' : 'Split'}
                              </button>
                              <button
                                onClick={() => setItems(prev => prev.filter(x => x.name_key !== key))}
                                style={s.deleteBtn}
                                title="Dismiss from list"
                              >
                                ×
                              </button>
                            </div>
                          </td>
                        </>
                      ) : (
                        <>
                          <td style={s.td} onClick={(ev) => ev.stopPropagation()}>
                            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                              <select
                                value={state.category || 'visitor'}
                                onChange={e => updateRow(key, 'category', e.target.value)}
                                style={{ ...s.teamSelect, fontSize: 12, minWidth: 120 }}
                              >
                                <option value="employee">Employee</option>
                                <option value="visitor">Visitor</option>
                                <option value="vendor">Vendor</option>
                                <option value="interview">Interview</option>
                                <option value="other">Other</option>
                              </select>
                              {state.category === 'employee' && (
                                <>
                                  <input
                                    type="text"
                                    placeholder="Assign to employee name"
                                    value={state.name || ''}
                                    onChange={e => updateRow(key, 'name', e.target.value)}
                                    style={{ ...s.teamSelect, fontSize: 11, padding: '5px 8px', minWidth: 150 }}
                                  />
                                  <label style={s.rowCheckbox}>
                                    <input
                                      type="checkbox"
                                      checked={state.assign_attendance !== false}
                                      onChange={e => updateRow(key, 'assign_attendance', e.target.checked)}
                                    />
                                    Copy this participant's attendance
                                  </label>
                                </>
                              )}
                            </div>
                          </td>
                          <td style={s.td} onClick={(ev) => ev.stopPropagation()}>
                            <select
                              value={state.team_id || ''}
                              onChange={e => updateRow(key, 'team_id', e.target.value)}
                              style={{ ...s.teamSelect, fontSize: 12 }}
                            >
                              <option value="">— None —</option>
                              {teams.map(t => (
                                <option key={t.team_id} value={t.team_id}>{t.team_name}</option>
                              ))}
                            </select>
                          </td>
                          <td
                            style={{ ...s.td, ...(isOpen ? { background: '#eff6ff' } : {}), ...(i % 2 === 0 ? { background: isOpen ? '#eff6ff' : '#fafbfc' } : {}) }}
                            onClick={(ev) => ev.stopPropagation()}
                          >
                            <div style={{ display: 'flex', gap: 6 }}>
                              <button
                                onClick={() => saveRow(p)}
                                disabled={savingRow === key}
                                style={s.saveRowBtn}
                              >
                                {savingRow === key ? '...' : 'Save'}
                              </button>
                              <button
                                onClick={() => setItems(prev => prev.filter(x => x.name_key !== key))}
                                style={s.deleteBtn}
                                title="Dismiss from list"
                              >
                                ×
                              </button>
                            </div>
                          </td>
                        </>
                      )}
                    </tr>
                    {isOpen && (
                      <tr>
                        <td colSpan={12} style={s.detailCell}>
                          <DailyBreakdown detail={{ daily: p.daily || [] }} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Classified participants panel (visitors, vendors, interviews, others) ─
// Shows people already marked in the registry as non-employees, with their
// full monthly attendance (days present, active, break, isolation + daily
// breakdown) so they can be tracked month over month.
function ClassifiedPanel({ teams }) {
  const [year, setYear] = useState(new Date().getFullYear());
  const [month, setMonth] = useState(new Date().getMonth() + 1);
  const [categoryFilter, setCategoryFilter] = useState('all');
  const [search, setSearch] = useState('');
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);
  const [expandedKey, setExpandedKey] = useState(null);

  const CLASSIFIED_CATEGORIES = ['visitor', 'vendor', 'interview', 'other'];

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    setExpandedKey(null);
    try {
      const res = await fetchClassifiedMonthly(year, month, CLASSIFIED_CATEGORIES);
      setItems(res.participants || []);
    } catch (e) {
      setErr(e.message);
    }
    setLoading(false);
  }, [year, month]);

  useEffect(() => { load(); }, [load]);

  const teamNameById = useMemo(() => {
    const m = {};
    teams.forEach(t => { m[t.team_id] = t.team_name; });
    return m;
  }, [teams]);

  // Filtered rows: by category chip + search text
  const rows = useMemo(() => {
    return items.filter(p => {
      if (categoryFilter !== 'all' && (p.category || '').toLowerCase() !== categoryFilter) return false;
      if (search) {
        const q = search.toLowerCase();
        const hay = `${p.display_name || ''} ${p.participant_name || ''} ${p.participant_email || ''}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [items, categoryFilter, search]);

  // Counts per category for chip badges
  const counts = useMemo(() => {
    const c = { all: items.length };
    CLASSIFIED_CATEGORIES.forEach(cat => { c[cat] = 0; });
    items.forEach(p => { c[(p.category || '').toLowerCase()] = (c[(p.category || '').toLowerCase()] || 0) + 1; });
    return c;
  }, [items]);

  const totals = useMemo(() => {
    const count = rows.length;
    const totalHours = rows.reduce((sum, p) => sum + (p.total_active_mins || 0), 0) / 60;
    const totalBreak = rows.reduce((sum, p) => sum + (p.total_break_mins || 0), 0);
    const totalIso = rows.reduce((sum, p) => sum + (p.total_isolation_mins || 0), 0);
    return { count, totalHours, totalBreak, totalIso };
  }, [rows]);

  const chipLabels = [
    { value: 'all',       label: 'All' },
    { value: 'visitor',   label: 'Visitor' },
    { value: 'vendor',    label: 'Vendor' },
    { value: 'interview', label: 'Interview' },
    { value: 'other',     label: 'Other' },
  ];

  return (
    <div>
      {/* Controls: year/month + search */}
      <div style={s.controlBar}>
        <div style={s.controlGroup}>
          <label style={s.label}>Year</label>
          <select value={year} onChange={e => setYear(+e.target.value)} style={s.select}>
            {[2024, 2025, 2026].map(y => <option key={y} value={y}>{y}</option>)}
          </select>
        </div>
        <div style={s.controlGroup}>
          <label style={s.label}>Month</label>
          <select value={month} onChange={e => setMonth(+e.target.value)} style={s.select}>
            {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'].map((m, i) => (
              <option key={i} value={i + 1}>{m}</option>
            ))}
          </select>
        </div>
        <div style={{ ...s.controlGroup, flex: 1, minWidth: 180 }}>
          <label style={s.label}>Search</label>
          <input
            type="text"
            placeholder="Name or email"
            value={search}
            onChange={e => setSearch(e.target.value)}
            style={s.input}
          />
        </div>
        <div style={s.controlGroup}>
          <label style={s.label}>&nbsp;</label>
          <button onClick={load} style={s.refreshBtn} disabled={loading}>
            {loading ? 'Loading...' : 'Refresh'}
          </button>
        </div>
        <div style={s.controlGroup}>
          <label style={s.label}>&nbsp;</label>
          <button
            onClick={() => {
              if (!rows.length) return;
              const headers = ['Name', 'Email', 'Category', 'Team', 'Days Present', 'Total Active (min)', 'Break (min)', 'Isolation (min)'];
              const csvRows = rows.map(p => [
                p.display_name || p.participant_name || '',
                p.participant_email || '',
                p.category || '',
                teamNameById[p.team_id] || '',
                p.days_present || 0,
                p.total_active_mins || 0,
                p.total_break_mins || 0,
                p.total_isolation_mins || 0,
              ]);
              exportRowsCsv(`classified_${year}-${String(month).padStart(2,'0')}.csv`, headers, csvRows);
            }}
            style={s.refreshBtn}
            disabled={!rows.length}
          >
            Export CSV
          </button>
        </div>
      </div>

      {/* Category chips */}
      <div style={s.chipRow}>
        {chipLabels.map(c => (
          <button
            key={c.value}
            onClick={() => setCategoryFilter(c.value)}
            style={{
              ...s.chip,
              ...(categoryFilter === c.value ? s.chipOn : {}),
            }}
          >
            {c.label}
            <span style={{
              ...s.chipCount,
              background: categoryFilter === c.value ? 'rgba(255,255,255,0.25)' : '#f1f5f9',
              color: categoryFilter === c.value ? '#fff' : '#64748b',
            }}>
              {counts[c.value] ?? 0}
            </span>
          </button>
        ))}
      </div>

      {err && <div style={s.error}>{err}</div>}

      {!loading && items.length > 0 && (
        <div style={s.statsRow}>
          <Stat label="People" value={totals.count} color="#3b82f6" />
          <Stat label="Total Hours" value={`${totals.totalHours.toFixed(0)}h`} color="#10b981" />
          <Stat label="Total Break" value={fmtMins(totals.totalBreak)} color="#f97316" />
          <Stat label="Total Isolation" value={fmtMins(totals.totalIso)} color="#ef4444" />
        </div>
      )}

      {loading && <div style={s.loader}>Loading classified participants...</div>}

      {!loading && items.length === 0 && (
        <div style={s.empty}>
          No visitors, vendors, interviews or others registered yet. Classify unrecognized
          participants from the <strong>Unrecognized Participants</strong> tab.
        </div>
      )}

      {!loading && items.length > 0 && rows.length === 0 && (
        <div style={s.empty}>No results for the current filters.</div>
      )}

      {!loading && rows.length > 0 && (
        <div style={s.tableWrap}>
          <table style={s.table}>
            <thead>
              <tr>
                <th style={s.th}></th>
                <th style={s.th}>Name</th>
                <th style={s.th}>Category</th>
                <th style={s.th}>Days Present</th>
                <th style={s.th}>Total Hours</th>
                <th style={s.th}>Avg / Day</th>
                <th style={s.th}>Break</th>
                <th style={s.th}>Isolation</th>
                <th style={s.th}>Team</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((p, i) => {
                const key = p.employee_id || p.participant_name;
                const isOpen = expandedKey === key;
                const avgMins = p.days_present > 0 ? Math.round(p.total_active_mins / p.days_present) : 0;
                return (
                  <Fragment key={key}>
                    <tr
                      onClick={() => setExpandedKey(isOpen ? null : key)}
                      style={{
                        cursor: 'pointer',
                        ...(i % 2 === 0 ? s.trEven : {}),
                        ...(isOpen ? { background: '#eff6ff' } : {}),
                      }}
                    >
                      <td style={s.td}>
                        <span style={{ color: '#64748b', fontSize: 11 }}>{isOpen ? '▼' : '▶'}</span>
                      </td>
                      <td style={s.td}>
                        <div style={{ fontWeight: 600 }}>{p.display_name || p.participant_name}</div>
                        {p.participant_email && (
                          <div style={{ fontSize: 11, color: '#64748b' }}>{p.participant_email}</div>
                        )}
                      </td>
                      <td style={s.td}>
                        <span style={{ ...s.badge, ...catBadgeStyle(p.category) }}>
                          {p.category || 'other'}
                        </span>
                      </td>
                      <td style={{ ...s.td, fontWeight: 600 }}>{p.days_present || 0}</td>
                      <td style={{ ...s.td, color: '#10b981', fontWeight: 700 }}>
                        {fmtHours(p.total_active_mins)}
                      </td>
                      <td style={s.td}>{fmtMins(avgMins)}</td>
                      <td style={{ ...s.td, color: '#f97316' }}>{fmtMins(p.total_break_mins)}</td>
                      <td style={{ ...s.td, color: (p.total_isolation_mins || 0) > 120 ? '#ef4444' : '#64748b' }}>
                        {fmtMins(p.total_isolation_mins)}
                      </td>
                      <td style={s.td}>
                        <span style={{ fontSize: 12, color: p.team_id ? '#1e293b' : '#94a3b8' }}>
                          {p.team_id ? (teamNameById[p.team_id] || p.team_id) : '—'}
                        </span>
                      </td>
                    </tr>
                    {isOpen && (
                      <tr>
                        <td colSpan={9} style={s.detailCell}>
                          <DailyBreakdown detail={{ daily: p.daily || [] }} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Add-visitor/vendor modal ────────────────────────────
function AddModal({ teams, defaultTeamId, onClose, onSave }) {
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [category, setCategory] = useState('visitor');
  const [teamId, setTeamId] = useState(defaultTeamId || '');
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    if (!name.trim()) { setErr('Name is required'); return; }
    setSaving(true);
    setErr(null);
    try {
      await onSave({ name: name.trim(), email: email.trim(), category, team_id: teamId });
    } catch (ex) {
      setErr(ex.message);
      setSaving(false);
    }
  };

  return (
    <div style={s.modalOverlay} onClick={onClose}>
      <div style={s.modal} onClick={e => e.stopPropagation()}>
        <div style={s.modalHeader}>
          <h3 style={{ margin: 0, fontSize: 16 }}>Add Visitor / Vendor / Interview</h3>
          <button onClick={onClose} style={s.modalClose}>×</button>
        </div>
        <form onSubmit={submit} style={s.modalBody}>
          <div style={s.formGroup}>
            <label style={s.formLabel}>Name *</label>
            <input
              type="text"
              value={name}
              onChange={e => setName(e.target.value)}
              placeholder="Full name"
              style={s.formInput}
              autoFocus
              required
            />
          </div>
          <div style={s.formGroup}>
            <label style={s.formLabel}>Email</label>
            <input
              type="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              placeholder="optional"
              style={s.formInput}
            />
          </div>
          <div style={s.formGroup}>
            <label style={s.formLabel}>Category *</label>
            <select value={category} onChange={e => setCategory(e.target.value)} style={s.formInput} required>
              {ADD_CATEGORIES.map(c => <option key={c.value} value={c.value}>{c.label}</option>)}
            </select>
            <div style={s.formHint}>
              Use the <strong>Teams</strong> tab to add full-time employees. This dialog is for external people.
            </div>
          </div>
          <div style={s.formGroup}>
            <label style={s.formLabel}>Allocate to Team</label>
            <select value={teamId} onChange={e => setTeamId(e.target.value)} style={s.formInput}>
              <option value="">— Unassigned —</option>
              {teams.map(t => <option key={t.team_id} value={t.team_id}>{t.team_name}</option>)}
            </select>
          </div>
          {err && <div style={s.error}>{err}</div>}
          <div style={s.modalFooter}>
            <button type="button" onClick={onClose} style={s.btnSecondary}>Cancel</button>
            <button type="submit" disabled={saving} style={s.btnPrimary}>
              {saving ? 'Saving...' : 'Add'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────
function Stat({ label, value, color }) {
  return (
    <div style={s.statCard}>
      <div style={s.statLabel}>{label}</div>
      <div style={{ fontSize: 26, fontWeight: 800, color, lineHeight: 1.1 }}>{value}</div>
    </div>
  );
}

const detailTh = {
  padding: '6px 10px',
  textAlign: 'left',
  fontSize: 10,
  fontWeight: 600,
  color: '#64748b',
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
  borderBottom: '1px solid #e5e7eb',
};
const detailTd = {
  padding: '6px 10px',
  fontSize: 12,
  color: '#1e293b',
  borderBottom: '1px solid #f1f5f9',
};

const s = {
  header: {
    display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start',
    marginBottom: 16, flexWrap: 'wrap', gap: 12,
  },
  title: { fontSize: 22, fontWeight: 800, color: '#0f172a', margin: 0 },
  subtitle: { fontSize: 12, color: '#64748b', marginTop: 2 },
  addBtn: {
    padding: '9px 16px', background: '#0f172a', color: '#fff', border: 'none',
    borderRadius: 8, fontSize: 12, fontWeight: 600, cursor: 'pointer',
  },

  mainToggle: {
    display: 'flex', gap: 3, background: '#f1f5f9', padding: 3,
    borderRadius: 10, marginBottom: 14, width: 'fit-content',
  },
  mainToggleBtn: {
    padding: '9px 22px', border: 'none', borderRadius: 8,
    fontSize: 13, fontWeight: 500, cursor: 'pointer',
    background: 'transparent', color: '#64748b',
  },
  mainToggleBtnOn: { background: '#0f172a', color: '#fff', fontWeight: 700 },

  refreshBtn: {
    padding: '7px 14px', background: '#f1f5f9', color: '#475569',
    border: '1px solid #e5e7eb', borderRadius: 8, fontSize: 12,
    fontWeight: 500, cursor: 'pointer',
  },
  saveRowBtn: {
    padding: '6px 14px', background: '#10b981', color: '#fff',
    border: 'none', borderRadius: 6, fontSize: 11, fontWeight: 700,
    cursor: 'pointer',
  },
  deleteBtn: {
    padding: '6px 10px', background: '#fee2e2', color: '#dc2626',
    border: '1px solid #fecaca', borderRadius: 6, fontSize: 14, fontWeight: 700,
    cursor: 'pointer', lineHeight: 1,
  },

  // Sticky-right "Save" column so it stays visible even when the
  // unrecognized / classified tables overflow horizontally.
  stickyActionTh: {
    padding: '10px 14px', textAlign: 'center',
    fontSize: 11, fontWeight: 600, color: '#64748b',
    textTransform: 'uppercase', letterSpacing: '0.05em',
    borderBottom: '1px solid #e5e7eb', borderLeft: '1px solid #e5e7eb',
    background: '#f8fafc', whiteSpace: 'nowrap',
    position: 'sticky', right: 0, zIndex: 2,
    boxShadow: '-4px 0 6px -4px rgba(15, 23, 42, 0.12)',
  },
  stickyActionTd: {
    padding: '8px 14px', fontSize: 12,
    borderBottom: '1px solid #f1f5f9', borderLeft: '1px solid #e5e7eb',
    background: '#fff', whiteSpace: 'nowrap',
    position: 'sticky', right: 0, zIndex: 1,
    boxShadow: '-4px 0 6px -4px rgba(15, 23, 42, 0.12)',
  },

  controlBar: {
    display: 'flex', gap: 12, marginBottom: 12, flexWrap: 'wrap',
    background: '#fff', border: '1px solid #e5e7eb', borderRadius: 12, padding: '12px 16px',
  },
  controlGroup: { display: 'flex', flexDirection: 'column', gap: 4, minWidth: 120 },
  label: { fontSize: 10, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 600 },
  select: { padding: '7px 10px', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 13, background: '#fff', cursor: 'pointer' },
  input: { padding: '7px 10px', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 13 },

  chipRow: { display: 'flex', gap: 6, marginBottom: 16, flexWrap: 'wrap' },
  chip: {
    padding: '6px 14px', border: '1px solid #e5e7eb', borderRadius: 20,
    background: '#fff', color: '#475569', fontSize: 12, fontWeight: 500,
    cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 6,
  },
  chipOn: { background: '#0f172a', color: '#fff', borderColor: '#0f172a' },
  chipCount: { padding: '1px 8px', borderRadius: 10, fontSize: 10, fontWeight: 700 },

  statsRow: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 10, marginBottom: 20 },
  statCard: { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 12, padding: '14px 16px' },
  statLabel: { fontSize: 10, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4, fontWeight: 600 },

  error: { padding: '10px 14px', background: '#fef2f2', color: '#dc2626', border: '1px solid #fecaca', borderRadius: 10, fontSize: 13, marginBottom: 12 },
  loader: { textAlign: 'center', padding: '60px 20px', color: '#94a3b8' },
  empty: { textAlign: 'center', padding: '60px 20px', color: '#94a3b8', fontSize: 14 },

  tableWrap: { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 12, overflow: 'auto' },
  table: { width: '100%', borderCollapse: 'collapse', minWidth: 900 },
  th: { padding: '10px 14px', textAlign: 'left', fontSize: 11, fontWeight: 600, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.05em', borderBottom: '1px solid #e5e7eb', background: '#f8fafc', whiteSpace: 'nowrap' },
  td: { padding: '10px 14px', fontSize: 13, color: '#1e293b', borderBottom: '1px solid #f1f5f9' },
  badge: { padding: '3px 10px', borderRadius: 12, fontSize: 10, fontWeight: 700, textTransform: 'capitalize', display: 'inline-block' },
  teamSelect: { padding: '5px 8px', border: '1px solid #e5e7eb', borderRadius: 6, fontSize: 11, background: '#fff', cursor: 'pointer' },
  rowCheckbox: { display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: '#475569' },
  detailCell: { padding: 0, background: '#f8fafc', borderBottom: '1px solid #e5e7eb' },

  // Modal
  modalOverlay: {
    position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
    background: 'rgba(15,23,42,0.45)',
    display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
  },
  modal: {
    background: '#fff', borderRadius: 14, width: 'min(440px, 92vw)',
    boxShadow: '0 20px 40px rgba(0,0,0,0.2)', overflow: 'hidden',
  },
  modalHeader: {
    padding: '16px 20px', borderBottom: '1px solid #e5e7eb',
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
  },
  modalClose: { background: 'none', border: 'none', fontSize: 24, cursor: 'pointer', color: '#94a3b8', padding: 0, lineHeight: 1 },
  modalBody: { padding: '18px 20px', display: 'flex', flexDirection: 'column', gap: 14 },
  modalFooter: { display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 4 },
  formGroup: { display: 'flex', flexDirection: 'column', gap: 4 },
  formLabel: { fontSize: 11, fontWeight: 600, color: '#475569', textTransform: 'uppercase', letterSpacing: '0.03em' },
  formInput: { padding: '8px 12px', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 13, background: '#fff' },
  formHint: { fontSize: 11, color: '#94a3b8', marginTop: 2 },
  btnPrimary: { padding: '8px 18px', background: '#0f172a', color: '#fff', border: 'none', borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: 'pointer' },
  btnSecondary: { padding: '8px 18px', background: '#f1f5f9', color: '#475569', border: '1px solid #e5e7eb', borderRadius: 8, fontSize: 13, fontWeight: 500, cursor: 'pointer' },

  // Employee detail drawer (right-side slide-out)
  drawerOverlay: {
    position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
    background: 'rgba(15,23,42,0.45)',
    display: 'flex', justifyContent: 'flex-end', zIndex: 1000,
  },
  drawer: {
    background: '#f8fafc', width: 'min(720px, 96vw)', height: '100%',
    boxShadow: '-8px 0 24px rgba(0,0,0,0.2)',
    display: 'flex', flexDirection: 'column',
  },
  drawerHeader: {
    padding: '16px 20px', borderBottom: '1px solid #e5e7eb', background: '#fff',
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
  },
  drawerBody: { padding: 20, overflowY: 'auto', flex: 1 },

  // Bulk action bar
  bulkBar: {
    display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap',
    padding: '10px 14px', background: '#0f172a', color: '#fff',
    borderRadius: 10, marginBottom: 12,
  },
  bulkSelect: { padding: '6px 10px', borderRadius: 6, border: '1px solid #334155', background: '#1e293b', color: '#fff', fontSize: 12 },
  bulkApply: { padding: '7px 16px', background: '#10b981', color: '#fff', border: 'none', borderRadius: 6, fontSize: 12, fontWeight: 700, cursor: 'pointer' },
};
