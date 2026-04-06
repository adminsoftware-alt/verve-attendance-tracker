import { timeToMin } from './parser';

/**
 * Analyze isolation for a given date's employees.
 * An employee is "isolated" when they are the only person in a named room.
 * Returns isolation scores and details for each employee.
 */
export function analyzeIsolation(employees) {
  if (!employees || !employees.length) return [];

  // Build a timeline of all named room occupancy
  // For each named room, track who was there and when
  const roomTimelines = {};

  employees.forEach(emp => {
    emp.rooms.forEach(room => {
      if (!room.isNamed) return;
      const rName = room.name;
      if (!roomTimelines[rName]) roomTimelines[rName] = [];
      roomTimelines[rName].push({
        employee: emp.name,
        email: emp.email,
        start: timeToMin(room.start),
        end: timeToMin(room.end),
        startStr: room.start,
        endStr: room.end,
      });
    });
  });

  // For each employee, calculate how many minutes they spent alone in named rooms
  const results = employees.map(emp => {
    let aloneMinutes = 0;
    let totalNamedMinutes = 0;
    const aloneRooms = [];

    emp.rooms.forEach(room => {
      if (!room.isNamed) return;
      const rName = room.name;
      const myStart = timeToMin(room.start);
      const myEnd = timeToMin(room.end);
      const myDur = Math.max(myEnd - myStart, 0);
      totalNamedMinutes += myDur;

      if (!roomTimelines[rName]) return;

      // Check overlap with others in the same room
      const others = roomTimelines[rName].filter(o =>
        (o.employee !== emp.name || o.email !== emp.email) &&
        o.start < myEnd && o.end > myStart
      );

      if (others.length === 0) {
        // Completely alone in this room
        aloneMinutes += myDur;
        aloneRooms.push({
          room: rName,
          start: room.start,
          end: room.end,
          duration: myDur,
          alone: true,
        });
      } else {
        // Calculate minutes with nobody else
        // Build a coverage array of when others are present
        let coveredMin = 0;
        const intervals = others.map(o => ({
          s: Math.max(o.start, myStart),
          e: Math.min(o.end, myEnd)
        })).filter(i => i.e > i.s);

        // Merge overlapping intervals
        intervals.sort((a, b) => a.s - b.s);
        const merged = [];
        for (const iv of intervals) {
          if (merged.length && iv.s <= merged[merged.length - 1].e) {
            merged[merged.length - 1].e = Math.max(merged[merged.length - 1].e, iv.e);
          } else {
            merged.push({ ...iv });
          }
        }
        coveredMin = merged.reduce((s, i) => s + (i.e - i.s), 0);
        const aloneInThisRoom = myDur - coveredMin;

        if (aloneInThisRoom > 0) {
          aloneMinutes += aloneInThisRoom;
          aloneRooms.push({
            room: rName,
            start: room.start,
            end: room.end,
            duration: myDur,
            aloneMinutes: aloneInThisRoom,
            alone: aloneInThisRoom === myDur,
          });
        }
      }
    });

    const isolationScore = totalNamedMinutes > 0
      ? Math.round((aloneMinutes / totalNamedMinutes) * 100)
      : 0;

    return {
      name: emp.name,
      email: emp.email,
      totalNamedMinutes,
      aloneMinutes,
      isolationScore,
      aloneRooms,
      uniqueAloneRooms: [...new Set(aloneRooms.map(r => r.room))].length,
    };
  });

  return results.sort((a, b) => b.isolationScore - a.isolationScore);
}

export function getIsolationLevel(score) {
  if (score >= 70) return { label: 'High', color: '#dc2626' };
  if (score >= 40) return { label: 'Moderate', color: '#f59e0b' };
  if (score >= 15) return { label: 'Low', color: '#3b82f6' };
  return { label: 'Minimal', color: '#22c55e' };
}
