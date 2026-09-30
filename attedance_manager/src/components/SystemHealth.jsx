// System Health — embeds the backend's self-contained /health/dashboard page
// (green/amber/red tiles + pipeline checks, auto-refreshes every 60s).
// Kept as an iframe on purpose: the page is served by Flask, so backend
// deploys update it with zero frontend rebuilds.
const HEALTH_URL = 'https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/health/dashboard';

export default function SystemHealth() {
  return (
    <div style={s.wrap}>
      <div style={s.header}>
        <div>
          <h1 style={s.title}>System Health</h1>
          <p style={s.sub}>
            Webhook feed, duplicates, unknown-room time, disputed rooms, last build.
            Green = fine. Amber/red = read the tile and check that area first.
          </p>
        </div>
        <a href={HEALTH_URL} target="_blank" rel="noreferrer" style={s.link}>
          Open full page {'↗'}
        </a>
      </div>
      <iframe title="System Health" src={HEALTH_URL} style={s.frame} />
    </div>
  );
}

const s = {
  wrap: { display: 'flex', flexDirection: 'column', height: '100%' },
  header: {
    display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start',
    gap: 12, marginBottom: 12, flexWrap: 'wrap',
  },
  title: { margin: 0, fontSize: 22, color: '#0f172a' },
  sub: { margin: '4px 0 0', fontSize: 13, color: '#64748b', maxWidth: 640 },
  link: {
    fontSize: 13, color: '#2563eb', textDecoration: 'none',
    padding: '6px 10px', border: '1px solid #bfdbfe', borderRadius: 6,
    whiteSpace: 'nowrap',
  },
  frame: {
    flex: 1, width: '100%', minHeight: 480,
    border: '1px solid #e2e8f0', borderRadius: 8, background: '#fff',
  },
};
