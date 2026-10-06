"""Read Claude Code's transcripts as they grow, and turn their lines into facts for the reducer.

A transcript is a `.jsonl` file: one JSON object per line. Facts taken from it:
- `usage`: one per API message (`message.id`), from its last line. Claude Code writes a message
  over 2-3 lines that repeat the usage, so summing lines would double count (docs/FINDINGS.md).
- `tool_result`: a tool call's result, with `is_error`. A "No" typed in the terminal fires no hook,
  but its rejected call shows up here, so this is what ends a "needs you" state.
- `task_notification`: a helper reporting back (`origin.kind == "task-notification"`).
- `meta`: a helper's `.meta.json` sidecar, which names the Agent call that launched it.
- `hook_decision`: Claude Code applied a PermissionRequest hook's answer (an `attachment` line of type
  `hook_permission_decision`). It shows an answer from the page really took effect; approvals.py
  reads it, the reducer never does.
- `continued`: the conversation moved on to another session (a `continued-in` line, written when it's moved
  to the background). No hook says so; the server stores it as a SessionEnd (docs/FINDINGS.md).
- `title`: the session's name, from a `custom-title` (`/rename`, `--name`) or `ai-title` line. They carry no
  time and repeat, so `seq` (the line's place in the file) says which is newest.
- `interrupt`: the lead's turn stopped with Esc ("[Request interrupted by user…]"). No hook fires for it.

Helper transcripts live next to the session's: `<session>/subagents/agent-<id>.jsonl`.
Files are polled, not watched, so this works the same on every platform.
"""
import datetime
import json
import os
import pathlib
import threading
import time

from .reducer import parse_task_notification

READ_LIMIT = 8 * 1024 * 1024  # most bytes read from one file in one poll; the rest comes next time
INTERRUPTED = '[Request interrupted by user'  # how Claude Code writes an Esc into the transcript
TITLES = {'custom-title': 'customTitle', 'ai-title': 'aiTitle'}


def to_ms(stamp, fallback):
    if isinstance(stamp, str):
        try:
            return int(datetime.datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp() * 1000)
        except ValueError:
            pass
    return fallback


def facts_from_line(d, session_id, agent, now_ms, seq=-1):
    """The facts in one parsed transcript line (often none). `seq` is the line's place in its file."""
    if not isinstance(d, dict):
        return []
    ts = to_ms(d.get('timestamp'), now_ms)
    kind = d.get('type')
    who = d.get('agentId') if d.get('isSidechain') and isinstance(d.get('agentId'), str) else agent
    m = d.get('message') if isinstance(d.get('message'), dict) else {}
    timed = to_ms(d.get('timestamp'), None) is not None  # a hand-over or an Esc needs its own time: a read time moves
    if kind == 'continued-in' and timed and isinstance(d.get('continuedInSessionId'), str) and d['continuedInSessionId'] and agent == 'main':
        return [{'ts': ts, 'session_id': session_id, 'kind': 'continued', 'continued_in': d['continuedInSessionId']}]
    if kind in TITLES and isinstance(d.get(TITLES[kind]), str) and d[TITLES[kind]].strip() and agent == 'main':
        return [{'ts': ts, 'session_id': session_id, 'kind': 'title', 'title': d[TITLES[kind]].strip(),
                 'custom': kind == 'custom-title', 'seq': seq}]
    if kind == 'assistant' and isinstance(m.get('id'), str) and isinstance(m.get('usage'), dict):
        u = m['usage']
        return [{'ts': ts, 'session_id': session_id, 'kind': 'usage', 'message_id': m['id'], 'agent': who,
                 'model': m.get('model') if isinstance(m.get('model'), str) else '',
                 'final': m.get('stop_reason') is not None,
                 'usage': {'in': u.get('input_tokens') or 0, 'out': u.get('output_tokens') or 0,
                           'cw': u.get('cache_creation_input_tokens') or 0, 'cr': u.get('cache_read_input_tokens') or 0}}]
    if kind == 'user':
        c = m.get('content')
        if (timed and who == 'main' and isinstance(c, list) and len(c) == 1 and isinstance(c[0], dict) and c[0].get('type') == 'text'
                and isinstance(c[0].get('text'), str) and c[0]['text'].startswith(INTERRUPTED)):
            return [{'ts': ts, 'session_id': session_id, 'kind': 'interrupt'}]
        if isinstance(c, list):
            # `toolDenialKind` marks a call that was refused (in the terminal, or by a hook), not one that ran and failed
            return [{'ts': ts, 'session_id': session_id, 'kind': 'tool_result', 'tool_use_id': b['tool_use_id'],
                     'is_error': bool(b.get('is_error')), 'refused': bool(d.get('toolDenialKind'))}
                    for b in c if isinstance(b, dict) and b.get('type') == 'tool_result' and isinstance(b.get('tool_use_id'), str)]
        origin = d.get('origin') if isinstance(d.get('origin'), dict) else {}
        if isinstance(c, str) and origin.get('kind') == 'task-notification':
            note = parse_task_notification(c)
            if note:
                return [{'ts': ts, 'session_id': session_id, 'kind': 'task_notification', **note}]
    if kind == 'attachment':
        a = d.get('attachment') if isinstance(d.get('attachment'), dict) else {}
        if (a.get('type') == 'hook_permission_decision' and a.get('hookEvent') == 'PermissionRequest'
                and isinstance(a.get('toolUseID'), str) and a.get('decision') in ('allow', 'deny')):
            return [{'ts': ts, 'session_id': session_id, 'kind': 'hook_decision', 'tool_use_id': a['toolUseID'],
                     'decision': a['decision']}]
    return []


def meta_fact(path: pathlib.Path, session_id: str):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        ts = int(path.stat().st_mtime * 1000)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    agent_id = path.name[len('agent-'):-len('.meta.json')]
    return {'ts': ts, 'session_id': session_id, 'kind': 'meta', 'agent_id': agent_id,
            'agent_type': data.get('agentType') if isinstance(data.get('agentType'), str) else '',
            'description': data.get('description') if isinstance(data.get('description'), str) else '',
            'tool_use_id': data.get('toolUseId') if isinstance(data.get('toolUseId'), str) else ''}


class _File:
    def __init__(self, session_id, agent):
        self.session_id, self.agent = session_id, agent
        self.offset, self.partial = 0, b''


class Tailer:
    """Follows the transcripts of the sessions it's told about. `emit(facts)` gets every new fact."""

    def __init__(self, emit, interval=0.3):
        self.emit, self.interval = emit, interval
        self.files = {}      # path -> _File
        self.sessions = {}   # session id -> {path, seen}
        self.metas = set()   # sidecars already read
        self.lock = threading.Lock()

    def watch(self, session_id, transcript_path):
        """Called for every hook event that names a transcript."""
        if not session_id or not transcript_path:
            return
        with self.lock:
            self.sessions[session_id] = {'path': pathlib.Path(transcript_path), 'seen': time.time()}

    def watch_helper(self, session_id, path):
        if session_id and path:
            with self.lock:
                p = pathlib.Path(path)
                name = p.name
                agent = name[len('agent-'):-len('.jsonl')] if name.startswith('agent-') and name.endswith('.jsonl') else ''
                self.files.setdefault(p, _File(session_id, agent or 'main'))

    def forget_idle(self, older_than_s=6 * 3600):
        cutoff = time.time() - older_than_s
        with self.lock:
            for sid in [s for s, v in self.sessions.items() if v['seen'] < cutoff]:
                del self.sessions[sid]
                for p in [p for p, f in self.files.items() if f.session_id == sid]:
                    del self.files[p]

    def poll(self):
        with self.lock:
            sessions = list(self.sessions.items())
        for sid, s in sessions:
            main = s['path']
            with self.lock:
                self.files.setdefault(main, _File(sid, 'main'))
            helpers = main.with_suffix('') / 'subagents'
            try:
                found = sorted(helpers.iterdir())
            except OSError:
                found = []
            for p in found:
                if p.name.startswith('agent-') and p.name.endswith('.jsonl'):
                    self.watch_helper(sid, p)
                elif p.name.startswith('agent-') and p.name.endswith('.meta.json') and p not in self.metas:
                    fact = meta_fact(p, sid)
                    if fact:
                        self.metas.add(p)
                        self.emit([fact])
        with self.lock:
            files = list(self.files.items())
        for path, f in files:
            facts = self.read(path, f)
            if facts:
                self.emit(facts)

    def read(self, path, f):
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        if size < f.offset:  # truncated or replaced: start over (facts are idempotent)
            f.offset, f.partial = 0, b''
        if size == f.offset:
            return []
        try:
            with open(path, 'rb') as fh:
                fh.seek(f.offset)
                data = fh.read(min(size - f.offset, READ_LIMIT))
        except OSError:
            return []
        at = f.offset - len(f.partial)  # where the held-back partial line starts in the file
        f.offset += len(data)
        lines = (f.partial + data).split(b'\n')
        f.partial = lines.pop()  # an unfinished last line waits for the rest
        now = int(time.time() * 1000)
        facts = []
        for line in lines:
            seq, at = at, at + len(line) + 1
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue  # a broken line is skipped, never fatal
            facts += facts_from_line(d, f.session_id, f.agent, now, seq)
        return facts

    def run(self, stop: threading.Event):
        while not stop.is_set():
            try:
                self.poll()
            except Exception:  # a bad file must never stop the tailer
                pass
            stop.wait(self.interval)
