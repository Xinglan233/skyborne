"""`skyborne record`: one session's events, scrubbed, as a file the page can play (and the demo site will).

    skyborne record <session id or its start> --out demo.json [--stand-ins]

The file holds the session's events and transcript facts with every time counted from the start, and the
city's view of the session over time (`frames`), rebuilt from the scrubbed data by the same reducer the
server uses. The format is in docs/RECORDING_FORMAT.md.

Scrubbing always happens (skyborne/scrub.py: ids, secrets, home folders, names, emails). `--stand-ins`
also replaces what was said and made: prompts, Claude's replies, file contents, tool output, errors,
helper briefs and reports, the project's folder and name. Only what the city shows of each tool call
stays (a command, a file's name, a search pattern), still scrubbed. Before writing, the output is
searched for anything that should be gone; one hit and nothing is written. Then the report lists every
line of text the page will show, to read before sharing.
"""
import json
import pathlib
import re
import time

from . import __version__, config
from .reducer import NOTE_TAG, Session, parse_task_notification
from .scrub import Scrubber, leaks, local_identity
from .store import Store

FORMAT, VERSION = 'skyborne-recording', 1
FRAME_MS = 200          # at most one frame per 200 ms, like the live stream
FEED = 50               # feed items per frame: what the page keeps
# what the city shows of each tool call (describe.py); with stand-ins, every other input field goes
SHOWN = {'Bash': ('command',), 'BashOutput': ('command',), 'Read': ('file_path',), 'NotebookRead': ('file_path',),
         'Edit': ('file_path',), 'MultiEdit': ('file_path',), 'NotebookEdit': ('file_path', 'notebook_path'),
         'Write': ('file_path',), 'Grep': ('pattern',), 'Glob': ('pattern',), 'WebSearch': ('query',),
         'Agent': ('subagent_type', 'description'), 'Task': ('subagent_type', 'description')}
# an Agent call's result: only what links a helper to its call and names its model
LINKS = ('agentId', 'resolvedModel', 'status', 'isAsync')
PROJECT = '/home/user/project'


class StandIns:
    """Replaces what was said and made with numbered stand-ins, the same original always the same one."""

    def __init__(self, sid):
        self.sid, self.n, self.seen = sid, 0, {}

    def _numbered(self, kind, original):
        key = (kind, original)
        if key not in self.seen:
            self.seen[key] = f'{kind} {sum(1 for k in self.seen if k[0] == kind) + 1}'
            self.n += 1
        return self.seen[key]

    def _input(self, tool, tool_input):
        if not isinstance(tool_input, dict):
            return {}
        keep = {k: tool_input[k] for k in SHOWN.get(tool, ()) if k in tool_input}
        if isinstance(keep.get('description'), str):
            keep['description'] = self._numbered('Helper task', keep['description'])
        self.n += len(tool_input) - len(keep)
        return keep

    def payload(self, p):
        p = dict(p)
        name = p.get('hook_event_name')
        for key in ('cwd',):
            if isinstance(p.get(key), str):
                p[key] = PROJECT
        for key in ('transcript_path', 'agent_transcript_path'):
            if isinstance(p.get(key), str):
                p[key] = f'/home/user/.claude/projects/-home-user-project/{self.sid}.jsonl'
        if isinstance(p.get('scratchpad_dir'), str):
            p['scratchpad_dir'] = f'/tmp/claude/-home-user-project/{self.sid}/scratchpad'
        if isinstance(p.get('session_title'), str):
            p['session_title'] = 'Project'
        if name == 'UserPromptSubmit' and isinstance(p.get('prompt'), str):
            note = parse_task_notification(p['prompt'])
            if note:  # keep the tags that link a helper's report to it; hide the report itself
                tags = ''.join(f'<{k}>{v}</{k}>' for k, v in NOTE_TAG.findall(p['prompt']))
                p['prompt'] = f'<task-notification>{tags}<result>(Report hidden)</result></task-notification>'
                self.n += 1
            else:
                p['prompt'] = self._numbered('Prompt', p['prompt'])
        for key in ('last_assistant_message',):
            if isinstance(p.get(key), str) and p[key]:
                p[key] = self._numbered('Answer', p[key])
        if isinstance(p.get('background_tasks'), list):  # helpers still running when a turn ended
            p['background_tasks'] = [{**t, 'description': self._numbered('Helper task', t['description'])}
                                     if isinstance(t, dict) and isinstance(t.get('description'), str) else t
                                     for t in p['background_tasks']]
        if 'tool_input' in p:
            p['tool_input'] = self._input(p.get('tool_name', ''), p['tool_input'])
        if 'tool_response' in p:
            r = p['tool_response']
            p['tool_response'] = {k: r[k] for k in LINKS if k in r} if isinstance(r, dict) else '(Output hidden)'
            self.n += 1
        for key, text in (('error', '(Error hidden)'), ('error_details', '(Error hidden)'), ('reason', '(Hidden)'), ('message', '(Hidden)'),
                          ('compact_summary', '(Summary hidden)')):
            v = p.get(key)  # a long error is stored cut, as {skyborneTruncated, bytes, head}
            if (isinstance(v, str) and v or isinstance(v, dict)) and not (key == 'reason' and name == 'SessionEnd'):
                p[key] = text
                self.n += 1
        return p


def _statusline(payload):
    """Only what the city reads from the status line: model, cost and context. Its rate limits carry real times."""
    return {k: payload[k] for k in ('model', 'cost', 'context_window') if k in payload}


def _visible(frames):
    """Every distinct line of text the page can show from these frames, in order of appearance."""
    seen = {}
    for f in frames:
        d = f['doc']
        for text in [d.get('title'), d.get('sessionName'), d.get('headline')] \
                + [x for a in d.get('agents', []) for x in (a.get('activity'), a.get('description'))] \
                + [i.get('text') for i in d.get('feed', [])]:
            if isinstance(text, str) and text:
                seen.setdefault(text, None)
    return list(seen)


def build(sid, events, facts, stand_ins=False, identity=None):
    """The recording (a dict) for one session's stored events and facts, and the scrubber that made it."""
    events = sorted(events, key=lambda e: e['ts'])
    # a fork's transcript starts with a copy of the conversation it came from (counted there, not here)
    fork_at = min((e['ts'] for e in events if e['payload'].get('hook_event_name') == 'SessionStart'
                   and e['payload'].get('source') == 'fork'), default=None)
    if fork_at is not None:  # usage and tool results alike: everything older than the fork was copied
        facts = [f for f in facts if not (f.get('kind') != 'title' and f.get('ts') is not None and f['ts'] < fork_at)]
        events = [e for e in events if not (e.get('source') == 'transcript' and e['ts'] < fork_at)]
    # titles carry the time they were read, not one of their own: they go at the start
    t0 = min([e['ts'] for e in events] + [f['ts'] for f in facts if f.get('ts') is not None and f.get('kind') != 'title'] or [0])
    facts = [{**f, 'ts': t0} if f.get('kind') == 'title' else f for f in facts]
    folders = []  # with stand-ins the project's folder becomes /home/user/project everywhere, paths in commands too
    if stand_ins:
        for cwd in {e['payload'].get('cwd') for e in events} - {None, ''}:
            if isinstance(cwd, str) and len(cwd.strip('/\\')) > 1:
                cwd = cwd.rstrip('/\\')
                folders += [(cwd, PROJECT), (re.sub(r'[^A-Za-z0-9]', '-', cwd), '-home-user-project')]
    scrub = Scrubber(identity if identity is not None else local_identity(), replace=sorted(folders, key=lambda kv: -len(kv[0])))
    fake_sid = scrub.text(sid)
    hide = StandIns(fake_sid) if stand_ins else None
    out_events, out_facts = [], []
    for e in events:
        p = hide.payload(e['payload']) if hide else e['payload']
        if isinstance(p.get('continued_in'), str):  # a recording holds one session
            p = {**p, 'continued_in': ''}
        out_events.append({'t': e['ts'] - t0, 'payload': scrub.value(p)})
    for f in sorted((f for f in facts if f.get('ts') is not None), key=lambda f: f['ts']):
        body = {k: v for k, v in f.items() if k not in ('ts', 'session_id')}
        if body.get('kind') == 'statusline':
            body['payload'] = _statusline(body.get('payload') or {})
        if body.get('kind') == 'title' and hide:
            body['title'] = 'Project'
        out_facts.append({'t': f['ts'] - t0, **scrub.value(body)})
    frames = _frames(fake_sid, out_events, out_facts)
    title = frames[-1]['doc'].get('title', 'session') if frames else 'session'
    report = {**scrub.report(), **({'stand-ins': hide.n} if hide else {})}
    rec = {'format': FORMAT, 'version': VERSION, 'createdWith': f'skyborne {__version__}', 'standIns': bool(stand_ins),
           'report': report, 'session': {'id': fake_sid, 'title': title},
           'duration': frames[-1]['t'] if frames else 0, 'events': out_events, 'facts': out_facts, 'frames': frames}
    return rec, scrub


def _frames(sid, events, facts):
    """The city's view of the session as it went: replayed through the reducer from the scrubbed data."""
    s = Session(sid)
    items = sorted([(e['t'], 0, i, 'e', e) for i, e in enumerate(events)] + [(f['t'], 1, i, 'f', f) for i, f in enumerate(facts)])
    frames, last_t, last_json, pending = [], None, None, None

    def emit(t, doc):
        nonlocal last_t, last_json
        frames.append({'t': t, 'doc': doc})
        last_t, last_json = t, json.dumps(doc, sort_keys=True)

    for t, _, _, kind, x in items:
        if pending is not None and t >= last_t + FRAME_MS:
            emit(last_t + FRAME_MS, pending)
            pending = None
        if kind == 'e':
            s.add_event({'ts': x['t'], 'payload': x['payload']})
        else:
            s.add_fact({**{k: v for k, v in x.items() if k != 't'}, 'ts': x['t'], 'session_id': sid})
        doc = s.doc()
        doc = {**doc, 'feed': doc['feed'][:FEED]}
        doc.pop('rateLimits', None)
        if json.dumps(doc, sort_keys=True) == last_json:
            pending = None
            continue
        if last_t is None or t >= last_t + FRAME_MS:
            emit(t, doc)
            pending = None
        else:
            pending = doc
    if pending is not None:
        emit(last_t + FRAME_MS, pending)
    return frames


def _recent(store, n=10):
    rows = []
    for sid in store.recent_session_ids(n):
        events, _ = store.load([sid])
        doc = Session(sid)
        for e in events:
            doc.add_event(e)
        d = doc.doc()
        when = time.strftime('%d %b %H:%M', time.localtime((d.get('updatedAt') or 0) / 1000))
        rows.append(f"  {sid[:8]}  {when}  {d.get('sessionName') or d.get('title')}  ({d.get('turns', 0)} turns)")
    return rows


def main(session, out, stand_ins=False, db_path=None, identity=None, say=print):
    store = Store(db_path or config.db_path())
    with store.connect() as db:
        ids = [r[0] for r in db.execute('SELECT DISTINCT session_id FROM events WHERE session_id LIKE ?', (session + '%',))] if session else []
    if len(ids) != 1:
        say('No session starts with that.' if session and not ids else f'{len(ids)} sessions start with that; give more of the id.'
            if session else 'Which session? Give its id, or the start of it.')
        say('The most recent ones:')
        for row in _recent(store):
            say(row)
        return 1
    if not out:
        say('Say where to write it, e.g. --out demo.json')
        return 1
    events, facts = store.load(ids)
    rec, scrub = build(ids[0], events, facts, stand_ins, identity)
    text = json.dumps(rec, ensure_ascii=False, separators=(',', ':'))
    found = leaks(text, scrub.identity)
    if found:
        kinds = sorted({k for k, _ in found})
        say(f"Not written: the leak check still found {len(found)} thing{'s' if len(found) != 1 else ''} "
            f"({', '.join(kinds)}). Nothing was saved.")
        return 1
    path = pathlib.Path(out)
    path.write_text(text + '\n', encoding='utf-8')
    r = rec['report']
    parts = [f"{r[k]} {k}" for k in ('home paths', 'usernames', 'names', 'computer names', 'emails', 'secrets', 'ids', 'paths', 'stand-ins') if r.get(k)]
    say(f"Wrote {path} ({path.stat().st_size / 1024:.0f} KB): {len(rec['events'])} events, {len(rec['frames'])} frames, "
        f"{rec['duration'] / 1000:.0f} s.")
    say('Scrubbed: ' + (' · '.join(parts) if parts else 'nothing needed replacing') + '. Leak check passed.')
    visible = _visible(rec['frames'])
    say(f'The page will show these {len(visible)} lines of text. Read them before sharing the file:')
    for line in visible[:300]:
        say('  ' + re.sub(r'\s+', ' ', line)[:160])
    if len(visible) > 300:
        say(f'  …and {len(visible) - 300} more.')
    return 0
