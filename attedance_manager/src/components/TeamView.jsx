import { useState, useEffect, useCallback, useMemo } from 'react';
import {
  fetchTeams, fetchTeamAttendance, fetchTeamAttendanceRange,
  fetchTeamMonthlyReport, getTeamRangeCsvUrl
} from '../utils/zoomApi';
import { downloadTeamPivotExcel } from '../utils/teamPivotExcel';
import MonthlyPivotTables from './MonthlyPivotTables';
import AttendanceEditModal from './AttendanceEditModal';

function istDate() {
  const now = new Date();
  return new Date(now.getTime() + 330 * 60000).toISOString().slice(0, 10);
}

function fmtMins(m) {
  if (!m) return '-';
  const h = Math.floor(m / 60);
  const min = m % 60;
  if (h > 0 && min > 0) return `${h}hr ${min}min`;
  if (h > 0) return `${h}hr`;
  return `${min}min`;
}

// Status badge color
function statusStyle(status) {
  switch (status) {
    case 'present': return { background: '#dcfce7', color: '#15803d' };
    case 'half_day': return { background: '#fef3c7', color: '#92400e' };
    case 'absent': default: return { background: '#fef2f2', color: '#dc2626' };
  }
}

function statusLabel(status) {
  switch (status) {
    case 'present': return 'Present';
    case 'half_day': return 'Half Day';
    case 'absent': default: return 'Absent';
  }
}

export default function TeamView({ user }) {
  const [teams, setTeams] = useState([]);
  const [selectedTeam, setSelectedTeam] = useState('');
  const [mode, setMode] = useState('monthly');      // daily | range | monthly
  const [date, setDate] = useState(istDate);
  const [startDate, setStartDate] = useState(istDate);
  const [endDate, setEndDate] = useState(istDate);
  const [year, setYear] = useState(new Date().getFullYear());
  const [month, setMonth] = useState(new Date().getMonth() + 1);
  const [attendance, setAttendance] = useState(null);
  const [rangeData, setRangeData] = useState(null);
  const [monthlyData, setMonthlyData] = useState(null);
  const [dataLoading, setDataLoading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [editModalMember, setEditModalMember] = useState(null);
  const [editModalDate, setEditModalDate] = useState(null);

  // Manager filtering
  const isManager = user?.role === 'manager';

  useEffect(() => {
    fetchTeams().then(d => {
      let teamList = d.teams || [];
      if (isManager && user?.name) {
        teamList = teamList.filter(t =>
          (t.manager_name || '').toLowerCase().trim() === user.name.toLowerCase().trim()
          || (t.manager_email || '').toLowerCase().trim() === (user?.email || '').toLowerCase().trim()
        );
      }
      setTeams(teamList);
      if (teamList.length > 0) setSelectedTeam(teamList[0].team_id);
    }).catch(e => setError(e.message)).finally(() => setLoading(false));
  }, [isManager, user?.name, user?.email]);

  const loadAttendance = useCallback(async () => {
    if (!selectedTeam) return;
    setDataLoading(true);
    setError(null);
    try {
      if (mode === 'daily') {
        const data = await fetchTeamAttendance(selectedTeam, date);
        setAttendance(data);
        setRangeData(null);
        setMonthlyData(null);
      } else if (mode === 'range') {
        const data = await fetchTeamAttendanceRange(selectedTeam, startDate, endDate);
        setRangeData(data);
        setAttendance(null);
        setMonthlyData(null);
      } else {
        const data = await fetchTeamMonthlyReport(selectedTeam, year, month);
        setMonthlyData(data);
        setAttendance(null);
        setRangeData(null);
      }
    } catch (e) { setError(e.message); }
    setDataLoading(false);
  }, [selectedTeam, mode, date, startDate, endDate, year, month]);

  useEffect(() => { loadAttendance(); }, [loadAttendance]);

  // Daily stats
  const dailyStats = useMemo(() => {
    const members = attendance?.participants;
    if (!members?.length) return null;
    const present = members.filter(m => m.status === 'present').length;
    const halfDay = members.filter(m => m.status === 'half_day').length;
    const absent = members.filter(m => m.status === 'absent').length;
    const total = members.length;
    const working = members.filter(m => m.status !== 'absent');
    const avgActive = working.length > 0 ? Math.round(working.reduce((s, m) => s + (m.total_duration_mins || 0), 0) / working.length) : 0;
    const totalBreak = working.reduce((s, m) => s + (m.break_minutes || 0), 0);
    return { present, halfDay, absent, total, avgActive, totalBreak };
  }, [attendance]);

  const downloadDailyCsv = () => {
    if (!selectedTeam) return;
    window.open(getTeamRangeCsvUrl(selectedTeam, date, date), '_blank');
  };
  const downloadRangeCsv = () => {
    if (!selectedTeam) return;
    window.open(getTeamRangeCsvUrl(selectedTeam, startDate, endDate), '_blank');
  };
  const downloadPivotExcel = async () => {
    if (!selectedTeam) return;
    try {
      // Ensure we have fresh data for the selected month
      let data = monthlyData;
      if (!data || data.team_id !== selectedTeam || data.year !== year || data.month !== month) {
        setDataLoading(true);
        data = await fetchTeamMonthlyReport(selectedTeam, year, month);
        setMonthlyData(data);
        setDataLoading(false);
      }
      const team = teams.find(t => t.team_id === selectedTeam) || {};
      downloadTeamPivotExcel(data, team, year, month);
    } catch (e) {
      setError(e.message);
      setDataLoading(false);
    }
  };

  if (loading && teams.length === 0) return <div style={s.loader}>Loading...</div>;

  const dailyMembers = attendance?.participants || [];
  const rangeSummary = rangeData?.member_summary || [];
  const rangeDaily = rangeData?.daily_data || [];

  return (
    <div>
      {/* Header */}
      <div style={s.header}>
        <div>
          <h2 style={s.title}>Team Attendance</h2>
          {isManager && <div style={{ fontSize: 11, color: '#64748b' }}>Showing your teams only</div>}
        </div>
        <div style={s.controls}>
          <select value={selectedTeam} onChange={e => setSelectedTeam(e.target.value)} style={s.select}>
            <option value="">Select team</option>
            {teams.map(t => <option key={t.team_id} value={t.team_id}>{t.team_name}</option>)}
          </select>
          <div style={s.modeToggle}>
            <button onClick={() => setMode('daily')} style={{ ...s.modeBtn, ...(mode === 'daily' ? s.modeBtnOn : {}) }}>Daily</button>
            <button onClick={() => setMode('range')} style={{ ...s.modeBtn, ...(mode === 'range' ? s.modeBtnOn : {}) }}>Range</button>
            <button onClick={() => setMode('monthly')} style={{ ...s.modeBtn, ...(mode === 'monthly' ? s.modeBtnOn : {}) }}>Monthly</button>
          </div>
        </div>
      </div>

      {error && <div style={s.error}>{error}</div>}

      {/* Date controls */}
      <div style={s.dateBar}>
        {mode === 'daily' && (
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input type="date" value={date} onChange={e => setDate(e.target.value)} style={s.dateInput} />
            <button onClick={downloadDailyCsv} style={s.csvBtn}>CSV</button>
          </div>
        )}
        {mode === 'range' && (
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <label style={{ fontSize: 12, color: '#64748b' }}>From</label>
            <input type="date" value={startDate} onChange={e => setStartDate(e.target.value)} style={s.dateInput} />
            <label style={{ fontSize: 12, color: '#64748b' }}>To</label>
            <input type="date" value={endDate} onChange={e => setEndDate(e.target.value)} style={s.dateInput} />
            <button onClick={downloadRangeCsv} style={s.csvBtn}>CSV</button>
          </div>
        )}
        {mode === 'monthly' && (
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <select value={year} onChange={e => setYear(+e.target.value)} style={s.select}>
              {[2024, 2025, 2026].map(y => <option key={y} value={y}>{y}</option>)}
            </select>
            <select value={month} onChange={e => setMonth(+e.target.value)} style={s.select}>
              {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'].map((m, i) => (
                <option key={i} value={i + 1}>{m}</option>
              ))}
            </select>
            <button onClick={downloadPivotExcel} style={s.pivotXlsxBtn} title="Monthly pivot Excel with hours, isolation, and leaves">Download Excel Report</button>
          </div>
        )}
        <button onClick={loadAttendance} disabled={dataLoading} style={s.refreshBtn}>
          {dataLoading ? 'Loading...' : 'Refresh'}
        </button>
      </div>

      {!selectedTeam && <div style={s.empty}>Select a team to view attendance</div>}

      {/* ═══ DAILY VIEW ═══ */}
      {mode === 'daily' && selectedTeam && !dataLoading && attendance && (
        <div>
          {attendance.team_name && (
            <div style={{ marginBottom: 12, fontSize: 13, color: '#64748b' }}>
              <strong style={{ color: '#1e293b' }}>{attendance.team_name}</strong>
              {attendance.manager_name ? ` — Manager: ${attendance.manager_name}` : ''}
              {` — ${attendance.date}`}
            </div>
          )}

          {dailyStats && (
            <div style={s.statsRow}>
              <StatCard label="Present" value={dailyStats.present} sub={`of ${dailyStats.total}`} color="#10b981" />
              {dailyStats.halfDay > 0 && <StatCard label="Half Day" value={dailyStats.halfDay} color="#f59e0b" />}
              <StatCard label="Absent" value={dailyStats.absent} sub={`of ${dailyStats.total}`} color="#ef4444" />
              <StatCard label="Avg Active" value={fmtMins(dailyStats.avgActive)} color="#3b82f6" />
              <StatCard label="Total Break" value={fmtMins(dailyStats.totalBreak)} color="#f97316" />
            </div>
          )}

          {dailyMembers.length > 0 ? (
            <div style={s.tableWrap}>
              <table style={s.table}>
                <thead>
                  <tr>
                    <th style={s.th}>Name</th>
                    <th style={s.th}>Status</th>
                    <th style={s.th}>First Seen</th>
                    <th style={s.th}>Last Seen</th>
                    <th style={s.th}>Total Time</th>
                    <th style={s.th}>Breakout</th>
                    <th style={s.th}>Main Room</th>
                    <th style={s.th}>Break</th>
                    <th style={s.th}>Isolation</th>
                    {user?.role === 'superadmin' && <th style={s.th}></th>}
                  </tr>
                </thead>
                <tbody>
                  {dailyMembers.map((m, i) => (
                    <tr key={i} style={{ ...(i % 2 === 0 ? s.trEven : {}), ...(m.status === 'absent' ? { opacity: 0.45 } : {}) }}>
                      <td style={s.td}>
                        <div style={{ fontWeight: 600 }}>{m.name}</div>
                        {m.email && <div style={{ fontSize: 11, color: '#64748b' }}>{m.email}</div>}
                      </td>
                      <td style={s.td}>
                        <span style={{ ...s.badge, ...statusStyle(m.status) }}>{statusLabel(m.status)}</span>
                      </td>
                      <td style={s.td}>{m.first_seen_ist || '-'}</td>
                      <td style={s.td}>{m.last_seen_ist || '-'}</td>
                      <td style={{ ...s.td, fontWeight: 600, color: m.total_duration_mins >= 300 ? '#10b981' : m.total_duration_mins >= 240 ? '#f59e0b' : m.total_duration_mins > 0 ? '#ef4444' : '#94a3b8' }}>
                        {fmtMins(m.total_duration_mins)}
                      </td>
                      <td style={{ ...s.td, color: '#3b82f6' }}>{fmtMins(m.breakout_mins)}</td>
                      <td style={{ ...s.td, color: '#8b5cf6' }}>{fmtMins(m.main_room_mins)}</td>
                      <td style={{ ...s.td, color: (m.break_minutes || 0) > 45 ? '#dc2626' : '#64748b' }}>
                        {m.status !== 'absent' ? fmtMins(m.break_minutes) : '-'}
                      </td>
                      <td style={{ ...s.td, color: m.isolation_minutes > 30 ? '#ef4444' : '#64748b' }}>
                        {m.status !== 'absent' ? fmtMins(m.isolation_minutes) : '-'}
                      </td>
                      {user?.role === 'superadmin' && (
                        <td style={s.td}>
                          <button onClick={() => { setEditModalMember(m); setEditModalDate(date); }} style={s.editBtn}>Edit</button>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div style={s.empty}>No attendance data for this date.</div>
          )}
        </div>
      )}

      {/* ═══ RANGE VIEW ═══ */}
      {mode === 'range' && selectedTeam && !dataLoading && rangeData && (
        <div>
          {rangeData.team_name && (
            <div style={{ marginBottom: 12, fontSize: 13, color: '#64748b' }}>
              <strong style={{ color: '#1e293b' }}>{rangeData.team_name}</strong>
              {` — ${rangeData.start_date} to ${rangeData.end_date}`}
            </div>
          )}

          {rangeSummary.length > 0 && (
            <>
              <h3 style={s.sectionTitle}>Member Summary</h3>
              <div style={{ ...s.tableWrap, marginBottom: 24 }}>
                <table style={s.table}>
                  <thead>
                    <tr>
                      <th style={s.th}>Name</th>
                      <th style={s.th}>Days Present</th>
                      <th style={s.th}>Total Active</th>
                      <th style={s.th}>Total Break</th>
                      <th style={s.th}>Total Isolation</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rangeSummary.map((m, i) => (
                      <tr key={i} style={i % 2 === 0 ? s.trEven : {}}>
                        <td style={{ ...s.td, fontWeight: 600 }}>{m.name}</td>
                        <td style={s.td}><span style={{ ...s.badge, background: '#eff6ff', color: '#2563eb' }}>{m.days_present}</span></td>
                        <td style={{ ...s.td, color: '#10b981', fontWeight: 600 }}>{fmtMins(m.total_active_mins)}</td>
                        <td style={{ ...s.td, color: '#f97316' }}>{fmtMins(m.total_break_mins)}</td>
                        <td style={{ ...s.td, color: '#64748b' }}>{fmtMins(m.total_isolation_mins)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {rangeDaily.length > 0 ? (
            <>
              <h3 style={s.sectionTitle}>Daily Breakdown</h3>
              <div style={s.tableWrap}>
                <table style={s.table}>
                  <thead>
                    <tr>
                      <th style={s.th}>Date</th>
                      <th style={s.th}>Name</th>
                      <th style={s.th}>First Seen</th>
                      <th style={s.th}>Last Seen</th>
                      <th style={s.th}>Active</th>
                      <th style={s.th}>Break</th>
                      <th style={s.th}>Isolation</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rangeDaily.map((d, i) => (
                      <tr key={i} style={i % 2 === 0 ? s.trEven : {}}>
                        <td style={{ ...s.td, fontWeight: 500 }}>{d.date}</td>
                        <td style={s.td}>{d.name}</td>
                        <td style={s.td}>{d.first_seen_ist || '-'}</td>
                        <td style={s.td}>{d.last_seen_ist || '-'}</td>
                        <td style={{ ...s.td, color: '#10b981', fontWeight: 600 }}>{fmtMins(d.active_minutes)}</td>
                        <td style={{ ...s.td, color: '#f97316' }}>{fmtMins(d.break_minutes)}</td>
                        <td style={{ ...s.td, color: '#64748b' }}>{fmtMins(d.isolation_minutes)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : (
            <div style={s.empty}>No attendance data for this date range.</div>
          )}
        </div>
      )}

      {/* ═══ MONTHLY VIEW ═══ */}
      {mode === 'monthly' && selectedTeam && !dataLoading && monthlyData && (
        <div>
          {monthlyData.team_name && (
            <div style={{ marginBottom: 12, fontSize: 13, color: '#64748b' }}>
              <strong style={{ color: '#1e293b' }}>{monthlyData.team_name}</strong>
              {` — ${monthlyData.start_date} to ${monthlyData.end_date}`}
            </div>
          )}

          <MonthlyPivotTables
            monthlyData={monthlyData}
            year={year}
            month={month}
            holidays={monthlyData?.holidays || []}
            user={user}
            onEditCell={(member, cellDate) => {
              setEditModalMember(member);
              setEditModalDate(cellDate);
            }}
          />
        </div>
      )}

      {editModalMember && (
        <AttendanceEditModal
          member={editModalMember}
          date={editModalDate || date}
          onClose={() => { setEditModalMember(null); setEditModalDate(null); }}
          onSave={loadAttendance}
        />
      )}

      {dataLoading && <div style={s.loadingOverlay}><div style={s.spinner} />Loading...</div>}
    </div>
  );
}

function StatCard({ label, value, sub, color }) {
  return (
    <div style={s.statCard}>
      <div style={s.statLabel}>{label}</div>
      <div style={{ fontSize: 26, fontWeight: 800, color, lineHeight: 1.1 }}>{value}</div>
      {sub && <div style={{ fontSize: 10, color: '#94a3b8', marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

const s = {
  header: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 16, flexWrap: 'wrap', gap: 12 },
  title: { fontSize: 22, fontWeight: 800, color: '#0f172a', margin: 0 },
  controls: { display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' },
  select: { padding: '8px 12px', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 13, background: '#fff', cursor: 'pointer' },
  modeToggle: { display: 'flex', background: '#f1f5f9', borderRadius: 8, padding: 3 },
  modeBtn: { padding: '7px 14px', border: 'none', borderRadius: 6, fontSize: 12, fontWeight: 500, cursor: 'pointer', background: 'transparent', color: '#64748b' },
  modeBtnOn: { background: '#0f172a', color: '#fff', fontWeight: 600 },

  dateBar: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20, flexWrap: 'wrap', gap: 10 },
  dateInput: { padding: '8px 12px', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 13 },
  refreshBtn: { padding: '8px 16px', background: '#f1f5f9', color: '#475569', border: '1px solid #e5e7eb', borderRadius: 8, fontSize: 12, cursor: 'pointer' },
  csvBtn: { padding: '7px 14px', background: '#10b981', color: '#fff', border: 'none', borderRadius: 8, fontSize: 12, fontWeight: 600, cursor: 'pointer' },
  pivotXlsxBtn: { padding: '8px 18px', background: '#f97316', color: '#fff', border: 'none', borderRadius: 8, fontSize: 13, fontWeight: 700, cursor: 'pointer', boxShadow: '0 2px 4px rgba(249,115,22,0.35)' },
  holidayBtn: { padding: '8px 14px', background: '#fff', color: '#0f172a', border: '1px solid #cbd5e1', borderRadius: 8, fontSize: 12, fontWeight: 600, cursor: 'pointer' },

  error: { padding: '10px 14px', background: '#fef2f2', color: '#dc2626', border: '1px solid #fecaca', borderRadius: 10, fontSize: 13, marginBottom: 16 },
  loader: { display: 'flex', alignItems: 'center', justifyContent: 'center', height: '50vh', color: '#94a3b8' },
  empty: { textAlign: 'center', padding: '60px 20px', color: '#94a3b8', fontSize: 14 },
  loadingOverlay: { position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, background: 'rgba(255,255,255,0.85)', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', color: '#64748b', zIndex: 100, gap: 12 },
  spinner: { width: 32, height: 32, border: '3px solid #e5e7eb', borderTopColor: '#3b82f6', borderRadius: '50%', animation: 'spin 0.7s linear infinite' },

  statsRow: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 10, marginBottom: 20 },
  statCard: { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 12, padding: '14px 16px' },
  statLabel: { fontSize: 10, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4, fontWeight: 600 },

  sectionTitle: { fontSize: 14, fontWeight: 700, color: '#1e293b', margin: '0 0 10px' },
  tableWrap: { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 12, overflow: 'auto' },
  table: { width: '100%', borderCollapse: 'collapse', minWidth: 750 },
  th: { padding: '10px 14px', textAlign: 'left', fontSize: 11, fontWeight: 600, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.05em', borderBottom: '1px solid #e5e7eb', background: '#f8fafc', whiteSpace: 'nowrap' },
  td: { padding: '10px 14px', fontSize: 13, color: '#1e293b', borderBottom: '1px solid #f1f5f9' },
  trEven: { background: '#fafbfc' },
  badge: { padding: '3px 10px', borderRadius: 12, fontSize: 11, fontWeight: 600, textTransform: 'capitalize', display: 'inline-block' },
  editBtn: { padding: '4px 10px', background: '#eff6ff', color: '#2563eb', border: '1px solid #bfdbfe', borderRadius: 6, fontSize: 11, fontWeight: 600, cursor: 'pointer' },
};
