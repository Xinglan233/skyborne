"""Turn Claude Code's hook events and transcript facts into one live document per session.

`reduce()` rebuilds every session from scratch; `Session.add_event()` / `add_fact()` feed one
session live. Both end in `Session.doc(now)`, and a document depends only on what a session was
given, never on the order it arrived in: each hook runs as its own background process, so two
events a few milliseconds apart can reach the server either way round (docs/FINDINGS.md).

The document (`v: 2`, docs/EVENT_FORMAT.md) keeps full text, every feed item and every helper
(finished helpers stay, as `done`). Kinds come from tool names only, never from any text.

An event is `{"ts": ms, "payload": <hook JSON>}`. A fact (from transcripts or the status line)
is `{"ts": ms, "session_id": ..., "kind": "usage" | "tool_result" | "task_notification" |
"meta" | "statusline" | "title" | "interrupt", ...}`; see skyborne/transcripts.py.
"""
import json
import math
import re

from .describe import describe_tool, role_name

LEAD = 'main'
DEFAULT_HEADLINE = 'No prompt yet'
NOTE_TAG = re.compile(r'<(task-id|tool-use-id|status)>(.*?)</\1>', re.S)

# the tools that edit a file, and the input field that names it (the session detail's "Files changed")
FILE_TOOLS = {'Edit': 'file_path', 'MultiEdit': 'file_path', 'Write': 'file_path', 'NotebookEdit': 'notebook_path'}
# when two things happen in the same millisecond, the later stage wins
STAGE = {'idle': 0, 'start': 1, 'prompt': 2, 'note': 3, 'call': 4, 'compact': 5, 'stop': 6, 'failure': 7, 'end': 8}


def _str(v):
    return v if isinstance(v, str) else ''


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else 0


def canonical(v) -> str:
    """The same JSON value always gives the same text: how a PermissionRequest finds its PreToolUse."""
    return json.dumps(v, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def parse_task_notification(text: str):
    """The tags Claude Code puts in its own <task-notification> prompt (system text, not model output)."""
    if not text.lstrip().startswith('<task-notification>'):
        return None
    tags = {k: v.strip() for k, v in NOTE_TAG.findall(text)}
    return {'task_id': tags.get('task-id', ''), 'tool_use_id': tags.get('tool-use-id', ''), 'status': tags.get('status', '')}


def _zero():
    return {'in': 0, 'out': 0, 'cw': 0, 'cr': 0}


# a question or a plan isn't a yes-or-no approval: say what it is, not the tool's name (the bot's activity, its log line)
ASKING = {'AskUserQuestion': ('Has a question for you', 'Asked you a question'),
          'ExitPlanMode': ('Has a plan for you to review', 'Asked you to review its plan')}


def _waiting_text(tool, feed=False):
    if tool in ASKING:
        return ASKING[tool][feed]
    return f'Needs approval for {tool}' if feed else f'Needs approval: {tool}'


def _folder(cwd: str) -> str:
    parts = [p for p in re.split(r'[\\/]', cwd) if p]
    return parts[-1] if parts else ''


class Session:
    def __init__(self, session_id: str):
        self.id = session_id
        self.first_ts = None
        self.last_ts = 0
        self.cwd = ('', 0)                # (cwd, ts of the first event that had one)
        self.names = []                   # (ts, session name)
        self.starts = []                  # SessionStart: {ts, source}
        self.prompts = []                 # real prompts: {ts, text}
        self.notes = []                   # task notifications: {ts, task_id, tool_use_id, status}
        self.calls = {}                   # tool_use_id -> call
        self.requests = []                # PermissionRequest: {ts, agent, tool, key}
        self.starts_of = {}               # helper id -> [{ts, type}]
        self.stops_of = {}                # helper id -> [{ts, message}]
        self.meta = {}                    # helper id -> {ts, type, description, tool_use_id}
        self.stops = []                   # lead's Stop: {ts, message, running}
        self.failures = []                # StopFailure: {ts, error}
        self.idles = []                   # Notification idle_prompt: ts
        self.compacts = []                # {ts, phase, trigger}
        self.ends = []                    # SessionEnd: {ts, reason, continued_in}; a later SessionStart (a resume) re-opens
        self.interrupts = []              # the lead's turn stopped with Esc (transcript): ts
        self.titles = {}                  # 'custom' | 'ai' -> (seq, title), from the transcript
        self.latest = 0                   # the newest event that isn't a SessionEnd
        self.usage = {}                   # message id -> {agent, model, in, out, cw, cr}
        self.results = {}                 # tool_use_id -> {ts, is_error} from the transcript
        self.models = []                  # (ts, agent, model)
        self.status = None                # latest status line: {ts, payload}
        self.imported = False             # rebuilt from a past transcript by `skyborne import`

    # ---------------------------------------------------------------- input
    def _seen(self, ts):
        self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
        self.last_ts = max(self.last_ts, ts)

    def _call(self, p, ts):
        tuid = _str(p.get('tool_use_id'))
        if not tuid:
            return None
        c = self.calls.setdefault(tuid, {'id': tuid, 'agent': LEAD, 'tool': '', 'input': None, 'pre': None,
                                         'post': None, 'duration': None, 'outcome': None, 'error': '', 'first': ts})
        c['first'] = min(c['first'], ts)
        c['agent'] = _str(p.get('agent_id')) or c['agent']
        c['tool'] = _str(p.get('tool_name')) or c['tool']
        if c['input'] is None and isinstance(p.get('tool_input'), dict):
            c['input'] = p['tool_input']
        return c

    def add_event(self, ev):
        p, ts = ev.get('payload') or {}, ev['ts']
        if not isinstance(p, dict):
            return
        if ev.get('source') == 'imported':
            self.imported = True
        self._seen(ts)
        cwd = _str(p.get('cwd'))
        if cwd and (not self.cwd[0] or ts < self.cwd[1]):
            self.cwd = (cwd, ts)
        if _str(p.get('session_title')):
            self.names.append((ts, p['session_title']))
        if p.get('hook_event_name') != 'SessionEnd':
            self.latest = max(self.latest, ts)
        handler = getattr(self, '_on_' + _str(p.get('hook_event_name')), None)
        if handler:
            handler(p, ts)
        if p.get('hook_event_name') == 'PermissionRequest' and ev.get('ask'):
            self.requests[-1]['ask'] = ev['ask']  # the server's id for a request it holds (live only, never stored)

    def _on_SessionStart(self, p, ts):
        self.starts.append({'ts': ts, 'source': _str(p.get('source'))})
        if _str(p.get('model')):
            self.models.append((ts, LEAD, p['model']))

    def _on_UserPromptSubmit(self, p, ts):
        prompt = _str(p.get('prompt'))
        note = parse_task_notification(prompt)
        if note:
            self.notes.append({'ts': ts, **note})
        else:
            self.prompts.append({'ts': ts, 'text': prompt})

    def _on_PreToolUse(self, p, ts):
        c = self._call(p, ts)
        if c:
            c['pre'] = ts if c['pre'] is None else min(c['pre'], ts)

    def _on_PostToolUse(self, p, ts):
        c = self._call(p, ts)
        if not c:
            return
        if not self._later_outcome(c, ts, 'ok'):
            return
        if isinstance(p.get('duration_ms'), (int, float)):
            c['duration'] = p['duration_ms']
        r = p.get('tool_response')
        if c['tool'] in ('Agent', 'Task') and isinstance(r, dict):
            c['helper'] = _str(r.get('agentId'))
            if c['helper'] and _str(r.get('resolvedModel')):
                self.models.append((ts, c['helper'], r['resolvedModel']))

    def _on_PostToolUseFailure(self, p, ts):
        c = self._call(p, ts)
        if not c:
            return
        if not self._later_outcome(c, ts, 'interrupted' if p.get('is_interrupt') else 'failed'):
            return
        c['error'] = _str(p.get('error'))
        if isinstance(p.get('duration_ms'), (int, float)):
            c['duration'] = p['duration_ms']

    @staticmethod
    def _later_outcome(c, ts, outcome):
        """Record how a call ended, unless an outcome from later (or the same moment, ranked by name) is in."""
        if c['post'] is not None and c['outcome'] in ('ok', 'failed', 'interrupted') and (ts, outcome) < (c['post'], c['outcome']):
            return False
        c['post'], c['outcome'] = ts, outcome
        return True

    def _on_PermissionRequest(self, p, ts):
        # the docs list a tool_use_id here, but Claude Code 2.1.289 sends none (docs/FINDINGS.md): used when present
        self.requests.append({'ts': ts, 'agent': _str(p.get('agent_id')) or LEAD, 'tool': _str(p.get('tool_name')),
                              'key': canonical(p.get('tool_input')), 'tuid': _str(p.get('tool_use_id'))})

    def _on_PermissionDenied(self, p, ts):
        c = self._call(p, ts)
        if c:
            c['outcome'], c['error'] = 'denied', _str(p.get('reason'))
            c['post'] = ts if c['post'] is None else c['post']

    def _on_SubagentStart(self, p, ts):
        aid = _str(p.get('agent_id'))
        if aid:
            self.starts_of.setdefault(aid, []).append({'ts': ts, 'type': _str(p.get('agent_type'))})

    def _on_SubagentStop(self, p, ts):
        aid = _str(p.get('agent_id'))
        if aid:  # kept even for an unknown id; it only counts if the helper is known (internal agents never are)
            self.stops_of.setdefault(aid, []).append({'ts': ts, 'message': _str(p.get('last_assistant_message'))})

    def _on_Stop(self, p, ts):
        tasks = p.get('background_tasks') if isinstance(p.get('background_tasks'), list) else []
        running = [_str(t.get('id')) for t in tasks if isinstance(t, dict) and t.get('type') == 'subagent' and t.get('status') == 'running']
        self.stops.append({'ts': ts, 'message': _str(p.get('last_assistant_message')), 'running': [r for r in running if r]})

    def _on_StopFailure(self, p, ts):
        self.failures.append({'ts': ts, 'error': _str(p.get('error'))})

    def _on_Notification(self, p, ts):
        if p.get('notification_type') == 'idle_prompt':
            self.idles.append(ts)

    def _on_PreCompact(self, p, ts):
        self.compacts.append({'ts': ts, 'phase': 'pre', 'trigger': _str(p.get('trigger'))})

    def _on_PostCompact(self, p, ts):
        self.compacts.append({'ts': ts, 'phase': 'post', 'trigger': _str(p.get('trigger'))})

    def _on_SessionEnd(self, p, ts):
        # 'continued': stored by the server from the transcript's continued-in line, the only sign of a hand-over
        self.ends.append({'ts': ts, 'reason': _str(p.get('reason')), 'continued_in': _str(p.get('continued_in'))})

    def _fork_at(self):
        """When a forked session began (moved to the background, /fork, /branch, --fork-session), else None. Its
        transcript starts with a copy of the conversation so far, with the old times: those aren't its own."""
        return min((s['ts'] for s in self.starts if s['source'] == 'fork'), default=None)

    def _ends(self):
        """The ends that count. `skyborne import` closes every session it rebuilds, but one still open then goes
        on live: when anything follows that closing, the session never ended there. A hand-over line older
        than a fork, or pointing at itself, came with the copied history; one followed by a prompt was resumed
        (its SessionStart may have been missed)."""
        fork_at = self._fork_at()
        return [e for e in self.ends if not (e['reason'] == 'imported' and self.latest > e['ts'])
                and not (e['reason'] == 'continued' and (e['continued_in'] == self.id or fork_at is not None and e['ts'] < fork_at
                                                          or any(p['ts'] > e['ts'] for p in self.prompts)))]

    def _interrupts(self):
        """The lead's Escs, without any a fork copied from the conversation it came from."""
        fork_at = self._fork_at()
        return [t for t in self.interrupts if fork_at is None or t >= fork_at]

    def _end(self):
        """How the session ended, or None while it runs (never ended, or resumed after its last end). A hand-over
        says more than the exit that follows it (`/bg` in the terminal writes continued-in, then sends SessionEnd
        and quits), so it wins unless the session was resumed after it."""
        ends = self._ends()
        if not ends:
            return None
        last = max(ends, key=lambda e: (e['ts'], e['reason']))
        if any(s['ts'] > last['ts'] for s in self.starts):
            return None
        moved = [e for e in ends if e['reason'] == 'continued' and not any(s['ts'] > e['ts'] for s in self.starts)]
        return max(moved, key=lambda e: e['ts']) if moved else last

    def add_fact(self, f):
        ts, kind = f['ts'], f.get('kind')
        if kind == 'title':
            # no time of its own (it's when the line was read): it must not make the session look active
            slot, title = 'custom' if f.get('custom') else 'ai', _str(f.get('title'))
            seq = f.get('seq') if isinstance(f.get('seq'), int) else -1
            if title and (slot not in self.titles or (seq, title) > self.titles[slot]):
                self.titles[slot] = (seq, title)
            return
        self._seen(ts)
        if kind == 'usage':
            # a message is written over 2-3 lines; the one with a stop reason has the final numbers
            u = f.get('usage') or {}
            rank = (bool(f.get('final')), ts, *(_num(u.get(k)) for k in ('out', 'in', 'cw', 'cr')))
            old = self.usage.get(f['message_id'])
            if old is None or rank >= old['rank']:
                self.usage[f['message_id']] = {'agent': f.get('agent') or LEAD, 'model': _str(f.get('model')), 'rank': rank, 'ts': ts,
                                               **{k: _num(u.get(k)) for k in ('in', 'out', 'cw', 'cr')}}
            if _str(f.get('model')):
                self.models.append((ts, f.get('agent') or LEAD, f['model']))
        elif kind == 'tool_result':
            r = self.results.get(f['tool_use_id'])
            if r is None or ts < r['ts']:
                self.results[f['tool_use_id']] = {'ts': ts, 'is_error': bool(f.get('is_error'))}
        elif kind == 'task_notification':
            self.notes.append({'ts': ts, 'task_id': _str(f.get('task_id')), 'tool_use_id': _str(f.get('tool_use_id')),
                               'status': _str(f.get('status'))})
        elif kind == 'meta':
            self.meta[f['agent_id']] = {'ts': ts, 'type': _str(f.get('agent_type')), 'description': _str(f.get('description')),
                                        'tool_use_id': _str(f.get('tool_use_id'))}
        elif kind == 'statusline':
            if self.status is None or ts >= self.status['ts']:
                self.status = {'ts': ts, 'payload': f.get('payload') or {}}
        elif kind == 'interrupt':
            self.interrupts.append(ts)

    # ---------------------------------------------------------------- output
    def doc(self, now=None):
        helpers = self._helpers()
        names = {LEAD: 'Skybot', **{h['id']: h['name'] for h in helpers}}
        requests = self._open_requests(helpers)
        waiting = max(requests, key=lambda r: (r['ts'], r['agent'])) if requests else None

        usage_by, fork_at = {}, self._fork_at()
        for u in self.usage.values():
            if fork_at is not None and u['ts'] < fork_at:
                continue  # copied from the session it forked from, counted there
            a = usage_by.setdefault(u['agent'], _zero())
            for k in a:
                a[k] += u[k]
        total = _zero()
        for a in usage_by.values():
            for k in total:
                total[k] += a[k]

        agents = [self._agent(LEAD, 'Skybot', 'Lead Agent', 'lead', None, '', requests, usage_by)]
        for h in helpers:
            agents.append(self._agent(h['id'], h['name'], f"{h['type']} Agent", h['type'], h['parent'], h['description'],
                                      requests, usage_by, helper=h))

        doc = {
            'v': 2,
            'title': _folder(self.cwd[0]) or 'session',
            'headline': self._headline(),
            'startedAt': self._origin(),
            'updatedAt': self.last_ts,
            'turns': len(self.prompts),
            'tokens': {'total': sum(total.values()), **total,
                       'helpers': [{'id': h['id'], 'name': h['name'], 'role': f"{h['type']} Agent", 'usage': usage_by.get(h['id'], _zero()),
                                    **({'model': m} if (m := self._model_of(h['id'])) else {})} for h in helpers]},
            'waiting': {'agent': waiting['agent'], 'tool': waiting['tool'], 'since': waiting['ts']} if waiting else None,
            'agents': agents,
            'feed': self._feed(helpers, names),
        }
        name = self._session_name()
        if name:
            doc['sessionName'] = name
        s = (self.status or {}).get('payload') or {}
        cw = s.get('context_window') if isinstance(s.get('context_window'), dict) else None
        if cw and isinstance(cw.get('used_percentage'), (int, float)) and _num(cw.get('context_window_size')):
            doc['context'] = {'tokens': _num(cw.get('total_input_tokens')), 'window': cw['context_window_size'], 'percent': cw['used_percentage']}
        cost = s.get('cost') if isinstance(s.get('cost'), dict) else None
        if cost and isinstance(cost.get('total_cost_usd'), (int, float)):
            doc['cost'] = {'usd': cost['total_cost_usd']}
        if isinstance(s.get('rate_limits'), dict):
            doc['rateLimits'] = s['rate_limits']
        end = self._end()
        if end:
            doc['ended'] = {'at': end['ts'], 'reason': end['reason'], **({'continuedIn': end['continued_in']} if end['continued_in'] else {})}
        if self.imported:
            doc['imported'] = True
        return doc

    def _origin(self):
        """When the session began: a fork at its fork, not at its copied history's oldest line."""
        fork_at = self._fork_at()
        return fork_at if fork_at is not None else min([s['ts'] for s in self.starts] + [self.first_ts or 0])

    def _session_name(self):
        """The name you gave the session (the transcript's newest /rename), else the newest name a hook or the status
        line sent (the status line's `session_name` is the custom name, else Claude's own title), else Claude's own
        title from the transcript."""
        if 'custom' in self.titles:
            return self.titles['custom'][1]
        names = list(self.names)
        s = self.status
        if s and _str(s['payload'].get('session_name')):
            names.append((s['ts'], s['payload']['session_name']))
        if names:
            return max(names)[1]
        return self.titles['ai'][1] if 'ai' in self.titles else ''

    def _headline(self):
        options = [(p['ts'], STAGE['prompt'], p['text']) for p in self.prompts if p['text']]
        options += [(s['ts'], STAGE['stop'], s['message']) for s in self.stops if s['message']]
        options += [(f['ts'], STAGE['failure'], 'Hit a snag') for f in self.failures]
        if options:
            return max(options)[2]
        return 'Continued from another session' if self._fork_at() is not None else DEFAULT_HEADLINE

    def _model_of(self, agent):
        history = self._models(agent)
        return history[-1] if history else ''

    def _models(self, agent):
        out, fork_at = [], self._fork_at()
        for ts, a, m in sorted(self.models):
            if fork_at is not None and ts < fork_at:
                continue  # the copied history's
            if a == agent and m and m != '<synthetic>' and (not out or out[-1] != m):
                out.append(m)
        return out

    def _link(self, aid):
        """The Agent call that launched a helper: from its sidecar, the call's response, or its notification."""
        meta = self.meta.get(aid)
        if meta and meta['tool_use_id']:
            return meta['tool_use_id']
        for c in sorted(self.calls.values(), key=lambda c: c['id']):
            if c.get('helper') == aid:
                return c['id']
        for n in sorted(self.notes, key=lambda n: n['ts']):
            if n['task_id'] == aid and n['tool_use_id']:
                return n['tool_use_id']
        return ''

    def _helpers(self):
        known = set(self.starts_of) | set(self.meta)
        out = []
        for aid in known:
            starts = self.starts_of.get(aid, [])
            meta = self.meta.get(aid, {})
            first = min([s['ts'] for s in starts] + ([meta['ts']] if meta else []))
            call = self.calls.get(self._link(aid)) or {}
            i = call.get('input') or {}
            agent_type = next((s['type'] for s in starts if s['type']), '') or meta.get('type') or _str(i.get('subagent_type')) or 'general-purpose'
            out.append({'id': aid, 'first': first, 'type': agent_type, 'parent': call.get('agent') or LEAD,
                        'description': _str(i.get('description')) or meta.get('description', ''),
                        'starts': starts, 'stops': self.stops_of.get(aid, []), 'link': call.get('id', '')})
        out.sort(key=lambda h: (h['first'], h['id']))
        counts = {}
        for h in out:
            role = role_name(h['type'])
            counts[role] = counts.get(role, 0) + 1
            h['name'] = f'{role} {counts[role]}'
        return out

    def _how_settled(self, tuid):
        """How a call ended ('ran', 'failed', 'denied', 'refused'), or None while it hasn't."""
        c, r = self.calls.get(tuid), self.results.get(tuid)
        outcome = c['outcome'] if c else None
        if outcome == 'ok':
            return 'ran'
        if outcome in ('failed', 'interrupted'):
            return 'failed'
        if outcome == 'denied':
            return 'denied'
        if r is not None:
            return 'refused' if r['is_error'] else 'ran'
        return None

    def _request_states(self, helpers):
        """Every permission request with the call it belongs to and why it's over: (request, call id, why).
        `why` is None while it's open, else 'ran' or 'failed' (the call finished), 'denied' (auto mode),
        'refused' (a "No" in the terminal), 'ended', 'prompt', 'idle' or 'stopped'."""
        stopped = {h['id']: max([s['ts'] for s in h['stops']], default=None) for h in helpers}
        end = self._end()
        taken, out = set(), []
        for r in sorted(self.requests, key=lambda r: (r['ts'], r['agent'], r['key'])):
            # the PreToolUse it belongs to: its tool_use_id when the request has one, else the same agent,
            # tool and input, nearest in time
            match = self.calls.get(r.get('tuid')) if r.get('tuid') and r['tuid'] not in taken else None
            if match is None:
                same = [c for c in self.calls.values() if c['id'] not in taken and c['agent'] == r['agent']
                        and c['tool'] == r['tool'] and canonical(c['input']) == r['key']]
                match = min(same, key=lambda c: (abs((c['pre'] if c['pre'] is not None else c['first']) - r['ts']), c['id']), default=None)
            call = match['id'] if match else ''
            if match:
                taken.add(call)
                how = self._how_settled(call)
                if how:
                    out.append((r, call, how))
                    continue
            later = lambda ts: ts is not None and ts > r['ts']
            if (end and r['ts'] >= end['ts']) or any(e['ts'] >= r['ts'] for e in self._ends()):
                why = 'ended'  # including a request that landed with, or a few ms after, the session's end
            elif r['agent'] == LEAD and any(later(t) for t in [s['ts'] for s in self.stops + self.failures] + self._interrupts()):
                why = 'stopped'
            elif r['agent'] != LEAD and later(stopped.get(r['agent'])):
                why = 'stopped'
            elif any(later(p['ts']) for p in self.prompts):
                why = 'prompt'
            elif any(later(t) for t in self.idles):
                why = 'idle'
            else:
                why = None
            out.append((r, call, why))
        return out

    def _open_requests(self, helpers):
        return [r for r, _call, why in self._request_states(helpers) if why is None]

    def request_state(self, ts, agent, key, ask=None):
        """Where one permission request stands: ('unseen', '') before its event is in, ('open', call id), or
        (why it's over, call id); see _request_states. Found by the server's id for it when it has one (two
        identical requests in the same millisecond can't be told apart otherwise), else by time, agent and input."""
        states = self._request_states(self._helpers())
        found = next(((r, c, w) for r, c, w in states if ask and r.get('ask') == ask), None)
        found = found or next(((r, c, w) for r, c, w in states if r['ts'] == ts and r['agent'] == agent and r['key'] == key), None)
        return (found[2] or 'open', found[1]) if found else ('unseen', '')

    def _states(self, agent, helper):
        """Every moment that set this agent's state: (ts, stage, tiebreak, status, kind, activity, tool)."""
        out = []
        for c in self.calls.values():
            if c['agent'] == agent:
                kind, text = describe_tool(c['tool'], c['input'])
                ts = c['pre'] if c['pre'] is not None else c['first']
                out.append((ts, STAGE['call'], c['id'], 'working', kind, text, c['tool']))
        if helper is None:
            out.append((self._origin(), STAGE['idle'], '', 'idle', 'idle', 'Waiting for a prompt', ''))
            # a call that didn't run and fired no hook (a "No" typed in the terminal): the turn is over
            # unless something else follows
            for c in self.calls.values():
                r = self.results.get(c['id'])
                if c['agent'] == agent and c['outcome'] is None and r and r['is_error']:
                    out.append((r['ts'], STAGE['call'], c['id'] + '~', 'idle', 'idle', 'Waiting for a prompt', ''))
            out += [(p['ts'], STAGE['prompt'], '', 'working', 'think', 'Thinking', '') for p in self.prompts]
            out += [(n['ts'], STAGE['note'], n['task_id'], 'working', 'think', "Reading a helper's report", '') for n in self.notes
                    if any(n['task_id'] == aid for aid in self.starts_of)]
            for s in self.stops:
                running = [r for r in s['running'] if not self.stops_of.get(r)]
                state = ('working', 'think', f"Waiting for {len(running)} helper{'s' if len(running) != 1 else ''}") if running else ('done', 'done', 'Answered')
                out.append((s['ts'], STAGE['stop'], '', *state, ''))
            out += [(f['ts'], STAGE['failure'], '', 'error', 'error', f"Turn ended on an error ({f['error'] or 'unknown'})", '') for f in self.failures]
            out += [(t, STAGE['idle'], '', 'idle', 'idle', 'Waiting for the next prompt', '') for t in self.idles]
            out += [(t, STAGE['stop'], '', 'idle', 'idle', 'Interrupted', '') for t in self._interrupts()]
            for c in self.compacts:
                if c['phase'] == 'pre':
                    out.append((c['ts'], STAGE['compact'], '', 'working', 'think', 'Summarising the conversation', ''))
                elif c['trigger'] == 'manual':
                    out.append((c['ts'], STAGE['compact'], 'post', 'idle', 'idle', 'Waiting for the next prompt', ''))
            ends = self._ends()
            for e in ends:
                out.append((e['ts'], STAGE['end'], '', 'idle', 'leave', 'Continued in another session' if e['reason'] == 'continued' else 'Left the city', ''))
            if ends:
                first_end = min(e['ts'] for e in ends)
                out += [(s['ts'], STAGE['start'], '', 'idle', 'idle', 'Waiting for a prompt', '') for s in self.starts if s['ts'] > first_end]
            out = self._until(out, self._end())
        else:
            first = helper['first']
            out.append((first, STAGE['start'], '', 'working', 'think', helper['description'] or 'Starting up', ''))
            out += [(s['ts'], STAGE['start'], '', 'working', 'think', helper['description'] or 'Back at work', '') for s in helper['starts']]
            failed = any(n['task_id'] == agent and n['status'] in ('failed', 'killed') for n in self.notes)
            for s in helper['stops']:
                state = ('error', 'error', 'Ran into a problem') if failed else ('done', 'done', s['message'] or 'Finished')
                out.append((s['ts'], STAGE['stop'], '', *state, ''))
            if not helper['stops']:  # rebuilt from transcripts without its SubagentStop: its report says how it ended
                for n in self.notes:
                    if n['task_id'] == agent and n['status']:
                        state = ('error', 'error', 'Ran into a problem') if n['status'] in ('failed', 'killed') else ('done', 'done', 'Finished')
                        out.append((n['ts'], STAGE['stop'], '', *state, ''))
            if not helper['stops']:
                gone = [e['ts'] for e in self._ends() if e['ts'] >= first]
                if gone:
                    out.append((min(gone), STAGE['end'], '', 'done', 'done', 'Stopped when the session ended', ''))
            if helper['stops']:  # a stop is final unless the helper starts again
                last = max(s['ts'] for s in helper['stops'])
                if not any(s['ts'] > last for s in helper['starts']):
                    out = self._until(out, {'ts': last})
        return out

    @staticmethod
    def _until(states, end):
        """Drop states after a final end: a hook a few ms late must not wake a bot that has left."""
        return states if end is None else [s for s in states if s[0] <= end['ts']]

    def _agent(self, aid, name, role, agent_type, parent, description, requests, usage_by, helper=None):
        states = self._states(aid, helper)
        # the whole tuple breaks same-moment ties, so the arrival order never decides
        ts, _stage, _tie, status, kind, activity, tool = max(states) if states else (0, 0, '', 'idle', 'idle', '', '')
        mine = [r for r in requests if r['agent'] == aid]
        if mine:
            r = max(mine, key=lambda r: r['ts'])
            status = 'working' if status in ('idle', 'done') else status
            kind, activity, tool, ts = 'wait', _waiting_text(r['tool']), r['tool'], r['ts']
        a = {'id': aid, 'name': name, 'role': role, 'type': agent_type, 'status': status, 'kind': kind, 'tool': tool,
             'activity': activity, 'activitySince': ts, 'waiting': bool(mine),
             'tools': sum(1 for c in self.calls.values() if c['agent'] == aid), 'usage': usage_by.get(aid, _zero())}
        if parent:
            a['parent'] = parent
        if description:
            a['description'] = description
        models = self._models(aid)
        if models:
            a['model'], a['models'] = models[-1], models
        return a

    def _feed(self, helpers, names):
        items = []  # (ts, stage, tiebreak, item)

        def add(ts, stage, tie, agent, kind, text, **extra):
            items.append((ts, stage, tie, {'ts': ts, 'agent': names.get(agent, 'Helper'), 'agentId': agent, 'kind': kind, 'text': text, **extra}))

        for s in self.starts:
            if s['source'] != 'compact':
                add(s['ts'], STAGE['start'], '', LEAD, 'join', f"Moved into the {_folder(self.cwd[0]) or 'session'} district")
        for p in self.prompts:
            add(p['ts'], STAGE['prompt'], '', LEAD, 'prompt', p['text'] or 'New prompt')
        for c in self.calls.values():
            kind, text = describe_tool(c['tool'], c['input'])
            start = c['pre'] if c['pre'] is not None else c['first']
            extra = {'tool': c['tool'], 'toolUseId': c['id']}
            ms = self._duration(c)
            if ms is not None:
                extra['durationMs'] = ms
            add(start, STAGE['call'], c['id'], c['agent'], kind, text, **extra)
            end = c['post'] if c['post'] is not None else (self.results.get(c['id']) or {}).get('ts')
            if c['outcome'] in ('failed', 'interrupted'):
                add(end, STAGE['call'], c['id'] + '~', c['agent'], 'error', f"{c['tool']} failed" + (f": {c['error']}" if c['error'] else ''),
                    tool=c['tool'], toolUseId=c['id'])
            elif c['outcome'] == 'denied':
                add(end, STAGE['call'], c['id'] + '~', c['agent'], 'error', f"Auto mode refused {c['tool']}" + (f": {c['error']}" if c['error'] else ''),
                    tool=c['tool'], toolUseId=c['id'])
            elif c['outcome'] is None and (self.results.get(c['id']) or {}).get('is_error'):
                add(end, STAGE['call'], c['id'] + '~', c['agent'], 'error', f"{c['tool']} didn't run", tool=c['tool'], toolUseId=c['id'])
        for r in self.requests:
            add(r['ts'], STAGE['call'], 'wait' + r['key'], r['agent'], 'wait', _waiting_text(r['tool'], feed=True), tool=r['tool'])
        for h in helpers:
            add(h['first'], STAGE['start'], h['id'], h['parent'], 'spawn', f"Brought in {h['name']}" + (f": {h['description']}" if h['description'] else ''))
            add(h['first'], STAGE['start'], h['id'] + '~', h['id'], 'join', f"{h['name']} beamed in")
            for s in h['stops']:
                add(s['ts'], STAGE['stop'], h['id'], h['id'], 'done', 'Finished and handed back the result')
        for s in self.stops:
            add(s['ts'], STAGE['stop'], '', LEAD, 'answer', s['message'] or 'Finished the turn')
        for f in self.failures:
            add(f['ts'], STAGE['failure'], '', LEAD, 'error', f"Turn ended on an error ({f['error'] or 'unknown'})")
        for c in self.compacts:
            if c['phase'] == 'pre':
                add(c['ts'], STAGE['compact'], '', LEAD, 'think', 'Summarising the conversation')
        for t in self._interrupts():
            add(t, STAGE['stop'], 'esc', LEAD, 'idle', 'Interrupted')
        end = self._end()
        for e in self._ends():
            if end and end['reason'] == 'continued' and e['ts'] > end['ts']:
                continue  # the exit that follows a hand-over
            add(e['ts'], STAGE['end'], '', LEAD, 'leave', 'Continued in another session' if e['reason'] == 'continued' else 'Left the city')
        items.sort(key=lambda i: (*i[:3], i[3]['kind'], i[3]['text'], i[3]['agentId'] or ''), reverse=True)
        return [i[3] for i in items]

    @staticmethod
    def _duration(c):
        """How long a call ran: Claude Code's duration_ms (no approval wait), else PostToolUse time minus
        PreToolUse time; None when neither is known."""
        if c['duration'] is not None:
            return c['duration']
        if c['pre'] is not None and c['post'] is not None and c['outcome'] != 'denied':  # a refused call never ran
            return max(0, c['post'] - c['pre'])
        return None

    def detail(self):
        """The whole session step by step, for the page's session detail (GET /api/session): each agent and
        when it ran, each tool call with its start, end and outcome, the conversation, and the files edited."""
        helpers = self._helpers()
        end = self._end()
        end_ts = end['ts'] if end else None
        # starts from hooks first: a fact's time (a helper's sidecar file) can be far off
        agents = [{'id': LEAD, 'name': 'Skybot', 'role': 'Lead Agent', 'type': 'lead', 'parent': None,
                   'start': min(s['ts'] for s in self.starts) if self.starts else (self.first_ts or 0), 'end': end_ts}]
        for h in helpers:
            last = max([s['ts'] for s in h['stops']], default=None)
            done = last if last is not None and not any(s['ts'] > last for s in h['starts']) else end_ts
            agents.append({'id': h['id'], 'name': h['name'], 'role': f"{h['type']} Agent", 'type': h['type'], 'parent': h['parent'],
                           'start': min(s['ts'] for s in h['starts']) if h['starts'] else h['first'], 'end': done})
        steps = []
        for c in self.calls.values():
            kind, text = describe_tool(c['tool'], c['input'])
            start = c['pre'] if c['pre'] is not None else c['first']
            result = self.results.get(c['id'])
            stop = c['post'] if c['post'] is not None else (result or {}).get('ts')
            outcome = c['outcome']
            if outcome is None and result:
                outcome = 'refused' if result['is_error'] else 'ok'
            if stop is None and end_ts is not None:
                stop = max(start, end_ts)  # never finished: it stopped with the session
            steps.append({'id': c['id'], 'agent': c['agent'], 'tool': c['tool'], 'kind': kind, 'text': text,
                          'start': start, 'end': stop, 'ms': self._duration(c), 'outcome': outcome, 'error': c['error']})
        steps.sort(key=lambda s: (s['start'], s['id']))
        talk = [(p['ts'], 0, p['text']) for p in self.prompts] + [(s['ts'], 1, s['message']) for s in self.stops if s['message']]
        conversation = [{'ts': ts, 'who': 'claude' if who else 'you', 'text': text} for ts, who, text in sorted(talk)]
        files = {}
        for c in self.calls.values():
            field = FILE_TOOLS.get(c['tool'])
            path = _str((c['input'] or {}).get(field)) if field and isinstance(c['input'], dict) else ''
            if not path:
                continue
            f = files.setdefault(path, {'path': path, 'edits': 0, 'failed': 0})
            how = self._how_settled(c['id'])
            if how == 'ran':
                f['edits'] += 1
            elif how:
                f['failed'] += 1
        return {'id': self.id, 'updatedAt': self.last_ts, 'agents': agents, 'steps': steps, 'conversation': conversation,
                'files': sorted(files.values(), key=lambda f: (-f['edits'], -f['failed'], f['path']))}


def reduce(events, facts, now=None):
    """Every session's document, built from scratch."""
    sessions = {}
    for ev in events:
        sid = _str((ev.get('payload') or {}).get('session_id'))
        if sid:
            sessions.setdefault(sid, Session(sid)).add_event(ev)
    for f in facts:
        sid = _str(f.get('session_id'))
        if sid:
            sessions.setdefault(sid, Session(sid)).add_fact(f)
    return {sid: s.doc(now) for sid, s in sessions.items()}
