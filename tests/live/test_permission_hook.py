"""Opt-in live checks: can a *synchronous* PermissionRequest command hook wait for an answer
while every other hook stays a background curl? (macOS and Linux; spends a little usage.)

    SKYBORNE_LIVE=1 .venv/bin/python -m pytest tests/live/test_permission_hook.py -s

`test_the_shipped_permission_hook` (further down) runs the exact hook `skyborne install` writes against a
stand-in server, to settle what the approvals server is built on: what a terminal No does to a held hook, whether
a late "allow" can overrule it, a deny, a timeout, a Yes on a long command, `claude -p`, and the
AskUserQuestion and ExitPlanMode prompts.

The plugin under test sends 14 events as background curl to a port nothing listens on (as when Skyborne
is down) and runs PermissionRequest as a normal, blocking command hook: a small script that logs when it
starts and ends and, depending on a control file, sleeps, answers "allow", or exits at once. One session
(with a fixed --session-id) goes through the cases, then a second launch resumes it. Results are printed
and saved to tmp/live/permission-hook.json.
"""
import datetime
import http.server
import json
import os
import pathlib
import re
import select
import socket
import subprocess
import sys
import threading
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.environ.get('SKYBORNE_LIVE') != '1' or sys.platform == 'win32',
                                reason='live tests run only with SKYBORNE_LIVE=1 on macOS or Linux')

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
if sys.platform != 'win32':
    from driver import ESC as ESC_KEY, Session, clean_env  # noqa: E402

BASE = ROOT / 'tmp' / 'live' / 'perm'
MODEL = os.environ.get('SKYBORNE_LIVE_MODEL', 'claude-haiku-4-5-20251001')
DIALOG = r'(?i)do\s*you\s*want\s*to\s*proceed'  # the terminal may draw it without spaces

HOOK = r'''
import json, os, signal, sys, time
ctrl, log = sys.argv[1], sys.argv[2]
def note(**k):
    with open(log, 'a') as f:
        f.write(json.dumps({'t': time.time(), 'pid': os.getpid(), **k}) + '\n')
def stopped(sig, _):
    note(event='signal', sig=sig)
    sys.exit(0)
for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(s, stopped)
data = json.loads(sys.stdin.read() or '{}')
mode = open(ctrl).read().strip()
note(event='start', mode=mode, session=data.get('session_id'), tool=data.get('tool_name'))
kind, _, secs = mode.partition(':')
if kind in ('sleep', 'allow'):
    time.sleep(float(secs))
if kind == 'allow':
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': {'behavior': 'allow'}}}))
    sys.stdout.flush()
note(event='end')
'''


def closed_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def setup(monkeypatch):
    from conftest import REAL_CLAUDE_CONFIG_DIR
    from skyborne import install
    if REAL_CLAUDE_CONFIG_DIR:
        monkeypatch.setenv('CLAUDE_CONFIG_DIR', REAL_CLAUDE_CONFIG_DIR)
    else:
        monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    project = BASE / 'project'
    (project / '.claude').mkdir(parents=True, exist_ok=True)
    (project / '.claude' / 'settings.local.json').write_text(json.dumps({'enabledPlugins': {'skyborne@skills-dir': False}}))
    for f in project.glob('made-*.txt'):
        f.unlink()
    plugin = BASE / 'plugin'
    install.write_plugin(plugin, closed_port())  # the 14 background curls go nowhere, as if Skyborne were down
    (BASE / 'hook.py').write_text(HOOK)
    ctrl, log = BASE / 'mode.txt', BASE / 'hook-log.jsonl'
    log.write_text('')
    hooks_file = plugin / 'hooks' / 'hooks.json'
    hooks = json.loads(hooks_file.read_text())
    hooks['hooks']['PermissionRequest'] = [{'hooks': [{
        'type': 'command', 'command': sys.executable, 'args': [str(BASE / 'hook.py'), str(ctrl), str(log)], 'timeout': 60}]}]
    hooks_file.write_text(json.dumps(hooks, indent=2))
    return {'project': project, 'plugin': plugin, 'ctrl': ctrl, 'log': log}


def launch(st, *extra, debug):
    s = Session(['claude', '--plugin-dir', str(st['plugin']), '--permission-mode', 'default', '--model', MODEL,
                 '--debug-file', str(debug), *extra], cwd=str(st['project']), env=clean_env())
    if s.wait_for(r'(?i)trust (the files in )?this folder|do you trust', 20):
        s.send('\r')
    time.sleep(6)
    return s


def entries(log, since=0.0):
    return [json.loads(l) for l in log.read_text().splitlines() if l.strip() and json.loads(l)['t'] >= since]


def wait_until(check, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = check()
        if v:
            return v
        time.sleep(0.1)
    return None


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_sync_permission_hook_and_resume(setup):
    st, project, log = setup, setup['project'], setup['log']
    sid = str(uuid.uuid4())
    out = {'session': 'fixed id', 'cases': {}}
    s = launch(st, '--session-id', sid, debug=BASE / 'debug-1.txt')
    try:
        # 1) the hook holds for 20 s with no answer: does the dialog wait for it? does Yes end the hook?
        st['ctrl'].write_text('sleep:20')
        asked, mark = time.time(), s.mark()
        s.type_line('Run exactly this bash command and nothing else: touch made-1.txt')
        start = wait_until(lambda: next((e for e in entries(log, asked) if e['event'] == 'start'), None), 90)
        assert start, 'the PermissionRequest hook never ran'
        seen = s.wait_for(DIALOG, 40, since=mark)
        dialog_at = time.time()
        (BASE / 'screen-1.txt').write_text(s.text(mark))
        assert seen, 'no dialog within 40 s of the hook starting (screen in tmp/live/perm/screen-1.txt)'
        case = {'hook started -> dialog on screen (s)': round(dialog_at - start['t'], 2)}
        time.sleep(2)
        s.send('1')
        yes_at = time.time()
        made = wait_until(lambda: (project / 'made-1.txt').exists(), 15)
        case['Yes -> command ran (s)'] = round(time.time() - yes_at, 2) if made else None
        time.sleep(1)
        after = [e for e in entries(log, asked) if e['pid'] == start['pid']]
        case['hook process after Yes'] = ('still running' if alive(start['pid']) else
                                          'got a signal' if any(e['event'] == 'signal' for e in after) else
                                          'finished' if any(e['event'] == 'end' for e in after) else 'killed (no log line)')
        out['cases']['1 hook holds 20 s, person says Yes'] = case
        wait_until(lambda: not alive(start['pid']), 25)

        # 2) the hook answers "allow" after 5 s: the dialog closes and the command runs with no key pressed
        st['ctrl'].write_text('allow:5')
        asked, mark = time.time(), s.mark()
        s.type_line('Run exactly this bash command and nothing else: touch made-2.txt')
        start = wait_until(lambda: next((e for e in entries(log, asked) if e['event'] == 'start'), None), 90)
        assert start
        seen = s.wait_for(DIALOG, 10, since=mark)
        case = {'dialog shown while the hook ran': bool(seen)}
        made = wait_until(lambda: (project / 'made-2.txt').exists(), 30)
        case['hook started -> command ran (s)'] = round(time.time() - start['t'], 2) if made else None
        assert made, 'the hook\'s "allow" did not let the command run'
        out['cases']['2 hook answers allow after 5 s'] = case
        time.sleep(3)

        # 3) the hook exits at once with nothing to say (Skyborne down): the dialog shows, nothing else
        st['ctrl'].write_text('silent')
        asked, mark = time.time(), s.mark()
        s.type_line('Run exactly this bash command and nothing else: touch made-3.txt')
        assert s.wait_for(DIALOG, 90, since=mark)
        time.sleep(1)
        screen = s.text(mark)
        notices = [l.strip() for l in screen.splitlines() if 'hook error' in l.lower() or 'econnrefused' in l.lower()]
        s.send('1')
        assert wait_until(lambda: (project / 'made-3.txt').exists(), 15)
        out['cases']['3 hook exits at once, no output'] = {'hook notices in the normal view': notices or 'none'}
        assert not notices
    finally:
        s.type_line('/exit')
        time.sleep(4)
        s.close()

    # 4) resume: does the session keep its id?
    s = launch(st, '--resume', sid, debug=BASE / 'debug-2.txt')
    try:
        st['ctrl'].write_text('silent')
        asked, mark = time.time(), s.mark()
        s.type_line('Run exactly this bash command and nothing else: touch made-4.txt')
        start = wait_until(lambda: next((e for e in entries(log, asked) if e['event'] == 'start'), None), 90)
        assert start
        assert s.wait_for(DIALOG, 30, since=mark)
        s.send('1')
        wait_until(lambda: (project / 'made-4.txt').exists(), 15)
        out['cases']['4 claude --resume <id>'] = {'same session id in the hook': start['session'] == sid}
        assert start['session'] == sid
    finally:
        s.type_line('/exit')
        time.sleep(4)
        s.close()

    (BASE / 'permission-hook.json').write_text(json.dumps(out, indent=2))
    print('\n' + json.dumps(out, indent=2))
    first = out['cases']['1 hook holds 20 s, person says Yes']
    assert first['hook started -> dialog on screen (s)'] < 3, 'the dialog waited for the hook'


# ---------------------------------------------------------------- the shipped hook, against a stand-in server
BASE2 = ROOT / 'tmp' / 'live' / 'perm-shipped'
NOTICE = re.compile(r'(?i)hook error|non-blocking status|timed out|econnrefused')
ALLOW = {'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': {'behavior': 'allow'}}}
DENY = {'hookSpecificOutput': {'hookEventName': 'PermissionRequest',
                               'decision': {'behavior': 'deny', 'message': 'Denied from Skyborne'}}}


def hung_up(conn):
    try:
        ready, _, _ = select.select([conn], [], [], 0)
        return bool(ready) and conn.recv(1, socket.MSG_PEEK) == b''
    except OSError:
        return True


class Stub:
    """Stands in for Skyborne: logs every hook and status line, and answers /permission as told:
    `hold` (until the test answers, or Claude Code hangs up), `empty`, `404`, or `deny:<seconds>`."""

    def __init__(self):
        self.log, self.lock, self.mode, self.answer, self.max_hold = [], threading.Lock(), 'hold', None, 300
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                try:
                    p = json.loads(body)
                except ValueError:
                    p = {}
                p = p if isinstance(p, dict) else {}
                if self.path != '/permission':
                    stub.note('statusline' if self.path == '/statusline' else 'hook', event=p.get('hook_event_name'),
                              tool=p.get('tool_name'), transcript=p.get('transcript_path'))
                    return self.reply(200)
                n = stub.note('ask', tool=p.get('tool_name'), agent=p.get('agent_id'), keys=sorted(p),
                              wait=self.headers.get('X-Skyborne-Wait'), entrypoint=self.headers.get('X-Skyborne-Entrypoint'),
                              transcript=p.get('transcript_path'))['n']
                mode = stub.mode
                if mode == '404':
                    return self.reply(404)
                if mode == 'empty':
                    return self.reply(200)
                if mode.startswith('deny:'):
                    time.sleep(float(mode[5:]))
                    stub.note('answered', ask=n, answer='deny')
                    return self.reply(200, DENY)
                end = time.monotonic() + stub.max_hold
                while time.monotonic() < end:
                    if hung_up(self.connection):
                        stub.note('closed', ask=n)
                        return
                    if stub.answer:
                        kind = stub.answer
                        stub.note('answered', ask=n, answer=kind)
                        return self.reply(200, ALLOW if kind == 'allow' else DENY if kind == 'deny-json' else None)
                    time.sleep(0.05)
                stub.note('gave up', ask=n)
                self.reply(200)

            def reply(self, code, data=None):
                body = json.dumps(data).encode() if data else b''
                try:
                    self.send_response(code)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    stub.note('write failed')

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def note(self, what, **k):
        with self.lock:
            e = {'t': time.time(), 'what': what, 'n': len(self.log), **k}
            self.log.append(e)
            return e

    def since(self, t, what=None):
        with self.lock:
            return [e for e in self.log if e['t'] >= t and (what is None or e['what'] == what)]

    def set(self, mode, answer=None):
        self.mode, self.answer = mode, answer

    def stop(self):
        self.answer = 'empty'
        time.sleep(0.3)
        self.httpd.shutdown()


def transcript_lines(path, since):
    """Shape only (types, ids, flags) of the transcript lines stamped after `since`."""
    out = []
    try:
        lines = pathlib.Path(path).read_text(errors='ignore').splitlines()
    except (OSError, TypeError):
        return out
    for line in lines:
        try:
            d = json.loads(line)
            ts = datetime.datetime.fromisoformat(d['timestamp'].replace('Z', '+00:00')).timestamp()
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if ts < since:
            continue
        row = {'t': round(ts - since, 2), 'type': d.get('type')}
        a = d.get('attachment')
        if isinstance(a, dict):
            row['attachment'] = {k: v for k, v in a.items() if k in ('type', 'decision', 'hookEvent')}
        content = (d.get('message') or {}).get('content') if isinstance(d.get('message'), dict) else None
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get('type') == 'tool_result':
                text = json.dumps(b.get('content'))
                row['tool_result'] = {'is_error': b.get('is_error'), 'says Denied from Skyborne': 'Denied from Skyborne' in text}
            elif isinstance(b, dict) and b.get('type') == 'tool_use':
                row['tool_use'] = b.get('name')
        if d.get('toolDenialKind'):
            row['toolDenialKind'] = d['toolDenialKind']
        out.append(row)
    return out


def hook_pids(port):
    r = subprocess.run(['pgrep', '-f', f'127.0.0.1:{port}/permission'], capture_output=True, text=True)
    return [int(x) for x in r.stdout.split()]


def notices(s, mark):
    return [l.strip()[:160] for l in s.text(mark).splitlines() if NOTICE.search(l)]


@pytest.fixture
def shipped(monkeypatch):
    from conftest import REAL_CLAUDE_CONFIG_DIR
    from skyborne import install
    if REAL_CLAUDE_CONFIG_DIR:
        monkeypatch.setenv('CLAUDE_CONFIG_DIR', REAL_CLAUDE_CONFIG_DIR)
    else:
        monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    project = BASE2 / 'project'
    (project / '.claude').mkdir(parents=True, exist_ok=True)
    (project / '.claude' / 'settings.local.json').write_text(json.dumps({'enabledPlugins': {'skyborne@skills-dir': False}}))
    for f in project.glob('made-*.txt'):
        f.unlink()
    stub = Stub()
    install.write_plugin(BASE2 / 'plugin', stub.port)                  # exactly what `skyborne install` writes
    install.write_plugin(BASE2 / 'plugin-10s', stub.port, timeout=10)   # the same, with a 10 s timeout
    install.write_plugin(BASE2 / 'plugin-down', closed_port())          # Skyborne not running
    (BASE2 / 'settings.json').write_text(json.dumps({'statusLine': {
        'type': 'command', 'command': f'curl -s -m 1 --data-binary @- http://127.0.0.1:{stub.port}/statusline'}}))
    yield {'project': project, 'stub': stub}
    stub.stop()


def start(st, plugin, *extra, debug):
    s = Session(['claude', '--plugin-dir', str(BASE2 / plugin), '--settings', str(BASE2 / 'settings.json'),
                 '--model', MODEL, '--debug-file', str(BASE2 / debug), *(extra or ('--permission-mode', 'default'))],
                cwd=str(st['project']), env=clean_env())
    if s.wait_for(r'(?i)trust (the files in )?this folder|do you trust', 20):
        s.send('\r')
    time.sleep(6)
    return s


def ask(s, stub, prompt, tool='Bash', dialog=DIALOG, timeout=90):
    """Type a prompt; return (ask log entry, time the dialog showed, screen mark)."""
    asked, mark = time.time(), s.mark()
    s.type_line(prompt)
    a = wait_until(lambda: next((e for e in stub.since(asked, 'ask') if e['tool'] == tool), None), timeout)
    seen = s.wait_for(dialog, 30, since=mark) if a else None
    return a, (time.time() if seen else None), mark


def finish(s):
    s.type_line('/exit')
    time.sleep(4)
    s.close()


def test_the_shipped_permission_hook(shipped):
    st, stub, project = shipped, shipped['stub'], shipped['project']
    out = {'claude': subprocess.run(['claude', '--version'], capture_output=True, text=True).stdout.strip(), 'cases': {}}
    made = lambda name: (project / name).exists()
    try:
        s = start(st, 'plugin', debug='debug-1.txt')
        try:
            # (a) + (b): held, then No in the terminal; then a late "allow" from the server
            stub.set('hold')
            a, shown, mark = ask(s, stub, 'Run exactly this bash command and nothing else: touch made-b.txt')
            assert a and shown, 'no PermissionRequest or no dialog (screen in debug-1.txt)'
            case = {'request keys': a['keys'], 'X-Skyborne-Wait': a['wait'], 'ask -> dialog (s)': round(shown - a['t'], 2)}
            time.sleep(1.5)
            s.send('3')
            no_at = time.time()
            closed = wait_until(lambda: next((e for e in stub.since(a['t'], 'closed') if e['ask'] == a['n']), None), 5)
            case['No -> connection closed (s)'] = round(closed['t'] - no_at, 2) if closed else 'not closed within 5 s'
            time.sleep(max(0, no_at + 3 - time.time()))
            case['hook processes alive 3 s after No'] = len(hook_pids(stub.port))
            stub.set('hold', 'allow')
            sent = wait_until(lambda: next((e for e in stub.since(no_at, 'answered') if e['ask'] == a['n']), None), 3)
            case['late allow delivered to the hook'] = bool(sent)
            time.sleep(5)
            case['command ran after the late allow (must be False)'] = made('made-b.txt')
            case['notices'] = notices(s, mark)
            out['cases']['a+b No, then a late allow'] = case
            assert not made('made-b.txt'), 'GO/NO-GO FAILED: a late allow ran a command the person refused'

            # (c) deny from the server after 2 s
            stub.set('deny:2')
            a, shown, mark = ask(s, stub, 'Run exactly this bash command and nothing else: touch made-c.txt')
            assert a
            time.sleep(10)
            screen = s.text(mark)
            out['cases']['c deny'] = {
                'dialog shown': bool(shown), 'command ran': made('made-c.txt'),
                'screen mentions Denied from Skyborne': 'Denied from Skyborne' in screen.replace('\n', ' ') or 'DeniedfromSkyborne' in screen,
                'hooks after the answer': [e['event'] for e in stub.since(a['t'] + 1.9, 'hook')],
                'transcript after the request': transcript_lines(a['transcript'], a['t'] - 0.5), 'notices': notices(s, mark)}
            assert not made('made-c.txt')

            # (e) an older Skyborne without /permission: 404
            stub.set('404')
            a, shown, mark = ask(s, stub, 'Run exactly this bash command and nothing else: touch made-e1.txt')
            assert a and shown
            time.sleep(2)
            case = {'notices before Yes': notices(s, mark)}
            s.send('1')
            case['Yes ran it'] = bool(wait_until(lambda: made('made-e1.txt'), 15))
            out['cases']['e server answers 404'] = case
            assert not case['notices before Yes'] and case['Yes ran it']

            # (h) AskUserQuestion: answered empty at once; the question must show as usual
            stub.set('empty')
            a, shown, mark = ask(s, stub, 'Use the AskUserQuestion tool to ask me one question: tea or coffee? '
                                          'Offer exactly those two options.', tool='AskUserQuestion',
                                 dialog=r'(?i)coffee', timeout=60)
            case = {'PermissionRequest fired': bool(a), 'question shown': bool(shown)}
            if a:
                case['request keys'] = a['keys']
            time.sleep(1.5)
            s.send('1')
            time.sleep(1)
            s.send('\r')
            time.sleep(6)
            case['notices'] = notices(s, mark)
            out['cases']['h AskUserQuestion'] = case
            s.send(ESC_KEY)
        finally:
            finish(s)

        # (g) claude -p: does PermissionRequest fire, and does the run wait for a held hook?
        stub.set('hold')
        stub.max_hold = 45
        t0 = time.time()
        try:
            r = subprocess.run(['claude', '-p', 'Run exactly this bash command and nothing else: touch made-g.txt',
                                '--plugin-dir', str(BASE2 / 'plugin'), '--permission-mode', 'default', '--model', MODEL],
                               cwd=str(project), env=clean_env(), capture_output=True, text=True, timeout=150)
            reply = r.stdout.strip()[:300]
        except subprocess.TimeoutExpired:
            reply = 'TIMED OUT after 150 s'
        asks = [e for e in stub.since(t0, 'ask')]
        out['cases']['g claude -p'] = {
            'PermissionRequest fired': bool(asks), 'run took (s)': round(time.time() - t0, 1), 'command ran': made('made-g.txt'),
            'held request ended': [e['what'] for e in stub.since(t0) if e['what'] in ('closed', 'answered', 'gave up')],
            'reply': reply}
        stub.max_hold = 300

        # (d) a 10 s hook timeout with no answer
        s = start(st, 'plugin-10s', debug='debug-2.txt')
        try:
            stub.set('hold')
            a, shown, mark = ask(s, stub, 'Run exactly this bash command and nothing else: touch made-d.txt')
            assert a and shown
            closed = wait_until(lambda: next((e for e in stub.since(a['t'], 'closed') if e['ask'] == a['n']), None), 20)
            time.sleep(2)
            case = {'ask -> connection closed (s)': round(closed['t'] - a['t'], 2) if closed else 'not closed in 20 s',
                    'hook processes alive after': len(hook_pids(stub.port)), 'notices': notices(s, mark)}
            s.send('1')
            case['dialog still answerable after the timeout'] = bool(wait_until(lambda: made('made-d.txt'), 15))
            out['cases']['d 10 s timeout'] = case
        finally:
            stub.set('hold', 'empty')
            finish(s)

        # (e) Skyborne not running at all
        s = start(st, 'plugin-down', debug='debug-3.txt')
        try:
            mark = s.mark()
            s.type_line('Run exactly this bash command and nothing else: touch made-e2.txt')
            shown = s.wait_for(DIALOG, 90, since=mark)
            time.sleep(2)
            case = {'dialog shown': bool(shown), 'notices': notices(s, mark)}
            s.send('1')
            case['Yes ran it'] = bool(wait_until(lambda: made('made-e2.txt'), 15))
            out['cases']['e Skyborne down'] = case
            assert shown and not case['notices'] and case['Yes ran it']
        finally:
            finish(s)

        # (h) ExitPlanMode in plan mode: answered empty at once; the plan dialog must show as usual
        s = start(st, 'plugin', '--permission-mode', 'plan', debug='debug-4.txt')
        try:
            stub.set('empty')
            a, shown, mark = ask(s, stub, 'Plan only this: create an empty file named made-h.txt. The plan is one line. '
                                          'Call ExitPlanMode with it right away.', tool='ExitPlanMode',
                                 dialog=r'(?i)proceed|approve|keep\s*planning', timeout=120)
            time.sleep(2)
            out['cases']['h ExitPlanMode'] = {'PermissionRequest fired': bool(a), 'plan dialog shown': bool(shown),
                                              'request keys': a['keys'] if a else None, 'notices': notices(s, mark)}
            s.send(ESC_KEY)
            time.sleep(2)
        finally:
            finish(s)
    finally:
        BASE2.mkdir(parents=True, exist_ok=True)
        (BASE2 / 'results.json').write_text(json.dumps(out, indent=2))
        print('\n' + json.dumps(out, indent=2))


def test_yes_on_a_long_foreground_command(shipped):
    """(f) After a "Yes" typed in the terminal, is there any sign of it before the tool finishes?"""
    st, stub, project = shipped, shipped['stub'], shipped['project']
    out = {}
    s = start(st, 'plugin', debug='debug-f.txt')
    try:
        stub.set('hold')
        a, shown, mark = ask(s, stub, 'Run exactly this bash command in the foreground (run_in_background false) and '
                                      "nothing else: python3 -c 'import time; time.sleep(20)' && touch made-f.txt")
        assert a and shown
        time.sleep(1)
        s.send('1')
        yes_at = time.time()
        wait_until(lambda: (project / 'made-f.txt').exists(), 45)
        done_at = time.time()
        time.sleep(2)
        out = {'command finished after Yes (s)': round(done_at - yes_at, 2),
               'held hook closed after Yes (s)': next((round(e['t'] - yes_at, 2) for e in stub.since(a['t'], 'closed')), None),
               'stub saw, from Yes on': [{'dt': round(e['t'] - yes_at, 2), 'what': e['what'], 'event': e.get('event')}
                                         for e in stub.since(yes_at)],
               'transcript from Yes on': transcript_lines(a['transcript'], yes_at), 'notices': notices(s, mark)}
        stub.set('hold', 'empty')
    finally:
        finish(s)
        BASE2.mkdir(parents=True, exist_ok=True)
        (BASE2 / 'results-f.json').write_text(json.dumps(out, indent=2))
        print('\n' + json.dumps(out, indent=2))
    assert out['command finished after Yes (s)'] > 15, 'the command did not run in the foreground'


def test_print_mode_is_told_apart(shipped):
    """The hook passes on $CLAUDE_CODE_ENTRYPOINT: what does it hold in an interactive session and in `claude -p`?"""
    st, stub = shipped, shipped['stub']
    out = {}
    stub.set('empty')
    s = start(st, 'plugin', debug='debug-entry.txt')
    try:
        a, shown, _ = ask(s, stub, 'Run exactly this bash command and nothing else: touch made-i.txt')
        out['interactive'] = a and a['entrypoint']
        s.send('3')
        time.sleep(2)
    finally:
        finish(s)
    t0 = time.time()
    subprocess.run(['claude', '-p', 'Run exactly this bash command and nothing else: touch made-p.txt',
                    '--plugin-dir', str(BASE2 / 'plugin'), '--permission-mode', 'default', '--model', MODEL],
                   cwd=str(st['project']), env=clean_env(), capture_output=True, text=True, timeout=150)
    asks = stub.since(t0, 'ask')
    out['claude -p'] = asks[0]['entrypoint'] if asks else 'no PermissionRequest'
    (BASE2 / 'results-entrypoint.json').write_text(json.dumps(out, indent=2))
    print('\n' + json.dumps(out, indent=2))
    assert out['interactive'] == 'cli' and str(out['claude -p']).startswith('sdk-')


def test_a_late_answer_after_a_yes(shipped):
    """A "Yes" typed in the terminal doesn't stop the hook, so a page answer can still reach it while the
    tool runs. Does a late "deny" then change anything?"""
    st, stub, project = shipped, shipped['stub'], shipped['project']
    out = {}
    s = start(st, 'plugin', debug='debug-late.txt')
    try:
        stub.set('hold')
        a, shown, mark = ask(s, stub, 'Run exactly this bash command in the foreground (run_in_background false) and '
                                      "nothing else: python3 -c 'import time; time.sleep(12)' && touch made-late.txt")
        assert a and shown
        time.sleep(1)
        s.send('1')
        yes_at = time.time()
        time.sleep(3)
        stub.set('hold', 'deny-json')
        delivered = wait_until(lambda: next((e for e in stub.since(yes_at, 'answered') if e['ask'] == a['n']), None), 3)
        ran = wait_until(lambda: (project / 'made-late.txt').exists(), 25)
        time.sleep(5)
        screen = s.text(mark)
        out = {'late deny delivered to the hook': bool(delivered), 'the command still finished': bool(ran),
               'screen mentions Denied from Skyborne': 'Denied from Skyborne' in screen or 'DeniedfromSkyborne' in screen,
               'transcript from Yes on': transcript_lines(a['transcript'], yes_at), 'notices': notices(s, mark)}
    finally:
        stub.set('hold', 'empty')
        finish(s)
        (BASE2 / 'results-late.json').write_text(json.dumps(out, indent=2))
        print('\n' + json.dumps(out, indent=2))
