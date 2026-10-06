"""`skyborne import`: past Claude Code sessions, rebuilt from their transcripts so the city can show them.

Each session's transcript, with its helpers' transcripts, becomes the hook events Claude Code would have
sent (marked `imported` in the database), plus the usage of every API message, counted once from its
last line. Times come only from the lines' own timestamps: a line without one (Claude Code writes several
such header lines) never gets "now". Sessions already recorded live by the plugin, or imported before,
are skipped, so running it again is safe. What's read stays on this machine, in Skyborne's database.

Differences from a live recording, because transcripts don't hold them: no approval requests, no tool
run times (the time between the call and its result stands in), no status line. Slash commands and `!`
shell lines are not turns.
"""
import collections
import json
import pathlib
import time

from . import config
from .store import Store
from .transcripts import facts_from_line, to_ms

# Claude Code's own lines in the user role: not something the person typed
NOT_TYPED = ('<command-name>', '<command-message>', '<command-args>', '<local-command-', '<bash-', '<system-reminder>',
             '[Request interrupted')
TURN_ENDS = ('end_turn', 'stop_sequence', 'refusal')
REJECTED = 'User rejected tool use'


def transcripts(days: float, root: pathlib.Path = None):
    """Main session transcripts changed in the last `days` days, oldest first."""
    root = root or (config.claude_dir() / 'projects')
    cutoff = time.time() - days * 86400
    found = []
    for p in root.glob('*/*.jsonl'):
        try:
            if p.stat().st_mtime >= cutoff:
                found.append(p)
        except OSError:
            continue
    return sorted(found, key=lambda p: (p.stat().st_mtime, p.name))


def _lines(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        for raw in f:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if isinstance(d, dict):
                yield d


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str))
    return ''


def _str(v):
    return v if isinstance(v, str) else ''


class _Reader:
    """Turns one agent's lines (the lead's, or one helper's) into hook-shaped events."""

    def __init__(self, sid, base, agent_id='', agent_type=''):
        self.sid, self.base, self.agent_id, self.agent_type = sid, base, agent_id, agent_type
        self.events, self.facts = [], []
        self.calls = {}          # tool_use_id -> (tool_name, tool_input)
        self.messages = {}       # message id -> {ts of its last line, stop reason, text blocks}
        self.first = self.last = None
        self.skipped = collections.Counter()

    def _event(self, ts, name, **fields):
        p = {**self.base, 'hook_event_name': name, **fields}
        if self.agent_id:
            p['agent_id'], p['agent_type'] = self.agent_id, self.agent_type
        self.events.append((ts, p))

    def line(self, d):
        ts = to_ms(d.get('timestamp'), None)
        if ts is not None:
            self.first = ts if self.first is None else min(self.first, ts)
            self.last = ts if self.last is None else max(self.last, ts)
        for f in facts_from_line(d, self.sid, 'main', None):
            if f['ts'] is not None and f['kind'] in ('usage', 'tool_result'):
                self.facts.append(f)
        kind = d.get('type')
        m = d.get('message') if isinstance(d.get('message'), dict) else {}
        if ts is None or kind not in ('user', 'assistant'):
            if kind in ('user', 'assistant'):
                self.skipped['untimed'] += 1
            return
        if kind == 'assistant':
            mid = _str(m.get('id'))
            msg = self.messages.setdefault(mid, {'ts': ts, 'stop': None, 'texts': []}) if mid else None
            for b in m.get('content') or []:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'tool_use' and isinstance(b.get('id'), str) and b['id'] not in self.calls:
                    self.calls[b['id']] = (_str(b.get('name')), b.get('input') if isinstance(b.get('input'), dict) else {})
                    self._event(ts, 'PreToolUse', tool_name=self.calls[b['id']][0], tool_input=self.calls[b['id']][1], tool_use_id=b['id'])
                elif b.get('type') == 'text' and isinstance(b.get('text'), str) and msg is not None:
                    msg['texts'].append(b['text'])
            if msg is not None:  # a message spans 2-3 lines (thinking, text, tool use); the turn ends at its last one
                msg['ts'] = max(msg['ts'], ts)
                msg['stop'] = m.get('stop_reason') or msg['stop']
            return
        # the user role: tool results, prompts, helper reports, and Claude Code's own notes
        c = m.get('content')
        results = [b for b in c if isinstance(b, dict) and b.get('type') == 'tool_result'] if isinstance(c, list) else []
        if results:
            tur = d.get('toolUseResult')
            for b in results:
                tuid = b.get('tool_use_id')
                if not isinstance(tuid, str):
                    continue
                name, tool_input = self.calls.get(tuid, ('', {}))
                body = _text(b.get('content'))
                if b.get('is_error'):
                    if d.get('toolDenialKind') or tur == REJECTED:
                        self.skipped['refused calls'] += 1  # a "No": no hook fires live either; its result fact says it didn't run
                        continue
                    self._event(ts, 'PostToolUseFailure', tool_name=name, tool_input=tool_input, tool_use_id=tuid,
                                error=body, is_interrupt=body.startswith('[Request interrupted'))
                else:
                    response = tur if (len(results) == 1 and tur is not None) else body
                    self._event(ts, 'PostToolUse', tool_name=name, tool_input=tool_input, tool_use_id=tuid, tool_response=response)
            return
        if self.agent_id:
            return  # a helper's first line is the brief its lead wrote, not a turn
        if d.get('isMeta') or d.get('isCompactSummary'):
            self.skipped['notes from Claude Code'] += 1
            return
        text = _text(c)
        origin = _str((d.get('origin') or {}).get('kind')) if isinstance(d.get('origin'), dict) else ''
        if origin == 'task-notification' or (not origin and text.lstrip().startswith('<task-notification>')):
            self._event(ts, 'UserPromptSubmit', prompt=text)  # a helper's report: the reducer reads it, it isn't a turn
        elif origin in ('', 'human') and text and not text.lstrip().startswith(NOT_TYPED):
            self._event(ts, 'UserPromptSubmit', prompt=text)
        else:
            self.skipped['commands and notes'] += 1

    def finish(self):
        """A Stop (or, for a helper, a SubagentStop) for every message that ended a turn."""
        for msg in self.messages.values():
            if msg['stop'] in TURN_ENDS:
                text = '\n'.join(msg['texts'])
                if self.agent_id:
                    self._event(msg['ts'], 'SubagentStop', last_assistant_message=text,
                                agent_transcript_path=self.base.get('agent_transcript_path', ''))
                else:
                    self._event(msg['ts'], 'Stop', last_assistant_message=text, stop_hook_active=False)


def build(path: pathlib.Path):
    """(session id, events [(ts, payload)], facts, skipped counts) for one session's transcripts."""
    path = pathlib.Path(path)
    sid = path.stem
    lines = list(_lines(path))
    cwd = next((d['cwd'] for d in lines if isinstance(d.get('cwd'), str) and d['cwd']), '')
    titles = [d['customTitle'] for d in lines if d.get('type') == 'custom-title' and isinstance(d.get('customTitle'), str) and d['customTitle']]
    model = next((d['message']['model'] for d in lines if d.get('type') == 'assistant' and isinstance(d.get('message'), dict)
                  and isinstance(d['message'].get('model'), str) and d['message']['model'] != '<synthetic>'), '')
    base = {'session_id': sid, 'transcript_path': str(path), 'cwd': cwd}
    lead = _Reader(sid, base)
    for d in lines:
        lead.line(d)
    lead.finish()
    readers = [lead]
    skipped = collections.Counter(lead.skipped)
    for hp in sorted(path.with_suffix('').glob('subagents/agent-*.jsonl')):
        aid = hp.name[len('agent-'):-len('.jsonl')]
        meta = {}
        try:
            meta = json.loads(hp.with_name(f'agent-{aid}.meta.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            skipped['helpers without a sidecar'] += 1
        r = _Reader(sid, {**base, 'agent_transcript_path': str(hp)}, aid, _str(meta.get('agentType')) if isinstance(meta, dict) else '')
        for d in _lines(hp):
            r.line(d)
        r.finish()
        if r.first is None:
            skipped['helpers with no timed lines'] += 1
            continue
        r.events.insert(0, (r.first, {**base, 'hook_event_name': 'SubagentStart', 'agent_id': aid, 'agent_type': r.agent_type}))
        readers.append(r)
        skipped.update(r.skipped)
    times = [t for r in readers for t in (r.first, r.last) if t is not None]
    if not times:
        return sid, [], [], skipped + collections.Counter({'sessions with no timed lines': 1})
    start = {'source': 'startup', 'model': model} if model else {'source': 'startup'}
    if titles:
        start['session_title'] = titles[-1]  # the name the person gave it (Claude Code's own titles are not names)
    events = [(min(times), {**base, 'hook_event_name': 'SessionStart', **start})]
    for r in readers:
        events += r.events
    events.append((max(times), {**base, 'hook_event_name': 'SessionEnd', 'reason': 'imported'}))
    events.sort(key=lambda e: e[0])  # stable: lines keep their file order within a millisecond
    return sid, events, [f for r in readers for f in r.facts], skipped


def run(days: float = 7, db_path=None, root=None, out=print, progress=True):
    """Import sessions into the database. Returns a summary dict."""
    store = Store(db_path or config.db_path())
    files = transcripts(days, root)
    summary = collections.Counter()
    skipped = collections.Counter()
    out(f'Reading {len(files)} Claude Code session{"s" if len(files) != 1 else ""} from the last {days:g} days…')
    for i, path in enumerate(files, 1):
        sid = path.stem
        if store.has_events(sid, 'hook'):
            summary['recorded live'] += 1
        elif store.imported(sid):
            summary['imported before'] += 1
        else:
            try:
                sid, events, facts, why = build(path)
            except OSError:
                summary['unreadable'] += 1
                continue
            skipped.update(why)
            if not events:
                summary['empty'] += 1
                continue
            why_not = store.begin_import(sid)  # checked again: another import may have added it meanwhile
            if why_not:
                summary[why_not] += 1
                continue
            for ts, payload in events:
                store.add_event(ts, payload, source='imported')
            best = {}  # message id -> (rank, tokens): its last line has the final numbers
            for f in facts:
                if f['kind'] == 'usage':
                    store.add_usage(f)
                    u, rank = f['usage'], (f['final'], f['ts'], f['usage']['out'])
                    if f['message_id'] not in best or rank >= best[f['message_id']][0]:
                        best[f['message_id']] = (rank, u['in'] + u['out'] + u['cw'] + u['cr'])
                elif f.get('is_error'):
                    store.add_result(f)
            store.add_import(sid, str(path), len(events), len(best))
            store.commit()
            summary['imported'] += 1
            summary['events'] += len(events)
            summary['tokens'] += sum(t for _, t in best.values())
        if progress and (i % 10 == 0 or i == len(files)):
            out(f'  {i}/{len(files)}')
    return {'summary': dict(summary), 'skipped lines': dict(skipped)}
