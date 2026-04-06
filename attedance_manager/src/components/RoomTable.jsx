import { timeToMin } from '../utils/parser';

const COLORS = ['#3b82f6','#10b981','#f59e0b','#ef4444','#8b5cf6','#ec4899','#06b6d4','#f97316','#6366f1','#14b8a6','#e11d48','#84cc16'];
const colorMap = {};
let ci = 0;
function getColor(name) {
  if (!colorMap[name]) { colorMap[name] = COLORS[ci % COLORS.length]; ci++; }
  return colorMap[name];
}
function shortName(n) {
  return n.includes(':') ? n.split(':').slice(1).join(':').trim() : n;
}

export default function RoomTable({ rooms }) {
  if (!rooms || !rooms.length) {
    return <div style={{ padding: 12, color: '#94a3b8', fontSize: 12 }}>No room data</div>;
  }

  // Timeline
  const starts = rooms.map(r => timeToMin(r.start)).filter(t => t > 0);
  const ends = rooms.map(r => timeToMin(r.end)).filter(t => t > 0);
  const minT = Math.min(...starts, ...ends);
  const maxT = Math.max(...starts, ...ends);
  const range = Math.max(maxT - minT, 10);

  // Hour markers
  const hours = [];
  for (let h = Math.floor(minT / 60); h <= Math.ceil(maxT / 60); h++) {
    const p = ((h * 60 - minT) / range) * 100;
    if (p >= 0 && p <= 100) hours.push({ h, p });
  }

  return (
    <div>
      {/* Timeline bar */}
      <div style={s.timelineWrap}>
        <div style={s.hourRow}>
          {hours.map(({ h, p }) => (
            <span key={h} style={{ ...s.hourLabel, left: p + '%' }}>
              {String(h).padStart(2, '0') + ':00'}
            </span>
          ))}
        </div>
        <div style={s.barWrap}>
          {hours.map(({ h, p }) => (
            <div key={h} style={{ ...s.gridLine, left: p + '%' }} />
          ))}
          {rooms.map((r, i) => {
            const rs = timeToMin(r.start);
            const re = timeToMin(r.end);
            const left = ((rs - minT) / range) * 100;
            const width = Math.max(((re - rs) / range) * 100, 0.5);
            return (
              <div key={i}
                title={`${r.name}\n${r.start} – ${r.end} (${r.duration}min)`}
                style={{
                  position: 'absolute',
                  left: left + '%',
                  width: width + '%',
                  height: '100%',
                  background: r.isNamed ? getColor(r.name) : '#cbd5e1',
                  borderRadius: 4,
                  opacity: r.isNamed ? 0.8 : 0.4,
                  zIndex: r.isNamed ? 2 : 1,
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  overflow: 'hidden',
                }}
              >
                {width > 5 && <span style={{ fontSize: 9, color: '#fff', whiteSpace: 'nowrap', padding: '0 2px' }}>{shortName(r.name)}</span>}
              </div>
            );
          })}
        </div>
      </div>

      {/* Legend */}
      <div style={s.legend}>
        {[...new Set(rooms.filter(r => r.isNamed).map(r => r.name))].map(n => (
          <div key={n} style={s.legendItem}>
            <div style={{ ...s.legendDot, background: getColor(n) }} />
            <span style={s.legendText}>{shortName(n)}</span>
          </div>
        ))}
      </div>

      {/* Room table */}
      <table style={s.table}>
        <thead>
          <tr style={s.tableHead}>
            {['#', 'Room', 'Type', 'In', 'Out', 'Duration'].map(h => (
              <th key={h} style={s.th}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rooms.map((r, i) => (
            <tr key={i} style={s.tr}>
              <td style={s.td}><span style={{ ...s.badge, color: '#64748b', background: '#f1f5f9' }}>{i + 1}</span></td>
              <td style={s.td}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <div style={{ width: 8, height: 8, borderRadius: 2, background: r.isNamed ? getColor(r.name) : '#cbd5e1', flexShrink: 0 }} />
                  <span style={{ fontWeight: 500 }}>{shortName(r.name)}</span>
                </div>
              </td>
              <td style={s.td}>
                <span style={{ ...s.badge, color: r.isNamed ? '#2563eb' : '#64748b', background: r.isNamed ? '#eff6ff' : '#f1f5f9' }}>
                  {r.isNamed ? 'Named' : 'Temp'}
                </span>
              </td>
              <td style={s.td}><span style={{ color: '#059669', fontWeight: 500 }}>{r.start || '—'}</span></td>
              <td style={s.td}><span style={{ color: '#dc2626', fontWeight: 500 }}>{r.end || '—'}</span></td>
              <td style={s.td}><span style={{ fontWeight: 500 }}>{r.duration > 0 ? r.duration + 'm' : '<1m'}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const s = {
  timelineWrap: { marginBottom: 12 },
  hourRow: { position: 'relative', height: 18, marginBottom: 2 },
  hourLabel: { position: 'absolute', transform: 'translateX(-50%)', fontSize: 9, color: '#94a3b8', fontFamily: 'monospace' },
  barWrap: { position: 'relative', height: 28, background: '#f1f5f9', borderRadius: 6, overflow: 'hidden' },
  gridLine: { position: 'absolute', top: 0, height: '100%', borderLeft: '1px solid #e2e8f0', zIndex: 0 },
  legend: { display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 12 },
  legendItem: { display: 'flex', alignItems: 'center', gap: 4, padding: '2px 6px', background: '#f8fafc', borderRadius: 4 },
  legendDot: { width: 6, height: 6, borderRadius: 2 },
  legendText: { fontSize: 10, color: '#64748b' },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 12 },
  tableHead: { borderBottom: '1px solid #e5e7eb' },
  th: { padding: '6px 10px', textAlign: 'left', fontSize: 10, fontWeight: 600, color: '#94a3b8', textTransform: 'uppercase' },
  tr: { borderBottom: '1px solid #f1f5f9' },
  td: { padding: '6px 10px' },
  badge: { padding: '2px 8px', borderRadius: 4, fontSize: 10, fontWeight: 600 },
};
