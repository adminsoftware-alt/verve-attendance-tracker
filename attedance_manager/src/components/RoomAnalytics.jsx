import { useState } from 'react';
import Rooms from './Rooms';
import Isolation from './Isolation';

/**
 * RoomAnalytics — merged wrapper for Rooms (usage) and Isolation (alone-time)
 * views. Both answer facility/wellness questions from the same underlying
 * attendance data; keeping them under one tab with a sub-toggle reduces
 * top-level nav clutter.
 */
export default function RoomAnalytics({ allData, uploadedDates }) {
  const [tab, setTab] = useState('rooms');

  return (
    <div>
      <div style={s.header}>
        <h2 style={s.title}>Room Analytics</h2>
        <div style={s.tabs}>
          <button
            onClick={() => setTab('rooms')}
            style={{ ...s.tabBtn, ...(tab === 'rooms' ? s.tabBtnOn : {}) }}
          >
            Room Usage
          </button>
          <button
            onClick={() => setTab('isolation')}
            style={{ ...s.tabBtn, ...(tab === 'isolation' ? s.tabBtnOn : {}) }}
          >
            Isolation
          </button>
        </div>
      </div>

      {tab === 'rooms' && <Rooms allData={allData} uploadedDates={uploadedDates} />}
      {tab === 'isolation' && <Isolation allData={allData} uploadedDates={uploadedDates} />}
    </div>
  );
}

const s = {
  header: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 20,
    flexWrap: 'wrap',
    gap: 12,
  },
  title: { fontSize: 22, fontWeight: 800, color: '#0f172a', margin: 0 },
  tabs: { display: 'flex', background: '#f1f5f9', borderRadius: 8, padding: 3 },
  tabBtn: {
    padding: '7px 16px',
    border: 'none',
    borderRadius: 6,
    fontSize: 12,
    fontWeight: 500,
    cursor: 'pointer',
    background: 'transparent',
    color: '#64748b',
  },
  tabBtnOn: { background: '#0f172a', color: '#fff', fontWeight: 600 },
};
