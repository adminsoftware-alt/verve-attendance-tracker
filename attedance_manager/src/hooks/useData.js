import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { getAllData, getUploadedDates, getDayData } from '../utils/storage';
import { mergeDayEmployees, todayIST } from '../utils/parser';
import { fetchSummary, transformSummaryToEmployees, fetchLiveRooms, transformLiveToEmployees } from '../utils/zoomApi';

const LIVE_INTERVAL = 30000; // 30 seconds

// Central data hook — loads stored data + auto-fetches today from Zoom API
export function useAllData(refreshKey) {
  const [storedData, setStoredData] = useState({});
  const [storedDates, setStoredDates] = useState([]);
  const [liveData, setLiveData] = useState({});
  const [loading, setLoading] = useState(true);
  const liveTimer = useRef(null);

  // Load stored data from Supabase/localStorage
  const loadStored = useCallback(async () => {
    try {
      const [allData, allDates] = await Promise.all([getAllData(), getUploadedDates()]);
      setStoredData(allData);
      setStoredDates(allDates);
    } catch (err) {
      console.error('Stored data load error:', err);
    }
  }, []);

  // Fetch today's live data from Zoom API (lightweight, only today)
  // Tracks source so merge logic knows whether data is full or partial
  const loadLive = useCallback(async () => {
    const today = todayIST();
    try {
      // Try summary first (full data with times)
      const summary = await fetchSummary(today);
      const employees = transformSummaryToEmployees(summary);
      if (employees.length > 0) {
        setLiveData(prev => ({ ...prev, [today]: { employees, source: 'summary' } }));
        return;
      }
    } catch (e) {
      // Summary failed — try live endpoint as fallback
    }
    try {
      const live = await fetchLiveRooms(today);
      const employees = transformLiveToEmployees(live);
      if (employees.length > 0) {
        setLiveData(prev => ({ ...prev, [today]: { employees, source: 'live' } }));
      }
    } catch (e) {
      // Both failed — no live data available
    }
  }, []);

  // Initial load: stored + live
  useEffect(() => {
    setLoading(true);
    Promise.all([loadStored(), loadLive()]).finally(() => setLoading(false));
  }, [loadStored, loadLive, refreshKey]);

  // Auto-refresh live data every 30s
  useEffect(() => {
    liveTimer.current = window.setInterval(loadLive, LIVE_INTERVAL);
    return () => window.clearInterval(liveTimer.current);
  }, [loadLive]);

  // Merge stored + live data intelligently
  // - 'summary' source: full data, can override stored
  // - 'live' source: partial (no times), only use if no stored data exists for that date
  const data = useMemo(() => {
    const combined = { ...storedData };
    for (const [date, entry] of Object.entries(liveData)) {
      if (entry.source === 'summary') {
        // Full data — override stored
        combined[date] = entry.employees;
      } else if (!combined[date] || combined[date].length === 0) {
        // Partial live data — only use if no stored data exists
        combined[date] = entry.employees;
      }
      // else: stored data exists AND live is partial — keep stored
    }
    const merged = {};
    for (const date of Object.keys(combined)) {
      merged[date] = mergeDayEmployees(combined[date]);
    }
    return merged;
  }, [storedData, liveData]);

  const dates = useMemo(() => {
    const set = new Set([...storedDates, ...Object.keys(liveData)]);
    return [...set].sort();
  }, [storedDates, liveData]);

  const reload = useCallback(async () => {
    setLoading(true);
    await Promise.all([loadStored(), loadLive()]);
    setLoading(false);
  }, [loadStored, loadLive]);

  return { data, dates, loading, reload };
}

export function useDayData(dateStr, refreshKey) {
  const [employees, setEmployees] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getDayData(dateStr).then(result => {
      if (!cancelled) {
        setEmployees(mergeDayEmployees(result || []));
        setLoading(false);
      }
    });
    return () => { cancelled = true; };
  }, [dateStr, refreshKey]);

  return { employees, loading };
}
