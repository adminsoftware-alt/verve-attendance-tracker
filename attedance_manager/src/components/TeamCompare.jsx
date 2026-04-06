import { useState, useEffect } from 'react';
import { fetchTeams, fetchTeamComparison } from '../utils/zoomApi';

function istDate() {
  const now = new Date();
  return new Date(now.getTime() + 330 * 60000).toISOString().slice(0, 10);
}

export default function TeamCompare() {
  const [teams, setTeams] = useState([]);
  const [selectedIds, setSelectedIds] = useState([]);
  const [date, setDate] = useState(istDate);
  const [compareData, setCompareData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetchTeams().then(d => setTeams(d.teams || [])).catch(e => setError(e.message));
  }, []);

  const toggleTeam = (id) => {
    setSelectedIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  };

  const loadComparison = async () => {
    if (selectedIds.length < 2) return setError('Select at least 2 teams to compare');
    setLoading(true);
    setError(null);
    try {
      const data = await fetchTeamComparison(selectedIds, date);
      setCompareData(data);
    } catch (e) { setError(e.message); }
    setLoading(false);
  };

  const maxVal = (arr, key) => Math.max(...arr.map(t => t[key] || 0), 1);

  return (
    <div>
      <h2 style={s.title}>Team Comparison</h2>

      {/* Team selector */}
      <div style={s.selectorWrap}>
        <div style={s.selectorLabel}>Select teams to compare:</div>
        <div style={s.chipWrap}>
          {teams.map(t => (
            <button key={t.team_id} onClick={() => toggleTeam(t.team_id)}
              style={{ ...s.chip, ...(selectedIds.includes(t.team_id) ? s.chipOn : {}) }}>
              {t.team_name}
              {selectedIds.includes(t.team_id) && <span style={{ marginLeft: 6 }}>&#10003;</span>}
            </button>
          ))}
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginTop: 12 }}>
          <input type="date" value={date} onChange={e => setDate(e.target.value)} style={s.dateInput} />
          <button onClick={loadComparison} disabled={loading || selectedIds.length < 2} style={s.compareBtn}>
            {loading ? 'Loading...' : 'Compare'}
          </button>
          <span style={{ fontSize: 11, color: '#94a3b8' }}>{selectedIds.length} selected</span>
        </div>
      </div>

      {error && <div style={s.error}>{error}</div>}

      {/* Results */}
      {compareData?.teams?.length > 0 && (
        <div>
          <div style={{ fontSize: 12, color: '#64748b', marginBottom: 16 }}>Date: {compareData.date}</div>

          {/* Side-by-side cards */}
          <div style={s.grid}>
            {compareData.teams.map(t => (
              <div key={t.team_id} style={s.card}>
                <div style={s.cardName}>{t.team_name}</div>
                {t.manager_name && <div style={s.cardManager}>Manager: {t.manager_name}</div>}

                <div style={s.metricRow}>
                  <div style={s.metric}>
                    <div style={s.metricValue}>{t.attendance_pct}%</div>
                    <div style={s.metricLabel}>Attendance</div>
                    <div style={{ ...s.bar, background: t.attendance_pct >= 80 ? '#dcfce7' : t.attendance_pct >= 50 ? '#fef3c7' : '#fef2f2' }}>
                      <div style={{ ...s.barFill, width: `${t.attendance_pct}%`, background: t.attendance_pct >= 80 ? '#10b981' : t.attendance_pct >= 50 ? '#f59e0b' : '#ef4444' }} />
                    </div>
                  </div>
                </div>

                <div style={s.statsGrid}>
                  <Stat label="Present" value={t.present} sub={`of ${t.total_members}`} color="#10b981" />
                  <Stat label="Absent" value={t.absent} sub={`of ${t.total_members}`} color="#ef4444" />
                  <Stat label="Avg Active" value={`${t.avg_active_mins}m`} color="#3b82f6" />
                  <Stat label="Avg Break" value={`${t.avg_break_mins}m`} color={t.avg_break_mins > 30 ? '#f97316' : '#64748b'} />
                  <Stat label="Earliest In" value={t.earliest_arrival || '-'} color="#8b5cf6" />
                  <Stat label="Latest Out" value={t.latest_departure || '-'} color="#8b5cf6" />
                </div>
              </div>
            ))}
          </div>

          {/* Comparison table */}
          <div style={{ ...s.tableWrap, marginTop: 24 }}>
            <table style={s.table}>
              <thead>
                <tr>
                  <th style={s.th}>Metric</th>
                  {compareData.teams.map(t => <th key={t.team_id} style={s.th}>{t.team_name}</th>)}
                </tr>
              </thead>
              <tbody>
                {[
                  { key: 'attendance_pct', label: 'Attendance %', fmt: v => `${v}%`, good: 'high' },
                  { key: 'present', label: 'Present', fmt: v => v, good: 'high' },
                  { key: 'absent', label: 'Absent', fmt: v => v, good: 'low' },
                  { key: 'total_members', label: 'Total Members', fmt: v => v },
                  { key: 'avg_active_mins', label: 'Avg Active (min)', fmt: v => `${v}m`, good: 'high' },
                  { key: 'avg_break_mins', label: 'Avg Break (min)', fmt: v => `${v}m`, good: 'low' },
                  { key: 'earliest_arrival', label: 'Earliest Arrival', fmt: v => v || '-' },
                  { key: 'latest_departure', label: 'Latest Departure', fmt: v => v || '-' },
                ].map(({ key, label, fmt, good }) => {
                  const vals = compareData.teams.map(t => t[key]);
                  const numVals = vals.map(v => typeof v === 'number' ? v : null);
                  const best = good === 'high' ? Math.max(...numVals.filter(v => v !== null))
                             : good === 'low' ? Math.min(...numVals.filter(v => v !== null))
                             : null;
                  return (
                    <tr key={key}>
                      <td style={{ ...s.td, fontWeight: 600, color: '#475569' }}>{label}</td>
                      {compareData.teams.map(t => {
                        const v = t[key];
                        const isBest = good && typeof v === 'number' && v === best;
                        return (
                          <td key={t.team_id} style={{
                            ...s.td,
                            fontWeight: isBest ? 700 : 400,
                            color: isBest ? '#10b981' : '#1e293b',
                            background: isBest ? '#f0fdf4' : 'transparent'
                          }}>
                            {fmt(v)}
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {compareData?.teams?.length === 0 && !loading && (
        <div style={s.empty}>No data found for the selected teams on this date.</div>
      )}
    </div>
  );
}

function Stat({ label, value, sub, color }) {
  return (
    <div style={{ textAlign: 'center' }}>
      <div style={{ fontSize: 18, fontWeight: 800, color, lineHeight: 1.1 }}>{value}</div>
      {sub && <div style={{ fontSize: 10, color: '#94a3b8' }}>{sub}</div>}
      <div style={{ fontSize: 10, color: '#64748b', marginTop: 2 }}>{label}</div>
    </div>
  );
}

const s = {
  title: { fontSize: 22, fontWeight: 800, color: '#0f172a', margin: '0 0 20px' },

  selectorWrap: { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 14, padding: 20, marginBottom: 20 },
  selectorLabel: { fontSize: 13, fontWeight: 600, color: '#475569', marginBottom: 10 },
  chipWrap: { display: 'flex', flexWrap: 'wrap', gap: 8 },
  chip: { padding: '7px 16px', border: '1px solid #d1d5db', borderRadius: 20, background: '#fff', color: '#475569', fontSize: 13, cursor: 'pointer', fontWeight: 500, transition: 'all 0.15s' },
  chipOn: { background: '#0f172a', color: '#fff', borderColor: '#0f172a' },
  dateInput: { padding: '8px 12px', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 13 },
  compareBtn: { padding: '8px 20px', background: '#3b82f6', color: '#fff', border: 'none', borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: 'pointer' },

  error: { padding: '10px 14px', background: '#fef2f2', color: '#dc2626', border: '1px solid #fecaca', borderRadius: 10, fontSize: 13, marginBottom: 16 },
  empty: { textAlign: 'center', padding: '60px 20px', color: '#94a3b8', fontSize: 14 },

  grid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 16 },
  card: { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 14, padding: 20 },
  cardName: { fontSize: 16, fontWeight: 700, color: '#1e293b', marginBottom: 2 },
  cardManager: { fontSize: 12, color: '#64748b', marginBottom: 14 },

  metricRow: { marginBottom: 16 },
  metric: {},
  metricValue: { fontSize: 32, fontWeight: 800, color: '#1e293b' },
  metricLabel: { fontSize: 11, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 6 },
  bar: { height: 8, borderRadius: 4, overflow: 'hidden' },
  barFill: { height: '100%', borderRadius: 4, transition: 'width 0.5s ease' },

  statsGrid: { display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 10, padding: '12px 0', borderTop: '1px solid #f1f5f9' },

  tableWrap: { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 12, overflow: 'auto' },
  table: { width: '100%', borderCollapse: 'collapse', minWidth: 500 },
  th: { padding: '12px 16px', textAlign: 'left', fontSize: 11, fontWeight: 600, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.05em', borderBottom: '1px solid #e5e7eb', background: '#f8fafc' },
  td: { padding: '12px 16px', fontSize: 13, color: '#1e293b', borderBottom: '1px solid #f1f5f9' },
};
