import { useState, useMemo } from 'react';
import { analyzeIsolation, getIsolationLevel } from '../utils/isolation';
import { formatDuration } from '../utils/parser';

export default function Isolation({ allData, uploadedDates: dates }) {
  const [selectedDate, setSelectedDate] = useState(dates[dates.length - 1] || '');
  const [mode, setMode] = useState('daily');
  const [expandedIdx, setExpandedIdx] = useState(null);

  const dailyResults = useMemo(() => {
    if (!selectedDate) return [];
    const emps = allData[selectedDate] || [];
    return analyzeIsolation(emps);
  }, [selectedDate, allData]);

  // Aggregate isolation across all dates
  const aggregateResults = useMemo(() => {
    const empMap = {};
    for (const date of dates) {
      const emps = allData[date] || [];
      const dayResults = analyzeIsolation(emps);
      for (const r of dayResults) {
        const key = r.name.toLowerCase();
        if (!empMap[key]) {
          empMap[key] = {
            name: r.name, email: r.email,
            totalAloneMinutes: 0, totalNamedMinutes: 0,
            daysWithIsolation: 0, totalDays: 0,
            maxScore: 0, scores: [], aloneRoomsList: [],
          };
        }
        const agg = empMap[key];
        // Prefer capitalized version (e.g. "Dev" over "dev")
        if (r.name && r.name[0] !== r.name[0].toLowerCase()) agg.name = r.name;
        if (r.email && !agg.email) agg.email = r.email;
        agg.totalAloneMinutes += r.aloneMinutes;
        agg.totalNamedMinutes += r.totalNamedMinutes;
        agg.totalDays++;
        if (r.aloneMinutes > 0) agg.daysWithIsolation++;
        if (r.isolationScore > agg.maxScore) agg.maxScore = r.isolationScore;
        agg.scores.push(r.isolationScore);
        agg.aloneRoomsList.push(...r.aloneRooms.map(ar => ({ ...ar, date })));
      }
    }
    return Object.values(empMap)
      .map(a => ({
        ...a,
        isolationScore: a.totalNamedMinutes > 0
          ? Math.round((a.totalAloneMinutes / a.totalNamedMinutes) * 100)
          : 0,
        avgScore: a.scores.length ? Math.round(a.scores.reduce((x, y) => x + y, 0) / a.scores.length) : 0,
      }))
      .sort((a, b) => b.isolationScore - a.isolationScore);
  }, [allData, dates]);

  const results = mode === 'daily' ? dailyResults : aggregateResults;

  if (!dates.length) {
    return (
      <div style={{ textAlign: 'center', padding: '80px 20px', color: '#475569' }}>
        <h3>No data uploaded yet</h3>
      </div>
    );
  }

  const highCount = results.filter(r => r.isolationScore >= 70).length;
  const modCount = results.filter(r => r.isolationScore >= 40 && r.isolationScore < 70).length;

  return (
    <div>
      <div style={s.headerRow}>
        <div>
          <h2 style={s.heading}>Isolation Health Check</h2>
          <p style={s.desc}>Detects employees spending time alone in named rooms — potential isolation indicator.</p>
        </div>
        <div style={s.controls}>
          <div style={s.modeToggle}>
            <button onClick={() => { setMode('daily'); setExpandedIdx(null); }}
              style={{ ...s.modeBtn, background: mode === 'daily' ? '#0f172a' : '#f1f5f9', color: mode === 'daily' ? '#fff' : '#64748b' }}>
              Daily
            </button>
            <button onClick={() => { setMode('aggregate'); setExpandedIdx(null); }}
              style={{ ...s.modeBtn, background: mode === 'aggregate' ? '#0f172a' : '#f1f5f9', color: mode === 'aggregate' ? '#fff' : '#64748b' }}>
              All Dates
            </button>
          </div>
          {mode === 'daily' && (
            <select value={selectedDate} onChange={(e) => { setSelectedDate(e.target.value); setExpandedIdx(null); }} style={s.dateSelect}>
              {dates.map(d => <option key={d} value={d}>{d}</option>)}
            </select>
          )}
        </div>
      </div>

      {/* Summary */}
      <div style={s.summaryRow}>
        <div style={{ ...s.alertCard, borderColor: '#fecaca', background: '#fef2f2' }}>
          <div style={{ fontSize: 24, fontWeight: 700, color: '#dc2626' }}>{highCount}</div>
          <div style={{ fontSize: 11, color: '#dc2626', fontWeight: 600 }}>High Isolation</div>
        </div>
        <div style={{ ...s.alertCard, borderColor: '#fde68a', background: '#fffbeb' }}>
          <div style={{ fontSize: 24, fontWeight: 700, color: '#d97706' }}>{modCount}</div>
          <div style={{ fontSize: 11, color: '#d97706', fontWeight: 600 }}>Moderate</div>
        </div>
        <div style={{ ...s.alertCard, borderColor: '#bbf7d0', background: '#f0fdf4' }}>
          <div style={{ fontSize: 24, fontWeight: 700, color: '#059669' }}>{results.length - highCount - modCount}</div>
          <div style={{ fontSize: 11, color: '#059669', fontWeight: 600 }}>Low / Minimal</div>
        </div>
      </div>

      {/* Table */}
      <div style={s.tableWrap}>
        <table style={s.table}>
          <thead>
            <tr style={s.thead}>
              {['Employee', 'Score', 'Level', 'Alone Time', 'Named Room Time', 'Alone Rooms', ''].map(h => (
                <th key={h} style={s.th}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {results.map((r, idx) => {
              const level = getIsolationLevel(r.isolationScore);
              const isOpen = expandedIdx === idx;
              return (
                <IsolationRow key={r.name + r.email}
                  r={r} level={level} isOpen={isOpen} mode={mode}
                  onToggle={() => setExpandedIdx(isOpen ? null : idx)}
                />
              );
            })}
          </tbody>
        </table>
        {results.length === 0 && <div style={{ textAlign: 'center', padding: 40, color: '#94a3b8' }}>No data</div>}
      </div>
    </div>
  );
}

function IsolationRow({ r, level, isOpen, onToggle, mode }) {
  const rooms = mode === 'daily' ? r.aloneRooms : r.aloneRoomsList;
  const shortName = (n) => n.includes(':') ? n.split(':').slice(1).join(':').trim() : n;

  return (
    <>
      <tr onClick={onToggle} style={{ ...s.tr, cursor: 'pointer', background: isOpen ? '#f8fafc' : '#fff' }}>
        <td style={s.td}>
          <div style={{ fontWeight: 500 }}>{r.name}</div>
          {r.email && <div style={{ fontSize: 10, color: '#94a3b8' }}>{r.email}</div>}
        </td>
        <td style={s.td}>
          <div style={s.scoreBar}>
            <div style={{ ...s.scoreFill, width: r.isolationScore + '%', background: level.color }} />
            <span style={s.scoreText}>{r.isolationScore}%</span>
          </div>
        </td>
        <td style={s.td}>
          <span style={{ ...s.levelBadge, color: level.color, background: level.color + '15' }}>{level.label}</span>
        </td>
        <td style={s.td}>{formatDuration(r.aloneMinutes || r.totalAloneMinutes || 0)}</td>
        <td style={s.td}>{formatDuration(r.totalNamedMinutes)}</td>
        <td style={s.td}>{r.uniqueAloneRooms || (rooms ? [...new Set(rooms.map(x => x.room))].length : 0)}</td>
        <td style={s.td}><span style={{ fontSize: 12, color: '#94a3b8' }}>{isOpen ? '\u25B2' : '\u25BC'}</span></td>
      </tr>
      {isOpen && rooms && rooms.length > 0 && (
        <tr>
          <td colSpan={7} style={{ padding: '8px 16px 16px', background: '#f8fafc' }}>
            <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6, color: '#475569' }}>Rooms where alone:</div>
            <table style={{ ...s.table, fontSize: 12 }}>
              <thead>
                <tr style={s.thead}>
                  {(mode === 'aggregate' ? ['Date'] : []).concat(['Room', 'In', 'Out', 'Duration', 'Status']).map(h => (
                    <th key={h} style={s.th}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rooms.map((ar, i) => (
                  <tr key={i} style={s.tr}>
                    {mode === 'aggregate' && <td style={s.td}>{ar.date}</td>}
                    <td style={s.td}>{shortName(ar.room)}</td>
                    <td style={s.td}>{ar.start}</td>
                    <td style={s.td}>{ar.end}</td>
                    <td style={s.td}>{(ar.duration || ar.aloneMinutes || 0) + 'm'}</td>
                    <td style={s.td}>
                      <span style={{ ...s.levelBadge, color: ar.alone ? '#dc2626' : '#d97706', background: ar.alone ? '#fef2f2' : '#fffbeb', fontSize: 10 }}>
                        {ar.alone ? 'Fully alone' : 'Partially alone'}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </td>
        </tr>
      )}
    </>
  );
}

const s = {
  headerRow: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 20, gap: 16, flexWrap: 'wrap' },
  heading: { fontSize: 20, fontWeight: 700, color: '#1e293b', margin: 0 },
  desc: { fontSize: 13, color: '#64748b', marginTop: 2, maxWidth: 500 },
  controls: { display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' },
  modeToggle: { display: 'flex', gap: 2 },
  modeBtn: { padding: '7px 14px', border: 'none', borderRadius: 6, fontSize: 12, fontWeight: 600, cursor: 'pointer' },
  dateSelect: { padding: '7px 10px', border: '1px solid #d1d5db', borderRadius: 6, fontSize: 13, outline: 'none' },
  summaryRow: { display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 10, marginBottom: 20 },
  alertCard: { borderRadius: 10, border: '1px solid', padding: '14px 16px', textAlign: 'center' },
  tableWrap: { background: '#fff', borderRadius: 10, border: '1px solid #e5e7eb', overflow: 'hidden' },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 13 },
  thead: { borderBottom: '2px solid #e5e7eb' },
  th: { padding: '8px 12px', textAlign: 'left', fontSize: 10, fontWeight: 600, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.04em' },
  tr: { borderBottom: '1px solid #f1f5f9', transition: 'background 0.1s' },
  td: { padding: '8px 12px' },
  scoreBar: { position: 'relative', width: 80, height: 20, background: '#f1f5f9', borderRadius: 4, overflow: 'hidden' },
  scoreFill: { position: 'absolute', height: '100%', borderRadius: 4, transition: 'width 0.3s' },
  scoreText: { position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 10, fontWeight: 700 },
  levelBadge: { padding: '3px 8px', borderRadius: 4, fontSize: 11, fontWeight: 600 },
};
