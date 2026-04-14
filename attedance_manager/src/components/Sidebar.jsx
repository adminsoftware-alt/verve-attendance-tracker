const ADMIN_NAV = [
  { key: 'live', label: 'Live', icon: '\u{1F4E1}' },
  { key: 'day', label: 'Day View', icon: '\u{1F4C5}' },
  { key: 'employees', label: 'Employees', icon: '\u{1F465}' },
  { key: 'empsummary', label: 'Employee Summary', icon: '\u{1F4C8}' },
  { key: 'roomanalytics', label: 'Room Analytics', icon: '\u{1F3E2}' },
  { key: 'teams', label: 'Teams', icon: '\u{1F46A}' },
  { key: 'teamview', label: 'Team View', icon: '\u{1F4CA}' },
  { key: 'holidays', label: 'Holidays & Leave', icon: '\u{1F4C6}' },
  { key: 'reports', label: 'Reports', icon: '\u{1F4CB}' },
  { key: 'registry', label: 'Registry', icon: '\u{1F4CB}' },
];

const SUPERADMIN_EXTRA = [
  { key: 'compare', label: 'Compare', icon: '\u{1F504}' },
  { key: 'dataeditor', label: 'Data Editor', icon: '\u{1F527}' },
];

const MANAGER_NAV = [
  { key: 'teamview', label: 'Team View', icon: '\u{1F4CA}' },
  { key: 'reports', label: 'Reports', icon: '\u{1F4CB}' },
  { key: 'teams', label: 'My Teams', icon: '\u{1F46A}' },
];

function getRoleLabel(role) {
  switch (role) {
    case 'superadmin': return 'Super Admin';
    case 'admin': return 'Admin';
    case 'hr': return 'HR';
    case 'manager': return 'Manager';
    default: return role || 'User';
  }
}

export default function Sidebar({ active, onNav, user, onLogout, uploadedDates }) {
  const role = user?.role || 'admin';
  const isManager = role === 'manager';
  const isSuperAdmin = role === 'superadmin';
  const navItems = isManager ? MANAGER_NAV : (isSuperAdmin ? [...ADMIN_NAV, ...SUPERADMIN_EXTRA] : ADMIN_NAV);

  return (
    <nav style={s.sidebar} className="sidebar-desktop" role="navigation" aria-label="Main navigation">
      {/* Brand */}
      <div style={s.brand}>
        <svg viewBox="0 0 160 50" style={{ width: 110 }}>
          <text x="4" y="32" fontFamily="Arial Black, Arial, sans-serif" fontSize="30" fontWeight="900" fill="#ffffff" letterSpacing="-1">verve</text>
          <text x="36" y="46" fontFamily="Arial, sans-serif" fontSize="11" fontWeight="500" fill="rgba(255,255,255,0.7)">Advisory</text>
        </svg>
        <div style={s.brandSub}>Attendance</div>
      </div>

      {/* Navigation */}
      <nav style={s.nav}>
        {navItems.map(item => {
          const isActive = active === item.key;
          return (
            <button
              key={item.key}
              onClick={() => onNav(item.key)}
              aria-current={isActive ? 'page' : undefined}
              style={{
                ...s.navBtn,
                background: isActive ? 'rgba(255,255,255,0.12)' : 'transparent',
                color: isActive ? '#ffffff' : 'rgba(255,255,255,0.55)',
                fontWeight: isActive ? 600 : 400,
              }}
            >
              <span style={{ fontSize: 15, width: 22, textAlign: 'center' }}>{item.icon}</span>
              <span>{item.label}</span>
            </button>
          );
        })}
      </nav>

      {/* User footer */}
      <div style={s.footer}>
        <div style={s.userInfo}>
          <div style={s.userAvatar}>{(user?.name || 'U')[0]}</div>
          <div>
            <div style={s.userName}>{user?.name || 'User'}</div>
            <div style={s.userRole}>{getRoleLabel(role)}</div>
          </div>
        </div>
        <button onClick={onLogout} style={s.logoutBtn}>Sign Out</button>
      </div>
    </nav>
  );
}

const s = {
  sidebar: {
    width: 220,
    minWidth: 220,
    height: '100vh',
    background: 'linear-gradient(180deg, #060c1d 0%, #0b1a35 50%, #122a52 100%)',
    display: 'flex',
    flexDirection: 'column',
    overflow: 'hidden',
    borderRight: '1px solid rgba(255,255,255,0.04)',
  },
  brand: {
    padding: '20px 18px 16px',
    borderBottom: '1px solid rgba(255,255,255,0.08)',
  },
  brandSub: {
    fontSize: 10,
    color: 'rgba(255,255,255,0.4)',
    fontWeight: 500,
    letterSpacing: '0.08em',
    textTransform: 'uppercase',
    marginTop: 2,
  },
  nav: {
    flex: 1,
    padding: '12px 8px',
    display: 'flex',
    flexDirection: 'column',
    gap: 2,
    overflowY: 'auto',
  },
  navBtn: {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    padding: '10px 12px',
    border: 'none',
    borderRadius: 8,
    cursor: 'pointer',
    fontSize: 13,
    textAlign: 'left',
    transition: 'all 0.15s',
    whiteSpace: 'nowrap',
  },
  badge: {
    marginLeft: 'auto',
    background: 'rgba(255,255,255,0.2)',
    color: '#fff',
    fontSize: 10,
    fontWeight: 700,
    padding: '2px 7px',
    borderRadius: 10,
  },
  footer: {
    padding: '14px 14px',
    borderTop: '1px solid rgba(255,255,255,0.08)',
  },
  userInfo: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    marginBottom: 10,
  },
  userAvatar: {
    width: 32,
    height: 32,
    borderRadius: 10,
    background: 'linear-gradient(135deg, #60a5fa 0%, #3b82f6 100%)',
    color: '#fff',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    fontSize: 13,
    fontWeight: 700,
    boxShadow: '0 2px 6px rgba(59,130,246,0.35)',
  },
  userName: {
    fontSize: 12,
    fontWeight: 600,
    color: '#fff',
  },
  userRole: {
    fontSize: 10,
    color: 'rgba(255,255,255,0.4)',
  },
  logoutBtn: {
    width: '100%',
    padding: '7px 0',
    background: 'rgba(255,255,255,0.08)',
    color: 'rgba(255,255,255,0.6)',
    border: '1px solid rgba(255,255,255,0.1)',
    borderRadius: 6,
    fontSize: 11,
    fontWeight: 500,
    cursor: 'pointer',
    transition: 'all 0.15s',
  },
};
