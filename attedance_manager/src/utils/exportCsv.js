// CSV export utility for attendance data

function escapeCsv(val) {
  const s = String(val ?? '');
  return s.includes(',') || s.includes('"') || s.includes('\n') ? '"' + s.replace(/"/g, '""') + '"' : s;
}

function downloadCsv(filename, csvContent) {
  const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// Export Day View data
export function exportDayViewCsv(employees, date) {
  const headers = ['Name', 'Email', 'Login', 'Logout', 'Duration', 'Sessions', 'Rooms'];
  const rows = employees.map(emp => [
    emp.name,
    emp.email || '',
    emp.joined || '',
    emp.left || '',
    emp.duration || '',
    emp.sessions || 1,
    emp.rooms.filter(r => r.isNamed).length,
  ]);
  const csv = [headers, ...rows].map(r => r.map(escapeCsv).join(',')).join('\n');
  downloadCsv(`attendance_${date}.csv`, csv);
}

// Export Employee profile across dates
export function exportEmployeeCsv(emp) {
  const headers = ['Date', 'Login', 'Logout', 'Duration', 'Named Rooms'];
  const days = Object.entries(emp.days).sort((a, b) => a[0].localeCompare(b[0]));
  const rows = days.map(([date, d]) => [
    date,
    d.joined || '',
    d.left || '',
    d.duration || '',
    d.rooms.filter(r => r.isNamed).length,
  ]);
  const csv = [headers, ...rows].map(r => r.map(escapeCsv).join(',')).join('\n');
  downloadCsv(`employee_${emp.name.replace(/\s+/g, '_')}.csv`, csv);
}
