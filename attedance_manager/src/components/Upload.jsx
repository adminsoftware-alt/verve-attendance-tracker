import { useState, useRef, useEffect } from 'react';
import { parseFile, todayIST } from '../utils/parser';
import { saveDayData, getUploadedDates, deleteDayData } from '../utils/storage';
import { fetchSummary, transformSummaryToEmployees } from '../utils/zoomApi';

export default function Upload({ onDataChange, user }) {
  const [date, setDate] = useState(todayIST);
  const [dragging, setDragging] = useState(false);
  const [status, setStatus] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [dates, setDates] = useState([]);
  const fileRef = useRef(null);

  // Load dates on mount (Fix: useEffect instead of render-time side effect)
  useEffect(() => {
    getUploadedDates().then(d => setDates(d));
  }, []);

  const refresh = async () => {
    const d = await getUploadedDates();
    setDates(d);
    onDataChange();
  };

  const processFile = async (file) => {
    if (!date) {
      setStatus({ type: 'error', msg: 'Please select a date first.' });
      return;
    }
    setUploading(true);
    setStatus(null);
    try {
      const employees = await parseFile(file);
      if (!employees.length) {
        setStatus({ type: 'error', msg: 'No valid employee data found in file.' });
      } else {
        await saveDayData(date, employees, user?.username);
        setStatus({ type: 'success', msg: `Uploaded ${employees.length} employees for ${date}` });
        await refresh();
      }
    } catch (err) {
      setStatus({ type: 'error', msg: 'Failed to parse file: ' + err.message });
    }
    setUploading(false);
  };

  const handleFile = (e) => {
    const f = e.target.files?.[0];
    if (f) processFile(f);
    e.target.value = '';
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files?.[0];
    if (f) processFile(f);
  };

  const fetchFromZoom = async () => {
    if (!date) {
      setStatus({ type: 'error', msg: 'Please select a date first.' });
      return;
    }
    setUploading(true);
    setStatus(null);
    try {
      const summary = await fetchSummary(date);
      const employees = transformSummaryToEmployees(summary);
      if (!employees.length) {
        setStatus({ type: 'error', msg: `No attendance data found in Zoom tracker for ${date}` });
      } else {
        await saveDayData(date, employees, user?.username);
        setStatus({ type: 'success', msg: `Fetched ${employees.length} employees from Zoom for ${date}` });
        await refresh();
      }
    } catch (err) {
      setStatus({ type: 'error', msg: 'Zoom API error: ' + err.message });
    }
    setUploading(false);
  };

  const handleDelete = async (d) => {
    if (window.confirm(`Delete data for ${d}?`)) {
      await deleteDayData(d);
      refresh();
    }
  };

  return (
    <div style={s.page}>
      <h2 style={s.heading}>Upload Attendance Report</h2>
      <p style={s.desc}>Select a date and upload the CSV or XLSX file for that day.</p>

      <div style={s.row}>
        <div style={{ display: 'flex', alignItems: 'flex-end', gap: 12 }}>
          <div style={s.dateWrap}>
            <label style={s.label}>Report Date</label>
            <input
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              style={s.dateInput}
            />
          </div>
          <button onClick={fetchFromZoom} disabled={uploading} style={s.zoomBtn}>
            {uploading ? 'Fetching...' : 'Fetch from Zoom'}
          </button>
        </div>
      </div>

      <div
        style={{ ...s.dropZone, borderColor: dragging ? '#3b82f6' : '#d1d5db', background: dragging ? '#eff6ff' : '#fafafa' }}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        onClick={() => fileRef.current?.click()}
      >
        <input ref={fileRef} type="file" accept=".csv,.xlsx,.xls" onChange={handleFile} style={{ display: 'none' }} />
        <div style={s.dropIcon}>&#128196;</div>
        <div style={s.dropText}>
          {uploading ? 'Processing...' : 'Drop file here or click to browse'}
        </div>
        <div style={s.dropHint}>Supports .csv, .xlsx, .xls</div>
      </div>

      {status && (
        <div style={{ ...s.status, background: status.type === 'success' ? '#f0fdf4' : '#fef2f2', color: status.type === 'success' ? '#15803d' : '#dc2626', borderColor: status.type === 'success' ? '#bbf7d0' : '#fecaca' }}>
          {status.msg}
        </div>
      )}

      <div style={s.section}>
        <h3 style={s.subHead}>Uploaded Dates ({dates.length})</h3>
        {dates.length === 0 && <p style={s.empty}>No data uploaded yet.</p>}
        <div style={s.dateList}>
          {dates.map(d => (
            <div key={d} style={s.dateItem}>
              <span style={s.dateLabel}>{d}</span>
              <button onClick={() => handleDelete(d)} style={s.deleteBtn} title="Delete">&times;</button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

const s = {
  page: { maxWidth: 600, margin: '0 auto', padding: '24px 0' },
  heading: { fontSize: 20, fontWeight: 700, color: '#1a1a2e', marginBottom: 4 },
  desc: { fontSize: 13, color: '#64748b', marginBottom: 24 },
  row: { marginBottom: 16 },
  dateWrap: { display: 'flex', flexDirection: 'column', gap: 4 },
  label: { fontSize: 12, fontWeight: 600, color: '#374151' },
  dateInput: { padding: '9px 12px', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 14, width: 200, outline: 'none' },
  zoomBtn: { padding: '9px 18px', background: '#2D8CFF', color: '#fff', border: 'none', borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: 'pointer', whiteSpace: 'nowrap' },
  dropZone: {
    border: '2px dashed #d1d5db',
    borderRadius: 14,
    padding: '48px 20px',
    textAlign: 'center',
    cursor: 'pointer',
    transition: 'all 0.15s',
    marginBottom: 16,
    background: '#fff',
  },
  dropIcon: { fontSize: 32, marginBottom: 8 },
  dropText: { fontSize: 14, fontWeight: 500, color: '#374151' },
  dropHint: { fontSize: 11, color: '#94a3b8', marginTop: 4 },
  status: {
    padding: '10px 14px',
    borderRadius: 8,
    fontSize: 13,
    fontWeight: 500,
    marginBottom: 16,
    border: '1px solid',
  },
  section: { marginTop: 24 },
  subHead: { fontSize: 14, fontWeight: 600, color: '#1e293b', marginBottom: 10 },
  empty: { fontSize: 13, color: '#94a3b8' },
  dateList: { display: 'flex', flexDirection: 'column', gap: 4 },
  dateItem: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '8px 12px',
    background: '#fff',
    border: '1px solid #e5e7eb',
    borderRadius: 8,
  },
  dateLabel: { fontSize: 13, fontWeight: 500, color: '#1e293b' },
  deleteBtn: {
    background: 'none',
    border: 'none',
    color: '#dc2626',
    fontSize: 18,
    cursor: 'pointer',
    padding: '0 4px',
    lineHeight: 1,
  },
};
