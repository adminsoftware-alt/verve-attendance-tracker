import { useState } from 'react';
import { validateLogin, setSession } from '../utils/storage';

export default function Login({ onLogin }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [focused, setFocused] = useState(null);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const user = await validateLogin(username.trim(), password);
      if (user) {
        setSession(user);
        onLogin(user);
      } else {
        setError('Invalid username or password');
      }
    } catch (err) {
      setError('Login failed. Please try again.');
    }
    setLoading(false);
  };

  const inputStyle = (key) => ({
    ...s.input,
    borderColor: focused === key ? '#0f172a' : '#e2e8f0',
    boxShadow: focused === key ? '0 0 0 4px rgba(15,23,42,0.06)' : 'none',
    background: focused === key ? '#fff' : '#f8fafc',
  });

  return (
    <div style={s.container}>
      {/* ─── Left brand panel ─────────────────────────────── */}
      <div style={s.leftPanel}>
        {/* Decorative grid pattern */}
        <svg style={s.bgPattern} aria-hidden="true">
          <defs>
            <pattern id="grid" width="48" height="48" patternUnits="userSpaceOnUse">
              <path d="M 48 0 L 0 0 0 48" fill="none" stroke="rgba(255,255,255,0.05)" strokeWidth="1" />
            </pattern>
            <radialGradient id="glow" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stopColor="rgba(99,179,237,0.18)" />
              <stop offset="100%" stopColor="rgba(99,179,237,0)" />
            </radialGradient>
          </defs>
          <rect width="100%" height="100%" fill="url(#grid)" />
          <circle cx="20%" cy="80%" r="280" fill="url(#glow)" />
          <circle cx="85%" cy="15%" r="200" fill="url(#glow)" />
        </svg>

        <div style={s.leftContent}>
          {/* Wordmark */}
          <div style={s.brand}>
            <div style={s.brandWordmark}>verve</div>
            <div style={s.brandSub}>ADVISORY</div>
          </div>

          {/* Headline */}
          <h1 style={s.heroTitle}>
            Attendance,
            <br />
            <span style={s.heroAccent}>intelligently tracked.</span>
          </h1>
          <p style={s.heroSub}>
            Real-time visibility into your team's day — rooms, breaks, isolation
            and leaves, all in one place.
          </p>

          {/* Feature pills */}
          <div style={s.pillRow}>
            <Pill>Live room view</Pill>
            <Pill>Monthly pivots</Pill>
            <Pill>Auto-classify</Pill>
          </div>
        </div>

        <div style={s.leftFooter}>
          © {new Date().getFullYear()} Verve Advisory Private Limited
        </div>
      </div>

      {/* ─── Right form panel ─────────────────────────────── */}
      <div style={s.rightPanel}>
        <div style={s.formWrap}>
          <div style={s.formHeader}>
            <h2 style={s.welcome}>Welcome back</h2>
            <p style={s.welcomeSub}>Sign in to continue to the attendance dashboard.</p>
          </div>

          <form onSubmit={handleSubmit} style={s.form} autoComplete="on">
            <div style={s.field}>
              <label htmlFor="username" style={s.label}>Username</label>
              <input
                id="username"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="e.g. admin"
                style={inputStyle('username')}
                onFocus={() => setFocused('username')}
                onBlur={() => setFocused(null)}
                autoFocus
                required
                autoComplete="username"
              />
            </div>

            <div style={s.field}>
              <div style={s.labelRow}>
                <label htmlFor="password" style={s.label}>Password</label>
                <button
                  type="button"
                  onClick={() => setShowPassword(v => !v)}
                  style={s.linkBtn}
                  tabIndex={-1}
                >
                  {showPassword ? 'Hide' : 'Show'}
                </button>
              </div>
              <input
                id="password"
                type={showPassword ? 'text' : 'password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Enter your password"
                style={inputStyle('password')}
                onFocus={() => setFocused('password')}
                onBlur={() => setFocused(null)}
                required
                autoComplete="current-password"
              />
            </div>

            {error && (
              <div style={s.error} role="alert">
                <span style={s.errorDot} /> {error}
              </div>
            )}

            <button
              type="submit"
              style={{ ...s.button, opacity: loading ? 0.7 : 1 }}
              disabled={loading}
            >
              {loading ? (
                <span style={s.loadingDot}>
                  <span style={{ ...s.dot, animationDelay: '0s' }} />
                  <span style={{ ...s.dot, animationDelay: '0.15s' }} />
                  <span style={{ ...s.dot, animationDelay: '0.3s' }} />
                </span>
              ) : (
                <>
                  Sign in
                  <span style={{ marginLeft: 8 }}>→</span>
                </>
              )}
            </button>
          </form>

          <div style={s.helper}>
            Trouble signing in? Contact <a href="mailto:hr@verveadvisory.com" style={s.helperLink}>hr@verveadvisory.com</a>
          </div>
        </div>

        <div style={s.rightFooter}>
          <div style={s.footerLogo}>
            <span style={{ fontWeight: 800, color: '#0f172a' }}>verve</span>
            <span style={{ fontWeight: 500, color: '#64748b', marginLeft: 4 }}>· advisory</span>
          </div>
          <div style={s.footerMeta}>v2026 · secure session</div>
        </div>
      </div>

      {/* Inline keyframes for the loading dots */}
      <style>{`
        @keyframes loginDot {
          0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
          40%           { transform: scale(1);   opacity: 1; }
        }
      `}</style>
    </div>
  );
}

function Pill({ children }) {
  return (
    <span style={s.pill}>
      <span style={s.pillBullet} /> {children}
    </span>
  );
}

const s = {
  container: { display: 'flex', height: '100vh', overflow: 'hidden', fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif' },

  // ── LEFT (brand panel) ─────────────────────────────────
  leftPanel: {
    flex: '0 0 52%',
    background: 'linear-gradient(150deg, #060c1d 0%, #0b1a35 35%, #122a52 70%, #19376d 100%)',
    color: '#fff',
    position: 'relative',
    display: 'flex',
    flexDirection: 'column',
    justifyContent: 'space-between',
    padding: '56px 64px',
    overflow: 'hidden',
  },
  bgPattern: { position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' },
  leftContent: { position: 'relative', maxWidth: 520 },

  brand: { marginBottom: 80 },
  brandWordmark: { fontFamily: '"Segoe UI", Arial, sans-serif', fontSize: 36, fontWeight: 900, letterSpacing: '-0.02em', lineHeight: 1, color: '#fff' },
  brandSub: { fontSize: 11, fontWeight: 600, letterSpacing: '0.32em', color: 'rgba(255,255,255,0.55)', marginTop: 6 },

  heroTitle: { fontSize: 44, fontWeight: 700, lineHeight: 1.1, letterSpacing: '-0.025em', margin: 0, color: '#fff' },
  heroAccent: { background: 'linear-gradient(90deg, #93c5fd 0%, #60a5fa 60%, #38bdf8 100%)', WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent', backgroundClip: 'text' },
  heroSub: { fontSize: 15, lineHeight: 1.6, color: 'rgba(255,255,255,0.7)', maxWidth: 460, marginTop: 18, marginBottom: 32, fontWeight: 400 },

  pillRow: { display: 'flex', gap: 8, flexWrap: 'wrap' },
  pill: { display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 14px', borderRadius: 999, background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.12)', color: 'rgba(255,255,255,0.9)', fontSize: 12, fontWeight: 500, backdropFilter: 'blur(8px)' },
  pillBullet: { width: 6, height: 6, borderRadius: '50%', background: '#34d399', boxShadow: '0 0 8px rgba(52,211,153,0.6)' },

  leftFooter: { position: 'relative', fontSize: 11, color: 'rgba(255,255,255,0.35)', letterSpacing: '0.04em' },

  // ── RIGHT (form panel) ─────────────────────────────────
  rightPanel: { flex: 1, background: '#fff', display: 'flex', flexDirection: 'column', justifyContent: 'space-between', padding: '56px 64px', minWidth: 360 },
  formWrap: { width: '100%', maxWidth: 380, margin: '0 auto', marginTop: 60 },

  formHeader: { marginBottom: 36 },
  welcome: { fontSize: 30, fontWeight: 700, color: '#0f172a', margin: 0, marginBottom: 8, letterSpacing: '-0.02em' },
  welcomeSub: { fontSize: 14, color: '#64748b', margin: 0, lineHeight: 1.5 },

  form: { display: 'flex', flexDirection: 'column', gap: 18 },
  field: { display: 'flex', flexDirection: 'column', gap: 7 },
  labelRow: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' },
  label: { fontSize: 12, fontWeight: 600, color: '#0f172a', letterSpacing: '0.01em' },
  linkBtn: { background: 'none', border: 'none', color: '#475569', fontSize: 11, fontWeight: 600, cursor: 'pointer', padding: 0, textDecoration: 'underline', textUnderlineOffset: 2 },

  input: {
    padding: '13px 16px',
    border: '1.5px solid #e2e8f0',
    borderRadius: 10,
    fontSize: 14,
    color: '#0f172a',
    outline: 'none',
    transition: 'all 0.18s ease',
    background: '#f8fafc',
    fontFamily: 'inherit',
  },

  error: { display: 'flex', alignItems: 'center', gap: 8, background: '#fef2f2', color: '#b91c1c', padding: '11px 14px', borderRadius: 9, fontSize: 13, fontWeight: 500, border: '1px solid #fecaca' },
  errorDot: { width: 8, height: 8, borderRadius: '50%', background: '#dc2626', flexShrink: 0 },

  button: {
    padding: '14px 0', background: '#0f172a', color: '#fff', border: 'none',
    borderRadius: 10, fontSize: 14, fontWeight: 600, cursor: 'pointer',
    marginTop: 8, letterSpacing: '0.01em',
    boxShadow: '0 4px 12px rgba(15,23,42,0.18)',
    transition: 'transform 0.12s ease, background 0.18s ease',
    display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
  },

  loadingDot: { display: 'inline-flex', gap: 5, alignItems: 'center' },
  dot: { width: 6, height: 6, borderRadius: '50%', background: '#fff', display: 'inline-block', animation: 'loginDot 1s infinite ease-in-out' },

  helper: { marginTop: 28, paddingTop: 22, borderTop: '1px solid #f1f5f9', fontSize: 12, color: '#94a3b8', textAlign: 'center' },
  helperLink: { color: '#475569', textDecoration: 'underline', textUnderlineOffset: 2, fontWeight: 500 },

  rightFooter: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 11, color: '#94a3b8' },
  footerLogo: { display: 'flex', alignItems: 'baseline', fontSize: 14 },
  footerMeta: { letterSpacing: '0.04em' },
};
