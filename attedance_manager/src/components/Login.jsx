import { useState } from 'react';
import { validateLogin, setSession } from '../utils/storage';

export default function Login({ onLogin }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

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

  return (
    <div style={s.container}>
      {/* Left panel */}
      <div style={s.leftPanel}>
        <div style={s.leftContent}>
          <svg viewBox="0 0 220 70" style={{ width: 220, marginBottom: 32 }}>
            <text x="10" y="46" fontFamily="Arial Black, Arial, sans-serif" fontSize="44" fontWeight="900" fill="#ffffff" letterSpacing="-1">verve</text>
            <text x="60" y="66" fontFamily="Arial, sans-serif" fontSize="17" fontWeight="500" fill="rgba(255,255,255,0.85)">Advisory</text>
          </svg>
          <h1 style={s.leftTitle}>Attendance</h1>
          <h1 style={s.leftTitle}>Management System</h1>
          <div style={s.leftBadge}>Workplace Analytics 2026</div>
        </div>
        <div style={s.leftFooter}>Verve Advisory Private Limited</div>
      </div>

      {/* Right panel */}
      <div style={s.rightPanel}>
        <div style={s.formWrap}>
          <h2 style={s.welcome}>Welcome Back</h2>
          <p style={s.welcomeSub}>Sign in to access your attendance dashboard.</p>

          <form onSubmit={handleSubmit} style={s.form}>
            <div style={s.field}>
              <label style={s.label}>USERNAME</label>
              <input type="text" value={username} onChange={(e) => setUsername(e.target.value)}
                placeholder="Enter your username" style={s.input} autoFocus required autoComplete="username"
                onFocus={(e) => { e.target.style.borderColor = '#1a365d'; e.target.style.boxShadow = '0 0 0 3px rgba(26,54,93,0.08)'; }}
                onBlur={(e) => { e.target.style.borderColor = '#d1d5db'; e.target.style.boxShadow = 'none'; }} />
            </div>
            <div style={s.field}>
              <label style={s.label}>PASSWORD</label>
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                placeholder="Enter your password" style={s.input} required autoComplete="current-password"
                onFocus={(e) => { e.target.style.borderColor = '#1a365d'; e.target.style.boxShadow = '0 0 0 3px rgba(26,54,93,0.08)'; }}
                onBlur={(e) => { e.target.style.borderColor = '#d1d5db'; e.target.style.boxShadow = 'none'; }} />
            </div>
            {error && <div style={s.error}>{error}</div>}
            <button type="submit" style={s.button} disabled={loading}
              onMouseEnter={(e) => { e.target.style.background = '#142d50'; }}
              onMouseLeave={(e) => { e.target.style.background = '#1a365d'; }}>
              {loading ? 'Signing in...' : 'Sign In \u2192'}
            </button>
          </form>

          <div style={s.divider} />
          <div style={s.bottomLogo}>
            <svg viewBox="0 0 180 55" style={{ width: 120 }}>
              <text x="5" y="36" fontFamily="Arial Black, Arial, sans-serif" fontSize="34" fontWeight="900" fill="#1a365d" letterSpacing="-1">verve</text>
              <text x="42" y="52" fontFamily="Arial, sans-serif" fontSize="13" fontWeight="500" fill="#3b6fb6">Advisory</text>
            </svg>
          </div>
        </div>
      </div>
    </div>
  );
}

const s = {
  container: { display: 'flex', height: '100vh', overflow: 'hidden' },
  leftPanel: { width: '46%', background: 'linear-gradient(160deg, #0f2847 0%, #1a3a6b 40%, #1e4d8c 100%)', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', position: 'relative', padding: 40 },
  leftContent: { textAlign: 'center' },
  leftTitle: { fontSize: 32, fontWeight: 700, color: '#ffffff', margin: 0, lineHeight: 1.2, letterSpacing: '-0.02em' },
  leftBadge: { marginTop: 24, display: 'inline-block', padding: '8px 20px', border: '1.5px solid rgba(255,255,255,0.35)', borderRadius: 20, color: 'rgba(255,255,255,0.8)', fontSize: 13, fontWeight: 500 },
  leftFooter: { position: 'absolute', bottom: 24, color: 'rgba(255,255,255,0.3)', fontSize: 11 },
  rightPanel: { flex: 1, background: '#faf9f7', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 40 },
  formWrap: { width: '100%', maxWidth: 380 },
  welcome: { fontSize: 28, fontWeight: 700, color: '#1a1a2e', margin: 0, marginBottom: 6 },
  welcomeSub: { fontSize: 14, color: '#6b7280', marginBottom: 32, lineHeight: 1.5 },
  form: { display: 'flex', flexDirection: 'column', gap: 20 },
  field: { display: 'flex', flexDirection: 'column', gap: 6 },
  label: { fontSize: 11, fontWeight: 700, color: '#374151', letterSpacing: '0.08em' },
  input: { padding: '13px 16px', border: '1.5px solid #d1d5db', borderRadius: 10, fontSize: 14, color: '#1a1a2e', outline: 'none', transition: 'all 0.2s', background: '#fff' },
  error: { background: '#fef2f2', color: '#dc2626', padding: '10px 14px', borderRadius: 8, fontSize: 13, fontWeight: 500, border: '1px solid #fecaca' },
  button: { padding: '14px 0', background: '#1a365d', color: '#fff', border: 'none', borderRadius: 10, fontSize: 15, fontWeight: 600, cursor: 'pointer', marginTop: 4, transition: 'background 0.15s' },
  divider: { height: 1, background: '#e5e7eb', margin: '28px 0' },
  bottomLogo: { textAlign: 'center' },
};
