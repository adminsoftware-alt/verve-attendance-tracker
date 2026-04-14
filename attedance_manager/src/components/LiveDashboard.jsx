import { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import { fetchLiveRooms, fetchHeatmap } from '../utils/zoomApi';

const HEAT_COLORS = [
  { bg: 'transparent', fg: '#94a3b8' },
  { bg: '#dcfce7', fg: '#15803d' },
  { bg: '#bbf7d0', fg: '#166534' },
  { bg: '#fef08a', fg: '#854d0e' },
  { bg: '#fed7aa', fg: '#c2410c' },
  { bg: '#fecaca', fg: '#dc2626' },
];
const REFRESH_SEC = 30;
const AVATARS = ['#3b82f6','#10b981','#f59e0b','#8b5cf6','#ec4899','#06b6d4','#f97316','#ef4444','#6366f1','#14b8a6','#e11d48','#84cc16'];

function istDate() {
  const now = new Date();
  return new Date(now.getTime() + 330 * 60000).toISOString().slice(0, 10);
}
function initials(name) {
  return name.split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase();
}

export default function LiveDashboard() {
  const [date, setDate] = useState(istDate);
  const [tab, setTab] = useState('live');
  const [liveData, setLiveData] = useState(null);
  const [heatData, setHeatData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [floor, setFloor] = useState('all');
  const [interval, setInterval_] = useState(15);
  const [search, setSearch] = useState('');
  const [countdown, setCountdown] = useState(REFRESH_SEC);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [expandedRoom, setExpandedRoom] = useState(null);
  const [sortBy, setSortBy] = useState('count');
  const [prevData, setPrevData] = useState(null);
  const [activityLog, setActivityLog] = useState([]);
  const timerRef = useRef(null);
  const countdownRef = useRef(null);

  // Detect activity changes
  const detectChanges = useCallback((oldData, newData) => {
    if (!oldData?.rooms || !newData?.rooms) return;
    const oldMap = {};
    oldData.rooms.forEach(r => {
      oldMap[r.room_name] = new Set(r.participants.map(p => p.participant_name));
    });
    const logs = [];
    const now = new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' });
    newData.rooms.forEach(r => {
      const oldSet = oldMap[r.room_name] || new Set();
      r.participants.forEach(p => {
        if (!oldSet.has(p.participant_name)) {
          logs.push({ time: now, name: p.participant_name, room: r.room_name, type: 'join' });
        }
      });
      oldSet.forEach(name => {
        if (!r.participants.some(p => p.participant_name === name)) {
          logs.push({ time: now, name, room: r.room_name, type: 'leave' });
        }
      });
    });
    if (logs.length > 0) {
      setActivityLog(prev => [...logs, ...prev].slice(0, 30));
    }
  }, []);

  const loadLive = useCallback(async (showLoader = false) => {
    if (showLoader) setLoading(true);
    setError(null);
    try {
      const data = await fetchLiveRooms(date);
      if (liveData) detectChanges(liveData, data);
      setPrevData(liveData);
      setLiveData(data);
      setLastUpdated(new Date());
      setCountdown(REFRESH_SEC);
    } catch (e) { setError(e.message); }
    if (showLoader) setLoading(false);
  }, [date, liveData, detectChanges]);

  // Auto-load on mount
  useEffect(() => {
    setLoading(true);
    setActivityLog([]);
    Promise.all([
      fetchLiveRooms(date).then(d => { setLiveData(d); setLastUpdated(new Date()); }).catch(() => {}),
      fetchHeatmap(date, interval).then(d => setHeatData(d)).catch(() => {})
    ]).finally(() => setLoading(false));
  }, [date]);

  // Auto-refresh
  useEffect(() => {
    if (autoRefresh) timerRef.current = window.setInterval(() => loadLive(false), REFRESH_SEC * 1000);
    return () => { if (timerRef.current) window.clearInterval(timerRef.current); };
  }, [autoRefresh, loadLive]);

  // Countdown
  useEffect(() => {
    if (autoRefresh) {
      countdownRef.current = window.setInterval(() => setCountdown(c => c <= 1 ? REFRESH_SEC : c - 1), 1000);
    } else setCountdown(REFRESH_SEC);
    return () => { if (countdownRef.current) window.clearInterval(countdownRef.current); };
  }, [autoRefresh]);

  // Derived
  const floors = useMemo(() => {
    if (!liveData?.rooms) return [];
    const set = new Set();
    liveData.rooms.forEach(r => { const m = r.room_name.match(/^(\d+)\./); if (m) set.add(m[1]); });
    return [...set].sort((a, b) => a - b);
  }, [liveData]);

  const filteredRooms = useMemo(() => {
    if (!liveData?.rooms) return [];
    let rooms = [...liveData.rooms];
    if (floor !== 'all') rooms = rooms.filter(r => r.room_name.startsWith(floor + '.'));
    if (search) {
      const q = search.toLowerCase();
      rooms = rooms.filter(r => r.room_name.toLowerCase().includes(q) || r.participants.some(p => p.participant_name.toLowerCase().includes(q)));
    }
    if (sortBy === 'count') rooms.sort((a, b) => b.participant_count - a.participant_count);
    else if (sortBy === 'name') rooms.sort((a, b) => a.room_name.localeCompare(b.room_name));
    else rooms.sort((a, b) => a.participant_count - b.participant_count);
    return rooms;
  }, [liveData, floor, search, sortBy]);

  const allParticipants = useMemo(() => {
    if (!liveData?.rooms) return [];
    const list = [];
    liveData.rooms.forEach(r => r.participants.forEach(p => list.push({ ...p, room: r.room_name })));
    return list;
  }, [liveData]);

  const occupiedCount = liveData?.rooms?.filter(r => r.participant_count > 0).length || 0;
  const totalRooms = liveData?.total_rooms || liveData?.rooms?.length || 0;
  const emptyCount = totalRooms - occupiedCount;
  const occupancyPct = totalRooms > 0 ? Math.round(occupiedCount / totalRooms * 100) : 0;
  const changedRooms = useMemo(() => {
    if (!prevData?.rooms || !liveData?.rooms) return new Set();
    const prev = {}; prevData.rooms.forEach(r => { prev[r.room_name] = r.participant_count; });
    const c = new Set(); liveData.rooms.forEach(r => { if (prev[r.room_name] !== undefined && prev[r.room_name] !== r.participant_count) c.add(r.room_name); });
    return c;
  }, [liveData, prevData]);

  // ════════════════════════════════════════════════════
  // LOADING SCREEN
  // ════════════════════════════════════════════════════
  if (loading && !liveData) {
    return (
      <div style={z.bootScreen}>
        <div style={z.bootGlow} />
        <div style={z.bootLogo}>V</div>
        <div style={z.bootTitle}>Connecting to Zoom Tracker</div>
        <div style={z.bootBarTrack}><div style={z.bootBarFill} /></div>
        <div style={z.bootSub}>Fetching live room data...</div>
      </div>
    );
  }

  return (
    <div>
      {/* ═══ HEADER ═══ */}
      <div style={z.header}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <h2 style={z.title}>Live Dashboard</h2>
          {autoRefresh && <LiveBadge />}
        </div>
        <div style={z.headerR}>
          <input type="date" value={date} onChange={e => { setDate(e.target.value); setExpandedRoom(null); }} style={z.dateInput} />
          <button onClick={() => loadLive(true)} style={z.iconBtn} title="Refresh">
            <RefreshIcon />
          </button>
          <div style={{ position: 'relative' }}>
            <button onClick={() => setAutoRefresh(v => !v)}
              style={{ ...z.autoBtn, background: autoRefresh ? 'linear-gradient(135deg,#10b981,#059669)' : '#94a3b8' }}>
              {autoRefresh ? 'Auto' : 'Off'}
            </button>
            {autoRefresh && <CountdownRing value={countdown} max={REFRESH_SEC} />}
          </div>
        </div>
      </div>

      {/* Timestamp + activity ticker */}
      <div style={z.tickerBar}>
        <div style={z.tickerLeft}>
          <span style={z.tickerDot} />
          {lastUpdated && <span>{lastUpdated.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>}
        </div>
        <div style={z.tickerScroll}>
          {activityLog.length > 0 ? activityLog.slice(0, 5).map((a, i) => (
            <span key={i} style={z.tickerItem}>
              {a.type === 'join' ? '\u{1F7E2}' : '\u{1F534}'} {a.name} {a.type === 'join' ? 'joined' : 'left'} {a.room.replace(/^\d+[\.:]\s*/, '')}
            </span>
          )) : <span style={{ color: '#94a3b8' }}>Waiting for activity...</span>}
        </div>
      </div>

      {error && <div style={z.error}>{error}</div>}

      {/* ═══ TABS ═══ */}
      <div style={z.tabs}>
        {[['live', '\u{1F7E2}', 'Rooms'], ['people', '\u{1F465}', 'People'], ['heatmap', '\u{1F525}', 'Heatmap']].map(([k, ico, label]) => (
          <button key={k} onClick={() => setTab(k)} style={{ ...z.tab, ...(tab === k ? z.tabOn : {}) }}>
            <span>{ico}</span> {label}
          </button>
        ))}
      </div>

      {/* ═══ LIVE TAB ═══ */}
      {tab === 'live' && liveData && (
        <div>
          {/* Stats row with gauge */}
          <div style={z.statsRow}>
            <AnimNum label="People" value={liveData.total_participants} color="#3b82f6" icon={'\u{1F465}'} />
            <AnimNum label="Occupied" value={occupiedCount} color="#10b981" icon={'\u{1F7E2}'} />
            <AnimNum label="Empty" value={emptyCount} color="#64748b" icon={'\u26AA'} />
            <OccupancyGauge pct={occupancyPct} />
          </div>

          {/* Filters */}
          <div style={z.filterBar}>
            <div style={z.pills}>
              <button onClick={() => setFloor('all')} style={{ ...z.pill, ...(floor === 'all' ? z.pillOn : {}) }}>All ({totalRooms})</button>
              {floors.map(f => {
                const n = liveData.rooms.filter(r => r.room_name.startsWith(f + '.')).length;
                return <button key={f} onClick={() => setFloor(f)} style={{ ...z.pill, ...(floor === f ? z.pillOn : {}) }}>F{f} ({n})</button>;
              })}
            </div>
            <div style={{ display: 'flex', gap: 6 }}>
              <div style={z.searchBox}>
                <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search..." style={z.searchIn} />
                {search && <button onClick={() => setSearch('')} style={z.clearBtn}>&times;</button>}
              </div>
              <select value={sortBy} onChange={e => setSortBy(e.target.value)} style={z.sel}>
                <option value="count">Most people</option>
                <option value="name">Room name</option>
                <option value="empty">Least people</option>
              </select>
            </div>
          </div>

          {/* Room grid */}
          <div style={z.grid}>
            {filteredRooms.map((room, i) => (
              <RoomCard key={room.room_name} room={room} idx={i} search={search}
                expanded={expandedRoom === room.room_name} changed={changedRooms.has(room.room_name)}
                onClick={() => setExpandedRoom(expandedRoom === room.room_name ? null : room.room_name)} />
            ))}
          </div>
          {filteredRooms.length === 0 && <div style={z.empty}>No rooms match.</div>}
        </div>
      )}

      {/* ═══ PEOPLE TAB ═══ */}
      {tab === 'people' && liveData && <PeopleTab list={allParticipants} search={search} setSearch={setSearch} />}

      {/* ═══ HEATMAP TAB ═══ */}
      {tab === 'heatmap' && (
        <div>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 16 }}>
            <select value={interval} onChange={e => setInterval_(+e.target.value)} style={z.sel}>
              <option value={15}>15 min</option><option value={30}>30 min</option><option value={60}>1 hour</option>
            </select>
            <button onClick={() => fetchHeatmap(date, interval).then(d => setHeatData(d))} style={z.iconBtn}><RefreshIcon /></button>
          </div>
          {heatData ? <HeatmapTable data={heatData} /> : <div style={z.empty}>No heatmap data.</div>}
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════
// COMPONENTS
// ═══════════════════════════════════════════════════════

function LiveBadge() {
  return (
    <div style={z.liveBadge}>
      <span style={z.liveOuter}><span style={z.liveInner} /></span>
      <span>LIVE</span>
    </div>
  );
}

function CountdownRing({ value, max }) {
  const r = 11, c = 2 * Math.PI * r;
  return (
    <svg width="28" height="28" viewBox="0 0 28 28" style={{ position: 'absolute', top: -4, right: -4 }}>
      <circle cx="14" cy="14" r={r} fill="none" stroke="#e2e8f0" strokeWidth="2" />
      <circle cx="14" cy="14" r={r} fill="none" stroke="#10b981" strokeWidth="2.5"
        strokeDasharray={c} strokeDashoffset={c * (1 - value / max)}
        strokeLinecap="round"
        style={{ transition: 'stroke-dashoffset 1s linear', transform: 'rotate(-90deg)', transformOrigin: 'center' }} />
      <text x="14" y="18" textAnchor="middle" fontSize="9" fontWeight="700" fill="#10b981">{value}</text>
    </svg>
  );
}

function OccupancyGauge({ pct }) {
  const r = 36, c = 2 * Math.PI * r;
  const color = pct > 80 ? '#f59e0b' : pct > 50 ? '#3b82f6' : '#10b981';
  return (
    <div style={{ ...z.statCard, display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 14 }} className="interactive-card">
      <svg width="90" height="90" viewBox="0 0 90 90">
        <circle cx="45" cy="45" r={r} fill="none" stroke="#f1f5f9" strokeWidth="7" />
        <circle cx="45" cy="45" r={r} fill="none" stroke={color} strokeWidth="7"
          strokeDasharray={c} strokeDashoffset={c * (1 - pct / 100)}
          strokeLinecap="round"
          style={{ transition: 'stroke-dashoffset 0.8s ease', transform: 'rotate(-90deg)', transformOrigin: 'center', animation: 'ringSweep 1s ease forwards' }} />
        <text x="45" y="42" textAnchor="middle" fontSize="20" fontWeight="700" fill={color}>{pct}%</text>
        <text x="45" y="56" textAnchor="middle" fontSize="8" fill="#94a3b8" fontWeight="600">OCCUPANCY</text>
      </svg>
    </div>
  );
}

function AnimNum({ label, value, color, icon }) {
  const [disp, setDisp] = useState(0);
  const prev = useRef(0);
  useEffect(() => {
    const s = prev.current, e = value, dur = 700, t0 = performance.now();
    const tick = now => {
      const p = Math.min((now - t0) / dur, 1);
      setDisp(Math.round(s + (e - s) * (1 - Math.pow(1 - p, 3))));
      if (p < 1) requestAnimationFrame(tick); else prev.current = e;
    };
    requestAnimationFrame(tick);
  }, [value]);
  return (
    <div style={z.statCard} className="interactive-card">
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        <div>
          <div style={z.statLabel}>{label}</div>
          <div style={{ fontSize: 30, fontWeight: 800, color, lineHeight: 1, animation: 'flipIn 0.4s ease' }} key={value}>{disp}</div>
        </div>
        <span style={{ fontSize: 24, opacity: 0.4 }}>{icon}</span>
      </div>
    </div>
  );
}

function RoomCard({ room, idx, search, expanded, changed, onClick }) {
  const cnt = room.participant_count;
  const col = AVATARS[idx % AVATARS.length];
  const bar = Math.min(cnt / 10 * 100, 100);

  return (
    <div onClick={onClick} className="interactive-card" style={{
      ...z.card,
      borderColor: cnt > 5 ? '#fbbf24' : cnt > 0 ? col + '40' : '#e5e7eb',
      animation: changed ? 'scaleIn 0.3s ease' : `slideInRight 0.3s ease ${idx * 0.03}s both`,
    }}>
      {/* Top accent */}
      <div style={{ height: 3, background: cnt > 0 ? `linear-gradient(90deg, ${col}, ${col}88)` : '#e5e7eb' }} />

      <div style={z.cardHead}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={z.cardName} title={room.room_name}>{search ? hl(room.room_name, search) : room.room_name}</div>
          <div style={z.cardBar}><div style={{ ...z.cardBarFill, width: bar + '%', background: col, transition: 'width 0.6s ease' }} /></div>
        </div>
        <div style={{
          ...z.badge,
          background: cnt > 5 ? '#f59e0b' : cnt > 0 ? col : '#f1f5f9',
          color: cnt > 0 ? '#fff' : '#94a3b8',
          animation: changed ? 'popIn 0.4s ease' : undefined,
        }}>{cnt}</div>
      </div>

      {/* Participant avatars */}
      <div style={z.avatarRow}>
        {room.participants.slice(0, expanded ? 100 : 5).map((p, i) => (
          <div key={i} title={p.participant_name} style={{
            ...z.avatar,
            background: AVATARS[(idx + i) % AVATARS.length],
            animation: `popIn 0.3s ease ${i * 0.05}s both`,
            zIndex: 10 - i,
            marginLeft: i > 0 ? -6 : 0,
          }}>{initials(p.participant_name)}</div>
        ))}
        {!expanded && cnt > 5 && <span style={z.moreTag}>+{cnt - 5}</span>}
      </div>

      {/* Expanded names */}
      {expanded && cnt > 0 && (
        <div style={z.nameList}>
          {room.participants.map((p, i) => (
            <div key={i} style={{ ...z.nameItem, animation: `fadeIn 0.2s ease ${i * 0.03}s both` }}>
              <span style={{ ...z.dot, background: AVATARS[(idx + i) % AVATARS.length] }} />
              {search ? hl(p.participant_name, search) : p.participant_name}
            </div>
          ))}
        </div>
      )}

      {cnt === 0 && <div style={z.emptyLabel}>Empty</div>}

      {cnt > 3 && (
        <div style={z.expandBtn}>{expanded ? '\u25B2 Less' : '\u25BC Details'}</div>
      )}
    </div>
  );
}

function PeopleTab({ list, search, setSearch }) {
  const filtered = useMemo(() => {
    let f = [...list];
    if (search) { const q = search.toLowerCase(); f = f.filter(p => p.participant_name.toLowerCase().includes(q) || p.room.toLowerCase().includes(q)); }
    return f.sort((a, b) => a.participant_name.localeCompare(b.participant_name));
  }, [list, search]);

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14 }}>
        <span style={{ fontSize: 13, color: '#64748b' }}>{filtered.length} people{search ? ` matching "${search}"` : ''}</span>
        <div style={z.searchBox}>
          <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Filter..." style={{ ...z.searchIn, width: 240 }} />
          {search && <button onClick={() => setSearch('')} style={z.clearBtn}>&times;</button>}
        </div>
      </div>
      <div style={z.peopleGrid}>
        {filtered.map((p, i) => (
          <div key={i} className="interactive-card" style={{ ...z.personCard, animation: `slideInRight 0.2s ease ${i * 0.02}s both` }}>
            <div style={{ ...z.avatar, width: 36, height: 36, fontSize: 12, background: AVATARS[i % AVATARS.length] }}>
              {initials(p.participant_name)}
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 600, fontSize: 13, color: '#1e293b' }}>{search ? hl(p.participant_name, search) : p.participant_name}</div>
              <div style={{ fontSize: 11, color: '#94a3b8', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {p.participant_email || p.room}
              </div>
            </div>
            <span style={z.roomChip}>{p.room.replace(/^\d+[\.:]\s*/, '').slice(0, 15)}</span>
          </div>
        ))}
      </div>
      {filtered.length === 0 && <div style={z.empty}>No people found.</div>}
    </div>
  );
}

function HeatmapTable({ data }) {
  const { rooms, time_slots } = data;
  if (!rooms?.length) return <div style={z.empty}>No data.</div>;
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={z.hTable}>
        <thead><tr>
          <th style={{ ...z.hTh, textAlign: 'left', minWidth: 180, position: 'sticky', left: 0, zIndex: 2, background: '#f8fafc' }}>Room</th>
          {time_slots.map(s => <th key={s} style={z.hTh}>{s}</th>)}
          <th style={z.hTh}>Peak</th><th style={z.hTh}>Avg</th>
        </tr></thead>
        <tbody>{rooms.map(r => (
          <tr key={r.room_name}><td style={{ ...z.hTd, textAlign: 'left', fontWeight: 500, position: 'sticky', left: 0, background: '#fff', zIndex: 1, maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.room_name}>{r.room_name}</td>
            {time_slots.map(s => { const c = r.time_slots[s] || 0; const h = c === 0 ? 0 : c <= 1 ? 1 : c <= 2 ? 2 : c <= 4 ? 3 : c <= 6 ? 4 : 5; const clr = HEAT_COLORS[h];
              return <td key={s} style={{ ...z.hTd, background: clr.bg, color: clr.fg, fontWeight: c > 0 ? 600 : 400 }}>{c || ''}</td>; })}
            <td style={{ ...z.hTd, fontWeight: 700, color: '#f97316' }}>{r.peak_count}</td>
            <td style={{ ...z.hTd, color: '#64748b' }}>{r.avg_count}</td>
          </tr>))}</tbody>
      </table>
      <div style={{ display: 'flex', gap: 8, marginTop: 12, fontSize: 11, color: '#64748b' }}>
        <span>Scale:</span>
        {['0','1','2','3-4','5-6','7+'].map((l, i) => <span key={i} style={{ background: HEAT_COLORS[i].bg || '#f1f5f9', color: HEAT_COLORS[i].fg, padding: '2px 8px', borderRadius: 4, border: i === 0 ? '1px solid #e2e8f0' : 'none' }}>{l}</span>)}
      </div>
    </div>
  );
}

function RefreshIcon() {
  return <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><path d="M1 4v6h6M23 20v-6h-6" /><path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 0 1 3.51 15" /></svg>;
}

function hl(text, q) {
  if (!q || q.length < 2) return text;
  const i = text.toLowerCase().indexOf(q.toLowerCase());
  if (i === -1) return text;
  return <span style={{whiteSpace:'pre'}}>{text.slice(0, i)}<mark style={{padding:0,margin:0}}>{text.slice(i, i + q.length)}</mark>{text.slice(i + q.length)}</span>;
}

// ═══════════════════════════════════════════════════════
// STYLES
// ═══════════════════════════════════════════════════════

const z = {
  // Boot
  bootScreen: { display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '65vh', gap: 16 },
  bootGlow: { width: 80, height: 80, borderRadius: '50%', background: 'radial-gradient(circle, rgba(59,130,246,0.3) 0%, transparent 70%)', animation: 'pulse 2s infinite', position: 'absolute' },
  bootLogo: { width: 56, height: 56, borderRadius: 16, background: 'linear-gradient(135deg,#060c1d,#0f172a)', color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 28, fontWeight: 800, animation: 'glow 2s ease infinite', position: 'relative', zIndex: 1 },
  bootTitle: { fontSize: 16, fontWeight: 700, color: '#1e293b', animation: 'fadeIn 0.5s ease 0.3s both' },
  bootBarTrack: { width: 220, height: 4, background: '#e2e8f0', borderRadius: 2, overflow: 'hidden' },
  bootBarFill: { height: '100%', background: 'linear-gradient(90deg,#3b82f6,#10b981,#3b82f6)', backgroundSize: '200%', borderRadius: 2, animation: 'progress 1.8s ease-in-out infinite, gradientShift 3s ease infinite' },
  bootSub: { fontSize: 12, color: '#94a3b8', animation: 'fadeIn 0.5s ease 0.6s both' },

  // Header
  header: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, flexWrap: 'wrap', gap: 10 },
  title: { fontSize: 22, fontWeight: 800, color: '#0f172a', margin: 0, letterSpacing: '-0.02em' },
  liveBadge: { display: 'inline-flex', alignItems: 'center', gap: 6, background: '#ecfdf5', border: '1px solid #a7f3d0', padding: '4px 12px', borderRadius: 20, fontSize: 11, fontWeight: 700, color: '#059669', letterSpacing: '0.06em' },
  liveOuter: { width: 12, height: 12, borderRadius: '50%', background: 'rgba(16,185,129,0.2)', display: 'flex', alignItems: 'center', justifyContent: 'center', animation: 'breathe 2s infinite' },
  liveInner: { width: 6, height: 6, borderRadius: '50%', background: '#10b981' },
  headerR: { display: 'flex', alignItems: 'center', gap: 8 },
  dateInput: { padding: '8px 12px', border: '1px solid #d1d5db', borderRadius: 10, fontSize: 13, outline: 'none' },
  iconBtn: { width: 36, height: 36, borderRadius: 10, border: '1px solid #e2e8f0', background: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer', color: '#475569' },
  autoBtn: { padding: '8px 14px', color: '#fff', border: 'none', borderRadius: 10, fontSize: 12, fontWeight: 700, cursor: 'pointer', position: 'relative' },

  // Ticker
  tickerBar: { display: 'flex', alignItems: 'center', gap: 12, padding: '8px 14px', background: '#fff', borderRadius: 10, border: '1px solid #e5e7eb', marginBottom: 16, fontSize: 12, overflow: 'hidden' },
  tickerLeft: { display: 'flex', alignItems: 'center', gap: 6, color: '#475569', fontWeight: 500, flexShrink: 0 },
  tickerDot: { width: 6, height: 6, borderRadius: '50%', background: '#10b981', animation: 'pulse 2s infinite' },
  tickerScroll: { display: 'flex', gap: 16, overflow: 'hidden', flex: 1 },
  tickerItem: { whiteSpace: 'nowrap', fontSize: 12, color: '#475569', animation: 'fadeIn 0.4s ease' },

  // Tabs
  tabs: { display: 'flex', gap: 4, marginBottom: 20, background: '#fff', borderRadius: 12, padding: 4, border: '1px solid #e5e7eb' },
  tab: { flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6, padding: '10px 0', background: 'none', border: 'none', borderRadius: 8, fontSize: 13, fontWeight: 500, cursor: 'pointer', color: '#64748b' },
  tabOn: { background: '#0f172a', color: '#fff', fontWeight: 600, boxShadow: '0 2px 8px rgba(15,23,42,0.25)' },

  // Stats
  statsRow: { display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginBottom: 18 },
  statCard: { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 14, padding: '18px 20px' },
  statLabel: { fontSize: 10, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 6, fontWeight: 600 },

  // Filters
  filterBar: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, flexWrap: 'wrap', gap: 10 },
  pills: { display: 'flex', gap: 4, flexWrap: 'wrap' },
  pill: { padding: '6px 14px', border: '1px solid #d1d5db', borderRadius: 20, background: '#fff', color: '#64748b', fontSize: 12, cursor: 'pointer', fontWeight: 500 },
  pillOn: { background: '#0f172a', color: '#fff', borderColor: '#0f172a' },
  searchBox: { position: 'relative', display: 'flex', alignItems: 'center' },
  searchIn: { padding: '8px 28px 8px 12px', border: '1px solid #d1d5db', borderRadius: 10, fontSize: 13, width: 180, outline: 'none' },
  clearBtn: { position: 'absolute', right: 6, background: 'none', border: 'none', fontSize: 16, color: '#94a3b8', cursor: 'pointer' },
  sel: { padding: '7px 10px', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 12, background: '#fff', cursor: 'pointer' },

  // Grid
  grid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 14 },
  card: { background: '#fff', border: '1.5px solid #e5e7eb', borderRadius: 14, overflow: 'hidden', cursor: 'pointer', position: 'relative' },
  cardHead: { padding: '14px 16px 6px', display: 'flex', alignItems: 'center', gap: 10 },
  cardName: { fontSize: 13, fontWeight: 600, color: '#1e293b', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' },
  cardBar: { height: 3, background: '#f1f5f9', borderRadius: 2, marginTop: 5, overflow: 'hidden' },
  cardBarFill: { height: '100%', borderRadius: 2 },
  badge: { fontSize: 14, fontWeight: 700, width: 36, height: 36, borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0, transition: 'transform 0.3s' },

  // Avatars
  avatarRow: { display: 'flex', alignItems: 'center', padding: '8px 16px 6px', flexWrap: 'wrap', gap: 2 },
  avatar: { width: 28, height: 28, borderRadius: '50%', color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 10, fontWeight: 700, border: '2px solid #fff', flexShrink: 0 },
  moreTag: { fontSize: 11, color: '#64748b', fontWeight: 600, marginLeft: 4 },

  // Name list
  nameList: { padding: '4px 16px 8px', borderTop: '1px solid #f1f5f9' },
  nameItem: { padding: '3px 0', fontSize: 12, color: '#475569', display: 'flex', alignItems: 'center', gap: 6 },
  dot: { width: 6, height: 6, borderRadius: '50%', flexShrink: 0 },
  emptyLabel: { padding: '12px 16px', fontSize: 12, color: '#cbd5e1', textAlign: 'center', fontStyle: 'italic' },
  expandBtn: { padding: '7px', fontSize: 11, color: '#3b82f6', fontWeight: 600, borderTop: '1px solid #f1f5f9', textAlign: 'center', background: '#fafbfc' },

  // People tab
  peopleGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 8 },
  personCard: { display: 'flex', alignItems: 'center', gap: 12, padding: '10px 14px', background: '#fff', border: '1px solid #e5e7eb', borderRadius: 12 },
  roomChip: { fontSize: 10, background: '#eef2ff', color: '#4338ca', padding: '3px 8px', borderRadius: 6, fontWeight: 600, whiteSpace: 'nowrap' },

  // Heatmap
  hTable: { borderCollapse: 'collapse', width: '100%', minWidth: 800 },
  hTh: { padding: '6px 4px', fontSize: 11, textAlign: 'center', border: '1px solid #e5e7eb', background: '#f8fafc', color: '#64748b', fontWeight: 500 },
  hTd: { padding: '5px 4px', fontSize: 11, textAlign: 'center', border: '1px solid #e5e7eb' },

  error: { padding: '10px 14px', background: '#fef2f2', color: '#dc2626', border: '1px solid #fecaca', borderRadius: 10, fontSize: 13, marginBottom: 16 },
  empty: { textAlign: 'center', padding: '60px 20px', color: '#94a3b8', fontSize: 14 },
};
