"""Permission requests the page can answer ("asks"), and the log of how each one ended.

Claude Code's PermissionRequest hook posts to `/permission` and the server holds that request open while
the person can answer in the terminal or in the city page: whoever answers first wins (docs/FINDINGS.md).
An ask ends when:
- the page answers: `decide()` hands the answer to the held request, which writes it back to the hook;
- Claude Code hangs up the hook (a "No" typed in the terminal, or the session closing);
- the reducer shows the request is over (`sync()`): its call ran or failed (a "Yes" in the terminal, seen
  only once the tool finishes), was refused (a "No", from the transcript), or the session ended, the
  lead moved on or the helper stopped;
- the server stops.
When the hold reaches its deadline the hook gets no answer, the dialog stays in the terminal, and the
card says "Answer in the terminal" until the reducer shows the request is over.

Skyborne never answers by itself: only `decide()` writes an answer, and only the page's
`POST /api/answer` calls it. AskUserQuestion and ExitPlanMode need more than yes or no, so they are
never held or answered here: their cards only say to answer in the terminal.

An answer from the page is only shown as given once Claude Code confirms it: it writes a
`hook_permission_decision` line in the transcript (the helper's own, for a helper) when it applies a
hook's answer, before the call's result. Until then the card says the answer was sent. A result without
that line means the terminal had answered first (a "Yes" there doesn't stop the hook, so the page's
answer still reaches it, too late): the card says so and closes. Every ask leaves one row in the
`decisions` table, corrected by what the transcript showed.
"""
import json
import logging
import os
import secrets
import threading
import time

from .reducer import LEAD, canonical
from .transcripts import READ_LIMIT, facts_from_line

log = logging.getLogger('skyborne')

TERMINAL_ONLY = ('AskUserQuestion', 'ExitPlanMode')
# a helper's dialog can outlive the lead's next prompt or stop, so only these end a helper's ask
HELPER_OVER = ('ran', 'failed', 'refused', 'denied', 'ended', 'stopped')
CONFIRM_WITHIN_MS = 10 * 60 * 1000  # an answer sent this long ago with no word from the transcript is let go
VERDICT_GRACE_S = 3                 # after the call is over, how long the transcript may take to say who answered
TERMINAL_HOW = ('ran', 'failed', 'refused', 'hangup')  # endings that mean the terminal answered
ANSWERS = {
    'allow': {'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': {'behavior': 'allow'}}},
    'deny': {'hookSpecificOutput': {'hookEventName': 'PermissionRequest',
                                    'decision': {'behavior': 'deny', 'message': 'Denied from Skyborne'}}},
}
# how an ask ended (the reducer's reasons, plus the server's own) -> (decision, answered in)
OUTCOMES = {
    'ran': ('allow', 'terminal'), 'failed': ('allow', 'terminal'), 'refused': ('deny', 'terminal'),
    'denied': ('deny', 'nowhere'), 'hangup': ('deny', 'terminal'),
    'prompt': ('unknown', 'terminal'), 'idle': ('unknown', 'terminal'), 'stopped': ('unknown', 'terminal'),
    'ended': ('none', 'nowhere'), 'server_stop': ('none', 'nowhere'), 'error': ('unknown', 'terminal'),
}


def over(agent, state):
    """Whether the reducer's state for a request means it's over for its ask. A helper's dialog can outlive
    the lead's next prompt or stop, so only the helper's own ending (HELPER_OVER) counts for it."""
    return state not in ('open', 'unseen') and (agent == LEAD or state in HELPER_OVER)


def now_ms():
    return int(time.time() * 1000)


def _str(v):
    return v if isinstance(v, str) else ''


class Ask:
    def __init__(self, payload, received_at):
        self.id = secrets.token_urlsafe(12)
        self.sid = payload['session_id']
        self.agent = _str(payload.get('agent_id')) or LEAD
        self.tool = payload['tool_name']
        self.input = payload.get('tool_input')
        self.key = canonical(self.input)
        self.ts = received_at
        self.suggestions = payload.get('permission_suggestions')
        self.transcript = _str(payload.get('transcript_path'))
        self.terminal_only = self.tool in TERMINAL_ONLY
        self.state = 'terminal' if self.terminal_only else 'open'   # open | terminal | decided | sent | closed
        self.claimed = False    # its event has reached the reducer, tagged with this ask's id
        self.held = not self.terminal_only
        self.until = None       # ms when Skyborne stops holding it and leaves it to the terminal (held asks only)
        self.timed_out = False
        self.decision = None    # the page's answer
        self.call = ''          # the tool call it belongs to, once the reducer has matched it
        self.wake = threading.Event()
        self.delivered = threading.Event()
        self.delivered_ok = False
        self.watch = None       # [transcript file, offset where the request came in, unfinished line]
        self.sent_at = None     # monotonic time the page's answer reached the hook
        self.over_at = None     # monotonic time the reducer first said the call is over

    def card(self):
        return {'id': self.id, 'session': self.sid, 'agent': self.agent, 'tool': self.tool, 'input': self.input,
                'since': self.ts, 'until': self.until, 'state': self.state, 'terminalOnly': self.terminal_only}

    def transcript_file(self):
        """Where Claude Code writes what happens to this request: the session's transcript, or for a helper its
        own, `<session>/subagents/agent-<id>.jsonl` next to it (docs/FINDINGS.md)."""
        if not self.transcript.endswith('.jsonl'):
            return None
        if self.agent == LEAD:
            return self.transcript
        return os.path.join(self.transcript[:-len('.jsonl')], 'subagents', f'agent-{self.agent}.jsonl')

    def row(self, decision, answered_in, how, confirmed=None):
        at = now_ms()
        return {'id': self.id, 'session_id': self.sid, 'agent_id': self.agent, 'tool_name': self.tool,
                'request': {'tool_input': self.input, 'permission_suggestions': self.suggestions},
                'asked_at': self.ts, 'answered_at': at, 'decision': decision, 'answered_in': answered_in, 'how': how,
                'timed_out': int(self.timed_out), 'waited_ms': max(0, at - self.ts), 'confirmed': confirmed}


class Approvals:
    """`broadcast(event, data)` reaches every /events listener; `record(item)` queues a database write;
    `state(sid, ts, agent, key, ask id)` asks the reducer where a request stands: (state, call id)."""

    def __init__(self, broadcast, record, state):
        self.broadcast, self.record, self.state = broadcast, record, state
        self.lock = threading.Lock()
        self.asks = {}    # id -> Ask with a card on the page (open, terminal, decided or sent)
        self.ended = {}   # id -> (why a late answer is refused: 'terminal' | 'answered' | 'closed', monotonic time)
        self.v = 0

    # ---- what the page sees ----
    def snapshot(self):
        with self.lock:
            return self._snapshot()

    def _snapshot(self):  # the caller holds the lock
        self.v += 1
        cards = [a.card() for a in sorted(self.asks.values(), key=lambda a: (a.ts, a.id)) if a.state in ('open', 'terminal', 'sent')]
        return {'v': self.v, 'asks': cards}

    def _changed(self, snap, answer=None):
        if answer:  # first, so a page can mark the card before the list drops it
            self.broadcast('answer', answer)
        self.broadcast('asks', snap)

    def _close(self, ask, reason):  # the caller holds the lock
        ask.state = 'closed'
        self.asks.pop(ask.id, None)
        self.ended[ask.id] = (reason, time.monotonic())

    # ---- the held request ----
    def open(self, payload, received_at, hold_s=None):
        """A new PermissionRequest, held for `hold_s` seconds at most; None when it can't be one (no session or tool)."""
        if not (isinstance(payload, dict) and _str(payload.get('session_id')) and _str(payload.get('tool_name'))):
            return None
        ask = Ask(payload, received_at)
        if ask.held and hold_s:
            ask.until = received_at + int(hold_s * 1000)
        # what Claude Code writes about this request (its hook decision, its result) can only come after it, and
        # either may come before the page's answer is sent: the transcript is read from here
        path = ask.transcript_file()
        try:
            offset = os.path.getsize(path) if path else 0
        except OSError:
            offset = 0  # not written yet (a helper's first lines): everything in it will be new
        ask.watch = [path, offset, b''] if path else None
        with self.lock:
            self.asks[ask.id] = ask
            snap = self._snapshot()
        self._changed(snap)
        return ask

    def claim(self, sid, ts, payload):
        """The writer is about to hand this PermissionRequest event to the reducer: the id of its ask, if any."""
        agent, key = _str(payload.get('agent_id')) or LEAD, canonical(payload.get('tool_input'))
        with self.lock:
            for a in self.asks.values():
                if not a.claimed and (a.sid, a.ts, a.agent, a.key) == (sid, ts, agent, key):
                    a.claimed = True
                    return a.id
        return None

    def expire(self, ask):
        """The hold reached its deadline: the dialog stays in the terminal. False if an answer just came in."""
        with self.lock:
            if ask.state != 'open':
                return False
            ask.state, ask.held, ask.timed_out = 'terminal', False, True
            snap = self._snapshot()
        log.info('approval for %s in session %s: no answer from the page in time; left to the terminal', ask.tool, ask.sid[:8])
        self._changed(snap)
        return True

    def hung_up(self, ask):
        """Claude Code closed the hook: the person answered in the terminal (or the session closed). Forced, so a
        page answer arriving at the same moment learns it didn't get through."""
        self.release(ask, 'hangup', force=True)

    def delivered(self, ask, ok):
        """The held request wrote the page's answer to the hook (ok), or couldn't (Claude Code hung up first)."""
        with self.lock:
            if ask.state != 'decided':
                return
            ask.delivered_ok = ok
            if ok:
                ask.state, ask.sent_at = 'sent', time.monotonic()
            snap = self._snapshot() if ok else None
        if ok:
            row = ask.row(ask.decision, 'skyborne', 'page')
            self.record(('decision', row['answered_at'], row))
            self._log(row)
            self._changed(snap)
            ask.delivered.set()  # what came of it is read by sync(), on the flusher thread only
        else:
            self.release(ask, 'hangup', force=True)  # sets `delivered` once the refusal's reason is recorded

    # ---- the page's answer ----
    def decide(self, ask_id, decision, wait=5.0):
        """The page's answer. Returns (True, '') once it reached the hook, else (False, why): 'terminal' (the
        terminal answered first), 'answered' (another page did) or 'closed' (the request is over)."""
        if decision not in ANSWERS:
            raise ValueError(decision)
        with self.lock:
            ask = self.asks.get(ask_id)
            refused = self._refusal(ask_id, ask)
        if refused:
            return False, refused
        state, call = self.state(ask.sid, ask.ts, ask.agent, ask.key, ask.id)
        if over(ask.agent, state):  # it's over; sync() just hasn't seen it yet
            self.release(ask, state, call)
            with self.lock:
                return False, self._refusal(ask_id, None) or 'closed'
        with self.lock:
            refused = self._refusal(ask_id, ask)
            if refused:
                return False, refused
            ask.state, ask.decision, ask.call = 'decided', decision, call or ask.call
        ask.wake.set()
        if not ask.delivered.wait(wait):
            log.warning('approval for %s in session %s: the answer was not delivered in time', ask.tool, ask.sid[:8])
            return False, 'closed'
        if ask.delivered_ok:
            return True, ''
        with self.lock:
            return False, self._refusal(ask_id, None) or 'closed'

    def _refusal(self, ask_id, ask):  # the caller holds the lock
        """Why a page answer can't be taken now, or '' when it can."""
        if ask is not None:
            return '' if ask.state == 'open' else 'answered' if ask.state in ('decided', 'sent') else 'closed'
        return self.ended.get(ask_id, ('closed', 0))[0]

    # ---- endings seen elsewhere ----
    def release(self, ask, how, call='', force=False):
        """End an ask that the page didn't answer (or whose answer couldn't be delivered)."""
        with self.lock:
            if ask.state in ('closed', 'sent') or (ask.state == 'decided' and not force):
                return
            ask.call = call or ask.call
            self._close(ask, 'terminal' if how in TERMINAL_HOW else 'closed')
            snap = self._snapshot()
        decision, answered_in = OUTCOMES.get(how, ('unknown', 'terminal'))
        row = ask.row(decision, answered_in, how)
        self.record(('decision', row['answered_at'], row))
        self._log(row)
        ask.wake.set()
        ask.delivered.set()  # a page answer still waiting in decide() learns it didn't get through
        self._changed(snap)

    def sync(self, sids=None):
        """After the reducer took in new events: end the asks whose request is over, and settle the answers
        sent from the page once the transcript says what came of them."""
        with self.lock:
            asks = [a for a in self.asks.values() if (a.state in ('open', 'terminal') and (sids is None or a.sid in sids))
                    or a.state == 'sent']
        for a in asks:
            if a.state == 'sent':
                self._check_sent(a)
                continue
            state, call = self.state(a.sid, a.ts, a.agent, a.key, a.id)
            if call:
                a.call = call
            if over(a.agent, state):
                self.release(a, state, call)
        self._forget_ended()

    def _check_sent(self, ask):
        """Read what Claude Code wrote since the page's answer went out, and settle the card when it says."""
        if not ask.call:
            ask.call = self.state(ask.sid, ask.ts, ask.agent, ask.key, ask.id)[1]
        # read only once the call is known: a line read now couldn't be matched, and would be lost
        for f in self._new_facts(ask) if ask.call else []:
            if f.get('tool_use_id') != ask.call:
                continue
            if f['kind'] == 'hook_decision' and f['decision'] == ask.decision:
                return self._settle(ask, True, f['decision'])
            if f['kind'] == 'tool_result':  # its result came with no hook decision of ours before it: answered elsewhere
                return self._settle(ask, False, 'deny' if f.get('refused') else 'allow')
        now = time.monotonic()
        if ask.over_at is None and over(ask.agent, self.state(ask.sid, ask.ts, ask.agent, ask.key, ask.id)[0]):
            ask.over_at = now
        if (ask.over_at is not None and now - ask.over_at > VERDICT_GRACE_S) or now - ask.sent_at > CONFIRM_WITHIN_MS / 1000:
            self._settle(ask, None, None)  # the transcript never said (a helper working elsewhere, a lost file)

    @staticmethod
    def _new_facts(ask):
        if not ask.watch:
            return []
        path, offset, partial = ask.watch
        try:
            with open(path, 'rb') as fh:
                fh.seek(offset)
                data = fh.read(READ_LIMIT)
        except OSError:
            return []
        ask.watch[1] = offset + len(data)
        lines = (partial + data).split(b'\n')
        ask.watch[2] = lines.pop()  # an unfinished last line waits for the rest
        facts = []
        for line in lines:
            try:
                facts += facts_from_line(json.loads(line), ask.sid, ask.agent, now_ms())
            except (ValueError, RecursionError):
                continue
        return facts

    def _settle(self, ask, applied, decision):
        """The outcome of an answer sent from the page: applied (True), the terminal had answered (False), or
        unknown (None)."""
        with self.lock:
            if ask.state != 'sent':
                return
            self._close(ask, 'answered' if applied is not False else 'terminal')
            snap = self._snapshot()
        if applied is True:
            self.record(('decision_confirm', 0, (ask.id, True, decision, 'skyborne')))
        elif applied is False:
            self.record(('decision_confirm', 0, (ask.id, False, decision, 'terminal')))
            log.info('approval for %s in session %s: the terminal had answered first', ask.tool, ask.sid[:8])
        self._changed(snap, {'id': ask.id, 'session': ask.sid, 'agent': ask.agent, 'decision': ask.decision, 'applied': applied})

    def forget_sessions(self, sids):
        """Sessions the server stopped following (quiet for hours): their asks can't end any other way."""
        with self.lock:
            asks = [a for a in self.asks.values() if a.sid in sids]
        for a in asks:
            if a.state == 'sent':
                self._settle(a, None, None)
            elif a.state in ('open', 'terminal'):
                self.release(a, 'ended')

    def _forget_ended(self):
        cutoff = time.monotonic() - CONFIRM_WITHIN_MS / 1000
        with self.lock:
            for k in [k for k, (_why, at) in self.ended.items() if at < cutoff]:
                del self.ended[k]

    def close_all(self):
        """The server is stopping: every held request gets no answer (the dialogs stay in the terminal)."""
        with self.lock:
            asks = [a for a in self.asks.values() if a.state != 'closed']
        for a in asks:
            if a.state == 'sent':
                self._settle(a, None, None)
            elif a.state != 'decided':  # one being written to its hook right now is left to finish
                self.release(a, 'server_stop', force=True)

    @staticmethod
    def _log(row):
        log.info('approval for %s in session %s: %s, answered in %s (%s) after %.1f s', row['tool_name'],
                 row['session_id'][:8], row['decision'], row['answered_in'], row['how'], row['waited_ms'] / 1000)
