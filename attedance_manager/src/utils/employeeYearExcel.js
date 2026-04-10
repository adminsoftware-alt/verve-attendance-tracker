/**
 * Employee Year Summary Excel export.
 *
 * Creates a single-sheet workbook with 12 rows (one per month)
 * showing detailed attendance metrics with color coding.
 */
import XLSX from 'xlsx-js-style';

const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December'
];

// Thresholds
const LOW_ATTENDANCE_PCT = 80;
const HIGH_ATTENDANCE_PCT = 95;
const LOW_HOURS_THRESHOLD = 140;
const HIGH_BREAK_THRESHOLD = 15;
const HIGH_ISOLATION_THRESHOLD = 10;

// ── Style presets ────────────────────────────────────────
const BORDER_THIN = {
  top: { style: 'thin', color: { rgb: 'FFBFC4CC' } },
  bottom: { style: 'thin', color: { rgb: 'FFBFC4CC' } },
  left: { style: 'thin', color: { rgb: 'FFBFC4CC' } },
  right: { style: 'thin', color: { rgb: 'FFBFC4CC' } },
};

const TITLE_STYLE = {
  font: { bold: true, sz: 16, color: { rgb: 'FF1E293B' } },
  alignment: { horizontal: 'left' },
};

const SUBTITLE_STYLE = {
  font: { bold: true, sz: 12, color: { rgb: 'FF64748B' } },
  alignment: { horizontal: 'left' },
};

const HEADER_STYLE = {
  font: { bold: true, color: { rgb: 'FFFFFFFF' }, sz: 11 },
  fill: { patternType: 'solid', fgColor: { rgb: 'FF8093B3' } },
  alignment: { horizontal: 'center', vertical: 'center', wrapText: true },
  border: BORDER_THIN,
};

const MONTH_CELL_STYLE = {
  font: { bold: true, color: { rgb: 'FF1E293B' } },
  fill: { patternType: 'solid', fgColor: { rgb: 'FFF4F6F8' } },
  alignment: { horizontal: 'left', vertical: 'center' },
  border: BORDER_THIN,
};

const NUM_CELL_STYLE = {
  alignment: { horizontal: 'center', vertical: 'center' },
  border: BORDER_THIN,
  numFmt: '0.0',
};

const INT_CELL_STYLE = {
  alignment: { horizontal: 'center', vertical: 'center' },
  border: BORDER_THIN,
  numFmt: '0',
};

const TEXT_CELL_STYLE = {
  alignment: { horizontal: 'center', vertical: 'center' },
  border: BORDER_THIN,
};

const RED_STYLE = {
  ...NUM_CELL_STYLE,
  fill: { patternType: 'solid', fgColor: { rgb: 'FFFEE2E2' } },
  font: { bold: true, color: { rgb: 'FFDC2626' } },
};

const GREEN_STYLE = {
  ...NUM_CELL_STYLE,
  fill: { patternType: 'solid', fgColor: { rgb: 'FFDCFCE7' } },
  font: { bold: true, color: { rgb: 'FF15803D' } },
};

const ORANGE_STYLE = {
  ...NUM_CELL_STYLE,
  fill: { patternType: 'solid', fgColor: { rgb: 'FFFFEDD5' } },
  font: { bold: true, color: { rgb: 'FFC2410C' } },
};

const YELLOW_STYLE = {
  ...NUM_CELL_STYLE,
  fill: { patternType: 'solid', fgColor: { rgb: 'FFFEF9C3' } },
  font: { bold: true, color: { rgb: 'FF854D0E' } },
};

const PINK_STYLE = {
  ...NUM_CELL_STYLE,
  fill: { patternType: 'solid', fgColor: { rgb: 'FFFCE7F3' } },
  font: { bold: true, color: { rgb: 'FFBE185D' } },
};

const TOTAL_STYLE = {
  font: { bold: true, color: { rgb: 'FF1E293B' } },
  fill: { patternType: 'solid', fgColor: { rgb: 'FFDFE4EC' } },
  alignment: { horizontal: 'center', vertical: 'center' },
  border: BORDER_THIN,
  numFmt: '0.0',
};

const TOTAL_INT_STYLE = {
  ...TOTAL_STYLE,
  numFmt: '0',
};

const LEGEND_STYLE = {
  font: { sz: 10, color: { rgb: 'FF64748B' } },
  alignment: { horizontal: 'left' },
};

// ── Main Export Function ─────────────────────────────────

export function downloadEmployeeYearExcel(summaryData, employeeName, year) {
  if (!summaryData || !summaryData.monthly_summary) return;

  const wb = XLSX.utils.book_new();
  const rows = [];

  // Header rows
  rows.push([{ v: `Employee Year Summary: ${employeeName}`, s: TITLE_STYLE }]);
  rows.push([{ v: `Year: ${year}`, s: SUBTITLE_STYLE }]);
  rows.push([{ v: `Generated: ${new Date().toLocaleString()}`, s: SUBTITLE_STYLE }]);
  rows.push([]); // Empty row

  // Table headers
  const headers = [
    'Month', 'Working Days', 'Present', 'Half Day', 'Absent',
    'Attendance %', 'Total Hours', 'Avg Login', 'Avg Logout', 'Break Hours', 'Isolation Hours'
  ];
  rows.push(headers.map(h => ({ v: h, s: HEADER_STYLE })));

  // Data rows (12 months)
  for (const m of summaryData.monthly_summary) {
    const attStyle = getAttendanceStyle(m.attendance_pct);
    const hoursStyle = getHoursStyle(m.total_hours);
    const breakStyle = getBreakStyle(m.break_hours);
    const isoStyle = getIsolationStyle(m.isolation_hours);

    rows.push([
      { v: m.month_name, s: MONTH_CELL_STYLE },
      { v: m.working_days, s: INT_CELL_STYLE },
      { v: m.present_days, s: INT_CELL_STYLE },
      { v: m.half_days, s: INT_CELL_STYLE },
      { v: m.absent_days, s: m.absent_days > 0 ? RED_STYLE : INT_CELL_STYLE },
      { v: m.attendance_pct, s: attStyle },
      { v: m.total_hours, s: hoursStyle },
      { v: m.avg_login || '-', s: TEXT_CELL_STYLE },
      { v: m.avg_logout || '-', s: TEXT_CELL_STYLE },
      { v: m.break_hours, s: breakStyle },
      { v: m.isolation_hours, s: isoStyle },
    ]);
  }

  // Total row
  const totals = summaryData.yearly_totals;
  const totalAttStyle = getAttendanceStyle(totals.attendance_pct);
  rows.push([
    { v: 'TOTAL', s: { ...TOTAL_STYLE, font: { bold: true, sz: 12, color: { rgb: 'FF1E293B' } } } },
    { v: totals.working_days, s: TOTAL_INT_STYLE },
    { v: totals.present_days, s: TOTAL_INT_STYLE },
    { v: totals.half_days, s: TOTAL_INT_STYLE },
    { v: totals.absent_days, s: { ...TOTAL_INT_STYLE, font: { bold: true, color: { rgb: 'FFDC2626' } } } },
    { v: totals.attendance_pct, s: { ...TOTAL_STYLE, ...totalAttStyle } },
    { v: totals.total_hours, s: TOTAL_STYLE },
    { v: '-', s: TOTAL_STYLE },
    { v: '-', s: TOTAL_STYLE },
    { v: totals.total_break_hours, s: TOTAL_STYLE },
    { v: totals.total_isolation_hours, s: TOTAL_STYLE },
  ]);

  // Empty row + Legend
  rows.push([]);
  rows.push([{ v: 'Color Legend:', s: { font: { bold: true, sz: 11 } } }]);
  rows.push([{ v: 'Red: < 80% Attendance or Absent days', s: LEGEND_STYLE }]);
  rows.push([{ v: 'Green: >= 95% Attendance', s: LEGEND_STYLE }]);
  rows.push([{ v: 'Orange: Low hours for the month', s: LEGEND_STYLE }]);
  rows.push([{ v: 'Yellow: > 15 hours break time', s: LEGEND_STYLE }]);
  rows.push([{ v: 'Pink: > 10 hours isolation time', s: LEGEND_STYLE }]);

  // Create worksheet
  const ws = XLSX.utils.aoa_to_sheet(rows);

  // Column widths
  ws['!cols'] = [
    { wch: 14 },  // Month
    { wch: 14 },  // Working Days
    { wch: 10 },  // Present
    { wch: 10 },  // Half Day
    { wch: 10 },  // Absent
    { wch: 14 },  // Attendance %
    { wch: 12 },  // Total Hours
    { wch: 12 },  // Avg Login
    { wch: 12 },  // Avg Logout
    { wch: 12 },  // Break Hours
    { wch: 14 },  // Isolation Hours
  ];

  // Freeze header rows
  ws['!freeze'] = { xSplit: 0, ySplit: 5 };

  // Add sheet and save
  XLSX.utils.book_append_sheet(wb, ws, 'Year Summary');

  const filename = `Employee_Summary_${employeeName.replace(/\s+/g, '_')}_${year}.xlsx`;
  XLSX.writeFile(wb, filename);
}

// ── Style Helper Functions ───────────────────────────────

function getAttendanceStyle(pct) {
  if (pct < LOW_ATTENDANCE_PCT) return RED_STYLE;
  if (pct >= HIGH_ATTENDANCE_PCT) return GREEN_STYLE;
  return NUM_CELL_STYLE;
}

function getHoursStyle(hours) {
  if (hours > 0 && hours < LOW_HOURS_THRESHOLD) return ORANGE_STYLE;
  return NUM_CELL_STYLE;
}

function getBreakStyle(hours) {
  if (hours > HIGH_BREAK_THRESHOLD) return YELLOW_STYLE;
  return NUM_CELL_STYLE;
}

function getIsolationStyle(hours) {
  if (hours > HIGH_ISOLATION_THRESHOLD) return PINK_STYLE;
  return NUM_CELL_STYLE;
}
