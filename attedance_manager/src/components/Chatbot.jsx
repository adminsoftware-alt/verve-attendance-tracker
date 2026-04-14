import { useState, useRef, useEffect, useCallback } from 'react';
import { sendChatPrompt } from '../utils/zoomApi';

/**
 * Floating chatbot panel.
 * - Click the bubble (bottom-right) to open the chat panel.
 * - Type a prompt; LLM dispatches to the right intent (lookup, export, edit).
 * - For edit intents, a confirm card appears — click Confirm to apply.
 * - Download links open the matching CSV/JSON in a new tab.
 */
export default function Chatbot({ user }) {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState([
    {
      role: 'bot',
      message:
        "Hi! I can fetch attendance, export CSVs, and (for admins) apply overrides.\n\n" +
        "Try:\n" +
        "• show Shashank's attendance\n" +
        "• download team Accurest April report\n" +
        "• mark Shashank present on 2026-04-12",
    },
  ]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 60);
  }, [open]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 999999, behavior: 'smooth' });
  }, [messages]);

  const send = useCallback(async (prompt, confirmToken = null) => {
    if (busy) return;
    let nextMessages = messages;
    if (prompt) {
      nextMessages = [...messages, { role: 'user', message: prompt }];
      setMessages(nextMessages);
    }
    setBusy(true);
    try {
      // Send the last 8 turns as context so general_chat can keep a thread.
      const history = nextMessages
        .slice(-8)
        .map(m => ({ role: m.role, message: m.message || '' }))
        .filter(m => m.message);
      const res = await sendChatPrompt({
        prompt: prompt || '',
        user: user?.name || user?.username || '',
        role: user?.role || '',
        confirmToken,
        history,
      });
      setMessages(prev => [...prev, { role: 'bot', ...res }]);
    } catch (e) {
      setMessages(prev => [...prev, { role: 'bot', success: false, message: `Network error: ${e.message}` }]);
    }
    setBusy(false);
  }, [busy, user, messages]);

  const onSubmit = (e) => {
    e.preventDefault();
    const text = input.trim();
    if (!text || busy) return;
    setInput('');
    send(text);
  };

  const onConfirm = (token) => {
    send('', token);
  };

  const onCancel = (idx) => {
    setMessages(prev => prev.map((m, i) =>
      i === idx ? { ...m, confirm_required: false, message: m.message + '\n\n_Cancelled._' } : m
    ));
  };

  return (
    <>
      {/* Floating bubble */}
      {!open && (
        <button onClick={() => setOpen(true)} style={s.bubble} title="Ask the assistant">
          <span style={{ fontSize: 22 }}>💬</span>
        </button>
      )}

      {open && (
        <div style={s.panel}>
          <div style={s.header}>
            <div>
              <div style={s.headerTitle}>Attendance Assistant</div>
              <div style={s.headerSub}>Ask, export, edit — natural language.</div>
            </div>
            <button onClick={() => setOpen(false)} style={s.headerClose} title="Close">×</button>
          </div>

          <div ref={scrollRef} style={s.body}>
            {messages.map((m, i) => (
              <ChatMessage key={i} m={m} idx={i} onConfirm={onConfirm} onCancel={onCancel} />
            ))}
            {busy && (
              <div style={{ ...s.msgRow, justifyContent: 'flex-start' }}>
                <div style={{ ...s.msgBot, opacity: 0.7 }}>
                  <span style={s.dot} /><span style={{ ...s.dot, animationDelay: '0.15s' }} /><span style={{ ...s.dot, animationDelay: '0.3s' }} />
                </div>
              </div>
            )}
          </div>

          <form onSubmit={onSubmit} style={s.composer}>
            <input
              ref={inputRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              placeholder='e.g. "show Shashank attendance"'
              style={s.composerInput}
              disabled={busy}
            />
            <button type="submit" disabled={busy || !input.trim()} style={s.sendBtn}>
              {busy ? '…' : 'Send'}
            </button>
          </form>

          {/* Local keyframes for typing dots */}
          <style>{`
            @keyframes chatDot {
              0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
              40% { transform: scale(1); opacity: 1; }
            }
          `}</style>
        </div>
      )}
    </>
  );
}

// Tiny markdown: **bold** + line breaks. Avoids pulling in a full lib.
function renderRich(text) {
  if (!text) return null;
  const lines = String(text).split('\n');
  const elems = [];
  lines.forEach((line, li) => {
    const parts = line.split(/(\*\*[^*]+\*\*)/g);
    elems.push(
      <span key={li}>
        {parts.map((p, pi) => {
          if (p.startsWith('**') && p.endsWith('**')) {
            return <strong key={pi}>{p.slice(2, -2)}</strong>;
          }
          return <span key={pi}>{p}</span>;
        })}
        {li < lines.length - 1 ? <br /> : null}
      </span>
    );
  });
  return elems;
}

function ChatTable({ table }) {
  if (!table || !table.rows?.length) return null;
  return (
    <div style={s.tableWrap}>
      {table.title && <div style={s.tableTitle}>{table.title}</div>}
      <div style={{ overflow: 'auto', maxHeight: 240 }}>
        <table style={s.dataTable}>
          <thead>
            <tr>
              {(table.columns || []).map((c, i) => (
                <th key={i} style={s.th}>{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((row, ri) => (
              <tr key={ri} style={ri % 2 === 0 ? { background: '#fafbfc' } : {}}>
                {row.map((cell, ci) => (
                  <td key={ci} style={s.td}>{String(cell)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ChatMessage({ m, idx, onConfirm, onCancel }) {
  const isUser = m.role === 'user';
  const bubble = isUser ? s.msgUser : s.msgBot;

  return (
    <div style={{ ...s.msgRow, justifyContent: isUser ? 'flex-end' : 'flex-start' }}>
      <div style={bubble}>
        <div style={s.msgText}>{renderRich(m.message)}</div>

        {/* Inline data table (employee daily, team summary, attendance for date) */}
        <ChatTable table={m.table} />

        {/* Edit confirm card */}
        {m.confirm_required && m.confirm_token && (
          <div style={s.confirmCard}>
            <div style={{ fontSize: 12, marginBottom: 8, color: '#92400e' }}>
              ⚠ This will modify attendance data. Review before confirming.
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              <button onClick={() => onConfirm(m.confirm_token)} style={s.btnConfirm}>
                Confirm
              </button>
              <button onClick={() => onCancel(idx)} style={s.btnCancel}>
                Cancel
              </button>
            </div>
          </div>
        )}

        {/* Download link */}
        {m.download_url && (
          <a
            href={m.download_url}
            download={m.filename || true}
            target="_blank"
            rel="noreferrer"
            style={s.downloadBtn}
          >
            ⬇ Download {m.filename ? `· ${m.filename}` : 'CSV'}
          </a>
        )}

        {/* Inline data link */}
        {m.data?.detail_url && !m.download_url && !m.table && (
          <a href={m.data.detail_url} target="_blank" rel="noreferrer" style={s.linkBtn}>
            Open data →
          </a>
        )}
      </div>
    </div>
  );
}

const s = {
  bubble: {
    position: 'fixed', bottom: 24, right: 24, zIndex: 999,
    width: 56, height: 56, borderRadius: '50%',
    background: 'linear-gradient(135deg,#0f172a 0%,#1e3a8a 100%)',
    color: '#fff', border: 'none', cursor: 'pointer',
    boxShadow: '0 8px 24px rgba(15,23,42,0.35)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    transition: 'transform 0.18s ease',
  },

  panel: {
    position: 'fixed', bottom: 24, right: 24, zIndex: 999,
    width: 380, maxWidth: 'calc(100vw - 48px)',
    height: 560, maxHeight: 'calc(100vh - 48px)',
    background: '#fff', borderRadius: 16, overflow: 'hidden',
    display: 'flex', flexDirection: 'column',
    boxShadow: '0 20px 50px rgba(15,23,42,0.28)',
    border: '1px solid #e2e8f0',
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
  },

  header: {
    background: 'linear-gradient(135deg,#0f172a 0%,#1e3a8a 100%)',
    color: '#fff', padding: '14px 16px',
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
  },
  headerTitle: { fontSize: 15, fontWeight: 700, letterSpacing: '-0.01em' },
  headerSub: { fontSize: 11, color: 'rgba(255,255,255,0.7)', marginTop: 2 },
  headerClose: {
    background: 'rgba(255,255,255,0.12)', color: '#fff',
    border: 'none', borderRadius: 8, width: 28, height: 28,
    fontSize: 18, cursor: 'pointer', lineHeight: 1,
  },

  body: { flex: 1, overflowY: 'auto', padding: '14px 14px 4px', background: '#f8fafc' },

  msgRow: { display: 'flex', marginBottom: 10 },
  msgUser: {
    background: '#0f172a', color: '#fff', padding: '10px 14px',
    borderRadius: '14px 14px 4px 14px', maxWidth: '85%', fontSize: 13, lineHeight: 1.45,
  },
  msgBot: {
    background: '#fff', color: '#0f172a', padding: '10px 14px',
    borderRadius: '14px 14px 14px 4px', maxWidth: '90%', fontSize: 13, lineHeight: 1.5,
    border: '1px solid #e2e8f0',
  },
  msgText: { whiteSpace: 'pre-wrap', wordBreak: 'break-word' },

  confirmCard: {
    marginTop: 10, padding: 10, background: '#fff7ed',
    border: '1px solid #fed7aa', borderRadius: 8,
  },
  btnConfirm: {
    padding: '7px 14px', background: '#10b981', color: '#fff',
    border: 'none', borderRadius: 6, fontSize: 12, fontWeight: 700,
    cursor: 'pointer', flex: 1,
  },
  btnCancel: {
    padding: '7px 14px', background: '#fff', color: '#475569',
    border: '1px solid #e2e8f0', borderRadius: 6, fontSize: 12, fontWeight: 600,
    cursor: 'pointer',
  },

  downloadBtn: {
    display: 'inline-block', marginTop: 8, padding: '7px 12px',
    background: '#10b981', color: '#fff', borderRadius: 8,
    fontSize: 12, fontWeight: 600, textDecoration: 'none',
  },
  linkBtn: {
    display: 'inline-block', marginTop: 8, fontSize: 12,
    color: '#1d4ed8', textDecoration: 'underline', textUnderlineOffset: 2,
  },

  composer: {
    display: 'flex', gap: 8, padding: 12, background: '#fff',
    borderTop: '1px solid #e2e8f0',
  },
  composerInput: {
    flex: 1, padding: '10px 12px', border: '1px solid #e2e8f0',
    borderRadius: 10, fontSize: 13, outline: 'none', background: '#f8fafc',
  },
  sendBtn: {
    padding: '10px 16px', background: '#0f172a', color: '#fff',
    border: 'none', borderRadius: 10, fontSize: 13, fontWeight: 600,
    cursor: 'pointer', whiteSpace: 'nowrap',
  },

  dot: {
    display: 'inline-block', width: 6, height: 6, borderRadius: '50%',
    background: '#94a3b8', margin: '0 2px',
    animation: 'chatDot 1s infinite ease-in-out',
  },

  // Inline data table inside a bot bubble
  tableWrap: { marginTop: 10, border: '1px solid #e2e8f0', borderRadius: 8, background: '#fff' },
  tableTitle: {
    padding: '6px 10px', fontSize: 10, fontWeight: 700, color: '#64748b',
    textTransform: 'uppercase', letterSpacing: '0.05em',
    borderBottom: '1px solid #e2e8f0', background: '#f8fafc',
  },
  dataTable: { borderCollapse: 'collapse', width: '100%', fontSize: 11 },
  th: {
    padding: '6px 8px', textAlign: 'left', background: '#f8fafc',
    color: '#475569', fontWeight: 600, borderBottom: '1px solid #e2e8f0',
    whiteSpace: 'nowrap',
  },
  td: { padding: '5px 8px', borderBottom: '1px solid #f1f5f9', color: '#0f172a' },
};
