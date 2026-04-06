import { useState, useMemo } from 'react';
import { timeToMin, minToTime, formatDuration } from '../utils/parser';
import { exportEmployeeCsv } from '../utils/exportCsv';
import RoomTable from './RoomTable';

export default function Employees({ allData, uploadedDates }) {
  const [search, setSearch] = useState('');
  const [sortBy, setSortBy] = useState('name');
  const [selectedEmp, setSelectedEmp] = useState(null);
  const [expandedDate, setExpandedDate] = useState(null);

  const employeeIndex = useMemo(() => {
    const map = {};
    for (const date of uploadedDates) {
      const emps = allData[date] || [];
      for (const emp of emps) {
        // Always group by name — same name across days = same person
        const key = emp.name.toLowerCase();
        if (!map[key]) map[key] = { name: emp.name, email: emp.email || '', days: {} };
        // Prefer capitalized version (e.g. "Dev" over "dev")
        if (emp.name && emp.name[0] !== emp.name[0].toLowerCase()) map[key].name = emp.name;
        // Collect email from whichever day has it
        if (emp.email && !map[key].email) map[key].email = emp.email;
        map[key].days[date] = emp;
      }
    }
    return Object.values(map);
  }, [allData, uploadedDates]);

  const filtered = useMemo(() => {
    let list = employeeIndex.filter(e =>
      e.name.toLowerCase().includes(search.toLowerCase()) ||
      e.email.toLowerCase().includes(search.toLowerCase())
    );
    if (sortBy === 'name') list.sort((a, b) => a.name.localeCompare(b.name));
    else if (sortBy === 'days') list.sort((a, b) => Object.keys(b.days).length - Object.keys(a.days).length);
    else if (sortBy === 'hours') {
      list.sort((a, b) => {
        const ta = Object.values(a.days).reduce((s, d) => s + (d.totalMinutes || 0), 0);
        const tb = Object.values(b.days).reduce((s, d) => s + (d.totalMinutes || 0), 0);
        return tb - ta;
      });
    }
    return list;
  }, [employeeIndex, search, sortBy]);

  const selectedData = selectedEmp !== null ? filtered[selectedEmp] : null;

  if (!uploadedDates.length) {
    return <div style={{ textAlign: 'center', padding: '80px 20px', color: '#475569' }}><h3>No data uploaded yet</h3></div>;
  }

  return (
    <div style={{ display: 'flex', gap: 20, height: 'calc(100vh - 80px)' }}>
      <div style={s.listPanel}>
        <h3 style={s.heading}>All Employees</h3>
        <input value={search} onChange={(e) => { setSearch(e.target.value); setSelectedEmp(null); }}
          placeholder="Search..." style={s.searchInput} />
        <div style={s.sortRow}>
          {[['name', 'A-Z'], ['days', 'Most Days'], ['hours', 'Most Hours']].map(([k, l]) => (
            <button key={k} onClick={() => { setSortBy(k); setSelectedEmp(null); }}
              style={{ ...s.sortBtn, background: sortBy === k ? '#1a365d' : '#f1f5f9', color: sortBy === k ? '#fff' : '#64748b' }}>{l}</button>
          ))}
        </div>
        <div style={s.listScroll}>
          {filtered.map((emp, idx) => {
            const dayCount = Object.keys(emp.days).length;
            const totalMin = Object.values(emp.days).reduce((s, d) => s + (d.totalMinutes || 0), 0);
            const isActive = selectedEmp === idx;
            return (
              <div key={emp.email || emp.name}
                onClick={() => { setSelectedEmp(idx); setExpandedDate(null); }}
                style={{ ...s.listItem, background: isActive ? '#eef2ff' : '#fff', borderLeft: isActive ? '3px solid #1a365d' : '3px solid transparent' }}>
                <div style={s.listName}>{emp.name}</div>
                <div style={s.listMeta}><span>{dayCount} day{dayCount !== 1 ? 's' : ''}</span><span>{formatDuration(totalMin)}</span></div>
              </div>
            );
          })}
        </div>
        <div style={s.listCount}>{filtered.length} employees</div>
      </div>

      <div style={s.detailPanel}>
        {!selectedData ? (
          <div style={s.placeholder}>
            <div style={{ fontSize: 28, marginBottom: 8 }}>{'\u{1F448}'}</div>
            <p style={{ fontWeight: 600 }}>Select an employee</p>
            <p style={{ fontSize: 12, color: '#94a3b8' }}>Click a name from the list</p>
          </div>
        ) : (
          <EmployeeDetail emp={selectedData} expandedDate={expandedDate} setExpandedDate={setExpandedDate} />
        )}
      </div>
    </div>
  );
}

function EmployeeDetail({ emp, expandedDate, setExpandedDate }) {
  const days = Object.entries(emp.days).sort((a, b) => b[0].localeCompare(a[0]));
  const totalMin = days.reduce((s, [, d]) => s + (d.totalMinutes || 0), 0);
  const avgMin = days.length ? Math.round(totalMin / days.length) : 0;
  const joins = days.map(([, d]) => timeToMin(d.joined)).filter(t => t > 0);
  const avgJoin = joins.length ? minToTime(Math.round(joins.reduce((a, b) => a + b, 0) / joins.length)) : '--';
  const leaves = days.map(([, d]) => timeToMin(d.left)).filter(t => t > 0);
  const avgLeave = leaves.length ? minToTime(Math.round(leaves.reduce((a, b) => a + b, 0) / leaves.length)) : '--';

  return (
    <div>
      <div style={s.empHeader}>
        <div style={s.avatar}>{emp.name.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase()}</div>
        <div style={{ flex: 1 }}>
          <h2 style={s.empName}>{emp.name}</h2>
          <div style={s.empEmail}>{emp.email || 'No email'}</div>
        </div>
        <button onClick={() => exportEmployeeCsv(emp)} style={s.exportBtn} aria-label="Export employee CSV">Export CSV</button>
      </div>

      <div style={s.summaryRow}>
        {[
          { label: 'Days Present', value: days.length, color: '#1a365d' },
          { label: 'Total Hours', value: formatDuration(totalMin), color: '#059669' },
          { label: 'Avg/Day', value: formatDuration(avgMin), color: '#2563eb' },
          { label: 'Avg Login', value: avgJoin, color: '#059669' },
          { label: 'Avg Logout', value: avgLeave, color: '#dc2626' },
        ].map((item, i) => (
          <div key={i} style={st.card}>
            <div style={st.cardLabel}>{item.label}</div>
            <div style={{ ...st.cardValue, color: item.color }}>{item.value}</div>
          </div>
        ))}
      </div>

      <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 10, color: '#1e293b' }}>Day-wise Attendance</h3>
      <div style={s.dayList}>
        {days.map(([date, dayData]) => {
          const isOpen = expandedDate === date;
          return (
            <div key={date} style={s.dayCard}>
              <div onClick={() => setExpandedDate(isOpen ? null : date)} style={s.dayRow}>
                <span style={s.dayDate}>{date}</span>
                <span style={s.dayWeekday}>{new Date(date + 'T00:00').toLocaleDateString('en-IN', { weekday: 'short' })}</span>
                <span style={{ color: '#059669' }}>{dayData.joined || '\u2014'}</span>
                <span style={{ color: '#dc2626' }}>{dayData.left || '\u2014'}</span>
                <span style={{ fontWeight: 600 }}>{dayData.duration}</span>
                <span style={{ fontSize: 12, color: '#94a3b8' }}>{dayData.rooms.filter(r => r.isNamed).length} rooms</span>
                <span style={{ fontSize: 12, color: '#94a3b8' }}>{isOpen ? '\u25B2' : '\u25BC'}</span>
              </div>
              {isOpen && <div style={s.dayExpanded}><RoomTable rooms={dayData.rooms} /></div>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

const s = {
  listPanel: { width: 280, minWidth: 280, background: '#fff', borderRadius: 12, border: '1px solid #e5e7eb', display: 'flex', flexDirection: 'column', overflow: 'hidden', boxShadow: '0 1px 3px rgba(0,0,0,0.04)' },
  heading: { fontSize: 14, fontWeight: 600, padding: '14px 14px 8px', color: '#1e293b' },
  searchInput: { margin: '0 10px 8px', padding: '8px 10px', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 12, outline: 'none' },
  sortRow: { display: 'flex', gap: 3, padding: '0 10px 8px' },
  sortBtn: { flex: 1, padding: '5px 0', border: 'none', borderRadius: 6, fontSize: 10, fontWeight: 600, cursor: 'pointer' },
  listScroll: { flex: 1, overflow: 'auto', padding: '0 4px' },
  listItem: { padding: '8px 10px', marginBottom: 1, borderRadius: 6, cursor: 'pointer', transition: 'all 0.1s' },
  listName: { fontSize: 12, fontWeight: 500, color: '#1e293b' },
  listMeta: { display: 'flex', gap: 10, fontSize: 10, color: '#94a3b8', marginTop: 2 },
  listCount: { padding: '8px 14px', borderTop: '1px solid #e5e7eb', fontSize: 10, color: '#94a3b8', textAlign: 'center' },
  detailPanel: { flex: 1, overflow: 'auto', paddingRight: 4 },
  placeholder: { display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '50vh', color: '#475569' },
  empHeader: { display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20 },
  avatar: { width: 44, height: 44, borderRadius: 10, background: '#1a365d', color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 16, fontWeight: 700 },
  empName: { fontSize: 20, fontWeight: 700, margin: 0, color: '#1e293b' },
  empEmail: { fontSize: 12, color: '#64748b' },
  exportBtn: { padding: '6px 12px', background: '#059669', color: '#fff', border: 'none', borderRadius: 6, fontSize: 11, fontWeight: 600, cursor: 'pointer' },
  summaryRow: { display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 8, marginBottom: 20 },
  dayList: { display: 'flex', flexDirection: 'column', gap: 4 },
  dayCard: { background: '#fff', borderRadius: 8, border: '1px solid #e5e7eb', overflow: 'hidden' },
  dayRow: { display: 'grid', gridTemplateColumns: '100px 50px 60px 60px 70px 70px 20px', alignItems: 'center', padding: '10px 12px', cursor: 'pointer', fontSize: 13, gap: 8 },
  dayDate: { fontWeight: 600, color: '#1e293b' },
  dayWeekday: { fontSize: 11, color: '#94a3b8' },
  dayExpanded: { padding: '4px 12px 12px', borderTop: '1px solid #f1f5f9' },
};

const st = {
  card: { background: '#fff', borderRadius: 10, border: '1px solid #e5e7eb', padding: '10px 12px' },
  cardLabel: { fontSize: 9, fontWeight: 600, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 3 },
  cardValue: { fontSize: 18, fontWeight: 700 },
};
