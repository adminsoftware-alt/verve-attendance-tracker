import { useState, useEffect, useCallback, useRef } from 'react';
import { fetchTeams, fetchTeamDetail, createTeam, updateTeam, deleteTeam, addTeamMember, removeTeamMember, fetchParticipants, bulkImportTeams, fetchTeamsHolidaysSummary } from '../utils/zoomApi';

export default function Teams({ user }) {
  const [teams, setTeams] = useState([]);
  const [allTeams, setAllTeams] = useState([]);
  const [participants, setParticipants] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const [editingTeam, setEditingTeam] = useState(null);
  const isManager = user?.role === 'manager';
  const isAdmin = !user?.role || user?.role === 'admin' || user?.role === 'hr';
  // Maps team_id -> { members: [...], loaded: true }
  const [teamDetails, setTeamDetails] = useState({});
  const [expandedTeam, setExpandedTeam] = useState(null);
  const [addingMemberTo, setAddingMemberTo] = useState(null);
  const [searchMember, setSearchMember] = useState('');
  const [addingInProgress, setAddingInProgress] = useState(false);
  const [uploadResult, setUploadResult] = useState(null);
  const fileInputRef = useRef(null);
  const [holidaysSummary, setHolidaysSummary] = useState({});
  const [selectedMonth, setSelectedMonth] = useState(() => {
    const now = new Date();
    return { year: now.getFullYear(), month: now.getMonth() + 1 };
  });

  // Form state
  const [formName, setFormName] = useState('');
  const [formManager, setFormManager] = useState('');
  const [formManagerEmail, setFormManagerEmail] = useState('');

  const loadTeams = useCallback(async () => {
    try {
      const teamsData = await fetchTeams();
      const all = teamsData.teams || [];
      setAllTeams(all);
      // Manager sees only their teams
      if (isManager && user?.name) {
        setTeams(all.filter(t =>
          (t.manager_name || '').toLowerCase().trim() === user.name.toLowerCase().trim()
          || (t.manager_email || '').toLowerCase().trim() === (user.email || '').toLowerCase().trim()
        ));
      } else {
        setTeams(all);
      }
    } catch (e) {
      setError(e.message);
    }
  }, [isManager, user?.name, user?.email]);

  const loadParticipants = useCallback(async () => {
    try {
      const partData = await fetchParticipants();
      setParticipants(partData.participants || []);
    } catch (e) {
      console.error('Failed to load participants:', e);
    }
  }, []);

  const loadHolidaysSummary = useCallback(async () => {
    try {
      const data = await fetchTeamsHolidaysSummary(selectedMonth.year, selectedMonth.month);
      setHolidaysSummary(data.summary || {});
    } catch (e) {
      console.error('Failed to load holidays summary:', e);
    }
  }, [selectedMonth.year, selectedMonth.month]);

  const loadTeamMembers = useCallback(async (teamId) => {
    try {
      const data = await fetchTeamDetail(teamId);
      setTeamDetails(prev => ({
        ...prev,
        [teamId]: { members: data.team?.members || [], loaded: true }
      }));
    } catch (e) {
      console.error('Failed to load team members:', e);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    Promise.all([loadTeams(), loadParticipants(), loadHolidaysSummary()]).finally(() => setLoading(false));
  }, [loadTeams, loadParticipants, loadHolidaysSummary]);

  const handleCreate = async () => {
    if (!formName.trim()) return;
    try {
      await createTeam(formName, formManager, formManagerEmail);
      setShowCreate(false);
      setFormName(''); setFormManager(''); setFormManagerEmail('');
      loadTeams();
    } catch (e) { setError(e.message); }
  };

  const handleUpdate = async () => {
    if (!editingTeam || !formName.trim()) return;
    try {
      await updateTeam(editingTeam.team_id, formName, formManager, formManagerEmail);
      setEditingTeam(null);
      setFormName(''); setFormManager(''); setFormManagerEmail('');
      loadTeams();
    } catch (e) { setError(e.message); }
  };

  const handleDelete = async (teamId) => {
    if (!window.confirm('Delete this team and all its members?')) return;
    try {
      await deleteTeam(teamId);
      setTeamDetails(prev => { const n = { ...prev }; delete n[teamId]; return n; });
      if (expandedTeam === teamId) setExpandedTeam(null);
      if (addingMemberTo === teamId) setAddingMemberTo(null);
      loadTeams();
    } catch (e) { setError(e.message); }
  };

  const handleAddMember = async (teamId, name, email) => {
    setAddingInProgress(true);
    try {
      await addTeamMember(teamId, name, email);
      // Reload this team's members and team list (for count)
      await Promise.all([loadTeamMembers(teamId), loadTeams()]);
    } catch (e) { setError(e.message); }
    setAddingInProgress(false);
  };

  const handleRemoveMember = async (teamId, memberId) => {
    try {
      await removeTeamMember(teamId, memberId);
      await Promise.all([loadTeamMembers(teamId), loadTeams()]);
    } catch (e) { setError(e.message); }
  };

  // CSV upload: parse CSV, auto-create teams, add members
  const handleCsvUpload = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploadResult(null);
    setError(null);

    try {
      const text = await file.text();
      const lines = text.split('\n').map(l => l.trim()).filter(l => l);
      if (lines.length < 2) throw new Error('CSV must have header + at least 1 row');

      // Parse header (flexible: Name/name, Email/email, Team/team_name)
      const header = lines[0].split(',').map(h => h.trim().toLowerCase().replace(/['"]/g, ''));
      const nameIdx = header.findIndex(h => h === 'name' || h === 'participant_name' || h === 'employee');
      const emailIdx = header.findIndex(h => h === 'email' || h === 'participant_email');
      const teamIdx = header.findIndex(h => h === 'team' || h === 'team_name');
      const managerIdx = header.findIndex(h => h === 'manager' || h === 'manager_name');

      if (nameIdx === -1) throw new Error('CSV must have a "Name" column');
      if (teamIdx === -1) throw new Error('CSV must have a "Team" column');

      const members = [];
      for (let i = 1; i < lines.length; i++) {
        // Handle quoted CSV fields
        const cols = lines[i].match(/(".*?"|[^",]+)(?=\s*,|\s*$)/g)?.map(c => c.trim().replace(/^"|"$/g, '')) || lines[i].split(',').map(c => c.trim());
        const name = cols[nameIdx]?.trim();
        if (!name) continue;
        members.push({
          name,
          email: emailIdx >= 0 ? (cols[emailIdx] || '').trim() : '',
          team_name: (cols[teamIdx] || '').trim(),
          manager_name: managerIdx >= 0 ? (cols[managerIdx] || '').trim() : ''
        });
      }

      if (members.length === 0) throw new Error('No valid rows found');

      const result = await bulkImportTeams(members);
      setUploadResult({
        teams_created: result.teams_created,
        members_added: result.members_added,
        members_skipped: result.members_skipped,
        total: members.length
      });
      loadTeams();
    } catch (err) {
      setError('CSV upload error: ' + err.message);
    }
    // Reset file input
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const openEdit = (team) => {
    setEditingTeam(team);
    setFormName(team.team_name);
    setFormManager(team.manager_name || '');
    setFormManagerEmail(team.manager_email || '');
    setShowCreate(false);
  };

  const openCreate = () => {
    setShowCreate(true);
    setEditingTeam(null);
    setFormName(''); setFormManager(''); setFormManagerEmail('');
  };

  const toggleExpand = (teamId) => {
    if (expandedTeam === teamId) {
      setExpandedTeam(null);
    } else {
      setExpandedTeam(teamId);
      if (!teamDetails[teamId]?.loaded) loadTeamMembers(teamId);
    }
  };

  const toggleAddMember = (teamId) => {
    if (addingMemberTo === teamId) {
      setAddingMemberTo(null);
      setSearchMember('');
    } else {
      setAddingMemberTo(teamId);
      setSearchMember('');
      // Also expand to show members
      if (expandedTeam !== teamId) {
        setExpandedTeam(teamId);
        if (!teamDetails[teamId]?.loaded) loadTeamMembers(teamId);
      }
      // Load participants if not loaded
      if (participants.length === 0) loadParticipants();
    }
  };

  // Filter participants: exclude already-in-team members, apply search
  const getFilteredParticipants = (teamId) => {
    const members = teamDetails[teamId]?.members || [];
    const memberNames = new Set(members.map(m => (m.participant_name || '').toLowerCase().trim()));

    return participants.filter(p => {
      const name = (p.participant_name || '').toLowerCase().trim();
      const email = (p.participant_email || '').toLowerCase();
      // Exclude already-in-team
      if (memberNames.has(name)) return false;
      // Apply search
      if (searchMember) {
        const q = searchMember.toLowerCase();
        return name.includes(q) || email.includes(q);
      }
      return true;
    });
  };

  if (loading && teams.length === 0) {
    return <div style={s.loader}>Loading teams...</div>;
  }

  return (
    <div>
      <div style={s.header}>
        <div>
          <h2 style={s.title}>Teams</h2>
          {isManager && <div style={{ fontSize: 11, color: '#64748b', marginTop: 2 }}>Showing your teams only</div>}
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          {isAdmin && (
            <>
              <input type="file" accept=".csv" ref={fileInputRef} onChange={handleCsvUpload}
                style={{ display: 'none' }} />
              <button onClick={() => fileInputRef.current?.click()} style={s.uploadBtn}>Upload CSV</button>
              <button onClick={openCreate} style={s.createBtn}>+ Create Team</button>
            </>
          )}
        </div>
      </div>

      {/* Upload result */}
      {uploadResult && (
        <div style={s.uploadResult}>
          CSV imported: {uploadResult.teams_created} teams created, {uploadResult.members_added} members added
          {uploadResult.members_skipped > 0 && `, ${uploadResult.members_skipped} duplicates skipped`}
          <button onClick={() => setUploadResult(null)} style={{ marginLeft: 10, background: 'none', border: 'none', color: '#15803d', cursor: 'pointer', fontWeight: 600 }}>OK</button>
        </div>
      )}

      {/* CSV format hint */}
      {isAdmin && teams.length === 0 && !loading && (
        <div style={{ background: '#f8fafc', border: '1px solid #e5e7eb', borderRadius: 10, padding: 16, marginBottom: 16, fontSize: 12, color: '#64748b' }}>
          <strong>Upload CSV format:</strong> Name, Email, Team, Manager (optional)
          <br />Example: <code>Shashank Channawar, shashank@verve.com, TEAM AARON, HARSH</code>
        </div>
      )}

      {error && (
        <div style={s.error}>
          {error}
          <button onClick={() => setError(null)} style={{ marginLeft: 10, background: 'none', border: 'none', color: '#dc2626', cursor: 'pointer', fontWeight: 600 }}>Dismiss</button>
        </div>
      )}

      {/* Create/Edit Form */}
      {(showCreate || editingTeam) && (
        <div style={s.formCard}>
          <h3 style={s.formTitle}>{editingTeam ? 'Edit Team' : 'Create Team'}</h3>
          <div style={s.formRow}>
            <input value={formName} onChange={e => setFormName(e.target.value)} placeholder="Team name *" style={s.input} />
          </div>
          <div style={s.formRow}>
            <input value={formManager} onChange={e => setFormManager(e.target.value)} placeholder="Manager name" style={s.input} />
            <input value={formManagerEmail} onChange={e => setFormManagerEmail(e.target.value)} placeholder="Manager email" style={s.input} />
          </div>
          <div style={s.formActions}>
            <button onClick={() => { setShowCreate(false); setEditingTeam(null); }} style={s.cancelBtn}>Cancel</button>
            <button onClick={editingTeam ? handleUpdate : handleCreate} style={s.saveBtn}>
              {editingTeam ? 'Update' : 'Create'}
            </button>
          </div>
        </div>
      )}

      {/* Month selector for holidays */}
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 16, background: '#f8fafc', padding: '10px 14px', borderRadius: 10, border: '1px solid #e5e7eb' }}>
        <span style={{ fontSize: 12, color: '#64748b', fontWeight: 500 }}>Holidays for:</span>
        <select
          value={selectedMonth.year}
          onChange={e => setSelectedMonth(m => ({ ...m, year: +e.target.value }))}
          style={{ padding: '6px 10px', border: '1px solid #d1d5db', borderRadius: 6, fontSize: 13 }}
        >
          {[2024, 2025, 2026].map(y => <option key={y} value={y}>{y}</option>)}
        </select>
        <select
          value={selectedMonth.month}
          onChange={e => setSelectedMonth(m => ({ ...m, month: +e.target.value }))}
          style={{ padding: '6px 10px', border: '1px solid #d1d5db', borderRadius: 6, fontSize: 13 }}
        >
          {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'].map((m, i) => (
            <option key={i} value={i + 1}>{m}</option>
          ))}
        </select>
        <span style={{ fontSize: 11, color: '#94a3b8', marginLeft: 8 }}>
          Teams with holidays configured show a badge
        </span>
      </div>

      {/* Teams Grid */}
      <div style={s.grid}>
        {teams.map(team => {
          const members = teamDetails[team.team_id]?.members || [];
          const isExpanded = expandedTeam === team.team_id;
          const isAdding = addingMemberTo === team.team_id;
          const filteredParts = isAdding ? getFilteredParticipants(team.team_id) : [];
          const holidayCount = holidaysSummary[team.team_id]?.holiday_count || 0;

          return (
            <div key={team.team_id} style={s.card}>
              <div style={s.cardHeader}>
                <div>
                  <div style={s.teamName}>{team.team_name}</div>
                  {team.manager_name && <div style={s.manager}>Manager: {team.manager_name}</div>}
                </div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  {holidayCount > 0 && (
                    <div style={s.holidayBadge} title={`${holidayCount} holidays configured`}>
                      {holidayCount} H
                    </div>
                  )}
                  <div style={s.memberCount}>{team.member_count || 0}</div>
                </div>
              </div>

              <div style={s.cardActions}>
                <button onClick={() => toggleExpand(team.team_id)} style={{ ...s.actionBtn, ...(isExpanded ? s.actionBtnActive : {}) }}>
                  {isExpanded ? 'Hide' : 'Members'}
                </button>
                <button onClick={() => toggleAddMember(team.team_id)} style={{ ...s.actionBtn, ...(isAdding ? s.actionBtnAdd : {}) }}>
                  + Add
                </button>
                {isAdmin && <button onClick={() => openEdit(team)} style={s.actionBtn}>Edit</button>}
                {isAdmin && <button onClick={() => handleDelete(team.team_id)} style={{ ...s.actionBtn, color: '#dc2626' }}>Delete</button>}
              </div>

              {/* Add Member Panel */}
              {isAdding && (
                <div style={s.addPanel}>
                  <div style={s.addPanelHeader}>
                    <span style={{ fontSize: 13, fontWeight: 600, color: '#1e293b' }}>Add Members</span>
                    <span style={{ fontSize: 11, color: '#64748b' }}>{filteredParts.length} available</span>
                  </div>
                  <input
                    value={searchMember}
                    onChange={e => setSearchMember(e.target.value)}
                    placeholder="Search by name or email..."
                    style={{ ...s.input, marginBottom: 8, fontSize: 13 }}
                    autoFocus
                  />
                  <div style={s.participantList}>
                    {filteredParts.map((p, i) => (
                      <div key={i} style={s.participantItem}>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ fontWeight: 500, fontSize: 13 }}>{p.participant_name}</div>
                          {p.participant_email && <div style={{ fontSize: 11, color: '#64748b', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.participant_email}</div>}
                        </div>
                        <button
                          onClick={() => handleAddMember(team.team_id, p.participant_name, p.participant_email || '')}
                          disabled={addingInProgress}
                          style={{ ...s.addMemberBtn, opacity: addingInProgress ? 0.5 : 1 }}
                        >
                          {addingInProgress ? '...' : 'Add'}
                        </button>
                      </div>
                    ))}
                    {filteredParts.length === 0 && (
                      <div style={{ color: '#94a3b8', fontSize: 12, padding: 12, textAlign: 'center' }}>
                        {searchMember ? 'No matching participants' : 'All participants already added'}
                      </div>
                    )}
                  </div>
                </div>
              )}

              {/* Members List */}
              {isExpanded && (
                <div style={s.membersList}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                    <span style={{ fontSize: 12, fontWeight: 600, color: '#64748b', textTransform: 'uppercase', letterSpacing: 0.5 }}>
                      Members ({members.length})
                    </span>
                  </div>
                  {members.length === 0 ? (
                    <div style={{ color: '#94a3b8', fontSize: 13, padding: '12px 0', textAlign: 'center' }}>
                      No members yet. Click "+ Add" to add members.
                    </div>
                  ) : (
                    members.map(m => (
                      <div key={m.member_id} style={s.memberItem}>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ fontWeight: 500, fontSize: 13 }}>{m.participant_name}</div>
                          {m.participant_email && <div style={{ fontSize: 11, color: '#64748b' }}>{m.participant_email}</div>}
                        </div>
                        <button onClick={() => handleRemoveMember(team.team_id, m.member_id)} style={s.removeBtn}>Remove</button>
                      </div>
                    ))
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {teams.length === 0 && !loading && (
        <div style={s.empty}>No teams yet. Create your first team!</div>
      )}
    </div>
  );
}

const s = {
  header: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 },
  title: { fontSize: 22, fontWeight: 800, color: '#0f172a', margin: 0 },
  createBtn: { padding: '10px 20px', background: '#0f172a', color: '#fff', border: 'none', borderRadius: 10, fontSize: 13, fontWeight: 600, cursor: 'pointer' },
  uploadBtn: { padding: '10px 20px', background: '#10b981', color: '#fff', border: 'none', borderRadius: 10, fontSize: 13, fontWeight: 600, cursor: 'pointer' },
  uploadResult: { padding: '10px 14px', background: '#f0fdf4', color: '#15803d', border: '1px solid #bbf7d0', borderRadius: 10, fontSize: 13, marginBottom: 16, display: 'flex', alignItems: 'center' },
  error: { padding: '10px 14px', background: '#fef2f2', color: '#dc2626', border: '1px solid #fecaca', borderRadius: 10, fontSize: 13, marginBottom: 16, display: 'flex', alignItems: 'center' },
  loader: { display: 'flex', alignItems: 'center', justifyContent: 'center', height: '50vh', color: '#94a3b8' },
  empty: { textAlign: 'center', padding: '60px 20px', color: '#94a3b8', fontSize: 14 },

  formCard: { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 14, padding: 20, marginBottom: 20 },
  formTitle: { margin: '0 0 16px', fontSize: 16, fontWeight: 700, color: '#1e293b' },
  formRow: { display: 'flex', gap: 12, marginBottom: 12 },
  input: { flex: 1, padding: '10px 14px', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 14, outline: 'none' },
  formActions: { display: 'flex', gap: 10, justifyContent: 'flex-end', marginTop: 16 },
  cancelBtn: { padding: '8px 16px', background: '#f1f5f9', color: '#475569', border: 'none', borderRadius: 8, fontSize: 13, cursor: 'pointer' },
  saveBtn: { padding: '8px 20px', background: '#0f172a', color: '#fff', border: 'none', borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: 'pointer' },

  grid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(380px, 1fr))', gap: 16 },
  card: { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 14, overflow: 'hidden' },
  cardHeader: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', padding: '18px 20px 12px' },
  teamName: { fontSize: 16, fontWeight: 700, color: '#1e293b' },
  manager: { fontSize: 12, color: '#64748b', marginTop: 4 },
  memberCount: { fontSize: 24, fontWeight: 800, color: '#3b82f6', background: '#eff6ff', width: 48, height: 48, borderRadius: 12, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 },
  holidayBadge: { fontSize: 12, fontWeight: 700, color: '#7c3aed', background: '#ede9fe', padding: '6px 10px', borderRadius: 8, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 },

  cardActions: { display: 'flex', gap: 8, padding: '0 20px 14px' },
  actionBtn: { padding: '6px 12px', background: '#f8fafc', color: '#475569', border: '1px solid #e5e7eb', borderRadius: 6, fontSize: 12, cursor: 'pointer', transition: 'all 0.15s' },
  actionBtnActive: { background: '#0f172a', color: '#fff', borderColor: '#0f172a' },
  actionBtnAdd: { background: '#10b981', color: '#fff', borderColor: '#10b981' },

  addPanel: { padding: 16, background: '#f0fdf4', borderTop: '1px solid #bbf7d0', borderBottom: '1px solid #bbf7d0' },
  addPanelHeader: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 },
  participantList: { maxHeight: 300, overflowY: 'auto', border: '1px solid #e5e7eb', borderRadius: 8, background: '#fff' },
  participantItem: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 12px', borderBottom: '1px solid #f1f5f9' },
  addMemberBtn: { padding: '5px 14px', background: '#10b981', color: '#fff', border: 'none', borderRadius: 6, fontSize: 12, fontWeight: 600, cursor: 'pointer', flexShrink: 0 },

  membersList: { padding: '12px 20px 16px', borderTop: '1px solid #f1f5f9' },
  memberItem: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '10px 12px', borderRadius: 8, background: '#f8fafc', marginBottom: 6 },
  removeBtn: { padding: '4px 10px', background: '#fef2f2', color: '#dc2626', border: '1px solid #fecaca', borderRadius: 6, fontSize: 11, cursor: 'pointer', flexShrink: 0 },
};
