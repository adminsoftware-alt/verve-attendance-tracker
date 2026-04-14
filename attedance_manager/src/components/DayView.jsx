import { useState, useMemo, useEffect, useRef } from 'react';
import { timeToMin, minToTime, formatDuration, todayIST } from '../utils/parser';
import { exportDayViewCsv } from '../utils/exportCsv';
import { useDayData } from '../hooks/useData';
import RoomTable from './RoomTable';

export default function DayView({ allData, uploadedDates, onNavigateUpload }) {
  const today = todayIST();
  const [date, setDate] = useState(uploadedDates.includes(today) ? today : (uploadedDates[uploadedDates.length - 1] || today));
  const [search, setSearch] = useState('');
  const [sortBy, setSortBy] = useState('name');
  const [expanded, setExpanded] = useState(null);
  const hasInitialized = useRef(false);

  // Re-sync selected date when uploadedDates arrives asynchronously
  useEffect(() => {
    if (hasInitialized.current) return; // only auto-select once
    if (uploadedDates.length > 0) {
      hasInitialized.current = true;
      if (uploadedDates.includes(today)) {
        setDate(today);
      } else {
        setDate(uploadedDates[uploadedDates.length - 1]);
      }
    }
  }, [uploadedDates, today]);

  // Load data on-demand for selected date (lazy loading)
  const { employees: dayEmployees, loading: dayLoading } = useDayData(date, 0);

  // Use live data from allData if available, otherwise use lazy-loaded data
  const employees = allData[date] || dayEmployees || [];

  // Step by ±1 calendar day. Don't constrain to uploadedDates — useDayData
  // fetches for any date and the UI handles "No data" gracefully.
  const changeDate = (dir) => {
    const [y, m, d] = date.split('-').map(Number);
    const cur = new Date(y, m - 1, d);
    cur.setDate(cur.getDate() + dir);
    const yy = cur.getFullYear();
    const mm = String(cur.getMonth() + 1).padStart(2, '0');
    const dd = String(cur.getDate()).padStart(2, '0');
    setDate(`${yy}-${mm}-${dd}`);
    setExpanded(null);
  };

  const filtered = useMemo(() => {
    let list = employees.filter(e =>
      e.name.toLowerCase().includes(search.toLowerCase()) ||
      e.email.toLowerCase().includes(search.toLowerCase())
    );
    if (sortBy === 'name') list.sort((a, b) => a.name.localeCompare(b.name));
    else if (sortBy === 'early') list.sort((a, b) => (timeToMin(a.joined) || 9999) - (timeToMin(b.joined) || 9999));
    else if (sortBy === 'late') list.sort((a, b) => (timeToMin(b.joined) || 0) - (timeToMin(a.joined) || 0));
    else if (sortBy === 'duration') list.sort((a, b) => (b.totalMinutes || 0) - (a.totalMinutes || 0));
    return list;
  }, [employees, search, sortBy]);

  const stats = useMemo(() => {
    if (!employees.length) return null;
    const durs = employees.map(e => e.totalMinutes).filter(d => d > 0);
    const joins = employees.map(e => timeToMin(e.joined)).filter(t => t > 0);
    const leaves = employees.map(e => timeToMin(e.left)).filter(t => t > 0);
    return {
      count: employees.length,
      avgDur: durs.length ? formatDuration(durs.reduce((a, b) => a + b, 0) / durs.length) : '--',
      avgJoin: joins.length ? minToTime(Math.round(joins.reduce((a, b) => a + b, 0) / joins.length)) : '--',
      avgLeave: leaves.length ? minToTime(Math.round(leaves.reduce((a, b) => a + b, 0) / leaves.length)) : '--',
    };
  }, [employees]);

  if (!uploadedDates.length) {
    return (
      <div style={s.emptyState}>
        <div style={s.emptyIcon}>{'\u{1F4C4}'}</div>
        <h3 style={s.emptyTitle}>No attendance data yet</h3>
        <p style={s.emptyDesc}>Upload a CSV or XLSX attendance report to get started.</p>
        <button onClick={onNavigateUpload} style={s.primaryBtn}>Go to Upload</button>
      </div>
    );
  }

  // Show loading state while fetching data
  if (dayLoading && !allData[date]) {
    return (
      <div>
        <div style={s.dateNav}>
          <button onClick={() => changeDate(-1)} style={s.arrowBtn}>{'\u2190'}</button>
          <div style={s.dateCenter}>
            <input type="date" value={date}
              onChange={(e) => { setDate(e.target.value); setExpanded(null); }}
              style={s.dateInput} />
            <div style={s.dateLabel}>
              {new Date(date + 'T00:00').toLocaleDateString('en-IN', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })}
            </div>
          </div>
          <button onClick={() => changeDate(1)} style={s.arrowBtn}
            disabled={date >= today}>{'\u2192'}</button>
        </div>
        <div style={s.emptyState}>
          <p style={{ fontSize: 14 }}>Loading data for <strong>{date}</strong>...</p>
        </div>
      </div>
    );
  }

  return (
    <div>
      <div style={s.dateNav}>
        <button onClick={() => changeDate(-1)} style={s.arrowBtn}
          disabled={uploadedDates.indexOf(date) <= 0}>{'\u2190'}</button>
        <div style={s.dateCenter}>
          <input type="date" value={date}
            onChange={(e) => { setDate(e.target.value); setExpanded(null); }}
            style={s.dateInput} />
          <div style={s.dateLabel}>
            {new Date(date + 'T00:00').toLocaleDateString('en-IN', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })}
          </div>
        </div>
        <button onClick={() => changeDate(1)} style={s.arrowBtn}
          disabled={uploadedDates.indexOf(date) >= uploadedDates.length - 1}>{'\u2192'}</button>
      </div>

      {!employees.length ? (
        <div style={s.emptyState}>
          <p style={{ fontSize: 14 }}>No data for <strong>{date}</strong></p>
          <button onClick={onNavigateUpload} style={{ ...s.primaryBtn, padding: '8px 20px', fontSize: 13 }}>Upload for this date</button>
        </div>
      ) : (
        <>
          <div style={s.statsGrid}>
            {[
              { label: 'Employees', value: stats.count, color: '#1a365d', bg: '#eef2ff' },
              { label: 'Avg Duration', value: stats.avgDur, color: '#047857', bg: '#ecfdf5' },
              { label: 'Avg Login', value: stats.avgJoin, color: '#1d4ed8', bg: '#eff6ff' },
              { label: 'Avg Logout', value: stats.avgLeave, color: '#b91c1c', bg: '#fef2f2' },
            ].map((item, i) => (
              <div key={i} style={{ ...s.statCard, background: item.bg }}>
                <div style={s.statLabel}>{item.label}</div>
                <div style={{ ...s.statValue, color: item.color }}>{item.value}</div>
              </div>
            ))}
          </div>

          <div style={s.toolbar}>
            <input value={search} onChange={(e) => setSearch(e.target.value)}
              placeholder="Search by name or email..." style={s.searchInput}
              aria-label="Search employees" />
            <button onClick={() => exportDayViewCsv(filtered, date)} style={s.exportBtn} aria-label="Export as CSV">
              Export CSV
            </button>
            <div style={s.sortGroup}>
              {[['name', 'A-Z'], ['early', 'Early'], ['late', 'Late'], ['duration', 'Longest']].map(([k, l]) => (
                <button key={k} onClick={() => { setSortBy(k); setExpanded(null); }}
                  style={{ ...s.sortBtn, background: sortBy === k ? '#1a365d' : '#fff', color: sortBy === k ? '#fff' : '#64748b', border: sortBy === k ? '1px solid #1a365d' : '1px solid #e5e7eb' }}>
                  {l}
                </button>
              ))}
            </div>
          </div>

          <div style={s.tableCard}>
            <table style={s.table}>
              <thead>
                <tr>
                  {['Name', 'Email', 'Login', 'Logout', 'Duration', 'Sessions', 'Rooms', ''].map(h => (
                    <th key={h} style={s.th}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {filtered.map((emp, idx) => {
                  const isExpanded = expanded === idx;
                  return (
                    <EmployeeRow key={emp.name + emp.email} emp={emp}
                      isExpanded={isExpanded} onToggle={() => setExpanded(isExpanded ? null : idx)} />
                  );
                })}
              </tbody>
            </table>
            {filtered.length === 0 && <div style={s.noResults}>No employees match your search.</div>}
          </div>
          <div style={s.count}>{filtered.length} of {employees.length} employees</div>
        </>
      )}
    </div>
  );
}

function EmployeeRow({ emp, isExpanded, onToggle }) {
  const namedRooms = emp.rooms.filter(r => r.isNamed);
  return (
    <>
      <tr onClick={onToggle} style={{ cursor: 'pointer', background: isExpanded ? '#fafaf8' : 'transparent' }}>
        <td style={s.td}><span style={{ fontWeight: 500, color: '#1e293b' }}>{emp.name}</span></td>
        <td style={{ ...s.td, color: '#94a3b8', fontSize: 12 }}>{emp.email || '\u2014'}</td>
        <td style={s.td}><span style={{ color: '#047857', fontWeight: 600, fontFamily: 'monospace', fontSize: 13 }}>{emp.joined || '\u2014'}</span></td>
        <td style={s.td}><span style={{ color: '#b91c1c', fontWeight: 600, fontFamily: 'monospace', fontSize: 13 }}>{emp.left || '\u2014'}</span></td>
        <td style={s.td}><span style={{ fontWeight: 600 }}>{emp.duration}</span></td>
        <td style={s.td}><span style={s.chipNeutral}>{emp.sessions}</span></td>
        <td style={s.td}><span style={s.chipBlue}>{namedRooms.length}</span></td>
        <td style={s.td}><span style={{ fontSize: 11, color: '#94a3b8' }}>{isExpanded ? '\u25B2' : '\u25BC'}</span></td>
      </tr>
      {isExpanded && (
        <tr><td colSpan={8} style={{ padding: '4px 16px 16px', background: '#fafaf8', borderBottom: '2px solid #e5e7eb' }}>
          <RoomTable rooms={emp.rooms} />
        </td></tr>
      )}
    </>
  );
}

const s = {
  emptyState: { textAlign: 'center', padding: '100px 20px', color: '#475569' },
  emptyIcon: { fontSize: 40, marginBottom: 12, opacity: 0.4 },
  emptyTitle: { fontWeight: 600, fontSize: 18, marginBottom: 6 },
  emptyDesc: { fontSize: 13, color: '#94a3b8', marginBottom: 20 },
  primaryBtn: { padding: '10px 28px', background: '#1a365d', color: '#fff', border: 'none', borderRadius: 8, fontSize: 14, fontWeight: 600, cursor: 'pointer' },
  dateNav: { display: 'flex', alignItems: 'center', gap: 14, marginBottom: 24 },
  arrowBtn: { width: 38, height: 38, borderRadius: 10, border: '1px solid #e5e7eb', background: '#fff', fontSize: 16, cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#1a365d', fontWeight: 600 },
  dateCenter: { display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 3 },
  dateInput: { padding: '7px 12px', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 13, outline: 'none', background: '#fff' },
  dateLabel: { fontSize: 12, color: '#64748b', fontWeight: 500 },
  statsGrid: { display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginBottom: 22 },
  statCard: { borderRadius: 12, padding: '16px 18px', border: '1px solid rgba(0,0,0,0.04)' },
  statLabel: { fontSize: 10, fontWeight: 700, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 6 },
  statValue: { fontSize: 24, fontWeight: 700 },
  toolbar: { display: 'flex', gap: 10, marginBottom: 16, flexWrap: 'wrap' },
  searchInput: { flex: 1, minWidth: 200, padding: '10px 14px', border: '1px solid #e5e7eb', borderRadius: 10, fontSize: 13, outline: 'none', background: '#fff' },
  sortGroup: { display: 'flex', gap: 4 },
  exportBtn: { padding: '8px 14px', background: '#059669', color: '#fff', border: 'none', borderRadius: 8, fontSize: 12, fontWeight: 600, cursor: 'pointer', whiteSpace: 'nowrap' },
  sortBtn: { padding: '8px 14px', borderRadius: 8, fontSize: 11, fontWeight: 600, cursor: 'pointer', transition: 'all 0.15s' },
  tableCard: { background: '#fff', borderRadius: 12, border: '1px solid #e5e7eb', overflow: 'hidden', boxShadow: '0 1px 3px rgba(0,0,0,0.04)' },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 13 },
  th: { padding: '12px 14px', textAlign: 'left', fontSize: 10, fontWeight: 700, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.06em', borderBottom: '2px solid #f1f5f9', background: '#fafaf8' },
  td: { padding: '11px 14px', borderBottom: '1px solid #f1f5f9' },
  chipNeutral: { padding: '3px 10px', borderRadius: 6, fontSize: 11, fontWeight: 600, background: '#f1f5f9', color: '#475569' },
  chipBlue: { padding: '3px 10px', borderRadius: 6, fontSize: 11, fontWeight: 600, background: '#eef2ff', color: '#1a365d' },
  noResults: { padding: 32, textAlign: 'center', color: '#94a3b8', fontSize: 13 },
  count: { marginTop: 10, fontSize: 12, color: '#94a3b8', textAlign: 'right' },
};
