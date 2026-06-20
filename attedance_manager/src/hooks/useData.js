import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { getUploadedDates, getDayData } from '../utils/storage';
import { mergeDayEmployees, todayIST } from '../utils/parser';
import { fetchSummary, transformSummaryToEmployees, fetchLiveRooms, transformLiveToEmployees } from '../utils/zoomApi';

const LIVE_INTERVAL = 30000; // 30 seconds

// Central data hook — loads dates list + auto-fetches today from Zoom API
// Data is loaded on-demand per date to prevent performance issues as history grows
export function useAllData(refreshKey) {
  const [storedDates, setStoredDates] = useState([]);
  const [liveData, setLiveData] = useState({});
  const [loading, setLoading] = useState(true);
  const liveTimer = useRef(null);

  // Load only the dates list (lightweight) - individual day data loaded on demand
  const loadStored = useCallback(async () => {
    try {
      const allDates = await getUploadedDates();
      setStoredDates(allDates);
    } catch (err) {
      console.error('Dates load error:', err);
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

  // Live data only (historical data loaded on-demand via useDayData hook)
  const data = useMemo(() => {
    const merged = {};
    for (const [date, entry] of Object.entries(liveData)) {
      merged[date] = mergeDayEmployees(entry.employees);
    }
    return merged;
  }, [liveData]);

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

    // Priority: 1) Zoom summary API (has correct duration totals from presence_intervals)
    // 2) Live rooms API (for ongoing meetings); 3) Uploaded CSV data (fallback only).
    // This ensures totals always come from backend calculations, not stale uploads.
    (async () => {
      let result = null;

      // Try Zoom summary first - this has correct totals from presence_intervals
      try {
        const summary = await fetchSummary(dateStr);
        const emps = transformSummaryToEmployees(summary);
        if (emps.length > 0) result = emps;
      } catch (e) { /* try live */ }

      // Try live rooms for ongoing meetings
      if (!result || result.length === 0) {
        try {
          const live = await fetchLiveRooms(dateStr);
          const emps = transformLiveToEmployees(live);
          if (emps.length > 0) result = emps;
        } catch (e) { /* try stored */ }
      }

      // Fall back to uploaded CSV data only if API unavailable
      if (!result || result.length === 0) {
        try {
          result = await getDayData(dateStr);
        } catch (e) { /* no data */ }
      }

      if (!cancelled) {
        setEmployees(mergeDayEmployees(result || []));
        setLoading(false);
      }
    })();

    return () => { cancelled = true; };
  }, [dateStr, refreshKey]);

  return { employees, loading };
}
