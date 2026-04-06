import { useState, useEffect, useCallback, useMemo } from 'react';
import { fetchTeams, fetchTeamMonthlyReport } from '../utils/zoomApi';

function statusColor(status) {
  switch (status) {
    case 'Present': return '#10b981';
    case 'Half Day': return '#f59e0b';
    case 'Absent': return '#ef4444';
    default: return '#e5e7eb';
  }
}

function statusBg(status) {
  switch (status) {
    case 'Present': return '#dcfce7';
    case 'Half Day': return '#fef3c7';
    case 'Absent': return '#fef2f2';
    default: return '#f8fafc';
  }
}

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

export default function CalendarView({ user }) {
  const [teams, setTeams] = useState([]);
  const [selectedTeam, setSelectedTeam] = useState('');
  const [year, setYear] = useState(new Date().getFullYear());
  const [month, setMonth] = useState(new Date().getMonth() + 1);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [dataLoading, setDataLoading] = useState(false);
  const [selectedMember, setSelectedMember] = useState('all');

  const isManager = user?.role === 'manager';

  useEffect(() => {
    fetchTeams().then(d => {
      let list = d.teams || [];
      if (isManager && user?.name) {
        list = list.filter(t =>
          (t.manager_name || '').toLowerCase().trim() === user.name.toLowerCase().trim()
          || (t.manager_email || '').toLowerCase().trim() === (user?.email || '').toLowerCase().trim()
        );
      }
      setTeams(list);
      if (list.length > 0) setSelectedTeam(list[0].team_id);
    }).catch(console.error).finally(() => setLoading(false));
  }, [isManager, user?.name, user?.email]);

  const loadData = useCallback(async () => {
    if (!selectedTeam) return;
    setDataLoading(true);
    try {
      const d = await fetchTeamMonthlyReport(selectedTeam, year, month);
      setData(d);
    } catch (e) { console.error(e); }
    setDataLoading(false);
  }, [selectedTeam, year, month]);

  useEffect(() => { loadData(); }, [loadData]);

  // Build lookup: { "name|date": status }
  const statusMap = useMemo(() => {
    if (!data?.daily_data) return {};
    const map = {};
    data.daily_data.forEach(d => {
      const active = d.active_minutes || 0;
      const status = active >= 300 ? 'Present' : active >= 240 ? 'Half Day' : 'Absent';
      map[`${d.name}|${d.date}`] = { status, mins: active };
    });
    return map;
  }, [data]);

  // Unique member names
  const members = useMemo(() => {
    if (!data?.member_summary) return [];
    return data.member_summary.map(m => m.name).sort();
  }, [data]);

  // Calendar grid
  const calendarDays = useMemo(() => {
    const firstDay = new Date(year, month - 1, 1);
    const lastDay = new Date(year, month, 0);
    const daysInMonth = lastDay.getDate();
    const startDow = firstDay.getDay();

    const days = [];
    // Padding
    for (let i = 0; i < startDow; i++) days.push(null);
    for (let d = 1; d <= daysInMonth; d++) {
      const dateStr = `${year}-${String(month).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
      const dow = new Date(year, month - 1, d).getDay();
      days.push({ day: d, date: dateStr, isWeekend: dow === 0 || dow === 6 });
    }
    return days;
  }, [year, month]);

  // Filter members
  const visibleMembers = selectedMember === 'all' ? members : [selectedMember];

  // Stats for a member in the month
  const getMemberStats = (name) => {
    let present = 0, half = 0, absent = 0;
    calendarDays.forEach(d => {
      if (!d) return;
      const key = `${name}|${d.date}`;
      const entry = statusMap[key];
      if (entry?.status === 'Present') present++;
      else if (entry?.status === 'Half Day') half++;
      else if (!d.isWeekend) absent++;
    });
    return { present, half, absent };
  };

  if (loading) return <div style={s.loader}>Loading...</div>;

  return (
    <div>
      <div style={s.header}>
        <h2 style={s.title}>Attendance Calendar</h2>
        <div style={s.controls}>
          <select value={selectedTeam} onChange={e => { setSelectedTeam(e.target.value); setSelectedMember('all'); }} style={s.select}>
            <option value="">Select team</option>
            {teams.map(t => <option key={t.team_id} value={t.team_id}>{t.team_name}</option>)}
          </select>
          <select value={month} onChange={e => setMonth(+e.target.value)} style={s.select}>
            {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'].map((m, i) => (
              <option key={i} value={i + 1}>{m}</option>
            ))}
          </select>
          <select value={year} onChange={e => setYear(+e.target.value)} style={s.select}>
            {[2024, 2025, 2026].map(y => <option key={y} value={y}>{y}</option>)}
          </select>
          <select value={selectedMember} onChange={e => setSelectedMember(e.target.value)} style={{ ...s.select, maxWidth: 180 }}>
            <option value="all">All Members</option>
            {members.map(m => <option key={m} value={m}>{m}</option>)}
          </select>
        </div>
      </div>

      {/* Legend */}
      <div style={s.legend}>
        <span style={s.legendItem}><span style={{ ...s.legendDot, background: '#10b981' }} /> Present (5h+)</span>
        <span style={s.legendItem}><span style={{ ...s.legendDot, background: '#f59e0b' }} /> Half Day (4-5h)</span>
        <span style={s.legendItem}><span style={{ ...s.legendDot, background: '#ef4444' }} /> Absent (&lt;4h)</span>
        <span style={s.legendItem}><span style={{ ...s.legendDot, background: '#e5e7eb' }} /> No Data / Weekend</span>
      </div>

      {dataLoading && <div style={{ textAlign: 'center', padding: 20, color: '#94a3b8' }}>Loading...</div>}

      {/* Per-member calendars */}
      {!dataLoading && visibleMembers.map(name => {
        const stats = getMemberStats(name);
        return (
          <div key={name} style={s.memberSection}>
            <div style={s.memberHeader}>
              <div>
                <span style={s.memberName}>{name}</span>
                <span style={s.memberStats}>
                  <span style={{ color: '#10b981' }}>{stats.present}P</span>
                  {stats.half > 0 && <span style={{ color: '#f59e0b' }}> {stats.half}H</span>}
                  <span style={{ color: '#ef4444' }}> {stats.absent}A</span>
                </span>
              </div>
            </div>

            {/* Calendar grid */}
            <div style={s.calGrid}>
              {/* Day headers */}
              {DAYS.map(d => <div key={d} style={s.calDayHeader}>{d}</div>)}

              {/* Day cells */}
              {calendarDays.map((d, i) => {
                if (!d) return <div key={`e${i}`} style={s.calEmpty} />;

                const key = `${name}|${d.date}`;
                const entry = statusMap[key];
                const bg = entry ? statusBg(entry.status) : d.isWeekend ? '#f1f5f9' : '#f8fafc';
                const dotColor = entry ? statusColor(entry.status) : '#e5e7eb';
                const tooltip = entry ? `${entry.status} (${entry.mins}m)` : d.isWeekend ? 'Weekend' : 'No data';

                return (
                  <div key={d.date} style={{ ...s.calCell, background: bg, opacity: d.isWeekend ? 0.5 : 1 }} title={tooltip}>
                    <div style={s.calDate}>{d.day}</div>
                    <div style={{ ...s.calDot, background: dotColor }} />
                    {entry && <div style={{ fontSize: 9, color: '#64748b', marginTop: 1 }}>{entry.mins}m</div>}
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}

      {!dataLoading && members.length === 0 && selectedTeam && (
        <div style={s.empty}>No attendance data for this month.</div>
      )}
    </div>
  );
}

const s = {
  header: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 16, flexWrap: 'wrap', gap: 12 },
  title: { fontSize: 22, fontWeight: 800, color: '#0f172a', margin: 0 },
  controls: { display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' },
  select: { padding: '8px 12px', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 13, background: '#fff', cursor: 'pointer' },
  loader: { display: 'flex', alignItems: 'center', justifyContent: 'center', height: '50vh', color: '#94a3b8' },
  empty: { textAlign: 'center', padding: '60px 20px', color: '#94a3b8', fontSize: 14 },

  legend: { display: 'flex', gap: 16, marginBottom: 20, flexWrap: 'wrap' },
  legendItem: { display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: '#64748b' },
  legendDot: { width: 10, height: 10, borderRadius: 3 },

  memberSection: { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 14, marginBottom: 16, overflow: 'hidden' },
  memberHeader: { padding: '12px 20px', borderBottom: '1px solid #f1f5f9', display: 'flex', justifyContent: 'space-between', alignItems: 'center' },
  memberName: { fontSize: 14, fontWeight: 700, color: '#1e293b' },
  memberStats: { marginLeft: 12, fontSize: 12, fontWeight: 600 },

  calGrid: { display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: 4, padding: 12 },
  calDayHeader: { textAlign: 'center', fontSize: 10, fontWeight: 600, color: '#94a3b8', textTransform: 'uppercase', padding: '4px 0' },
  calEmpty: { minHeight: 50 },
  calCell: { minHeight: 56, borderRadius: 8, padding: '6px 4px', display: 'flex', flexDirection: 'column', alignItems: 'center', cursor: 'default', transition: 'transform 0.1s' },
  calDate: { fontSize: 12, fontWeight: 600, color: '#475569' },
  calDot: { width: 8, height: 8, borderRadius: '50%', marginTop: 4 },
};
