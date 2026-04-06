import { useState, useCallback } from 'react';
import { getSession, clearSession } from './utils/storage';
import { useAllData } from './hooks/useData';
import Login from './components/Login';
import Sidebar from './components/Sidebar';
import DayView from './components/DayView';
import Employees from './components/Employees';
import Rooms from './components/Rooms';
import Isolation from './components/Isolation';
import Upload from './components/Upload';
import LiveDashboard from './components/LiveDashboard';
import Teams from './components/Teams';
import TeamView from './components/TeamView';
import TeamCompare from './components/TeamCompare';
import TeamDashboard from './components/TeamDashboard';
import CalendarView from './components/CalendarView';
import ReportBuilder from './components/ReportBuilder';
import { FullPageLoader } from './components/LoadingSpinner';

export default function App() {
  const [user, setUser] = useState(getSession);
  const [page, setPage] = useState('live');
  const [refreshKey, setRefreshKey] = useState(0);

  const { data: allData, dates: uploadedDates, loading } = useAllData(refreshKey);

  const handleLogin = useCallback((u) => setUser(u), []);
  const handleLogout = useCallback(() => { clearSession(); setUser(null); }, []);
  const handleDataChange = useCallback(() => setRefreshKey(k => k + 1), []);

  if (!user) {
    return <Login onLogin={handleLogin} />;
  }

  return (
    <div style={{ display: 'flex', height: '100vh', overflow: 'hidden' }}>
      <a href="#main-content" className="skip-link">Skip to main content</a>

      {/* Mobile header (visible < 768px) */}
      <div className="mobile-header" style={styles.mobileHeader}>
        <button onClick={() => setPage(p => p)} style={styles.menuBtn} aria-label="Menu">
          {'\u2630'}
        </button>
        <span style={{ fontSize: 14, fontWeight: 700, color: '#1a365d' }}>Verve Attendance</span>
      </div>

      <Sidebar
        active={page}
        onNav={setPage}
        user={user}
        onLogout={handleLogout}
        uploadedDates={uploadedDates}
      />
      <main id="main-content" role="main" style={styles.main}>
        {loading && <FullPageLoader message="Loading attendance data..." />}
        {!loading && page === 'day' && (
          <DayView allData={allData} uploadedDates={uploadedDates} onNavigateUpload={() => setPage('upload')} />
        )}
        {!loading && page === 'employees' && (
          <Employees allData={allData} uploadedDates={uploadedDates} />
        )}
        {!loading && page === 'rooms' && (
          <Rooms allData={allData} uploadedDates={uploadedDates} />
        )}
        {!loading && page === 'isolation' && (
          <Isolation allData={allData} uploadedDates={uploadedDates} />
        )}
        {page === 'upload' && (
          <Upload onDataChange={handleDataChange} user={user} />
        )}
        {page === 'live' && (
          <LiveDashboard />
        )}
        {page === 'dashboard' && (
          <TeamDashboard user={user} />
        )}
        {page === 'teams' && (
          <Teams user={user} />
        )}
        {page === 'teamview' && (
          <TeamView user={user} />
        )}
        {page === 'calendar' && (
          <CalendarView user={user} />
        )}
        {page === 'reports' && (
          <ReportBuilder user={user} />
        )}
        {page === 'compare' && (
          <TeamCompare />
        )}
      </main>
    </div>
  );
}

const styles = {
  main: {
    flex: 1,
    overflow: 'auto',
    padding: '28px 32px',
    background: '#f5f5f0',
  },
  loader: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    height: '50vh',
    color: '#94a3b8',
    fontSize: 14,
  },
  mobileHeader: {
    display: 'none',
    alignItems: 'center',
    gap: 10,
    padding: '10px 16px',
    background: '#0f2847',
    position: 'fixed',
    top: 0,
    left: 0,
    right: 0,
    zIndex: 100,
    color: '#fff',
  },
  menuBtn: {
    background: 'none',
    border: 'none',
    color: '#fff',
    fontSize: 20,
    cursor: 'pointer',
  },
};
