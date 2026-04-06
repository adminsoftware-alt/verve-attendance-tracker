-- Run this in Supabase SQL Editor (Dashboard > SQL Editor > New Query)

-- Users table for login
CREATE TABLE app_users (
  id SERIAL PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password TEXT NOT NULL,
  name TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Insert default users
INSERT INTO app_users (username, password, name) VALUES
  ('admin', 'verve2026', 'Admin'),
  ('hr1', 'verve2026', 'HR User 1'),
  ('hr2', 'verve2026', 'HR User 2'),
  ('manager1', 'verve2026', 'Manager 1'),
  ('manager2', 'verve2026', 'Manager 2');

-- Attendance data stored per date (JSON blob per day)
CREATE TABLE attendance_days (
  id SERIAL PRIMARY KEY,
  report_date DATE UNIQUE NOT NULL,
  employees JSONB NOT NULL,
  uploaded_by TEXT,
  uploaded_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for fast date lookups
CREATE INDEX idx_attendance_date ON attendance_days(report_date);

-- Enable Row Level Security (but allow all authenticated reads/writes for internal tool)
ALTER TABLE app_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE attendance_days ENABLE ROW LEVEL SECURITY;

-- Policies: allow all operations (internal tool, no public access needed)
CREATE POLICY "Allow all on app_users" ON app_users FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "Allow all on attendance_days" ON attendance_days FOR ALL USING (true) WITH CHECK (true);
