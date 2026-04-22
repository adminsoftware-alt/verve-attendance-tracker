import { useState, useEffect, useCallback } from 'react';
import { fetchTeams, fetchTeamAttendance } from '../utils/zoomApi';

function istDate() {
  const now = new Date();
  return new Date(now.getTime() + 330 * 60000).toISOString().slice(0, 10);
}

function fmtMins(m) {
  if (!m) return '0min';
  const h = Math.floor(m / 60);
  const min = m % 60;
  if (h > 0 && min > 0) return `${h}hr ${min}min`;
  if (h > 0) return `${h}hr`;
  return `${min}min`;
}

function trafficColor(status) {
  switch (status) {
    case 'present': return '#10b981';
    case 'half_day': return '#f59e0b';
    default: return '#ef4444';
  }
}

function trafficBg(status) {
  switch (status) {
    case 'present': return '#f0fdf4';
    case 'half_day': return '#fffbeb';
    default: return '#fef2f2';
  }
}

function statusLabel(s) {
  return s === 'present' ? 'Present' : s === 'half_day' ? 'Half Day' : 'Absent';
}

export default function TeamDashboard({ user }) {
  const [teams, setTeams] = useState([]);
  const [teamData, setTeamData] = useState({});
  const [loading, setLoading] = useState(true);
  const [date] = useState(istDate);

  const isManager = user?.role === 'manager';

  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchTeams();
      let teamList = res.teams || [];
      if (isManager && user?.name) {
        teamList = teamList.filter(t =>
          (t.manager_name || '').toLowerCase().trim() === user.name.toLowerCase().trim()
          || (t.manager_email || '').toLowerCase().trim() === (user?.email || '').toLowerCase().trim()
        );
      }
      setTeams(teamList);

      // Load attendance for each team in parallel
      const results = {};
      await Promise.all(teamList.map(async (t) => {
        try {
          const data = await fetchTeamAttendance(t.team_id, date);
          results[t.team_id] = data;
        } catch (e) {
          results[t.team_id] = { error: e.message };
        }
      }));
      setTeamData(results);
    } catch (e) {
      console.error(e);
    }
    setLoading(false);
  }, [date, isManager, user?.name, user?.email]);

  useEffect(() => { loadAll(); }, [loadAll]);

  if (loading) return <div style={s.loader}>Loading dashboard...</div>;

  // Aggregate stats across all teams
  let totalMembers = 0, totalPresent = 0, totalHalfDay = 0, totalAbsent = 0, totalHours = 0;
  const allMissing = [];

  teams.forEach(t => {
    const d = teamData[t.team_id];
    if (!d || d.error) return;
    const members = d.participants || [];
    members.forEach(m => {
      totalMembers++;
      if (m.status === 'present') { totalPresent++; totalHours += (m.total_duration_mins || 0); }
      else if (m.status === 'half_day') { totalHalfDay++; totalHours += (m.total_duration_mins || 0); }
      else { totalAbsent++; allMissing.push({ name: m.name, team: t.team_name }); }
    });
  });

  const overallPct = totalMembers > 0 ? Math.round((totalPresent + totalHalfDay) / totalMembers * 100) : 0;

  return (
    <div>
      <div style={s.header}>
        <div>
          <h2 style={s.title}>Dashboard</h2>
          <div style={{ fontSize: 12, color: '#64748b' }}>{date} (IST)</div>
        </div>
        <button onClick={loadAll} style={s.refreshBtn}>Refresh</button>
      </div>

      {/* Overview cards */}
      <div style={s.overviewGrid}>
        <div style={{ ...s.overviewCard, borderLeft: '4px solid #3b82f6' }}>
          <div style={s.overviewLabel}>Total Members</div>
          <div style={{ ...s.overviewValue, color: '#3b82f6' }}>{totalMembers}</div>
        </div>
        <div style={{ ...s.overviewCard, borderLeft: '4px solid #10b981' }}>
          <div style={s.overviewLabel}>Present</div>
          <div style={{ ...s.overviewValue, color: '#10b981' }}>{totalPresent}</div>
        </div>
        <div style={{ ...s.overviewCard, borderLeft: '4px solid #f59e0b' }}>
          <div style={s.overviewLabel}>Half Day</div>
          <div style={{ ...s.overviewValue, color: '#f59e0b' }}>{totalHalfDay}</div>
        </div>
        <div style={{ ...s.overviewCard, borderLeft: '4px solid #ef4444' }}>
          <div style={s.overviewLabel}>Absent</div>
          <div style={{ ...s.overviewValue, color: '#ef4444' }}>{totalAbsent}</div>
        </div>
        <div style={{ ...s.overviewCard, borderLeft: '4px solid #8b5cf6' }}>
          <div style={s.overviewLabel}>Attendance</div>
          <div style={{ ...s.overviewValue, color: overallPct >= 80 ? '#10b981' : overallPct >= 50 ? '#f59e0b' : '#ef4444' }}>{overallPct}%</div>
        </div>
        <div style={{ ...s.overviewCard, borderLeft: '4px solid #06b6d4' }}>
          <div style={s.overviewLabel}>Total Hours</div>
          <div style={{ ...s.overviewValue, color: '#06b6d4' }}>{fmtMins(totalHours)}</div>
        </div>
      </div>

      {/* Missing today */}
      {allMissing.length > 0 && (
        <div style={s.missingCard}>
          <div style={s.missingTitle}>Absent Today ({allMissing.length})</div>
          <div style={s.missingList}>
            {allMissing.map((m, i) => (
              <span key={i} style={s.missingChip}>{m.name} <span style={{ color: '#94a3b8', fontSize: 10 }}>({m.team})</span></span>
            ))}
          </div>
        </div>
      )}

      {/* Per-team sections */}
      {teams.map(team => {
        const d = teamData[team.team_id];
        if (!d || d.error) return (
          <div key={team.team_id} style={s.teamSection}>
            <div style={s.teamHeader}>{team.team_name}</div>
            <div style={{ padding: 16, color: '#94a3b8', fontSize: 13 }}>No data available</div>
          </div>
        );

        const members = (d.participants || []).sort((a, b) => {
          const order = { present: 0, half_day: 1, absent: 2 };
          return (order[a.status] || 3) - (order[b.status] || 3);
        });

        const present = members.filter(m => m.status === 'present').length;
        const half = members.filter(m => m.status === 'half_day').length;
        const total = members.length;
        const pct = total > 0 ? Math.round((present + half) / total * 100) : 0;

        return (
          <div key={team.team_id} style={s.teamSection}>
            <div style={s.teamHeader}>
              <div>
                <span style={s.teamName}>{team.team_name}</span>
                {team.manager_name && <span style={s.teamManager}> — {team.manager_name}</span>}
              </div>
              <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
                <span style={{ fontSize: 12, color: '#64748b' }}>{present + half}/{total}</span>
                <span style={{ fontSize: 18, fontWeight: 800, color: pct >= 80 ? '#10b981' : pct >= 50 ? '#f59e0b' : '#ef4444' }}>{pct}%</span>
              </div>
            </div>

            {/* Traffic light grid */}
            <div style={s.trafficGrid}>
              {members.map((m, i) => (
                <div key={i} style={{ ...s.trafficCard, background: trafficBg(m.status), borderColor: trafficColor(m.status) + '40' }}>
                  <div style={s.trafficDot}>
                    <div style={{ width: 10, height: 10, borderRadius: '50%', background: trafficColor(m.status) }} />
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={s.trafficName}>{m.name}</div>
                    <div style={s.trafficDetail}>
                      {m.status !== 'absent' ? (
                        <>
                          <span>{fmtMins(m.total_duration_mins)}</span>
                          <span style={{ color: '#94a3b8' }}>|</span>
                          <span>{m.first_seen_ist || '-'} - {m.last_seen_ist || '-'}</span>
                          {(m.break_minutes || 0) > 0 && <span style={{ color: '#f97316' }}>Break: {fmtMins(m.break_minutes)}</span>}
                        </>
                      ) : (
                        <span style={{ color: '#ef4444' }}>Not seen today</span>
                      )}
                    </div>
                  </div>
                  <div style={{ fontSize: 10, fontWeight: 700, color: trafficColor(m.status), textTransform: 'uppercase' }}>
                    {statusLabel(m.status)}
                  </div>
                </div>
              ))}
            </div>
          </div>
        );
      })}

      {teams.length === 0 && <div style={s.empty}>No teams assigned to you.</div>}
    </div>
  );
}

const s = {
  header: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 20 },
  title: { fontSize: 22, fontWeight: 800, color: '#0f172a', margin: 0 },
  refreshBtn: { padding: '8px 16px', background: '#f1f5f9', color: '#475569', border: '1px solid #e5e7eb', borderRadius: 8, fontSize: 12, cursor: 'pointer' },
  loader: { display: 'flex', alignItems: 'center', justifyContent: 'center', height: '50vh', color: '#94a3b8' },
  empty: { textAlign: 'center', padding: '60px 20px', color: '#94a3b8', fontSize: 14 },

  overviewGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12, marginBottom: 20 },
  overviewCard: { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 12, padding: '14px 18px' },
  overviewLabel: { fontSize: 10, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 600, marginBottom: 4 },
  overviewValue: { fontSize: 28, fontWeight: 800, lineHeight: 1 },

  missingCard: { background: '#fef2f2', border: '1px solid #fecaca', borderRadius: 12, padding: 16, marginBottom: 20 },
  missingTitle: { fontSize: 13, fontWeight: 700, color: '#dc2626', marginBottom: 8 },
  missingList: { display: 'flex', flexWrap: 'wrap', gap: 6 },
  missingChip: { padding: '4px 10px', background: '#fff', border: '1px solid #fecaca', borderRadius: 16, fontSize: 12, color: '#1e293b' },

  teamSection: { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 14, marginBottom: 16, overflow: 'hidden' },
  teamHeader: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '14px 20px', background: '#f8fafc', borderBottom: '1px solid #e5e7eb' },
  teamName: { fontSize: 15, fontWeight: 700, color: '#1e293b' },
  teamManager: { fontSize: 12, color: '#64748b' },

  trafficGrid: { padding: 12, display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 8 },
  trafficCard: { display: 'flex', alignItems: 'center', gap: 10, padding: '10px 14px', borderRadius: 10, border: '1px solid' },
  trafficDot: { flexShrink: 0 },
  trafficName: { fontSize: 13, fontWeight: 600, color: '#1e293b', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' },
  trafficDetail: { display: 'flex', gap: 6, fontSize: 11, color: '#64748b', flexWrap: 'wrap' },
};
