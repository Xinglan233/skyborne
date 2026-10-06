"""The local server: receives Claude Code's hook events and status line, stores them, streams each
session's live state, and serves the city page.

    POST /hook         a hook event's JSON (from the plugin's curl hooks). Answered 200 at once.
    POST /statusline   the status line's JSON (from `skyborne statusline`). Answered 200 at once.
    POST /permission   a PermissionRequest hook's JSON (from the plugin's one waiting curl). Held until the
                       page answers, the person answers in the terminal, or the hook's time runs out; the
                       answer is the decision JSON, or an empty body (no decision). See approvals.py.
    POST /api/answer   the page answers a permission request: {"id", "decision": "allow" | "deny"}. Needs the
                       page's token.
    POST /api/names    rename a district: {"id", "name"}; an empty name removes it. Needs the page's token.
    GET  /             the city page, carrying this launch's token.
    GET  /assets/...   the page's scripts, fonts, icons and licenses.
    GET  /events       Server-Sent Events: `names`, a `state` per recent session, `ready` (with this launch's
                       token), `asks`, then changes.
                       `?feed=N` keeps only each session's newest N feed items.
    GET  /api/session  ?id=: one session step by step (agents, tool calls, conversation, files, approvals), for
                       the page's session detail. Needs the page's token.
    GET  /api/step     ?session=&id=: one tool call's full input, output and error. Needs the page's token.
    GET  /status       a small read-only status page.
    GET  /health       version, database, event count, last event time.

Listens on 127.0.0.1 only. Every request must name this server in `Host` (blocks DNS rebinding),
and a browser request from any other page (`Origin`) is refused. No CORS headers are sent. The page's
Content-Security-Policy lets the browser load nothing from outside this server.
Logs never carry payloads: an event's name and a short session id at most.
"""
import contextlib
import gc
import hmac
import html
import http.server
import json
import logging
import os
import pathlib
import queue
import secrets
import select
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict

from . import __version__, config
from .approvals import ANSWERS, Approvals
from .describe import describe_tool
from .reducer import Session, reduce
from .store import Store
from .transcripts import Tailer

log = logging.getLogger('skyborne')

MAX_BODY = 2 * 1024 * 1024
HEARTBEAT_S = 15
FLUSH_S = 0.2
ACTIVE_MS = 6 * 3600 * 1000
RECENT = 60                # sessions the city shows: live ones plus the most recent past ones
PAST_WAIT_S = 10           # a new /events listener waits this long, at most, for the past districts to load
IMPORT_POLL_S = 5          # how often the server looks for sessions `skyborne import` added
HOLD_POLL_S = 0.2          # how often a held permission request checks whether Claude Code hung up
DEFAULT_WAIT_S = 595       # a held permission request's limit when the hook doesn't send X-Skyborne-Wait
MAX_WAIT_S = 3600
PAST_DETAILS = 4           # past sessions' details kept in memory (a long session's history is large to rebuild)

WEB = pathlib.Path(__file__).resolve().parent / 'web'
TOKEN_PLACEHOLDER = '__SKYBORNE_TOKEN__'
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'none'")
ASSET_TYPES = {'.js': 'text/javascript; charset=utf-8', '.woff2': 'font/woff2', '.txt': 'text/plain; charset=utf-8',
               '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png'}


def now_ms():
    return int(time.time() * 1000)


def _trim(doc, limit):
    """The doc with only its newest `limit` feed items (the feed is newest first)."""
    if limit is None or len(doc.get('feed') or ()) <= limit:
        return doc
    return {**doc, 'feed': doc['feed'][:limit]}


_gc_lock = threading.Lock()
_gc_pausers = 0


@contextlib.contextmanager
def gc_paused():
    """Rebuilding a long session from the database makes millions of short-lived objects, and Python's cycle
    collector then stops every thread for its passes over them: up to 300 ms measured on a 95,872-event session,
    which delayed the hooks. Nothing built here forms a cycle (counting frees it all), so the collector waits
    until the rebuild is done. Nested and concurrent pauses count up; the last one out turns it back on."""
    global _gc_pausers
    with _gc_lock:
        _gc_pausers += 1
        if _gc_pausers == 1:
            gc.disable()
    try:
        yield
    finally:
        with _gc_lock:
            _gc_pausers -= 1
            if _gc_pausers == 0:
                gc.enable()


def free_slowly(*lists):
    """Empty big lists a slice at a time. Dropping a long session's events in one go is a single uninterrupted
    free of millions of objects (635 ms measured on 95,872 events), and no hook can be answered meanwhile;
    between slices the hook threads get their turn."""
    for items in lists:
        while items:
            del items[-2000:]


def stored_docs(store, sid):
    """The document of one session rebuilt from the database, without stalling the hooks meanwhile."""
    with gc_paused():
        events, facts = store.load([sid])
        docs = reduce(events, facts)
        free_slowly(events, facts)
    return docs


def _json_in_pieces(obj):
    """The JSON of a dict whose lists can be long (a session's detail: tens of thousands of steps, megabytes),
    encoded an item at a time: one json.dumps of it all would hold Python's lock long enough to delay the
    hooks (measured: 224 ms for 16 MB); between items the hook threads get their turn."""
    enc = lambda v: json.dumps(v, ensure_ascii=False, separators=(',', ':'))
    parts = [enc(k) + ':' + ('[' + ','.join(enc(x) for x in v) + ']' if isinstance(v, list) else enc(v)) for k, v in obj.items()]
    return ('{' + ','.join(parts) + '}').encode('utf-8')


def _sse(event, data):
    return f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(",", ":"))}\n\n'.encode('utf-8')


class Hub:
    """Sessions in memory, plus the docs of recent past ones. Changes go to every /events listener."""

    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}               # sid -> Session, for sessions active in the last 6 h
        self.docs = {}                   # sid -> latest doc of a session in memory
        self.past = {}                   # sid -> doc of a recent session no longer in memory
        self.names = {}                  # sid -> the name the Mayor gave its district
        self.dirty = set()
        self.listeners = {}              # queue -> feed limit (None: the whole feed)
        self.ready = threading.Event()   # set once the past districts are loaded

    def _session(self, sid):
        s = self.sessions.get(sid)
        if s is None:
            s = self.sessions[sid] = Session(sid)
        return s

    def knows(self, sid):
        with self.lock:
            return sid in self.sessions

    def detail(self, sid):
        """The session detail of a session in memory, or None. Hook posts are queued, so they never wait for this
        lock; a permission request may (a 40,000-step session's detail holds it about 30 ms)."""
        with self.lock:
            s = self.sessions.get(sid)
            return s.detail() if s else None

    def load_history(self, sid, events, facts):
        """A session seen for the first time since the server started: its stored history goes in before
        its new events (the Writer calls this before writing them, so nothing is counted twice)."""
        with self.lock:
            if sid in self.sessions:
                return
            s = self.sessions[sid] = Session(sid)
            for ev in events:
                s.add_event(ev)
            for f in facts:
                s.add_fact(f)
            self.past.pop(sid, None)
            if events or facts:  # a brand-new session goes out with its first event, never empty
                self.dirty.add(sid)

    def rebuild(self, sid, events, facts):
        """Start a session in memory again from everything stored for it."""
        with self.lock:
            s = self.sessions[sid] = Session(sid)
            for ev in events:
                s.add_event(ev)
            for f in facts:
                s.add_fact(f)
            self.past.pop(sid, None)
            self.dirty.add(sid)

    def add_event(self, ev):
        sid = ev['payload'].get('session_id')
        if isinstance(sid, str) and sid:
            with self.lock:
                self._session(sid).add_event(ev)
                self.past.pop(sid, None)
                self.dirty.add(sid)

    def add_facts(self, facts):
        with self.lock:
            for f in facts:
                sid = f.get('session_id')
                if sid in self.sessions:
                    self.sessions[sid].add_fact(f)
                    self.dirty.add(sid)

    def flush(self):
        """Send every changed session's doc; returns the ids of the sessions that changed."""
        with self.lock:
            changed = [(sid, self.sessions[sid].doc(now_ms())) for sid in self.dirty if sid in self.sessions]
            self.dirty.clear()
            for sid, doc in changed:
                self.docs[sid] = doc
            listeners = list(self.listeners.items())
        self._send_states(changed, listeners)
        return {sid for sid, _doc in changed}

    def broadcast(self, event, data):
        msg = _sse(event, data)
        with self.lock:
            listeners = list(self.listeners)
        for q in listeners:
            q.put(msg)

    def request_state(self, sid, ts, agent, key, ask=None):
        """Where one permission request stands in the reducer: (state, call id); see Session.request_state."""
        with self.lock:
            s = self.sessions.get(sid)
            return s.request_state(ts, agent, key, ask) if s is not None else ('unseen', '')

    @staticmethod
    def _send_states(docs, listeners):
        made = {}  # each doc is serialised once per feed limit, however many listeners share it
        for sid, doc in docs:
            for q, limit in listeners:
                if (sid, limit) not in made:
                    made[(sid, limit)] = _sse('state', {'id': sid, 'doc': _trim(doc, limit)})
                q.put(made[(sid, limit)])

    def add_past(self, docs):
        """Docs of recent sessions that aren't in memory (from the database or an import)."""
        with self.lock:
            fresh = [(sid, d) for sid, d in docs.items() if sid not in self.sessions]
            for sid, d in fresh:
                self.past[sid] = d
            self._trim_past()
            fresh = [(sid, d) for sid, d in fresh if sid in self.past]
            listeners = list(self.listeners.items())
        self._send_states(fresh, listeners)

    def _trim_past(self):  # the caller holds the lock
        if len(self.past) > RECENT:
            keep = sorted(self.past.items(), key=lambda kv: (kv[1].get('updatedAt', 0), kv[0]), reverse=True)[:RECENT]
            self.past = dict(keep)

    def past_ids(self):
        with self.lock:
            return list(self.past)

    def forget(self, sids):
        """Past sessions whose history was pruned: every listener drops them."""
        with self.lock:
            gone = [sid for sid in sids if self.past.pop(sid, None) is not None]
            listeners = list(self.listeners)
        for sid in gone:
            msg = _sse('gone', {'id': sid})
            for q in listeners:
                q.put(msg)

    def set_names(self, names):
        with self.lock:
            self.names = dict(names)

    def set_name(self, sid, name):
        with self.lock:
            if name:
                self.names[sid] = name
            else:
                self.names.pop(sid, None)
            names, listeners = dict(self.names), list(self.listeners)
        msg = _sse('names', {'names': names})
        for q in listeners:
            q.put(msg)

    def names_now(self):
        with self.lock:
            return dict(self.names)

    def snapshot(self, limit=RECENT):
        """The most recent sessions, in memory or past, newest first."""
        with self.lock:
            merged = {**self.past, **self.docs}
        return sorted(merged.items(), key=lambda kv: (kv[1].get('updatedAt', 0), kv[0]), reverse=True)[:limit]

    def subscribe(self, feed_limit=None):
        q = queue.Queue()
        with self.lock:
            self.listeners[q] = feed_limit
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.listeners.pop(q, None)

    def evict(self, older_than_ms=ACTIVE_MS):
        """Sessions quiet for longer than this leave memory; their last doc stays as a past district.
        Returns the ids of the sessions that left."""
        cutoff = now_ms() - older_than_ms
        with self.lock:
            gone = [sid for sid, s in self.sessions.items() if s.last_ts < cutoff]
            for sid in gone:
                del self.sessions[sid]
                doc = self.docs.pop(sid, None)
                if doc is not None:
                    self.past[sid] = doc
            self._trim_past()
        return set(gone)


class Writer:
    """The one thread that writes to the database. Hooks hand it raw bytes and move on."""

    def __init__(self, store: Store, hub: Hub, tailer: Tailer):
        self.store, self.hub, self.tailer = store, hub, tailer
        self.q = queue.Queue()
        self.dropped = 0
        self.import_mark = 0
        self.approvals = None  # set by App: it ties each held PermissionRequest's event to its ask

    def put(self, item):
        self.q.put(item)

    def run(self, stop: threading.Event):
        while not stop.is_set() or not self.q.empty():
            try:
                batch = [self.q.get(timeout=0.2)]
            except queue.Empty:
                continue
            while len(batch) < 500:  # everything that's waiting goes into one commit
                try:
                    batch.append(self.q.get_nowait())
                except queue.Empty:
                    break
            try:
                self._load_new_sessions(batch)
            except Exception as e:  # history is a nicety: the batch is still written
                log.debug('could not load history: %s', type(e).__name__)
            applied = []
            for kind, received_at, data in batch:
                try:
                    applied.append(self._write(kind, received_at, data))
                except Exception as e:  # one bad item must never stop the writer
                    self.dropped += 1
                    log.debug('dropped one %s item: %s', kind, type(e).__name__)
            try:
                self.store.commit()
            except Exception as e:
                log.warning('database commit failed: %s', type(e).__name__)
            for a in applied:
                try:
                    if a:
                        a()
                except Exception as e:
                    log.debug('could not apply one item: %s', type(e).__name__)

    def _load_new_sessions(self, batch):
        """History for sessions the hub doesn't hold (resumed, or quiet for 6 h), read before this batch is
        written so none of the batch is read back and counted twice."""
        new = set()
        for i, (kind, received_at, data) in enumerate(batch):
            if kind == 'facts':  # a hand-over line ends a session that may not be in memory any more
                new.update(f['session_id'] for f in data if f.get('kind') == 'continued' and isinstance(f.get('session_id'), str)
                           and f['session_id'] and not self.hub.knows(f['session_id']))
            if kind != 'hook':
                continue
            try:
                payload = json.loads(data)
            except Exception:  # not JSON, or nested too deep (RecursionError): _write drops it
                continue
            batch[i] = (kind, received_at, payload)  # parsed once
            sid = payload.get('session_id') if isinstance(payload, dict) else None
            if isinstance(sid, str) and sid and not self.hub.knows(sid):
                new.add(sid)
        for sid in new:
            try:
                events, facts = self.store.load([sid])
            except Exception as e:
                log.debug('could not read history: %s', type(e).__name__)
                events, facts = [], []
            self.hub.load_history(sid, events, facts)

    def _write(self, kind, received_at, data):
        if kind == 'prune':
            self.store.prune(data)
            past = self.hub.past_ids()
            gone = set(past) - self.store.existing(past)
            return (lambda: self.hub.forget(gone)) if gone else None
        if kind == 'imports':
            sids, self.import_mark = self.store.imports_after(self.import_mark)
            docs = {}
            for sid in sids:
                if not self.hub.knows(sid):
                    docs.update(stored_docs(self.store, sid))
                else:
                    # its first live event came while the import was still writing, so the history read then
                    # missed it. Rebuilt from what's committed now: every applied event (this batch's are
                    # applied after it, and aren't committed or read yet) plus the import, each once.
                    self.hub.rebuild(sid, *self.store.load([sid]))
            return (lambda: self.hub.add_past(docs)) if docs else None
        if kind == 'decision':
            self.store.add_decision(data)
            return None
        if kind == 'decision_confirm':
            self.store.confirm_decision(*data)
            return None
        if kind == 'name':
            sid, name = data
            self.store.set_name(sid, name, received_at)
            return lambda: self.hub.set_name(sid, name)
        if kind == 'facts':
            titles, then, now = {}, [], now_ms()  # facts come queued without a time of their own
            for f in data:
                if f['kind'] == 'usage':
                    self.store.add_usage(f)
                elif f['kind'] == 'tool_result' and f.get('is_error'):
                    self.store.add_result(f)
                elif f['kind'] == 'title':  # a file repeats its titles: only each one's newest line is stored
                    key = (f['session_id'], bool(f.get('custom')))
                    if key not in titles or (f.get('seq', -1), f['title']) > (titles[key].get('seq', -1), titles[key]['title']):
                        titles[key] = f
                elif f['kind'] == 'continued':
                    then.append(self._continued(f, now))
            for (sid, custom), f in titles.items():
                self.store.set_title(sid, f['title'], custom, now)
            # approvals read hook decisions from the file themselves; a hand-over is now a stored SessionEnd
            rest = [f for f in data if f['kind'] not in ('hook_decision', 'continued')]
            if rest:
                then.append(lambda: self.hub.add_facts(rest))
            then = [a for a in then if a]
            return (lambda: [a() for a in then]) if then else None
        payload = data if isinstance(data, dict) else json.loads(data)
        if not isinstance(payload, dict):
            raise ValueError('not an object')
        sid = payload.get('session_id') if isinstance(payload.get('session_id'), str) else ''
        if kind == 'statusline':
            if not sid:
                raise ValueError('no session')
            self.store.set_statusline(sid, received_at, payload)
            fact = {'ts': received_at, 'session_id': sid, 'kind': 'statusline', 'payload': payload}
            return lambda: self.hub.add_facts([fact])
        ev = self.store.add_event(received_at, payload)
        if self.approvals is not None and payload.get('hook_event_name') == 'PermissionRequest':
            ask = self.approvals.claim(sid, received_at, payload)
            if ask:
                ev = {**ev, 'ask': ask}
        log.debug('hook %s %s', payload.get('hook_event_name'), sid[:8])
        if sid and isinstance(payload.get('transcript_path'), str):
            self.tailer.watch(sid, payload['transcript_path'])
        if payload.get('hook_event_name') == 'SubagentStop' and isinstance(payload.get('agent_transcript_path'), str):
            self.tailer.watch_helper(sid, payload['agent_transcript_path'])
        return lambda: self.hub.add_event(ev)


    def _continued(self, f, now):
        """A transcript says its conversation went on in another session (moved to the background). No hook says so,
        so it's stored as that session's SessionEnd, once (a restart reads the file again). A name given in
        Skyborne goes with the conversation."""
        sid, ts, new = f['session_id'], f['ts'], f.get('continued_in') or ''
        if not sid or not new or new == sid or not isinstance(ts, int) or self.store.has_transcript_end(sid, ts):
            return None  # a hand-over to itself can only be a copied line
        payload = {'session_id': sid, 'hook_event_name': 'SessionEnd', 'reason': 'continued', 'continued_in': new}
        ev = self.store.add_event(ts, payload, source='transcript')
        names = self.hub.names_now()
        name = names.get(sid) if not names.get(new) else None
        if name:
            self.store.set_name(new, name, now)
        return lambda: (self.hub.add_event(ev), name and self.hub.set_name(new, name))


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'Skyborne/' + __version__
    app = None  # set by App

    def log_message(self, fmt, *args):  # no request lines (they'd carry paths and clients)
        pass

    # ---- guards ----
    def _origins(self):
        port = self.server.server_address[1]
        return f'http://127.0.0.1:{port}', f'http://localhost:{port}'

    def _action_allowed(self):
        """An action (rename, answer) may come only from the page this server sent: it carries the launch token."""
        if self.headers.get('Origin') not in self._origins():
            return False
        token = (self.headers.get('X-Skyborne-Token') or '').encode()
        return hmac.compare_digest(token, self.app.token.encode())

    def _allowed(self):
        port = self.server.server_address[1]
        if self.headers.get('Host') not in (f'127.0.0.1:{port}', f'localhost:{port}'):
            return False
        origin = self.headers.get('Origin')
        return origin is None or origin in self._origins()

    def _refuse(self, code=403, body=b'Forbidden'):
        """Answer `code`, after reading the request's body (thrown away): a socket closed with unread data
        resets the connection on Windows, and the client would lose the answer. A client that never sends
        its body gets 2 s."""
        try:
            self.connection.settimeout(2)
            self._body()
        except (OSError, ValueError):
            pass
        return self._send(code, body)

    def _quiet_send(self, code, body=b'', ctype='text/plain; charset=utf-8'):
        """Answer a held request; the other end may have gone meanwhile."""
        try:
            self._send(code, body, ctype)
            self.wfile.flush()
            return True
        except OSError:
            return False

    def _hung_up(self):
        """Whether the client closed its end. Its body was read in full, so anything readable now is the close."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            return bool(ready) and self.connection.recv(1, socket.MSG_PEEK) == b''
        except (OSError, ValueError):  # a reset (Windows) or a closed socket
            return True

    def _send(self, code, body=b'', ctype='text/plain; charset=utf-8', headers=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _body(self):
        """The request body, or None when it's over the cap (then it's read and thrown away)."""
        if 'chunked' in (self.headers.get('Transfer-Encoding') or '').lower():
            chunks, total = [], 0
            while True:
                size = int(self.rfile.readline().split(b';')[0].strip() or b'0', 16)
                if size == 0:
                    self.rfile.readline()
                    break
                if size > MAX_BODY:
                    raise ValueError('chunk over the cap')
                data = self.rfile.read(size)
                self.rfile.readline()
                total += size
                if total <= MAX_BODY:
                    chunks.append(data)
            return b''.join(chunks) if total <= MAX_BODY else None
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            left = length
            while left > 0:
                got = self.rfile.read(min(65536, left))
                if not got:
                    break
                left -= len(got)
            return None
        return self.rfile.read(length) if length > 0 else b''

    # ---- routes ----
    def do_POST(self):
        if not self._allowed():
            return self._refuse()
        path = urllib.parse.urlsplit(self.path).path
        if path == '/api/names':
            return self._rename()
        if path == '/api/answer':
            return self._answer()
        if path == '/permission':
            return self._permission()
        if path not in ('/hook', '/statusline'):
            return self._refuse(404, b'Not found')
        received_at = now_ms()
        try:
            body = self._body()
        except (OSError, ValueError):
            body = None
        self._send(200)  # answer first: Claude Code never waits on Skyborne
        if body:
            self.app.writer.put(('hook' if path == '/hook' else 'statusline', received_at, body))

    def do_GET(self):
        if not self._allowed():
            return self._send(403, b'Forbidden')
        url = urllib.parse.urlsplit(self.path)
        if url.path == '/':
            return self._page()
        if url.path.startswith('/assets/'):
            return self._asset(urllib.parse.unquote(url.path[len('/assets/'):]))
        if url.path == '/events':
            limit = urllib.parse.parse_qs(url.query).get('feed', [''])[0]
            return self._events(int(limit) if limit.isdigit() else None)
        if url.path == '/health':
            return self._send(200, json.dumps(self.app.health()).encode(), 'application/json')
        if url.path == '/status':
            return self._send(200, self.app.status_page().encode('utf-8'), 'text/html; charset=utf-8')
        if url.path in ('/api/session', '/api/step'):
            return self._detail(url.path, urllib.parse.parse_qs(url.query))
        self._send(404, b'Not found')

    def _detail(self, path, query):
        """GET /api/session?id= (a session step by step, with its approvals) or /api/step?session=&id= (one tool
        call in full). They hold prompts and tool output, so they need the page's launch token too. A browser
        sends no Origin on a same-origin GET, so the Host and Origin check is _allowed()'s, not _action_allowed's."""
        token = (self.headers.get('X-Skyborne-Token') or '').encode()
        if not hmac.compare_digest(token, self.app.token.encode()):
            return self._send(403, b'Forbidden')
        arg = lambda k: query.get(k, [''])[0]
        found = self.app.session_detail(arg('id')) if path == '/api/session' else (
            self.app.store.call(arg('session'), arg('id')) if arg('session') and arg('id') else None)
        if found is None:
            return self._send(404, b'Not found')
        self._send(200, _json_in_pieces(found), 'application/json', {'Cache-Control': 'no-store'})

    def _page(self):
        try:
            page = (WEB / 'index.html').read_text(encoding='utf-8')
        except OSError:
            return self._send(503, b"The city page isn't built. Run `cd page && node build.js`.")
        body = page.replace(TOKEN_PLACEHOLDER, self.app.token, 1).encode('utf-8')
        self._send(200, body, 'text/html; charset=utf-8', {
            'Content-Security-Policy': CSP, 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

    def _asset(self, rel):
        root = (WEB / 'assets').resolve()
        try:
            target = (root / rel).resolve()
        except (OSError, ValueError):
            return self._send(404, b'Not found')
        ctype = ASSET_TYPES.get(target.suffix.lower())
        if root not in target.parents or not ctype or not target.is_file():
            return self._send(404, b'Not found')
        headers = {'Cache-Control': 'no-cache'}
        if target.suffix.lower() == '.svg':  # opened on its own, an SVG is a page: give it nothing to run or load
            headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        self._send(200, target.read_bytes(), ctype, headers)

    def _permission(self):
        """Hold a PermissionRequest until it's answered (approvals.py). Only Claude Code's curl posts here, never a
        browser (browsers always send an Origin on a POST)."""
        if self.headers.get('Origin') is not None:
            return self._refuse()
        received_at = now_ms()
        try:
            body = self._body()
        except (OSError, ValueError):
            body = None
        if not body:
            return self._send(200)
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError):
            payload = None
        approvals = self.app.approvals
        # `claude -p` and the Agent SDK (entrypoint sdk-*): no one is at a terminal, so it's never held
        print_mode = (self.headers.get('X-Skyborne-Entrypoint') or '').strip().startswith('sdk-')
        seconds = self._hold_seconds()
        ask = None if print_mode else approvals.open(payload, received_at, seconds)
        # stored and reduced like every other event; after the ask exists, so the writer can tie the two
        self.app.writer.put(('hook', received_at, body))
        if ask is None or not ask.held:  # not a request, or one only the terminal can answer
            return self._send(200)
        try:
            self._hold(ask, seconds)
        except Exception as e:  # one request must never take anything else down
            log.warning('a held permission request failed: %s', type(e).__name__)
            self._quiet_send(200)
        finally:  # whatever happened, the ask doesn't outlive its request
            if ask.held and ask.state != 'closed':
                approvals.release(ask, 'server_stop' if self.app.stopping.is_set() else 'error', force=True)

    def _hold_seconds(self):
        """How long the hook lets the server hold this request (X-Skyborne-Wait), within limits."""
        wait = (self.headers.get('X-Skyborne-Wait') or '').strip()
        return min(max(int(wait), 1), MAX_WAIT_S) if wait.isascii() and wait.isdigit() else DEFAULT_WAIT_S

    def _hold(self, ask, seconds):
        approvals = self.app.approvals
        deadline = time.monotonic() + seconds
        while not ask.wake.wait(HOLD_POLL_S):
            if self._hung_up():
                return approvals.hung_up(ask)
            if time.monotonic() >= deadline and approvals.expire(ask):
                return self._quiet_send(200)  # no decision: the dialog stays in the terminal
            if self.app.stopping.is_set():
                break
        if ask.state == 'decided':
            if self._hung_up():
                return approvals.delivered(ask, False)
            ok = self._quiet_send(200, json.dumps(ANSWERS[ask.decision], separators=(',', ':')).encode(), 'application/json')
            return approvals.delivered(ask, ok)
        self._quiet_send(200)  # answered elsewhere, or the server is stopping: no decision

    def _answer(self):
        if not self._action_allowed():
            return self._refuse()
        try:
            data = json.loads(self._body() or b'')
        except (ValueError, RecursionError):
            data = None
        ask_id = data.get('id') if isinstance(data, dict) else None
        decision = data.get('decision') if isinstance(data, dict) else None
        if not isinstance(ask_id, str) or not ask_id or len(ask_id) > 100 or decision not in ANSWERS:
            return self._send(400, b'Bad request')
        ok, reason = self.app.approvals.decide(ask_id, decision)
        body = json.dumps({'ok': True} if ok else {'ok': False, 'reason': reason}).encode()
        self._send(200 if ok else 409, body, 'application/json')

    def _rename(self):
        # a rename is an action: only the page this server sent (it carries the launch token) may ask
        if not self._action_allowed():
            return self._refuse()
        try:
            data = json.loads(self._body() or b'')
        except ValueError:
            data = None
        sid = data.get('id') if isinstance(data, dict) else None
        name = data.get('name') if isinstance(data, dict) else None
        if not isinstance(sid, str) or not sid or len(sid) > 200 or not (name is None or isinstance(name, str)):
            return self._send(400, b'Bad request')
        name = ' '.join((name or '').split())[:40]
        self.app.writer.put(('name', now_ms(), (sid, name or None)))
        self._send(200, b'{"ok":true}', 'application/json')

    def _events(self, feed_limit):
        hub = self.app.hub
        hub.ready.wait(PAST_WAIT_S)
        q = hub.subscribe(feed_limit)
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            # the stream carries the launch token: no other site's <script> or <img> may be handed it
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
            self.end_headers()
            self.wfile.write(_sse('names', {'names': hub.names_now()}))
            for sid, doc in hub.snapshot():
                self.wfile.write(_sse('state', {'id': sid, 'doc': _trim(doc, feed_limit)}))
            self.wfile.write(_sse('ready', {'token': self.app.token}))  # a page that reconnects after a restart takes it
            self.wfile.write(_sse('asks', self.app.approvals.snapshot()))
            self.wfile.flush()
            while not self.app.stopping.is_set():
                try:
                    msg = q.get(timeout=HEARTBEAT_S)
                except queue.Empty:
                    msg = b': heartbeat\n\n'
                self.wfile.write(msg)
                self.wfile.flush()
        except (OSError, ValueError):
            pass  # the listener went away
        finally:
            hub.unsubscribe(q)


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    # a quick restart must not fail on the last run's closing connections. On macOS and Linux this
    # never lets two servers share the port; on Windows the same option would, so it stays off there.
    allow_reuse_address = os.name != 'nt'
    request_queue_size = 128

    def handle_error(self, request, client_address):
        """One log line instead of a traceback on the terminal. A client that hung up early (a hook's curl
        timing out, a closed tab) is normal and isn't logged."""
        err = sys.exc_info()[1]
        if not isinstance(err, (ConnectionError, TimeoutError)):
            log.warning('a request failed: %s', type(err).__name__)


class App:
    def __init__(self, port=config.DEFAULT_PORT, db_path=None):
        self.port = port
        self.started = time.time()
        self.stopping = threading.Event()
        self.token = secrets.token_urlsafe(32)  # new each launch; the page gets it, other sites can't read it
        self.store = Store(db_path or config.db_path())
        self.hub = Hub()
        self.tailer = Tailer(lambda facts: self.writer.put(('facts', 0, facts)))
        self.writer = Writer(self.store, self.hub, self.tailer)
        self.writer.import_mark = self.store.import_mark()
        self.approvals = Approvals(self.hub.broadcast, self.writer.put, self.hub.request_state)
        self.writer.approvals = self.approvals
        handler = type('BoundHandler', (Handler,), {'app': self})
        self.httpd = _Server(('127.0.0.1', port), handler)
        self.port = self.httpd.server_address[1]
        self.threads = []
        self.past_lock = threading.Lock()   # one past-session rebuild at a time
        self.past_details = OrderedDict()   # sid -> (newest event id, detail)

    def session_detail(self, sid):
        """GET /api/session: a session step by step (Session.detail) plus its logged approvals; None if unknown."""
        if not sid:
            return None
        detail = self.hub.detail(sid) or self._past_detail(sid)
        if detail is None:
            return None
        approvals = []
        for r in self.store.decisions(sid):
            request = r['request'] if isinstance(r['request'], dict) else {}
            approvals.append({
                'tool': r['tool_name'] or '', 'agent': r['agent_id'] or 'main',
                'text': describe_tool(r['tool_name'] or '', request.get('tool_input'))[1],
                'askedAt': r['asked_at'], 'answeredAt': r['answered_at'], 'decision': r['decision'],
                'answeredIn': r['answered_in'], 'how': r['how'], 'waitedMs': r['waited_ms'],
                'timedOut': bool(r['timed_out']), 'confirmed': None if r['confirmed'] is None else bool(r['confirmed'])})
        return {**detail, 'approvals': approvals}

    def _past_detail(self, sid):
        """A session no longer in memory, rebuilt from the database. One rebuild runs at a time and the last
        few are kept, so asking twice for a long session doesn't load its whole history twice."""
        with self.past_lock:
            mark = self.store.last_event_id(sid)
            if mark is None:
                return None
            hit = self.past_details.get(sid)
            if hit and hit[0] == mark:
                self.past_details.move_to_end(sid)
                return hit[1]
            with gc_paused():
                events, facts = self.store.load([sid])
                s = Session(sid)
                for ev in events:
                    s.add_event(ev)
                for f in facts:
                    s.add_fact(f)
                detail = s.detail()
                calls = list(s.calls.values())
                s = None
                free_slowly(calls, events, facts)
            self.past_details[sid] = (mark, detail)
            while len(self.past_details) > PAST_DETAILS:
                self.past_details.popitem(last=False)  # not emptied in place: a reply may still be encoding it
            return detail

    def restore(self):
        """Rebuild sessions active in the last 6 hours from the database, then re-read their transcripts."""
        sids = self.store.recent_sessions(now_ms() - ACTIVE_MS)
        events, facts = self.store.load(sids)
        for ev in events:
            self.hub.add_event(ev)
            p = ev['payload']
            if isinstance(p.get('transcript_path'), str):
                self.tailer.watch(p.get('session_id'), p['transcript_path'])
            if p.get('hook_event_name') == 'SubagentStop' and isinstance(p.get('agent_transcript_path'), str):
                self.tailer.watch_helper(p.get('session_id'), p['agent_transcript_path'])
        self.hub.add_facts(facts)
        self.hub.flush()
        return len(sids)

    def _load_past(self, stop):
        """The city's past districts: the most recent sessions not active now, rebuilt from the database.
        Runs after the server is listening, so hooks never wait for it."""
        try:
            docs = {}
            for sid in self.store.recent_session_ids(RECENT):
                if stop.is_set():
                    break
                if not self.hub.knows(sid):
                    docs.update(stored_docs(self.store, sid))
            self.hub.add_past(docs)
        except Exception as e:
            log.warning('could not load past sessions: %s', type(e).__name__)
        finally:
            self.hub.ready.set()

    def start(self):
        # Python hands the GIL to a waiting thread every 5 ms by default; while a big detail is built and
        # encoded, a hook needing it a few times waited 20-45 ms. At 1 ms hooks stay under 10 ms (measured
        # on a 40,000-step live session) and the detail is no slower.
        sys.setswitchinterval(0.001)
        pruned = self.store.prune(config.retention_days())
        if pruned:
            log.info('removed %d events older than %d days', pruned, config.retention_days())
        restored = self.restore()
        if restored:
            log.info('restored %d recent sessions', restored)
        self.hub.set_names(self.store.names())
        for target in (self.writer.run, self.tailer.run, self._flusher, self._housekeeping, self._load_past):
            t = threading.Thread(target=target, args=(self.stopping,), daemon=True)
            t.start()
            self.threads.append(t)
        t = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': 0.2}, daemon=True)
        t.start()
        self.threads.append(t)

    def stop(self):
        if self.stopping.is_set():
            return
        self.approvals.close_all()  # before stopping: the writer still stores their rows on its way out
        self.stopping.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        for t in self.threads:
            t.join(timeout=5)
        self.store.commit()

    def _flusher(self, stop):
        while not stop.wait(FLUSH_S):
            changed = self.hub.flush()
            try:
                self.approvals.sync(changed)
            except Exception as e:  # a held request must never stop the flusher
                log.warning('could not check permission requests: %s', type(e).__name__)

    def _housekeeping(self, stop):
        last_evict = last_prune = time.time()
        while not stop.wait(IMPORT_POLL_S):
            self.writer.put(('imports', 0, None))
            if time.time() - last_evict > 60:
                last_evict = time.time()
                self.approvals.forget_sessions(self.hub.evict())
                self.tailer.forget_idle()
            if time.time() - last_prune > 86400:
                last_prune = time.time()
                self.writer.put(('prune', 0, config.retention_days()))

    def health(self):
        s = self.store.stats()
        return {'ok': True, 'name': 'skyborne', 'version': __version__, 'port': self.port, 'db': str(self.store.path),
                'events': s['events'], 'lastEventAt': s['lastEventAt'], 'importedSessions': s['importedSessions'],
                'sessions': len(self.hub.sessions), 'uptimeSec': int(time.time() - self.started)}

    def status_page(self):
        h = self.health()
        rows = []
        for sid, doc in self.hub.snapshot():
            lead = next((a for a in doc.get('agents', []) if a.get('id') == 'main'), {})
            name = self.hub.names_now().get(sid) or doc.get('sessionName') or doc.get('title') or sid[:8]
            ago = max(0, (now_ms() - (doc.get('updatedAt') or 0)) // 1000)
            when = f'{ago} s ago' if ago < 3600 else time.strftime('%d %b %H:%M', time.localtime((doc.get('updatedAt') or 0) / 1000))
            rows.append(f"<tr><td>{html.escape(name)}</td><td>{html.escape(str(lead.get('status', '')).capitalize())}</td>"
                        f"<td>{html.escape(str(lead.get('activity', '')))}</td><td>{len(doc.get('agents', [])) - 1}</td>"
                        f"<td>{doc.get('turns', 0)}</td><td>{when}</td></tr>")
        last = time.strftime('%H:%M:%S', time.localtime(h['lastEventAt'] / 1000)) if h['lastEventAt'] else 'None yet'
        table = ('<table><tr><th>Session</th><th>Lead</th><th>Doing</th><th>Helpers</th><th>Turns</th><th>Updated</th></tr>'
                 + ''.join(rows) + '</table>') if rows else '<p class="muted">No sessions yet. Start Claude Code and they appear here.</p>'
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="5">
<link rel="icon" href="/assets/icons/icon.svg" type="image/svg+xml">
<title>Skyborne status</title><style>
:root{{--bg:#F4F3EF;--fg:#1B1D22;--muted:#6b6c70;--line:#dcdad3;--accent:#9A5B0C}}
@media (prefers-color-scheme: dark){{:root{{--bg:#1B1D22;--fg:#F4F3EF;--muted:#a3a29d;--line:#34363d;--accent:#E8A54B}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
main{{max-width:960px;margin:0 auto;padding:32px 16px}} h1{{font-size:22px;margin:0 0 4px}} .muted{{color:var(--muted)}}
dl{{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:20px 0}} dt{{color:var(--muted)}} dd{{margin:0}}
table{{width:100%;border-collapse:collapse;margin-top:12px}} th,td{{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-weight:600;color:var(--muted);font-size:13px}} code,a{{color:var(--accent)}}
</style></head><body><main>
<h1>Skyborne</h1><div class="muted">Running · Version {html.escape(h['version'])} · <a href="/">Open the city</a></div>
<dl><dt>Listening on</dt><dd><code>127.0.0.1:{h['port']}</code></dd><dt>Database</dt><dd><code>{html.escape(h['db'])}</code></dd>
<dt>Events stored</dt><dd>{h['events']}</dd><dt>Imported sessions</dt><dd>{h['importedSessions']}</dd>
<dt>Last event</dt><dd>{last}</dd><dt>Live stream</dt><dd><code>/events</code></dd></dl>
{table}</main></body></html>"""


def serve(port=config.DEFAULT_PORT, db_path=None):
    """Run until Ctrl+C. Returns the App after it stops."""
    app = App(port, db_path)
    app.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
    return app
