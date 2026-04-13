import { useState, useEffect, useMemo, useCallback, Fragment } from 'react';
import {
  fetchTeams,
  fetchEmployees,
  createEmployee,
  updateEmployee,
  fetchTeamMonthlyReport,
  fetchEmployeeDetail,
  fetchUnrecognized,
  addTeamMember,
} from '../utils/zoomApi';

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
  return h > 0 ? `${h}h ${min}m` : `${min}m`;
}

function fmtHours(m) {
  if (!m) return '0.0h';
  return `${(m / 60).toFixed(1)}h`;
}

export default function Employees({ user }) {
  // Top-level view toggle
  const [mainView, setMainView] = useState('members'); // 'members' | 'unrecognized'

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

  // Load registry + monthly attendance for the selected team/month
  const loadData = useCallback(async () => {
    if (!selectedTeam) return;
    setLoading(true);
    setError(null);
    setExpandedId(null);
    setDetailCache({});
    try {
      const [empRes, monthlyRes] = await Promise.all([
        fetchEmployees({ team_id: selectedTeam }),
        fetchTeamMonthlyReport(selectedTeam, year, month),
      ]);
      setRegistryEmployees(empRes.employees || []);
      setMonthlyData(monthlyRes);
    } catch (e) {
      setError(e.message);
    }
    setLoading(false);
  }, [selectedTeam, year, month]);

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

  const selectedTeamName = teams.find(t => t.team_id === selectedTeam)?.team_name || '';

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

      {/* Main view toggle: Members vs Unrecognized */}
      <div style={s.mainToggle}>
        <button
          onClick={() => setMainView('members')}
          style={{ ...s.mainToggleBtn, ...(mainView === 'members' ? s.mainToggleBtnOn : {}) }}
        >
          Team Members
        </button>
        <button
          onClick={() => setMainView('unrecognized')}
          style={{ ...s.mainToggleBtn, ...(mainView === 'unrecognized' ? s.mainToggleBtnOn : {}) }}
        >
          Unrecognized Participants
        </button>
      </div>

      {mainView === 'unrecognized' && (
        <UnrecognizedPanel
          teams={teams}
          onClassified={() => { if (mainView === 'members') loadData(); }}
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
                const detail = detailCache[e.employee_id];
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
                    {isOpen && (
                      <tr>
                        <td colSpan={9} style={s.detailCell}>
                          {!detail ? (
                            <div style={{ color: '#94a3b8', fontSize: 12, padding: 12 }}>Loading daily breakdown...</div>
                          ) : (
                            <DailyBreakdown detail={detail} />
                          )}
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
              <td style={detailTd}>{d.status || '-'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── Unrecognized participants panel ─────────────────────
function UnrecognizedPanel({ teams, onClassified }) {
  const [date, setDate] = useState(istDate());
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);
  // per-row form state, keyed by raw participant_name
  const [rowState, setRowState] = useState({});
  const [savingRow, setSavingRow] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const res = await fetchUnrecognized(date);
      setItems(res.unrecognized || []);
      // Reset per-row state with sensible defaults
      const initial = {};
      (res.unrecognized || []).forEach(u => {
        initial[u.participant_name] = {
          category: 'visitor',
          team_id: '',
          name: u.normalized_name || u.participant_name,
          email: u.participant_email || '',
        };
      });
      setRowState(initial);
    } catch (e) {
      setErr(e.message);
    }
    setLoading(false);
  }, [date]);

  useEffect(() => { load(); }, [load]);

  const updateRow = (key, field, value) => {
    setRowState(prev => ({
      ...prev,
      [key]: { ...prev[key], [field]: value },
    }));
  };

  const saveRow = async (item) => {
    const key = item.participant_name;
    const state = rowState[key];
    if (!state) return;
    if (state.category === 'employee' && !state.team_id) {
      setErr('Select a team to add this person as an employee.');
      return;
    }
    setSavingRow(key);
    setErr(null);
    try {
      // 1. Always register in employee_registry with the chosen category
      await createEmployee({
        participant_name: state.name.trim() || item.participant_name,
        participant_email: state.email.trim(),
        category: state.category,
        team_id: state.team_id || null,
      });
      // 2. For "employee" category, also add to team_members so they show
      //    up in team monthly reports
      if (state.category === 'employee' && state.team_id) {
        try {
          await addTeamMember(
            state.team_id,
            state.name.trim() || item.participant_name,
            state.email.trim(),
          );
        } catch (e) {
          // Not fatal — registry save already succeeded
          console.warn('addTeamMember failed:', e);
        }
      }
      // Remove from list
      setItems(prev => prev.filter(x => x.participant_name !== key));
      onClassified && onClassified();
    } catch (e) {
      setErr(e.message);
    }
    setSavingRow(null);
  };

  return (
    <div>
      <div style={s.controlBar}>
        <div style={s.controlGroup}>
          <label style={s.label}>Date</label>
          <input
            type="date"
            value={date}
            onChange={e => setDate(e.target.value)}
            style={s.input}
          />
        </div>
        <div style={s.controlGroup}>
          <label style={s.label}>&nbsp;</label>
          <button onClick={load} style={s.refreshBtn} disabled={loading}>
            {loading ? 'Loading...' : 'Refresh'}
          </button>
        </div>
        <div style={{ flex: 1, minWidth: 220, alignSelf: 'flex-end', fontSize: 12, color: '#64748b' }}>
          {items.length > 0
            ? <>Found <strong>{items.length}</strong> unrecognized participant{items.length === 1 ? '' : 's'} on this date.</>
            : loading ? '' : 'No unrecognized participants — everyone is already classified.'}
        </div>
      </div>

      {err && <div style={s.error}>{err}</div>}

      {loading && <div style={s.loader}>Loading unrecognized participants...</div>}

      {!loading && items.length > 0 && (
        <div style={s.tableWrap}>
          <table style={s.table}>
            <thead>
              <tr>
                <th style={s.th}>Zoom Name</th>
                <th style={s.th}>Save As</th>
                <th style={s.th}>Email</th>
                <th style={s.th}>Category</th>
                <th style={s.th}>Team</th>
                <th style={s.th}></th>
              </tr>
            </thead>
            <tbody>
              {items.map((item, i) => {
                const key = item.participant_name;
                const state = rowState[key] || {};
                return (
                  <tr key={key} style={i % 2 === 0 ? s.trEven : {}}>
                    <td style={s.td}>
                      <div style={{ fontWeight: 600, fontSize: 12 }}>{item.participant_name}</div>
                      {item.normalized_name && item.normalized_name !== item.participant_name && (
                        <div style={{ fontSize: 10, color: '#94a3b8' }}>
                          cleaned: {item.normalized_name}
                        </div>
                      )}
                    </td>
                    <td style={s.td}>
                      <input
                        type="text"
                        value={state.name || ''}
                        onChange={e => updateRow(key, 'name', e.target.value)}
                        style={{ ...s.formInput, padding: '5px 8px', fontSize: 12, width: 170 }}
                      />
                    </td>
                    <td style={s.td}>
                      <input
                        type="email"
                        value={state.email || ''}
                        onChange={e => updateRow(key, 'email', e.target.value)}
                        placeholder="(optional)"
                        style={{ ...s.formInput, padding: '5px 8px', fontSize: 12, width: 180 }}
                      />
                    </td>
                    <td style={s.td}>
                      <select
                        value={state.category || 'visitor'}
                        onChange={e => updateRow(key, 'category', e.target.value)}
                        style={{ ...s.teamSelect, fontSize: 12 }}
                      >
                        <option value="employee">Employee (add to team)</option>
                        <option value="visitor">Visitor</option>
                        <option value="vendor">Vendor</option>
                        <option value="interview">Interview</option>
                        <option value="other">Other</option>
                      </select>
                    </td>
                    <td style={s.td}>
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
                    <td style={s.td}>
                      <button
                        onClick={() => saveRow(item)}
                        disabled={savingRow === key}
                        style={s.saveRowBtn}
                      >
                        {savingRow === key ? 'Saving...' : 'Save'}
                      </button>
                    </td>
                  </tr>
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
};
