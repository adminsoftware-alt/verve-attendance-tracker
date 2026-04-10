import { useState, useEffect, useCallback } from 'react';
import { fetchEmployees, fetchEmployeeYearlySummary } from '../utils/zoomApi';
import { downloadEmployeeYearExcel } from '../utils/employeeYearExcel';

function istDate() {
  const now = new Date();
  return new Date(now.getTime() + 330 * 60000).toISOString().slice(0, 10);
}

// Color coding thresholds
const LOW_ATTENDANCE_PCT = 80;
const HIGH_ATTENDANCE_PCT = 95;
const LOW_HOURS_THRESHOLD = 140; // ~17.5 days * 8 hours
const HIGH_BREAK_THRESHOLD = 15;
const HIGH_ISOLATION_THRESHOLD = 10;

function getCellStyle(value, type) {
  if (type === 'attendance') {
    if (value < LOW_ATTENDANCE_PCT) return { background: '#fee2e2', color: '#dc2626' };
    if (value >= HIGH_ATTENDANCE_PCT) return { background: '#dcfce7', color: '#15803d' };
  }
  if (type === 'hours' && value < LOW_HOURS_THRESHOLD && value > 0) {
    return { background: '#ffedd5', color: '#c2410c' };
  }
  if (type === 'break' && value > HIGH_BREAK_THRESHOLD) {
    return { background: '#fef9c3', color: '#854d0e' };
  }
  if (type === 'isolation' && value > HIGH_ISOLATION_THRESHOLD) {
    return { background: '#fce7f3', color: '#be185d' };
  }
  return {};
}

export default function EmployeeSummary({ user }) {
  const [employees, setEmployees] = useState([]);
  const [selectedEmployee, setSelectedEmployee] = useState('');
  const [year, setYear] = useState(new Date().getFullYear());
  const [summaryData, setSummaryData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [dataLoading, setDataLoading] = useState(false);
  const [error, setError] = useState(null);
  const [searchQuery, setSearchQuery] = useState('');

  // Load employees on mount
  useEffect(() => {
    fetchEmployees({ status: 'active' })
      .then(d => {
        const empList = d.employees || [];
        setEmployees(empList);
        if (empList.length > 0) setSelectedEmployee(empList[0].employee_id);
      })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  // Load yearly summary when employee or year changes
  const loadSummary = useCallback(async () => {
    if (!selectedEmployee) return;
    setDataLoading(true);
    setError(null);
    try {
      const data = await fetchEmployeeYearlySummary(selectedEmployee, year);
      setSummaryData(data);
    } catch (e) {
      setError(e.message);
      setSummaryData(null);
    }
    setDataLoading(false);
  }, [selectedEmployee, year]);

  useEffect(() => {
    loadSummary();
  }, [loadSummary]);

  // Filter employees by search
  const filteredEmployees = employees.filter(e =>
    (e.participant_name || '').toLowerCase().includes(searchQuery.toLowerCase())
  );

  // Download Excel
  const handleDownload = () => {
    if (!summaryData) return;
    const emp = employees.find(e => e.employee_id === selectedEmployee);
    const empName = emp?.participant_name || summaryData.employee_name || 'Employee';
    downloadEmployeeYearExcel(summaryData, empName, year);
  };

  // Generate year options
  const currentYear = new Date().getFullYear();
  const yearOptions = [currentYear - 2, currentYear - 1, currentYear];

  if (loading) {
    return (
      <div style={s.container}>
        <div style={s.loading}>Loading employees...</div>
      </div>
    );
  }

  return (
    <div style={s.container}>
      {/* Header */}
      <div style={s.header}>
        <h1 style={s.title}>Employee Year Summary</h1>
        <button style={s.downloadBtn} onClick={handleDownload} disabled={!summaryData || dataLoading}>
          Download Excel
        </button>
      </div>

      {/* Controls */}
      <div style={s.controls}>
        <div style={s.controlGroup}>
          <label style={s.label}>Employee</label>
          <div style={s.selectWrap}>
            <input
              type="text"
              placeholder="Search employee..."
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              style={s.searchInput}
            />
            <select
              value={selectedEmployee}
              onChange={e => setSelectedEmployee(e.target.value)}
              style={s.select}
            >
              {filteredEmployees.map(e => (
                <option key={e.employee_id} value={e.employee_id}>
                  {e.display_name || e.participant_name}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div style={s.controlGroup}>
          <label style={s.label}>Year</label>
          <select
            value={year}
            onChange={e => setYear(Number(e.target.value))}
            style={s.select}
          >
            {yearOptions.map(y => (
              <option key={y} value={y}>{y}</option>
            ))}
          </select>
        </div>

        <button style={s.refreshBtn} onClick={loadSummary} disabled={dataLoading}>
          {dataLoading ? 'Loading...' : 'Refresh'}
        </button>
      </div>

      {/* Error */}
      {error && <div style={s.error}>{error}</div>}

      {/* Loading overlay */}
      {dataLoading && (
        <div style={s.loadingOverlay}>
          <div style={s.spinner}></div>
          <span>Loading yearly data...</span>
        </div>
      )}

      {/* Summary Cards */}
      {summaryData && !dataLoading && (
        <>
          <div style={s.cards}>
            <div style={s.card}>
              <div style={s.cardLabel}>Working Days</div>
              <div style={s.cardValue}>{summaryData.yearly_totals?.working_days || 0}</div>
            </div>
            <div style={s.card}>
              <div style={s.cardLabel}>Present Days</div>
              <div style={s.cardValue}>{summaryData.yearly_totals?.present_days || 0}</div>
            </div>
            <div style={s.card}>
              <div style={s.cardLabel}>Half Days</div>
              <div style={s.cardValue}>{summaryData.yearly_totals?.half_days || 0}</div>
            </div>
            <div style={s.card}>
              <div style={s.cardLabel}>Absent Days</div>
              <div style={{ ...s.cardValue, color: '#dc2626' }}>{summaryData.yearly_totals?.absent_days || 0}</div>
            </div>
            <div style={{ ...s.card, ...getCellStyle(summaryData.yearly_totals?.attendance_pct || 0, 'attendance') }}>
              <div style={s.cardLabel}>Attendance %</div>
              <div style={s.cardValue}>{summaryData.yearly_totals?.attendance_pct || 0}%</div>
            </div>
            <div style={s.card}>
              <div style={s.cardLabel}>Total Hours</div>
              <div style={s.cardValue}>{summaryData.yearly_totals?.total_hours || 0}h</div>
            </div>
          </div>

          {/* 12-Month Table */}
          <div style={s.tableWrap}>
            <table style={s.table}>
              <thead>
                <tr>
                  <th style={s.th}>Month</th>
                  <th style={s.th}>Working Days</th>
                  <th style={s.th}>Present</th>
                  <th style={s.th}>Half Day</th>
                  <th style={s.th}>Absent</th>
                  <th style={s.th}>Attendance %</th>
                  <th style={s.th}>Total Hours</th>
                  <th style={s.th}>Avg Login</th>
                  <th style={s.th}>Avg Logout</th>
                  <th style={s.th}>Break Hours</th>
                  <th style={s.th}>Isolation Hours</th>
                </tr>
              </thead>
              <tbody>
                {summaryData.monthly_summary?.map((m, i) => (
                  <tr key={m.month} style={i % 2 === 0 ? s.trEven : {}}>
                    <td style={{ ...s.td, fontWeight: 600 }}>{m.month_name}</td>
                    <td style={s.td}>{m.working_days}</td>
                    <td style={{ ...s.td, ...getCellStyle(0, '') }}>{m.present_days}</td>
                    <td style={s.td}>{m.half_days}</td>
                    <td style={{ ...s.td, color: m.absent_days > 0 ? '#dc2626' : 'inherit' }}>{m.absent_days}</td>
                    <td style={{ ...s.td, ...getCellStyle(m.attendance_pct, 'attendance') }}>
                      {m.attendance_pct}%
                    </td>
                    <td style={{ ...s.td, ...getCellStyle(m.total_hours, 'hours') }}>
                      {m.total_hours}h
                    </td>
                    <td style={s.td}>{m.avg_login || '-'}</td>
                    <td style={s.td}>{m.avg_logout || '-'}</td>
                    <td style={{ ...s.td, ...getCellStyle(m.break_hours, 'break') }}>
                      {m.break_hours}h
                    </td>
                    <td style={{ ...s.td, ...getCellStyle(m.isolation_hours, 'isolation') }}>
                      {m.isolation_hours}h
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr style={s.totalRow}>
                  <td style={{ ...s.td, fontWeight: 700 }}>TOTAL</td>
                  <td style={{ ...s.td, fontWeight: 600 }}>{summaryData.yearly_totals?.working_days}</td>
                  <td style={{ ...s.td, fontWeight: 600 }}>{summaryData.yearly_totals?.present_days}</td>
                  <td style={{ ...s.td, fontWeight: 600 }}>{summaryData.yearly_totals?.half_days}</td>
                  <td style={{ ...s.td, fontWeight: 600, color: '#dc2626' }}>{summaryData.yearly_totals?.absent_days}</td>
                  <td style={{ ...s.td, fontWeight: 700, ...getCellStyle(summaryData.yearly_totals?.attendance_pct, 'attendance') }}>
                    {summaryData.yearly_totals?.attendance_pct}%
                  </td>
                  <td style={{ ...s.td, fontWeight: 600 }}>{summaryData.yearly_totals?.total_hours}h</td>
                  <td style={s.td}>-</td>
                  <td style={s.td}>-</td>
                  <td style={{ ...s.td, fontWeight: 600 }}>{summaryData.yearly_totals?.total_break_hours}h</td>
                  <td style={{ ...s.td, fontWeight: 600 }}>{summaryData.yearly_totals?.total_isolation_hours}h</td>
                </tr>
              </tfoot>
            </table>
          </div>

          {/* Color Legend */}
          <div style={s.legend}>
            <span style={{ ...s.legendItem, background: '#fee2e2', color: '#dc2626' }}>{'<'} 80% Attendance</span>
            <span style={{ ...s.legendItem, background: '#dcfce7', color: '#15803d' }}>{'>='} 95% Attendance</span>
            <span style={{ ...s.legendItem, background: '#ffedd5', color: '#c2410c' }}>Low Hours</span>
            <span style={{ ...s.legendItem, background: '#fef9c3', color: '#854d0e' }}>{'>'} 15h Breaks</span>
            <span style={{ ...s.legendItem, background: '#fce7f3', color: '#be185d' }}>{'>'} 10h Isolation</span>
          </div>
        </>
      )}

      {/* No data message */}
      {!summaryData && !dataLoading && !error && (
        <div style={s.noData}>Select an employee and year to view their attendance summary.</div>
      )}
    </div>
  );
}

// ─── Styles ─────────────────────────────────────────────────
const s = {
  container: { padding: '24px', maxWidth: 1400, margin: '0 auto' },
  header: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 },
  title: { fontSize: 24, fontWeight: 700, color: '#0f172a', margin: 0 },
  downloadBtn: {
    padding: '10px 20px', background: '#10b981', color: '#fff', border: 'none',
    borderRadius: 8, fontWeight: 600, cursor: 'pointer', fontSize: 14
  },
  controls: {
    display: 'flex', gap: 16, alignItems: 'flex-end', marginBottom: 24,
    flexWrap: 'wrap', background: '#f8fafc', padding: 16, borderRadius: 12
  },
  controlGroup: { display: 'flex', flexDirection: 'column', gap: 6 },
  label: { fontSize: 12, fontWeight: 600, color: '#64748b', textTransform: 'uppercase' },
  selectWrap: { display: 'flex', flexDirection: 'column', gap: 4 },
  searchInput: {
    padding: '8px 12px', border: '1px solid #e2e8f0', borderRadius: 6,
    fontSize: 14, width: 240
  },
  select: {
    padding: '10px 14px', border: '1px solid #e2e8f0', borderRadius: 8,
    fontSize: 14, background: '#fff', minWidth: 240, cursor: 'pointer'
  },
  refreshBtn: {
    padding: '10px 20px', background: '#3b82f6', color: '#fff', border: 'none',
    borderRadius: 8, fontWeight: 600, cursor: 'pointer', fontSize: 14, marginLeft: 'auto'
  },
  error: {
    padding: 16, background: '#fee2e2', color: '#dc2626', borderRadius: 8,
    marginBottom: 16, fontWeight: 500
  },
  loading: { padding: 40, textAlign: 'center', color: '#64748b' },
  loadingOverlay: {
    display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 12,
    padding: 40, color: '#64748b'
  },
  spinner: {
    width: 24, height: 24, border: '3px solid #e2e8f0',
    borderTopColor: '#3b82f6', borderRadius: '50%',
    animation: 'spin 1s linear infinite'
  },
  cards: {
    display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
    gap: 16, marginBottom: 24
  },
  card: {
    background: '#fff', padding: 16, borderRadius: 12, textAlign: 'center',
    boxShadow: '0 1px 3px rgba(0,0,0,0.1)', border: '1px solid #e2e8f0'
  },
  cardLabel: { fontSize: 12, color: '#64748b', marginBottom: 4, fontWeight: 500 },
  cardValue: { fontSize: 24, fontWeight: 700, color: '#0f172a' },
  tableWrap: { overflowX: 'auto', borderRadius: 12, border: '1px solid #e5e7eb' },
  table: { width: '100%', borderCollapse: 'collapse', background: '#fff', fontSize: 14 },
  th: {
    padding: '12px 16px', textAlign: 'left', fontWeight: 600, color: '#374151',
    background: '#f9fafb', borderBottom: '2px solid #e5e7eb', whiteSpace: 'nowrap'
  },
  td: {
    padding: '12px 16px', borderBottom: '1px solid #e5e7eb', whiteSpace: 'nowrap'
  },
  trEven: { background: '#fafbfc' },
  totalRow: { background: '#f1f5f9', fontWeight: 600 },
  legend: {
    display: 'flex', gap: 12, marginTop: 16, flexWrap: 'wrap'
  },
  legendItem: {
    padding: '4px 10px', borderRadius: 4, fontSize: 12, fontWeight: 500
  },
  noData: {
    padding: 40, textAlign: 'center', color: '#64748b', background: '#f8fafc',
    borderRadius: 12
  }
};
