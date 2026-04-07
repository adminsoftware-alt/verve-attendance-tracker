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
import EmployeeManager from './components/EmployeeManager';
import DataEditor from './components/DataEditor';
import { FullPageLoader } from './components/LoadingSpinner';

// Pages managers are allowed to see
const MANAGER_PAGES = new Set(['dashboard', 'teamview', 'calendar', 'reports', 'teams']);

export default function App() {
  const [user, setUser] = useState(getSession);
  const [refreshKey, setRefreshKey] = useState(0);

  const isManager = user?.role === 'manager';
  const defaultPage = isManager ? 'dashboard' : 'live';
  const [page, setPage] = useState(defaultPage);

  const { data: allData, dates: uploadedDates, loading } = useAllData(refreshKey);

  const handleLogin = useCallback((u) => {
    setUser(u);
    // Set default page based on role
    setPage(u?.role === 'manager' ? 'dashboard' : 'live');
  }, []);
  const handleLogout = useCallback(() => { clearSession(); setUser(null); }, []);
  const handleDataChange = useCallback(() => setRefreshKey(k => k + 1), []);

  // Guard: manager can't access admin pages
  const handleNav = useCallback((p) => {
    if (isManager && !MANAGER_PAGES.has(p)) return;
    setPage(p);
  }, [isManager]);

  if (!user) {
    return <Login onLogin={handleLogin} />;
  }

  return (
    <div style={{ display: 'flex', height: '100vh', overflow: 'hidden' }}>
      <a href="#main-content" className="skip-link">Skip to main content</a>

      {/* Mobile header (visible < 768px) */}
      <div className="mobile-header" style={styles.mobileHeader}>
        <button onClick={() => {}} style={styles.menuBtn} aria-label="Menu">
          {'\u2630'}
        </button>
        <span style={{ fontSize: 14, fontWeight: 700, color: '#1a365d' }}>Verve Attendance</span>
      </div>

      <Sidebar
        active={page}
        onNav={handleNav}
        user={user}
        onLogout={handleLogout}
        uploadedDates={uploadedDates}
      />
      <main id="main-content" role="main" style={styles.main}>
        {/* Admin/HR only pages */}
        {!isManager && loading && <FullPageLoader message="Loading attendance data..." />}
        {!isManager && !loading && page === 'day' && (
          <DayView allData={allData} uploadedDates={uploadedDates} onNavigateUpload={() => setPage('upload')} />
        )}
        {!isManager && !loading && page === 'employees' && (
          <Employees allData={allData} uploadedDates={uploadedDates} />
        )}
        {!isManager && !loading && page === 'rooms' && (
          <Rooms allData={allData} uploadedDates={uploadedDates} />
        )}
        {!isManager && !loading && page === 'isolation' && (
          <Isolation allData={allData} uploadedDates={uploadedDates} />
        )}
        {!isManager && page === 'upload' && (
          <Upload onDataChange={handleDataChange} user={user} />
        )}
        {!isManager && page === 'live' && (
          <LiveDashboard />
        )}
        {!isManager && page === 'compare' && (
          <TeamCompare />
        )}
        {!isManager && page === 'registry' && (
          <EmployeeManager user={user} />
        )}
        {user?.role === 'superadmin' && page === 'dataeditor' && (
          <DataEditor user={user} />
        )}

        {/* Shared pages (all roles) */}
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
