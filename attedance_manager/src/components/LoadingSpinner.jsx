export default function LoadingSpinner({ size = 'medium', message = 'Loading...' }) {
  const sizes = {
    small: { spinner: 24, border: 3, font: 12 },
    medium: { spinner: 48, border: 4, font: 14 },
    large: { spinner: 64, border: 5, font: 16 },
  };
  const s = sizes[size] || sizes.medium;

  return (
    <div style={styles.container}>
      <div style={styles.spinnerWrapper}>
        {/* Outer ring */}
        <div
          style={{
            ...styles.ring,
            width: s.spinner,
            height: s.spinner,
            borderWidth: s.border,
          }}
        />
        {/* Inner pulse */}
        <div
          style={{
            ...styles.pulse,
            width: s.spinner * 0.4,
            height: s.spinner * 0.4,
          }}
        />
      </div>
      {message && (
        <p style={{ ...styles.message, fontSize: s.font }}>{message}</p>
      )}
    </div>
  );
}

// Full page loader with overlay
export function FullPageLoader({ message = 'Loading attendance data...' }) {
  return (
    <div style={styles.fullPage}>
      <div style={styles.card}>
        <div style={styles.logoWrapper}>
          <div style={styles.logoCircle}>V</div>
        </div>
        <div style={styles.spinnerLarge}>
          <div style={{ ...styles.ring, width: 56, height: 56, borderWidth: 4 }} />
        </div>
        <p style={styles.fullPageMessage}>{message}</p>
        <div style={styles.dots}>
          <span style={{ ...styles.dot, animationDelay: '0s' }} />
          <span style={{ ...styles.dot, animationDelay: '0.2s' }} />
          <span style={{ ...styles.dot, animationDelay: '0.4s' }} />
        </div>
      </div>
    </div>
  );
}

// Inline skeleton loader for tables
export function TableSkeleton({ rows = 5, cols = 4 }) {
  return (
    <div style={styles.tableSkeleton}>
      {[...Array(rows)].map((_, i) => (
        <div key={i} style={styles.skeletonRow}>
          {[...Array(cols)].map((_, j) => (
            <div
              key={j}
              className="skeleton"
              style={{
                ...styles.skeletonCell,
                width: j === 0 ? '30%' : `${60 / (cols - 1)}%`,
              }}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

// Card skeleton
export function CardSkeleton({ count = 3 }) {
  return (
    <div style={styles.cardGrid}>
      {[...Array(count)].map((_, i) => (
        <div key={i} className="skeleton" style={styles.cardSkeleton} />
      ))}
    </div>
  );
}

const styles = {
  container: {
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 16,
    padding: 24,
  },
  spinnerWrapper: {
    position: 'relative',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
  },
  ring: {
    borderRadius: '50%',
    border: '4px solid #e2e8f0',
    borderTopColor: '#0f172a',
    animation: 'spin 0.8s linear infinite',
  },
  pulse: {
    position: 'absolute',
    borderRadius: '50%',
    background: '#0f172a',
    animation: 'pulse 1.5s ease-in-out infinite',
  },
  message: {
    color: '#64748b',
    fontWeight: 500,
    margin: 0,
  },
  fullPage: {
    position: 'fixed',
    inset: 0,
    background: 'rgba(245, 245, 240, 0.95)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    zIndex: 1000,
    backdropFilter: 'blur(4px)',
  },
  card: {
    background: '#fff',
    borderRadius: 20,
    padding: '40px 60px',
    boxShadow: '0 20px 60px rgba(26, 54, 93, 0.15)',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    gap: 20,
    animation: 'fadeIn 0.3s ease',
  },
  logoWrapper: {
    marginBottom: 8,
  },
  logoCircle: {
    width: 48,
    height: 48,
    borderRadius: 12,
    background: 'linear-gradient(135deg, #0f172a 0%, #2d5a87 100%)',
    color: '#fff',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    fontSize: 24,
    fontWeight: 700,
    boxShadow: '0 4px 12px rgba(26, 54, 93, 0.3)',
  },
  spinnerLarge: {
    position: 'relative',
  },
  fullPageMessage: {
    color: '#334155',
    fontSize: 15,
    fontWeight: 600,
    margin: 0,
  },
  dots: {
    display: 'flex',
    gap: 6,
    marginTop: 4,
  },
  dot: {
    width: 8,
    height: 8,
    borderRadius: '50%',
    background: '#0f172a',
    animation: 'bounce 1s ease-in-out infinite',
  },
  tableSkeleton: {
    display: 'flex',
    flexDirection: 'column',
    gap: 12,
    padding: 16,
  },
  skeletonRow: {
    display: 'flex',
    gap: 16,
  },
  skeletonCell: {
    height: 20,
  },
  cardGrid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
    gap: 16,
    padding: 16,
  },
  cardSkeleton: {
    height: 100,
    borderRadius: 12,
  },
};
