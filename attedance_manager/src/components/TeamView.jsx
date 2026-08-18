import { useState, useEffect, useCallback } from 'react';
import {
  fetchTeams, fetchTeamAttendanceRange,
  fetchTeamMonthlyReport, getTeamRangeCsvUrl
} from '../utils/zoomApi';
import { downloadTeamPivotExcel } from '../utils/teamPivotExcel';
import MonthlyPivotTables from './MonthlyPivotTables';
import AttendanceEditModal from './AttendanceEditModal';

// Pseudo-team covering everyone who joined the Zoom meeting, roster or not.
// Must match ALL_MEMBERS_TEAM_ID in app.py — the backend routes on this id.
const ALL_MEMBERS_ID = '__all__';
const ALL_MEMBERS_NAME = 'All Members';

function istDate() {
  // Business date, not calendar date: the attendance day runs 05:00->05:00
  // IST, so before 05:00 IST "today" is still yesterday's business day.
  // IST offset (+330 min) minus the 5h day-start (-300 min) = +30 min.
  const now = new Date();
  return new Date(now.getTime() + 30 * 60000).toISOString().slice(0, 10);
}

export default function TeamView({ user }) {
  const [teams, setTeams] = useState([]);
  const [selectedTeam, setSelectedTeam] = useState('');
  const [mode, setMode] = useState('monthly');      // monthly | custom
  const [startDate, setStartDate] = useState(istDate);
  const [endDate, setEndDate] = useState(istDate);
  const [year, setYear] = useState(new Date().getFullYear());
  const [month, setMonth] = useState(new Date().getMonth() + 1);
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
      if (mode === 'custom') {
        // Custom Period reuses the range endpoint, whose daily_data carries
        // the same per-day fields the monthly report does — so the pivots
        // below produce identical metrics over an arbitrary range.
        const data = await fetchTeamAttendanceRange(selectedTeam, startDate, endDate);
        setRangeData(data);
        setMonthlyData(null);
      } else {
        const data = await fetchTeamMonthlyReport(selectedTeam, year, month);
        setMonthlyData(data);
        setRangeData(null);
      }
    } catch (e) { setError(e.message); }
    setDataLoading(false);
  }, [selectedTeam, mode, startDate, endDate, year, month]);

  useEffect(() => { loadAttendance(); }, [loadAttendance]);

  const downloadRangeCsv = () => {
    if (!selectedTeam) return;
    window.open(getTeamRangeCsvUrl(selectedTeam, startDate, endDate), '_blank');
  };
  const downloadPivotExcel = async () => {
    if (!selectedTeam) return;
    try {
      const team = selectedTeam === ALL_MEMBERS_ID
        ? { team_id: ALL_MEMBERS_ID, team_name: ALL_MEMBERS_NAME }
        : (teams.find(t => t.team_id === selectedTeam) || {});

      if (mode === 'custom') {
        // Same workbook, bounded by the chosen period instead of a month.
        let data = rangeData;
        if (!data || data.start_date !== startDate || data.end_date !== endDate) {
          setDataLoading(true);
          data = await fetchTeamAttendanceRange(selectedTeam, startDate, endDate);
          setRangeData(data);
          setDataLoading(false);
        }
        downloadTeamPivotExcel(data, team, year, month, { startDate, endDate });
        return;
      }

      // Ensure we have fresh data for the selected month
      let data = monthlyData;
      if (!data || data.team_id !== selectedTeam || data.year !== year || data.month !== month) {
        setDataLoading(true);
        data = await fetchTeamMonthlyReport(selectedTeam, year, month);
        setMonthlyData(data);
        setDataLoading(false);
      }
      downloadTeamPivotExcel(data, team, year, month);
    } catch (e) {
      setError(e.message);
      setDataLoading(false);
    }
  };

  if (loading && teams.length === 0) return <div style={s.loader}>Loading...</div>;

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
            {/* Managers are scoped to their own teams, so they don't get the
                everyone-in-the-meeting view. */}
            {!isManager && <option value={ALL_MEMBERS_ID}>{ALL_MEMBERS_NAME}</option>}
            {teams.map(t => <option key={t.team_id} value={t.team_id}>{t.team_name}</option>)}
          </select>
          <div style={s.modeToggle}>
            <button onClick={() => setMode('monthly')} style={{ ...s.modeBtn, ...(mode === 'monthly' ? s.modeBtnOn : {}) }}>Monthly</button>
            <button onClick={() => setMode('custom')} style={{ ...s.modeBtn, ...(mode === 'custom' ? s.modeBtnOn : {}) }}>Custom Period</button>
          </div>
        </div>
      </div>

      {error && <div style={s.error}>{error}</div>}

      {/* Date controls */}
      <div style={s.dateBar}>
        {mode === 'custom' && (
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <label style={{ fontSize: 12, color: '#64748b' }}>From</label>
            <input type="date" value={startDate} onChange={e => setStartDate(e.target.value)} style={s.dateInput} />
            <label style={{ fontSize: 12, color: '#64748b' }}>To</label>
            <input type="date" value={endDate} onChange={e => setEndDate(e.target.value)} style={s.dateInput} />
            <button onClick={downloadRangeCsv} style={s.csvBtn}>CSV</button>
            <button onClick={downloadPivotExcel} style={s.pivotXlsxBtn} title="Excel report for this period">Download Excel Report</button>
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

      {/* ═══ MONTHLY / CUSTOM PERIOD — identical metrics ═══ */}
      {mode === 'custom' && selectedTeam && !dataLoading && rangeData && (
        <div>
          <div style={{ marginBottom: 12, fontSize: 13, color: '#64748b' }}>
            <strong style={{ color: '#1e293b' }}>{rangeData.team_name}</strong>
            {` — ${rangeData.start_date} to ${rangeData.end_date}`}
          </div>
          <MonthlyPivotTables
            monthlyData={rangeData}
            year={year}
            month={month}
            startDate={rangeData.start_date}
            endDate={rangeData.end_date}
            holidays={rangeData?.holidays || []}
            user={user}
            onEditCell={(member, cellDate) => {
              setEditModalMember(member);
              setEditModalDate(cellDate);
            }}
          />
        </div>
      )}

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
