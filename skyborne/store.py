"""SQLite storage: every hook event as it arrived, plus what the transcripts and status line said.

Each process has one writer (the server's Writer thread, or `skyborne import`); readers open their own
connections. SQLite's lock lets the two processes take turns.
The database runs in WAL mode, so reads never wait for a write to finish.
"""
import json
import pathlib
import sqlite3
import time

TRUNCATE_AT = 20 * 1024        # tool output kept per field, in bytes of JSON
TRUNCATED_FIELDS = ('tool_response', 'error')
KEPT_WHEN_CUT = ('agentId', 'resolvedModel', 'status', 'isAsync')  # an Agent call's result: what links it to its helper

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  received_at INTEGER NOT NULL,
  session_id TEXT,
  event TEXT,
  agent_id TEXT,
  tool_use_id TEXT,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_session ON events (session_id, id);
CREATE INDEX IF NOT EXISTS events_time ON events (received_at);
CREATE INDEX IF NOT EXISTS events_call ON events (session_id, tool_use_id) WHERE tool_use_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS usage (
  session_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  agent_id TEXT,
  model TEXT,
  in_tokens INTEGER, out_tokens INTEGER, cw_tokens INTEGER, cr_tokens INTEGER,
  final INTEGER,
  ts INTEGER,
  PRIMARY KEY (session_id, message_id)
);
CREATE TABLE IF NOT EXISTS statusline (
  session_id TEXT PRIMARY KEY,
  received_at INTEGER NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS names (
  session_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS titles (
  session_id TEXT PRIMARY KEY,
  custom TEXT,
  ai TEXT,
  at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
  session_id TEXT NOT NULL,
  tool_use_id TEXT NOT NULL,
  is_error INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  PRIMARY KEY (session_id, tool_use_id)
);
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  agent_id TEXT,
  tool_name TEXT,
  request TEXT NOT NULL,
  asked_at INTEGER NOT NULL,
  answered_at INTEGER NOT NULL,
  decision TEXT NOT NULL,
  answered_in TEXT NOT NULL,
  how TEXT NOT NULL,
  timed_out INTEGER NOT NULL DEFAULT 0,
  waited_ms INTEGER NOT NULL,
  confirmed INTEGER
);
CREATE INDEX IF NOT EXISTS decisions_time ON decisions (answered_at);
CREATE TABLE IF NOT EXISTS imports (
  session_id TEXT PRIMARY KEY,
  imported_at INTEGER NOT NULL,
  path TEXT,
  events INTEGER,
  messages INTEGER
);
"""


def truncate(payload: dict) -> dict:
    """Tool output beyond TRUNCATE_AT is cut, with a marker that says how big it was."""
    out = dict(payload)
    for key in TRUNCATED_FIELDS:
        if key not in out:
            continue
        text = out[key] if isinstance(out[key], str) else json.dumps(out[key], ensure_ascii=False)
        size = len(text.encode('utf-8'))
        if size > TRUNCATE_AT:
            head = text.encode('utf-8')[:TRUNCATE_AT].decode('utf-8', 'ignore')
            cut = {'skyborneTruncated': True, 'bytes': size, 'head': head}
            if isinstance(out[key], dict):
                cut.update({k: out[key][k] for k in KEPT_WHEN_CUT if isinstance(out[key].get(k), (str, bool, int, float))})
            out[key] = cut
    return out


def _str(v):
    return v if isinstance(v, str) else None


class Store:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        # the database holds prompts and tool output: a folder Skyborne creates is private to its owner
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = self.connect()
        self.db.executescript(SCHEMA)
        self._migrate()

    def connect(self):
        db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=NORMAL')
        return db

    def _migrate(self):
        """Columns added after the first release. Another process may add one at the same moment."""
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(events)')}
        if 'source' not in columns:
            try:
                self.db.execute("ALTER TABLE events ADD COLUMN source TEXT NOT NULL DEFAULT 'hook'")
                self.db.commit()
            except sqlite3.OperationalError as e:
                if 'duplicate column' not in str(e).lower():
                    raise

    # ---- writes (one writer per process: the server's Writer thread, or `skyborne import`) ----
    def add_event(self, received_at: int, payload: dict, source: str = 'hook') -> dict:
        """Store one hook event (or one rebuilt from a past transcript); returns it as the reducer takes it."""
        p = truncate(payload)
        self.db.execute(
            'INSERT INTO events (received_at, session_id, event, agent_id, tool_use_id, payload, source) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (received_at, _str(p.get('session_id')), _str(p.get('hook_event_name')), _str(p.get('agent_id')),
             _str(p.get('tool_use_id')), json.dumps(p, ensure_ascii=False), source))
        return {'ts': received_at, 'payload': p, 'source': source}

    def set_title(self, session_id: str, title: str, custom: bool, at: int):
        """A session's name from its transcript: the custom one (`/rename`) and Claude's own title are kept
        side by side. `at` moves only when one of them changes, so re-reading a file changes nothing."""
        values = (title, None) if custom else (None, title)
        self.db.execute('INSERT INTO titles (session_id, custom, ai, at) VALUES (?, ?, ?, ?) ON CONFLICT (session_id) DO UPDATE SET '
                        'custom = coalesce(excluded.custom, titles.custom), ai = coalesce(excluded.ai, titles.ai), at = excluded.at '
                        'WHERE coalesce(excluded.custom, titles.custom) IS NOT titles.custom OR coalesce(excluded.ai, titles.ai) IS NOT titles.ai',
                        (session_id, *values, at))

    def has_transcript_end(self, session_id: str, ts: int) -> bool:
        """Whether this end, read from a transcript, is already stored (a restart reads the file again)."""
        return self.db.execute("SELECT 1 FROM events WHERE session_id = ? AND event = 'SessionEnd' AND source = 'transcript' "
                               'AND received_at = ? LIMIT 1', (session_id, ts)).fetchone() is not None

    def add_result(self, f: dict):
        """A tool call's error result from the transcript: the only sign of a "No" typed in the terminal."""
        self.db.execute(
            'INSERT INTO results VALUES (?, ?, ?, ?) ON CONFLICT (session_id, tool_use_id) DO UPDATE SET '
            'is_error=excluded.is_error, ts=excluded.ts WHERE excluded.ts < results.ts',
            (f['session_id'], f['tool_use_id'], int(bool(f.get('is_error'))), f['ts']))

    def set_name(self, session_id: str, name, at: int):
        """The name the Mayor gave a district; None removes it."""
        if name:
            self.db.execute('INSERT INTO names VALUES (?, ?, ?) ON CONFLICT (session_id) DO UPDATE SET name=excluded.name, at=excluded.at',
                            (session_id, name, at))
        else:
            self.db.execute('DELETE FROM names WHERE session_id = ?', (session_id,))

    def begin_import(self, session_id: str):
        """Open the write transaction an import goes into, then check the session isn't already here: None to go
        ahead, or why not (the transaction is then closed). Checked inside the transaction, so two imports
        running at once can't both add the same session."""
        self.db.execute('BEGIN IMMEDIATE')
        if self.db.execute("SELECT 1 FROM events WHERE session_id = ? AND source = 'hook' LIMIT 1", (session_id,)).fetchone():
            why = 'recorded live'
        elif self.db.execute('SELECT 1 FROM imports WHERE session_id = ?', (session_id,)).fetchone():
            why = 'imported before'
        else:
            return None
        self.db.rollback()
        return why

    def add_import(self, session_id: str, path: str, events: int, messages: int):
        self.db.execute('INSERT OR REPLACE INTO imports VALUES (?, ?, ?, ?, ?)',
                        (session_id, int(time.time() * 1000), path, events, messages))

    def add_usage(self, f: dict):
        u = f['usage']
        self.db.execute(
            'INSERT INTO usage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (session_id, message_id) DO UPDATE SET '
            'agent_id=excluded.agent_id, model=excluded.model, in_tokens=excluded.in_tokens, out_tokens=excluded.out_tokens, '
            'cw_tokens=excluded.cw_tokens, cr_tokens=excluded.cr_tokens, final=excluded.final, ts=excluded.ts',
            (f['session_id'], f['message_id'], f.get('agent'), f.get('model'), u['in'], u['out'], u['cw'], u['cr'],
             int(bool(f.get('final'))), f['ts']))

    def set_statusline(self, session_id: str, received_at: int, payload: dict):
        self.db.execute(
            'INSERT INTO statusline VALUES (?, ?, ?) ON CONFLICT (session_id) DO UPDATE SET '
            'received_at=excluded.received_at, payload=excluded.payload WHERE excluded.received_at >= statusline.received_at',
            (session_id, received_at, json.dumps(payload, ensure_ascii=False)))

    DECISION_FIELDS = ('id', 'session_id', 'agent_id', 'tool_name', 'request', 'asked_at', 'answered_at', 'decision',
                       'answered_in', 'how', 'timed_out', 'waited_ms', 'confirmed')

    def add_decision(self, row: dict):
        """How one permission request ended (skyborne/approvals.py)."""
        self.db.execute(f'INSERT OR REPLACE INTO decisions ({", ".join(self.DECISION_FIELDS)}) '
                        f'VALUES ({", ".join("?" * len(self.DECISION_FIELDS))})',
                        tuple(json.dumps(row[k], ensure_ascii=False) if k == 'request' else row.get(k) for k in self.DECISION_FIELDS))

    def confirm_decision(self, decision_id: str, confirmed: bool, decision: str, answered_in: str):
        """The transcript showed whether Claude Code applied the page's answer: if not, the terminal answered first."""
        self.db.execute('UPDATE decisions SET confirmed = ?, decision = ?, answered_in = ? WHERE id = ?',
                        (int(confirmed), decision, answered_in, decision_id))

    def commit(self):
        self.db.commit()

    def prune(self, days: int) -> int:
        cutoff = int((time.time() - days * 86400) * 1000)
        n = self.db.execute('DELETE FROM events WHERE received_at < ?', (cutoff,)).rowcount
        self.db.execute('DELETE FROM usage WHERE ts < ?', (cutoff,))
        self.db.execute('DELETE FROM statusline WHERE received_at < ?', (cutoff,))
        self.db.execute('DELETE FROM results WHERE ts < ?', (cutoff,))
        self.db.execute('DELETE FROM titles WHERE at < ?', (cutoff,))
        self.db.execute('DELETE FROM decisions WHERE answered_at < ?', (cutoff,))
        self.db.commit()
        return n

    # ---- reads (any thread, own connection) ----
    def recent_sessions(self, since_ms: int):
        with self.connect() as db:
            return [r[0] for r in db.execute(
                'SELECT DISTINCT session_id FROM events WHERE received_at >= ? AND session_id IS NOT NULL', (since_ms,))]

    def recent_session_ids(self, limit: int):
        """The sessions with the most recent events, newest first."""
        with self.connect() as db:
            return [r[0] for r in db.execute(
                'SELECT session_id, MAX(received_at) AS last FROM events WHERE session_id IS NOT NULL '
                'GROUP BY session_id ORDER BY last DESC, session_id LIMIT ?', (limit,))]

    def last_event_id(self, session_id):
        """The id of a session's newest stored event (it grows with every event), or None if it has none."""
        with self.connect() as db:
            return db.execute('SELECT MAX(id) FROM events WHERE session_id = ?', (session_id,)).fetchone()[0]

    def existing(self, session_ids):
        """Which of these sessions still have events (after a prune)."""
        if not session_ids:
            return set()
        marks = ','.join('?' * len(session_ids))
        with self.connect() as db:
            return {r[0] for r in db.execute(f'SELECT DISTINCT session_id FROM events WHERE session_id IN ({marks})', list(session_ids))}

    def names(self):
        with self.connect() as db:
            return dict(db.execute('SELECT session_id, name FROM names'))

    def imports_after(self, rowid: int):
        """Sessions imported since `rowid` (a watermark, so a slow commit is never skipped): ([sid], new mark)."""
        with self.connect() as db:
            rows = db.execute('SELECT rowid, session_id FROM imports WHERE rowid > ? ORDER BY rowid', (rowid,)).fetchall()
        return [r[1] for r in rows], (rows[-1][0] if rows else rowid)

    def import_mark(self):
        with self.connect() as db:
            return db.execute('SELECT COALESCE(MAX(rowid), 0) FROM imports').fetchone()[0]

    def has_events(self, session_id: str, source: str = None) -> bool:
        with self.connect() as db:
            if source:
                q = db.execute('SELECT 1 FROM events WHERE session_id = ? AND source = ? LIMIT 1', (session_id, source))
            else:
                q = db.execute('SELECT 1 FROM events WHERE session_id = ? LIMIT 1', (session_id,))
            return q.fetchone() is not None

    def imported(self, session_id: str) -> bool:
        with self.connect() as db:
            return db.execute('SELECT 1 FROM imports WHERE session_id = ?', (session_id,)).fetchone() is not None

    def load(self, session_ids):
        """Everything stored for these sessions, ready for the reducer: (events, facts)."""
        events, facts = [], []
        if not session_ids:
            return events, facts
        marks = ','.join('?' * len(session_ids))
        with self.connect() as db:
            for ts, payload, source in db.execute(
                    f'SELECT received_at, payload, source FROM events WHERE session_id IN ({marks}) ORDER BY id', session_ids):
                try:
                    events.append({'ts': ts, 'payload': json.loads(payload), 'source': source})
                except ValueError:
                    continue
            for row in db.execute(f'SELECT session_id, message_id, agent_id, model, in_tokens, out_tokens, cw_tokens, cr_tokens, final, ts '
                                  f'FROM usage WHERE session_id IN ({marks})', session_ids):
                facts.append({'ts': row[9], 'session_id': row[0], 'kind': 'usage', 'message_id': row[1], 'agent': row[2],
                              'model': row[3] or '', 'final': bool(row[8]),
                              'usage': {'in': row[4], 'out': row[5], 'cw': row[6], 'cr': row[7]}})
            for sid, ts, payload in db.execute(f'SELECT session_id, received_at, payload FROM statusline WHERE session_id IN ({marks})', session_ids):
                try:
                    facts.append({'ts': ts, 'session_id': sid, 'kind': 'statusline', 'payload': json.loads(payload)})
                except ValueError:
                    continue
            for sid, tuid, is_error, ts in db.execute(f'SELECT session_id, tool_use_id, is_error, ts FROM results WHERE session_id IN ({marks})', session_ids):
                facts.append({'ts': ts, 'session_id': sid, 'kind': 'tool_result', 'tool_use_id': tuid, 'is_error': bool(is_error)})
            # seq -1: whatever a live read of the transcript finds later is newer
            for sid, custom, ai, at in db.execute(f'SELECT session_id, custom, ai, at FROM titles WHERE session_id IN ({marks})', session_ids):
                facts += [{'ts': at, 'session_id': sid, 'kind': 'title', 'title': t, 'custom': c, 'seq': -1}
                          for t, c in ((custom, True), (ai, False)) if t]
        return events, facts

    def decisions(self, session_id=None):
        """Every logged decision, or one session's, oldest first (`request` decoded)."""
        where, args = ('WHERE session_id = ? ', (session_id,)) if session_id is not None else ('', ())
        with self.connect() as db:
            rows = db.execute(f'SELECT {", ".join(self.DECISION_FIELDS)} FROM decisions {where}ORDER BY answered_at, id', args).fetchall()
        out = [dict(zip(self.DECISION_FIELDS, r)) for r in rows]
        for r in out:
            r['request'] = json.loads(r['request'])
        return out

    def call(self, session_id, tool_use_id):
        """One tool call as stored: {tool, input, output, error}, each only when an event had it (output and error
        over TRUNCATE_AT carry the `skyborneTruncated` marker); None when no event names this call."""
        with self.connect() as db:
            rows = db.execute('SELECT event, payload FROM events WHERE session_id = ? AND tool_use_id = ? ORDER BY id',
                              (session_id, tool_use_id)).fetchall()
        out = {}
        for event, payload in rows:
            try:
                p = json.loads(payload)
            except ValueError:
                continue
            if isinstance(p.get('tool_name'), str):
                out.setdefault('tool', p['tool_name'])
            if 'tool_input' in p:
                out.setdefault('input', p['tool_input'])
            if event == 'PostToolUse' and 'tool_response' in p:
                out['output'] = p['tool_response']
            elif event == 'PostToolUseFailure' and 'error' in p:
                out['error'] = p['error']
            elif event == 'PermissionDenied' and isinstance(p.get('reason'), str):
                out['error'] = p['reason']
        return out if rows else None

    def stats(self):
        """Events stored, and when the last one came from a hook (imported history doesn't count)."""
        with self.connect() as db:
            count, = db.execute('SELECT COUNT(*) FROM events').fetchone()
            last, = db.execute("SELECT MAX(received_at) FROM events WHERE source = 'hook'").fetchone()
            imported, = db.execute('SELECT COUNT(*) FROM imports').fetchone()
        return {'events': count, 'lastEventAt': last, 'importedSessions': imported}
