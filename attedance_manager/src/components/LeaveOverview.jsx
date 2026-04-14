import { useState, useEffect, useCallback, useMemo } from 'react';

const API_BASE = 'https://breakout-room-calibrator-1073587167150.us-central1.run.app';

function istDate() {
  const now = new Date();
  return new Date(now.getTime() + 330 * 60000).toISOString().slice(0, 10);
}

function fmtMins(m) {
  if (!m) return '-';
  return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`;
}

function statusStyle(status) {
  switch (status) {
    case 'present': return { background: '#dcfce7', color: '#15803d' };
    case 'half_day': return { background: '#fef3c7', color: '#92400e' };
    case 'absent': return { background: '#fef2f2', color: '#dc2626' };
    case 'leave': return { background: '#dbeafe', color: '#1d4ed8' };
    default: return { background: '#f1f5f9', color: '#64748b' };
  }
}

export default function LeaveOverview({ user }) {
  const [date, setDate] = useState(istDate);
  const [data, setData] = useState(null);
  const [conflicts, setConflicts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [expandedTeam, setExpandedTeam] = useState(null);
  const [viewMode, setViewMode] = useState('summary'); // 'summary' | 'conflicts'

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [summaryRes, conflictsRes] = await Promise.all([
        fetch(`${API_BASE}/admin/teams-leave-summary?date=${date}`).then(r => r.json()),
        fetch(`${API_BASE}/admin/conflicts?date=${date}`).then(r => r.json()),
      ]);
      if (summaryRes.success) setData(summaryRes);
      if (conflictsRes.success) setConflicts(conflictsRes.conflicts || []);
    } catch (e) {
      setError(e.message);
    }
    setLoading(false);
  }, [date]);

  useEffect(() => { loadData(); }, [loadData]);

  const teams = data?.teams || [];

  // Overall stats
  const totals = useMemo(() => {
    const totalMembers = teams.reduce((s, t) => s + (t.total_members || 0), 0);
    const totalPresent = teams.reduce((s, t) => s + (t.present || 0), 0);
    const totalHalfDay = teams.reduce((s, t) => s + (t.half_day || 0), 0);
    const totalOnLeave = teams.reduce((s, t) => s + (t.on_leave || 0), 0);
    const totalAbsent = teams.reduce((s, t) => s + (t.absent || 0), 0);
    const teamsOnHoliday = teams.filter(t => t.is_holiday).length;
    return { totalMembers, totalPresent, totalHalfDay, totalOnLeave, totalAbsent, teamsOnHoliday };
  }, [teams]);

  return (
    <div style={s.container}>
      {/* Header */}
      <div style={s.header}>
        <h1 style={s.title}>Leave & Attendance Overview</h1>
        <p style={s.subtitle}>Team-wise attendance summary with conflict detection</p>
      </div>

      {/* Controls */}
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
          <button onClick={loadData} style={s.refreshBtn} disabled={loading}>
            {loading ? 'Loading...' : 'Refresh'}
          </button>
        </div>

        {/* View toggle */}
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 4 }}>
          <button
            onClick={() => setViewMode('summary')}
            style={{ ...s.toggleBtn, ...(viewMode === 'summary' ? s.toggleBtnActive : {}) }}
          >
            Team Summary
          </button>
          <button
            onClick={() => setViewMode('conflicts')}
            style={{ ...s.toggleBtn, ...(viewMode === 'conflicts' ? s.toggleBtnActive : {}) }}
          >
            Conflicts {conflicts.length > 0 && <span style={s.badge}>{conflicts.length}</span>}
          </button>
        </div>
      </div>

      {error && <div style={s.error}>{error}</div>}

      {/* Overall Stats */}
      {!loading && data && (
        <div style={s.statsRow}>
          <StatCard label="Total Members" value={totals.totalMembers} color="#3b82f6" />
          <StatCard label="Present" value={totals.totalPresent} color="#10b981" />
          <StatCard label="Half Day" value={totals.totalHalfDay} color="#f59e0b" />
          <StatCard label="On Leave" value={totals.totalOnLeave} color="#6366f1" />
          <StatCard label="Absent" value={totals.totalAbsent} color="#ef4444" />
          {totals.teamsOnHoliday > 0 && (
            <StatCard label="Teams on Holiday" value={totals.teamsOnHoliday} color="#8b5cf6" />
          )}
        </div>
      )}

      {loading && <div style={s.loader}>Loading team data...</div>}

      {/* Summary View - Team Boxes */}
      {!loading && viewMode === 'summary' && teams.length > 0 && (
        <div style={s.teamGrid}>
          {teams.map(team => (
            <TeamBox
              key={team.team_id}
              team={team}
              expanded={expandedTeam === team.team_id}
              onToggle={() => setExpandedTeam(expandedTeam === team.team_id ? null : team.team_id)}
            />
          ))}
        </div>
      )}

      {/* Conflicts View */}
      {!loading && viewMode === 'conflicts' && (
        <div style={s.conflictsSection}>
          <h3 style={s.sectionTitle}>
            Attendance Conflicts
            <span style={{ fontSize: 12, fontWeight: 400, color: '#64748b', marginLeft: 8 }}>
              {conflicts.length} issue{conflicts.length !== 1 ? 's' : ''} found
            </span>
          </h3>

          {conflicts.length === 0 ? (
            <div style={s.emptyConflicts}>
              No conflicts detected for this date.
            </div>
          ) : (
            <div style={s.tableWrap}>
              <table style={s.table}>
                <thead>
                  <tr>
                    <th style={s.th}>Employee</th>
                    <th style={s.th}>Date</th>
                    <th style={s.th}>Issue</th>
                    <th style={s.th}>Details</th>
                    <th style={s.th}>Suggestion</th>
                  </tr>
                </thead>
                <tbody>
                  {conflicts.map((c, i) => (
                    <tr key={i} style={i % 2 === 0 ? s.trEven : {}}>
                      <td style={s.td}>
                        <div style={{ fontWeight: 600 }}>{c.employee_name}</div>
                      </td>
                      <td style={s.td}>{c.date}</td>
                      <td style={s.td}>
                        <span style={{
                          ...s.conflictBadge,
                          ...(c.conflict_type === 'leave_but_present'
                            ? { background: '#fef3c7', color: '#92400e' }
                            : { background: '#dbeafe', color: '#1d4ed8' })
                        }}>
                          {c.conflict_type === 'leave_but_present' ? 'Leave but Present' : 'Holiday but Worked'}
                        </span>
                      </td>
                      <td style={s.td}>
                        {c.leave_type && <div>Leave type: {c.leave_type}</div>}
                        {c.team_name && <div>Team: {c.team_name}</div>}
                        {c.approx_mins && <div>Active: ~{fmtMins(c.approx_mins)}</div>}
                      </td>
                      <td style={{ ...s.td, fontSize: 11, color: '#64748b' }}>
                        {c.suggestion}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function TeamBox({ team, expanded, onToggle }) {
  const total = team.total_members || 0;
  const presentPct = total > 0 ? Math.round((team.present / total) * 100) : 0;

  return (
    <div style={{ ...s.teamBox, ...(team.is_holiday ? s.teamBoxHoliday : {}) }}>
      {/* Header */}
      <div style={s.teamBoxHeader} onClick={onToggle}>
        <div>
          <div style={s.teamName}>{team.team_name}</div>
          {team.manager_name && (
            <div style={s.teamManager}>{team.manager_name}</div>
          )}
        </div>
        {team.is_holiday && (
          <span style={s.holidayBadge}>Holiday</span>
        )}
        <span style={{ fontSize: 11, color: '#64748b' }}>{expanded ? '▼' : '▶'}</span>
      </div>

      {/* Stats Row */}
      <div style={s.teamStats}>
        <div style={s.teamStatItem}>
          <span style={{ color: '#10b981', fontWeight: 700 }}>{team.present}</span>
          <span style={{ fontSize: 10, color: '#64748b' }}>Present</span>
        </div>
        <div style={s.teamStatItem}>
          <span style={{ color: '#f59e0b', fontWeight: 700 }}>{team.half_day}</span>
          <span style={{ fontSize: 10, color: '#64748b' }}>Half Day</span>
        </div>
        <div style={s.teamStatItem}>
          <span style={{ color: '#6366f1', fontWeight: 700 }}>{team.on_leave}</span>
          <span style={{ fontSize: 10, color: '#64748b' }}>Leave</span>
        </div>
        <div style={s.teamStatItem}>
          <span style={{ color: '#ef4444', fontWeight: 700 }}>{team.absent}</span>
          <span style={{ fontSize: 10, color: '#64748b' }}>Absent</span>
        </div>
      </div>

      {/* Progress bar */}
      <div style={s.progressBar}>
        <div style={{ ...s.progressFill, width: `${presentPct}%` }} />
      </div>
      <div style={{ fontSize: 10, color: '#64748b', textAlign: 'right' }}>
        {team.present + team.half_day}/{total} working
      </div>

      {/* Expanded Details */}
      {expanded && (
        <div style={s.teamDetails}>
          {team.on_leave > 0 && (
            <div style={s.detailSection}>
              <div style={s.detailTitle}>On Leave ({team.leave_details?.length || 0})</div>
              {(team.leave_details || []).map((l, i) => (
                <div key={i} style={s.detailRow}>
                  <span>{l.name}</span>
                  <span style={s.leaveTypeBadge}>{l.leave_type}</span>
                </div>
              ))}
            </div>
          )}

          {team.presence_details?.length > 0 && (
            <div style={s.detailSection}>
              <div style={s.detailTitle}>Attendance ({team.presence_details.length})</div>
              {team.presence_details.map((p, i) => (
                <div key={i} style={s.detailRow}>
                  <span>{p.name}</span>
                  <span style={{ fontSize: 11, color: '#64748b' }}>{fmtMins(p.active_mins)}</span>
                  <span style={{ ...s.statusBadge, ...statusStyle(p.status) }}>
                    {p.status === 'present' ? 'P' : p.status === 'half_day' ? 'HD' : 'A'}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function StatCard({ label, value, color }) {
  return (
    <div style={s.statCard}>
      <div style={s.statLabel}>{label}</div>
      <div style={{ fontSize: 26, fontWeight: 800, color, lineHeight: 1.1 }}>{value}</div>
    </div>
  );
}

const s = {
  container: { padding: 24, maxWidth: 1400, margin: '0 auto' },
  header: { marginBottom: 20 },
  title: { fontSize: 22, fontWeight: 800, color: '#0f172a', margin: 0 },
  subtitle: { fontSize: 13, color: '#64748b', marginTop: 4 },

  controlBar: {
    display: 'flex', gap: 12, marginBottom: 16, flexWrap: 'wrap', alignItems: 'flex-end',
    background: '#fff', border: '1px solid #e5e7eb', borderRadius: 12, padding: '12px 16px',
  },
  controlGroup: { display: 'flex', flexDirection: 'column', gap: 4 },
  label: { fontSize: 10, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 600 },
  input: { padding: '8px 12px', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 13 },
  refreshBtn: {
    padding: '8px 16px', background: '#3b82f6', color: '#fff', border: 'none',
    borderRadius: 8, fontSize: 12, fontWeight: 600, cursor: 'pointer',
  },
  toggleBtn: {
    padding: '8px 14px', background: '#f1f5f9', color: '#64748b', border: 'none',
    borderRadius: 8, fontSize: 12, fontWeight: 500, cursor: 'pointer',
  },
  toggleBtnActive: { background: '#1e293b', color: '#fff' },
  badge: {
    marginLeft: 6, background: '#ef4444', color: '#fff', fontSize: 10, fontWeight: 700,
    padding: '2px 6px', borderRadius: 10,
  },

  error: { background: '#fef2f2', color: '#dc2626', padding: 12, borderRadius: 8, marginBottom: 16 },
  loader: { textAlign: 'center', padding: 40, color: '#64748b' },

  statsRow: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 10, marginBottom: 20 },
  statCard: { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 12, padding: '14px 16px' },
  statLabel: { fontSize: 10, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4, fontWeight: 600 },

  teamGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 16 },
  teamBox: {
    background: '#fff', border: '1px solid #e5e7eb', borderRadius: 12, padding: 16,
    transition: 'box-shadow 0.15s',
  },
  teamBoxHoliday: { background: '#faf5ff', borderColor: '#c4b5fd' },
  teamBoxHeader: {
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    cursor: 'pointer', marginBottom: 12,
  },
  teamName: { fontSize: 14, fontWeight: 700, color: '#1e293b' },
  teamManager: { fontSize: 11, color: '#64748b', marginTop: 2 },
  holidayBadge: {
    background: '#8b5cf6', color: '#fff', fontSize: 10, fontWeight: 600,
    padding: '3px 8px', borderRadius: 12,
  },

  teamStats: { display: 'flex', gap: 12, marginBottom: 10 },
  teamStatItem: { display: 'flex', flexDirection: 'column', alignItems: 'center', flex: 1 },

  progressBar: {
    height: 6, background: '#f1f5f9', borderRadius: 3, overflow: 'hidden', marginBottom: 4,
  },
  progressFill: { height: '100%', background: '#10b981', borderRadius: 3, transition: 'width 0.3s' },

  teamDetails: { marginTop: 12, paddingTop: 12, borderTop: '1px solid #e5e7eb' },
  detailSection: { marginBottom: 10 },
  detailTitle: { fontSize: 11, fontWeight: 600, color: '#64748b', marginBottom: 6, textTransform: 'uppercase' },
  detailRow: {
    display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0',
    fontSize: 12, color: '#1e293b',
  },
  leaveTypeBadge: {
    fontSize: 10, padding: '2px 6px', borderRadius: 4,
    background: '#dbeafe', color: '#1d4ed8',
  },
  statusBadge: {
    fontSize: 10, padding: '2px 6px', borderRadius: 4, fontWeight: 600, marginLeft: 'auto',
  },

  conflictsSection: { marginTop: 20 },
  sectionTitle: { fontSize: 14, fontWeight: 700, color: '#1e293b', marginBottom: 12 },
  emptyConflicts: {
    textAlign: 'center', padding: 40, color: '#64748b', background: '#f8fafc',
    borderRadius: 12, border: '1px dashed #e5e7eb',
  },
  tableWrap: { background: '#fff', borderRadius: 12, border: '1px solid #e5e7eb', overflow: 'hidden' },
  table: { width: '100%', borderCollapse: 'collapse' },
  th: {
    padding: '10px 14px', textAlign: 'left', fontSize: 11, fontWeight: 600, color: '#64748b',
    textTransform: 'uppercase', letterSpacing: '0.05em', borderBottom: '1px solid #e5e7eb',
    background: '#f8fafc',
  },
  td: { padding: '10px 14px', fontSize: 13, borderBottom: '1px solid #f1f5f9' },
  trEven: { background: '#fafbfc' },
  conflictBadge: {
    fontSize: 11, padding: '3px 8px', borderRadius: 6, fontWeight: 500,
  },
};
