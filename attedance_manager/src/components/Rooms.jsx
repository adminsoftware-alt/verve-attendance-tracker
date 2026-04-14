import { useMemo, useState } from 'react';
import { formatDuration } from '../utils/parser';

const COLORS = ['#3b82f6','#10b981','#f59e0b','#ef4444','#8b5cf6','#ec4899','#06b6d4','#f97316','#6366f1','#14b8a6','#e11d48','#84cc16'];

export default function Rooms({ allData, uploadedDates: dates }) {
  const [search, setSearch] = useState('');

  const roomStats = useMemo(() => {
    const map = {};
    for (const date of dates) {
      const emps = allData[date] || [];
      for (const emp of emps) {
        for (const room of emp.rooms) {
          if (!room.isNamed) continue;
          const rKey = room.name.toLowerCase();
          if (!map[rKey]) {
            map[rKey] = { name: room.name, visits: 0, totalMinutes: 0, visitors: {}, dates: new Set() };
          }
          map[rKey].visits++;
          map[rKey].totalMinutes += room.duration || 0;
          map[rKey].visitors[emp.name.toLowerCase()] = emp.name;
          map[rKey].dates.add(date);
        }
      }
    }
    return Object.values(map)
      .map(r => ({ ...r, visitors: Object.values(r.visitors), dates: [...r.dates] }))
      .sort((a, b) => b.visits - a.visits);
  }, [allData, dates]);

  const filtered = roomStats.filter(r =>
    r.name.toLowerCase().includes(search.toLowerCase())
  );

  const shortName = (n) => n.includes(':') ? n.split(':').slice(1).join(':').trim() : n;

  if (!dates.length) {
    return (
      <div style={{ textAlign: 'center', padding: '80px 20px', color: '#475569' }}>
        <h3>No data uploaded yet</h3>
      </div>
    );
  }

  return (
    <div>
      <div style={s.header}>
        <div>
          <h2 style={s.heading}>Room Analytics</h2>
          <p style={s.desc}>Named room usage across all uploaded dates</p>
        </div>
        <input value={search} onChange={(e) => setSearch(e.target.value)}
          placeholder="Search rooms..." style={s.searchInput} />
      </div>

      {/* Summary */}
      <div style={s.summaryRow}>
        <div style={s.summaryCard}>
          <div style={s.summaryLabel}>Total Rooms</div>
          <div style={{ ...s.summaryValue, color: '#0f172a' }}>{roomStats.length}</div>
        </div>
        <div style={s.summaryCard}>
          <div style={s.summaryLabel}>Total Visits</div>
          <div style={{ ...s.summaryValue, color: '#059669' }}>{roomStats.reduce((s, r) => s + r.visits, 0)}</div>
        </div>
        <div style={s.summaryCard}>
          <div style={s.summaryLabel}>Total Time</div>
          <div style={{ ...s.summaryValue, color: '#2563eb' }}>{formatDuration(roomStats.reduce((s, r) => s + r.totalMinutes, 0))}</div>
        </div>
      </div>

      {/* Top rooms bar chart */}
      {filtered.length > 0 && (
        <div style={s.barSection}>
          <h3 style={s.subHead}>Top Rooms by Visits</h3>
          {filtered.slice(0, 15).map((room, i) => {
            const maxVisits = filtered[0]?.visits || 1;
            const color = COLORS[i % COLORS.length];
            return (
              <div key={room.name} style={s.barRow}>
                <span style={s.barRank}>{String(i + 1).padStart(2, '0')}</span>
                <div style={s.barTrack}>
                  <div style={{ ...s.barFill, width: (room.visits / maxVisits) * 100 + '%', background: color + '30' }} />
                  <div style={s.barContent}>
                    <span style={s.barName}>{shortName(room.name)}</span>
                    <span style={s.barMeta}>{room.visits} visits &middot; {room.visitors.length} visitors &middot; {formatDuration(room.totalMinutes)}</span>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Room cards */}
      <div style={s.grid}>
        {filtered.map((room, i) => {
          const color = COLORS[i % COLORS.length];
          return (
            <div key={room.name} style={s.card}>
              <div style={{ ...s.cardAccent, background: color }} />
              <div style={s.cardName}>{shortName(room.name)}</div>
              <div style={s.cardId}>{room.name.split(':')[0]}</div>
              <div style={s.cardStats}>
                <div>
                  <div style={s.cardStatLabel}>Visits</div>
                  <div style={{ ...s.cardStatValue, color }}>{room.visits}</div>
                </div>
                <div>
                  <div style={s.cardStatLabel}>Visitors</div>
                  <div style={s.cardStatValue}>{room.visitors.length}</div>
                </div>
                <div>
                  <div style={s.cardStatLabel}>Total Time</div>
                  <div style={s.cardStatValue}>{formatDuration(room.totalMinutes)}</div>
                </div>
                <div>
                  <div style={s.cardStatLabel}>Days Active</div>
                  <div style={s.cardStatValue}>{room.dates.length}</div>
                </div>
              </div>
              <div style={s.cardVisitors}>
                {room.visitors.slice(0, 8).map((v, vi) => (
                  <span key={vi} style={s.visitorTag}>{v}</span>
                ))}
                {room.visitors.length > 8 && <span style={s.moreTag}>+{room.visitors.length - 8}</span>}
              </div>
            </div>
          );
        })}
      </div>

      {filtered.length === 0 && <p style={{ textAlign: 'center', color: '#94a3b8', padding: 40 }}>No rooms found.</p>}
    </div>
  );
}

const s = {
  header: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 20, gap: 16 },
  heading: { fontSize: 20, fontWeight: 700, color: '#1e293b', margin: 0 },
  desc: { fontSize: 13, color: '#64748b', marginTop: 2 },
  searchInput: { padding: '8px 12px', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 13, outline: 'none', width: 220 },
  summaryRow: { display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 10, marginBottom: 20 },
  summaryCard: { background: '#fff', borderRadius: 10, border: '1px solid #e5e7eb', padding: '14px 16px' },
  summaryLabel: { fontSize: 10, fontWeight: 600, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 },
  summaryValue: { fontSize: 24, fontWeight: 700 },
  barSection: { background: '#fff', borderRadius: 10, border: '1px solid #e5e7eb', padding: 16, marginBottom: 20 },
  subHead: { fontSize: 13, fontWeight: 600, color: '#1e293b', marginBottom: 12 },
  barRow: { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 },
  barRank: { fontSize: 10, color: '#94a3b8', fontFamily: 'monospace', width: 18 },
  barTrack: { flex: 1, position: 'relative', height: 32, background: '#f8fafc', borderRadius: 6, overflow: 'hidden' },
  barFill: { position: 'absolute', height: '100%', borderRadius: 6 },
  barContent: { position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0 10px' },
  barName: { fontSize: 12, fontWeight: 500 },
  barMeta: { fontSize: 10, color: '#64748b' },
  grid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 12 },
  card: { background: '#fff', borderRadius: 10, border: '1px solid #e5e7eb', padding: 16, position: 'relative', overflow: 'hidden' },
  cardAccent: { position: 'absolute', top: 0, left: 0, right: 0, height: 3 },
  cardName: { fontSize: 14, fontWeight: 600, color: '#1e293b', marginBottom: 2 },
  cardId: { fontSize: 10, color: '#94a3b8', marginBottom: 12 },
  cardStats: { display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8, marginBottom: 12 },
  cardStatLabel: { fontSize: 9, fontWeight: 600, color: '#94a3b8', textTransform: 'uppercase' },
  cardStatValue: { fontSize: 16, fontWeight: 700, color: '#1e293b' },
  cardVisitors: { display: 'flex', flexWrap: 'wrap', gap: 3 },
  visitorTag: { padding: '2px 6px', background: '#f1f5f9', borderRadius: 4, fontSize: 9, color: '#64748b' },
  moreTag: { padding: '2px 4px', fontSize: 9, color: '#94a3b8' },
};
