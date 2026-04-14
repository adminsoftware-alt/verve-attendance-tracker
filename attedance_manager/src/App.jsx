import { useState, useCallback } from 'react';
import { getSession, clearSession } from './utils/storage';
import { useAllData } from './hooks/useData';
import Login from './components/Login';
import Sidebar from './components/Sidebar';
import DayView from './components/DayView';
import Employees from './components/Employees';
import RoomAnalytics from './components/RoomAnalytics';
import LiveDashboard from './components/LiveDashboard';
import Teams from './components/Teams';
import TeamView from './components/TeamView';
import TeamCompare from './components/TeamCompare';
import TeamDashboard from './components/TeamDashboard';
import ReportBuilder from './components/ReportBuilder';
import EmployeeManager from './components/EmployeeManager';
import EmployeeSummary from './components/EmployeeSummary';
import DataEditor from './components/DataEditor';
import HolidayManager from './components/HolidayManager';
import { FullPageLoader } from './components/LoadingSpinner';

// Pages managers are allowed to see
const MANAGER_PAGES = new Set(['teamview', 'reports', 'teams']);

export default function App() {
  const [user, setUser] = useState(getSession);

  const isManager = user?.role === 'manager';
  const defaultPage = isManager ? 'teamview' : 'live';
  const [page, setPage] = useState(defaultPage);

  const { data: allData, dates: uploadedDates, loading } = useAllData(0);

  const handleLogin = useCallback((u) => {
    setUser(u);
    // Set default page based on role
    setPage(u?.role === 'manager' ? 'teamview' : 'live');
  }, []);
  const handleLogout = useCallback(() => { clearSession(); setUser(null); }, []);

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
        {!isManager && page === 'employees' && (
          <Employees user={user} />
        )}
        {!isManager && !loading && page === 'roomanalytics' && (
          <RoomAnalytics allData={allData} uploadedDates={uploadedDates} />
        )}
        {!isManager && page === 'live' && (
          <LiveDashboard />
        )}
        {user?.role === 'superadmin' && page === 'compare' && (
          <TeamCompare />
        )}
        {!isManager && page === 'registry' && (
          <EmployeeManager user={user} />
        )}
        {!isManager && page === 'empsummary' && (
          <EmployeeSummary user={user} />
        )}
        {user?.role === 'superadmin' && page === 'dataeditor' && (
          <DataEditor user={user} />
        )}
        {!isManager && page === 'holidays' && (
          <HolidayManager user={user} />
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
