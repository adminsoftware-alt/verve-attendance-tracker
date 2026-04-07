import { useState, useCallback } from 'react';
import {
  adminSearchSnapshots, adminEditSnapshots, adminDeleteSnapshots, adminAddSnapshots,
  adminSearchEvents, adminEditEvents, adminDeleteEvents,
} from '../utils/zoomApi';

function istDate() {
  const now = new Date();
  return new Date(now.getTime() + 330 * 60000).toISOString().slice(0, 10);
}

function fmtTime(iso) {
  if (!iso) return '-';
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZone: 'Asia/Kolkata' });
  } catch { return iso; }
}

export default function DataEditor({ user }) {
  const [tab, setTab] = useState('snapshots');
  const [date, setDate] = useState(istDate);
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  // Snapshots
  const [snapshots, setSnapshots] = useState([]);
  const [summary, setSummary] = useState([]);
  const [selectedSnaps, setSelectedSnaps] = useState(new Set());

  // Events
  const [events, setEvents] = useState([]);
  const [selectedEvents, setSelectedEvents] = useState(new Set());

  // Edit modal
  const [editModal, setEditModal] = useState(null);
  const [editField, setEditField] = useState('');
  const [editValue, setEditValue] = useState('');

  // Add modal
  const [showAdd, setShowAdd] = useState(false);
  const [addRows, setAddRows] = useState([{ participant_name: '', room_name: 'Main Meeting', snapshot_time: '', event_date: date }]);

  const flash = (msg) => { setSuccess(msg); setTimeout(() => setSuccess(null), 3000); };

  const handleSearch = useCallback(async () => {
    setLoading(true);
    setError(null);
    setSelectedSnaps(new Set());
    setSelectedEvents(new Set());
    try {
      if (tab === 'snapshots') {
        const data = await adminSearchSnapshots(date, search);
        setSnapshots(data.snapshots || []);
        setSummary(data.summary || []);
      } else {
        const data = await adminSearchEvents(date, search);
        setEvents(data.events || []);
      }
    } catch (e) { setError(e.message); }
    setLoading(false);
  }, [tab, date, search]);

  const toggleSnap = (id) => {
    setSelectedSnaps(prev => {
      const n = new Set(prev);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });
  };

  const toggleEvent = (id) => {
    setSelectedEvents(prev => {
      const n = new Set(prev);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });
  };

  const selectAllSnaps = () => {
    if (selectedSnaps.size === snapshots.length) setSelectedSnaps(new Set());
    else setSelectedSnaps(new Set(snapshots.map(s => s.snapshot_id)));
  };

  const selectAllEvents = () => {
    if (selectedEvents.size === events.length) setSelectedEvents(new Set());
    else setSelectedEvents(new Set(events.map(e => e.event_id)));
  };

  const selectSummaryGroup = (group) => {
    setSelectedSnaps(new Set(group.snapshot_ids));
  };

  // Edit selected
  const openEdit = (type, field) => {
    setEditModal(type);
    setEditField(field);
    setEditValue('');
  };

  const handleEdit = async () => {
    if (!editValue.trim()) return;
    setLoading(true);
    try {
      if (editModal === 'snapshots') {
        await adminEditSnapshots([...selectedSnaps], { [editField]: editValue });
        flash(`Updated ${selectedSnaps.size} snapshots`);
      } else {
        await adminEditEvents([...selectedEvents], { [editField]: editValue });
        flash(`Updated ${selectedEvents.size} events`);
      }
      setEditModal(null);
      handleSearch();
    } catch (e) { setError(e.message); }
    setLoading(false);
  };

  // Delete selected
  const handleDelete = async (type) => {
    const ids = type === 'snapshots' ? [...selectedSnaps] : [...selectedEvents];
    if (!ids.length) return;
    if (!window.confirm(`Delete ${ids.length} ${type}? This cannot be undone.`)) return;
    setLoading(true);
    try {
      if (type === 'snapshots') {
        await adminDeleteSnapshots(ids);
        flash(`Deleted ${ids.length} snapshots`);
      } else {
        await adminDeleteEvents(ids);
        flash(`Deleted ${ids.length} events`);
      }
      handleSearch();
    } catch (e) { setError(e.message); }
    setLoading(false);
  };

  // Add snapshots
  const handleAdd = async () => {
    const valid = addRows.filter(r => r.participant_name.trim());
    if (!valid.length) return;
    setLoading(true);
    try {
      await adminAddSnapshots(valid.map(r => ({ ...r, event_date: r.event_date || date })));
      flash(`Added ${valid.length} snapshot rows`);
      setShowAdd(false);
      setAddRows([{ participant_name: '', room_name: 'Main Meeting', snapshot_time: '', event_date: date }]);
      handleSearch();
    } catch (e) { setError(e.message); }
    setLoading(false);
  };

  const updateAddRow = (i, field, val) => {
    setAddRows(prev => prev.map((r, idx) => idx === i ? { ...r, [field]: val } : r));
  };

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 20 }}>
        <h2 style={{ margin: 0, fontSize: 22, fontWeight: 800, color: '#1a365d' }}>Data Editor</h2>
        <span style={S.roleBadge}>SUPERADMIN</span>
      </div>

      {error && <div style={S.errorBar}>{error} <button onClick={() => setError(null)} style={S.dismissBtn}>x</button></div>}
      {success && <div style={S.successBar}>{success}</div>}

      {/* Controls */}
      <div style={S.controls}>
        <div style={S.tabs}>
          <button onClick={() => setTab('snapshots')} style={{ ...S.tabBtn, ...(tab === 'snapshots' ? S.tabActive : {}) }}>
            Room Snapshots
          </button>
          <button onClick={() => setTab('events')} style={{ ...S.tabBtn, ...(tab === 'events' ? S.tabActive : {}) }}>
            Webhook Events
          </button>
        </div>
        <input type="date" value={date} onChange={e => setDate(e.target.value)} style={S.dateInput} />
        <input
          placeholder="Search participant..."
          value={search}
          onChange={e => setSearch(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && handleSearch()}
          style={S.searchInput}
        />
        <button onClick={handleSearch} disabled={loading} style={S.searchBtn}>
          {loading ? 'Loading...' : 'Search'}
        </button>
      </div>

      {/* Snapshot Tab */}
      {tab === 'snapshots' && (
        <div>
          {/* Summary view - grouped by participant + room */}
          {summary.length > 0 && (
            <div style={{ marginBottom: 16 }}>
              <h3 style={S.sectionTitle}>Room Visit Summary (click to select snapshots)</h3>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                {summary.map((g, i) => (
                  <button key={i} onClick={() => selectSummaryGroup(g)} style={S.summaryCard}>
                    <div style={{ fontWeight: 700, fontSize: 13 }}>{g.participant_name}</div>
                    <div style={{ fontSize: 11, color: '#666' }}>{g.room_name}</div>
                    <div style={{ fontSize: 11, color: '#888' }}>
                      {fmtTime(g.first_seen)} - {fmtTime(g.last_seen)} ({g.snapshot_count} snaps)
                    </div>
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Action bar */}
          {snapshots.length > 0 && (
            <div style={S.actionBar}>
              <span style={{ fontSize: 12, color: '#666' }}>{selectedSnaps.size} selected of {snapshots.length}</span>
              <button onClick={selectAllSnaps} style={S.actionBtn}>
                {selectedSnaps.size === snapshots.length ? 'Deselect All' : 'Select All'}
              </button>
              <button onClick={() => openEdit('snapshots', 'room_name')} disabled={!selectedSnaps.size} style={{ ...S.actionBtn, ...S.editBtn }}>
                Edit Room
              </button>
              <button onClick={() => openEdit('snapshots', 'participant_name')} disabled={!selectedSnaps.size} style={{ ...S.actionBtn, ...S.editBtn }}>
                Edit Name
              </button>
              <button onClick={() => openEdit('snapshots', 'snapshot_time')} disabled={!selectedSnaps.size} style={{ ...S.actionBtn, ...S.editBtn }}>
                Edit Time
              </button>
              <button onClick={() => handleDelete('snapshots')} disabled={!selectedSnaps.size} style={{ ...S.actionBtn, ...S.deleteBtn }}>
                Delete Selected
              </button>
              <button onClick={() => setShowAdd(true)} style={{ ...S.actionBtn, background: '#2563eb', color: '#fff' }}>
                + Add Rows
              </button>
            </div>
          )}

          {/* Snapshot table */}
          {snapshots.length > 0 && (
            <div style={S.tableWrap}>
              <table style={S.table}>
                <thead>
                  <tr>
                    <th style={S.th}><input type="checkbox" checked={selectedSnaps.size === snapshots.length && snapshots.length > 0} onChange={selectAllSnaps} /></th>
                    <th style={S.th}>Time</th>
                    <th style={S.th}>Participant</th>
                    <th style={S.th}>Room</th>
                    <th style={S.th}>ID</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshots.map(s => (
                    <tr key={s.snapshot_id} style={selectedSnaps.has(s.snapshot_id) ? S.selectedRow : {}}>
                      <td style={S.td}><input type="checkbox" checked={selectedSnaps.has(s.snapshot_id)} onChange={() => toggleSnap(s.snapshot_id)} /></td>
                      <td style={S.td}>{fmtTime(s.snapshot_time)}</td>
                      <td style={S.td}>{s.participant_name}</td>
                      <td style={S.td}>{s.room_name}</td>
                      <td style={{ ...S.td, fontSize: 10, color: '#999' }}>{s.snapshot_id.slice(0, 8)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {!loading && snapshots.length === 0 && <div style={S.empty}>No snapshots found. Search by date and participant name.</div>}
        </div>
      )}

      {/* Events Tab */}
      {tab === 'events' && (
        <div>
          {events.length > 0 && (
            <div style={S.actionBar}>
              <span style={{ fontSize: 12, color: '#666' }}>{selectedEvents.size} selected of {events.length}</span>
              <button onClick={selectAllEvents} style={S.actionBtn}>
                {selectedEvents.size === events.length ? 'Deselect All' : 'Select All'}
              </button>
              <button onClick={() => openEdit('events', 'room_name')} disabled={!selectedEvents.size} style={{ ...S.actionBtn, ...S.editBtn }}>
                Edit Room
              </button>
              <button onClick={() => openEdit('events', 'participant_name')} disabled={!selectedEvents.size} style={{ ...S.actionBtn, ...S.editBtn }}>
                Edit Name
              </button>
              <button onClick={() => openEdit('events', 'event_type')} disabled={!selectedEvents.size} style={{ ...S.actionBtn, ...S.editBtn }}>
                Edit Type
              </button>
              <button onClick={() => openEdit('events', 'event_timestamp')} disabled={!selectedEvents.size} style={{ ...S.actionBtn, ...S.editBtn }}>
                Edit Time
              </button>
              <button onClick={() => handleDelete('events')} disabled={!selectedEvents.size} style={{ ...S.actionBtn, ...S.deleteBtn }}>
                Delete Selected
              </button>
            </div>
          )}

          {events.length > 0 && (
            <div style={S.tableWrap}>
              <table style={S.table}>
                <thead>
                  <tr>
                    <th style={S.th}><input type="checkbox" checked={selectedEvents.size === events.length && events.length > 0} onChange={selectAllEvents} /></th>
                    <th style={S.th}>Time</th>
                    <th style={S.th}>Type</th>
                    <th style={S.th}>Participant</th>
                    <th style={S.th}>Room</th>
                    <th style={S.th}>ID</th>
                  </tr>
                </thead>
                <tbody>
                  {events.map(e => (
                    <tr key={e.event_id} style={selectedEvents.has(e.event_id) ? S.selectedRow : {}}>
                      <td style={S.td}><input type="checkbox" checked={selectedEvents.has(e.event_id)} onChange={() => toggleEvent(e.event_id)} /></td>
                      <td style={S.td}>{fmtTime(e.event_timestamp)}</td>
                      <td style={S.td}>
                        <span style={{
                          ...S.typeBadge,
                          background: e.event_type?.includes('joined') ? '#dcfce7' : '#fee2e2',
                          color: e.event_type?.includes('joined') ? '#166534' : '#991b1b',
                        }}>{e.event_type}</span>
                      </td>
                      <td style={S.td}>{e.participant_name}</td>
                      <td style={S.td}>{e.room_name || '-'}</td>
                      <td style={{ ...S.td, fontSize: 10, color: '#999' }}>{e.event_id?.slice(0, 8)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {!loading && events.length === 0 && <div style={S.empty}>No events found. Search by date and participant name.</div>}
        </div>
      )}

      {/* Edit Modal */}
      {editModal && (
        <div style={S.overlay} onClick={() => setEditModal(null)}>
          <div style={S.modal} onClick={e => e.stopPropagation()}>
            <h3 style={{ margin: '0 0 12px', fontSize: 16 }}>
              Edit {editField.replace('_', ' ')} ({editModal === 'snapshots' ? selectedSnaps.size : selectedEvents.size} rows)
            </h3>
            <input
              type={editField === 'snapshot_time' || editField === 'event_timestamp' ? 'datetime-local' : 'text'}
              step={editField === 'snapshot_time' || editField === 'event_timestamp' ? '1' : undefined}
              value={editValue}
              onChange={e => setEditValue(e.target.value)}
              placeholder={`New ${editField.replace(/_/g, ' ')}...`}
              style={S.modalInput}
              autoFocus
              onKeyDown={e => e.key === 'Enter' && handleEdit()}
            />
            <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
              <button onClick={handleEdit} disabled={loading} style={S.saveBtn}>
                {loading ? 'Saving...' : 'Save'}
              </button>
              <button onClick={() => setEditModal(null)} style={S.cancelBtn}>Cancel</button>
            </div>
          </div>
        </div>
      )}

      {/* Add Snapshots Modal */}
      {showAdd && (
        <div style={S.overlay} onClick={() => setShowAdd(false)}>
          <div style={{ ...S.modal, width: 600 }} onClick={e => e.stopPropagation()}>
            <h3 style={{ margin: '0 0 12px', fontSize: 16 }}>Add Snapshot Rows</h3>
            {addRows.map((r, i) => (
              <div key={i} style={{ display: 'flex', gap: 6, marginBottom: 6 }}>
                <input placeholder="Participant name" value={r.participant_name} onChange={e => updateAddRow(i, 'participant_name', e.target.value)} style={{ ...S.modalInput, flex: 2 }} />
                <input placeholder="Room name" value={r.room_name} onChange={e => updateAddRow(i, 'room_name', e.target.value)} style={{ ...S.modalInput, flex: 2 }} />
                <input type="datetime-local" value={r.snapshot_time} onChange={e => updateAddRow(i, 'snapshot_time', e.target.value)} style={{ ...S.modalInput, flex: 2 }} />
                {addRows.length > 1 && (
                  <button onClick={() => setAddRows(prev => prev.filter((_, idx) => idx !== i))} style={S.removeRowBtn}>x</button>
                )}
              </div>
            ))}
            <button onClick={() => setAddRows(prev => [...prev, { participant_name: '', room_name: 'Main Meeting', snapshot_time: '', event_date: date }])} style={{ ...S.actionBtn, marginTop: 8 }}>
              + Add Row
            </button>
            <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
              <button onClick={handleAdd} disabled={loading} style={S.saveBtn}>
                {loading ? 'Adding...' : `Add ${addRows.filter(r => r.participant_name.trim()).length} rows`}
              </button>
              <button onClick={() => setShowAdd(false)} style={S.cancelBtn}>Cancel</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

const S = {
  roleBadge: { background: '#dc2626', color: '#fff', fontSize: 10, fontWeight: 700, padding: '3px 10px', borderRadius: 4, letterSpacing: '0.05em' },
  errorBar: { background: '#fee2e2', color: '#991b1b', padding: '10px 16px', borderRadius: 8, marginBottom: 12, fontSize: 13, display: 'flex', alignItems: 'center', justifyContent: 'space-between' },
  successBar: { background: '#dcfce7', color: '#166534', padding: '10px 16px', borderRadius: 8, marginBottom: 12, fontSize: 13 },
  dismissBtn: { background: 'none', border: 'none', cursor: 'pointer', fontSize: 16, color: '#991b1b' },
  controls: { display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16, flexWrap: 'wrap' },
  tabs: { display: 'flex', gap: 0, marginRight: 8 },
  tabBtn: { padding: '8px 16px', border: '1px solid #d1d5db', background: '#fff', cursor: 'pointer', fontSize: 12, fontWeight: 600, color: '#666' },
  tabActive: { background: '#1a365d', color: '#fff', borderColor: '#1a365d' },
  dateInput: { padding: '7px 10px', border: '1px solid #d1d5db', borderRadius: 6, fontSize: 13 },
  searchInput: { padding: '7px 12px', border: '1px solid #d1d5db', borderRadius: 6, fontSize: 13, width: 200 },
  searchBtn: { padding: '8px 18px', background: '#1a365d', color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer', fontWeight: 600, fontSize: 13 },
  sectionTitle: { fontSize: 13, fontWeight: 700, color: '#374151', marginBottom: 8 },
  summaryCard: { padding: '8px 14px', background: '#fff', border: '1px solid #e5e7eb', borderRadius: 8, cursor: 'pointer', textAlign: 'left', transition: 'all 0.15s' },
  actionBar: { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10, padding: '8px 12px', background: '#f8fafc', borderRadius: 8, border: '1px solid #e2e8f0' },
  actionBtn: { padding: '5px 12px', border: '1px solid #d1d5db', borderRadius: 5, background: '#fff', cursor: 'pointer', fontSize: 11, fontWeight: 600 },
  editBtn: { background: '#fef3c7', borderColor: '#fbbf24', color: '#92400e' },
  deleteBtn: { background: '#fee2e2', borderColor: '#fca5a5', color: '#991b1b' },
  tableWrap: { maxHeight: 500, overflow: 'auto', border: '1px solid #e5e7eb', borderRadius: 8 },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 12 },
  th: { padding: '8px 10px', background: '#f1f5f9', borderBottom: '1px solid #e2e8f0', textAlign: 'left', fontWeight: 700, fontSize: 11, position: 'sticky', top: 0 },
  td: { padding: '6px 10px', borderBottom: '1px solid #f1f5f9' },
  selectedRow: { background: '#eff6ff' },
  typeBadge: { padding: '2px 8px', borderRadius: 4, fontSize: 10, fontWeight: 600 },
  empty: { padding: 40, textAlign: 'center', color: '#9ca3af', fontSize: 14 },
  overlay: { position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.4)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 },
  modal: { background: '#fff', borderRadius: 12, padding: 24, width: 400, maxWidth: '90vw' },
  modalInput: { width: '100%', padding: '8px 12px', border: '1px solid #d1d5db', borderRadius: 6, fontSize: 13, boxSizing: 'border-box' },
  saveBtn: { padding: '8px 20px', background: '#1a365d', color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer', fontWeight: 600, fontSize: 13 },
  cancelBtn: { padding: '8px 20px', background: '#f3f4f6', color: '#374151', border: '1px solid #d1d5db', borderRadius: 6, cursor: 'pointer', fontSize: 13 },
  removeRowBtn: { background: '#fee2e2', color: '#991b1b', border: 'none', borderRadius: 4, cursor: 'pointer', padding: '0 8px', fontWeight: 700 },
};
