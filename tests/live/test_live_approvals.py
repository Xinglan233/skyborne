"""Opt-in live test: answering real Claude Code permission prompts from Skyborne (macOS and
Linux; spends a little usage).

    SKYBORNE_LIVE=1 .venv/bin/python -m pytest tests/live/test_live_approvals.py -s

A real Skyborne server runs on a free port with the plugin `skyborne install` writes, and a real `claude`
runs in a pseudo-terminal in tmp/live/approvals/project (--permission-mode default, so the dialog shows).
The page's answers are sent the way the page sends them: POST /api/answer with the launch token and the
server's own Origin. Cases: approve from Skyborne; deny from Skyborne; Yes typed in the terminal first; a
click after a Yes on a slow command (shown as too late); No typed in the terminal first; a helper's
request; `claude -p` (never held); Skyborne stopped. Timings and
the decision log are printed and saved to tmp/live/approvals/results.json.
"""
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.skipif(os.environ.get('SKYBORNE_LIVE') != '1' or sys.platform == 'win32',
                                reason='live tests run only with SKYBORNE_LIVE=1 on macOS or Linux')

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
if sys.platform != 'win32':
    from driver import Session, clean_env  # noqa: E402

BASE = ROOT / 'tmp' / 'live' / 'approvals'
MODEL = os.environ.get('SKYBORNE_LIVE_MODEL', 'claude-haiku-4-5-20251001')
DIALOG = r'(?i)do\s*you\s*want\s*to\s*proceed'
NOTICE = r'(?i)hook error|non-blocking status|econnrefused'


def wait_until(check, timeout, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = check()
        if v:
            return v
        time.sleep(step)
    return None


@pytest.fixture
def live(monkeypatch):
    from conftest import REAL_CLAUDE_CONFIG_DIR
    from skyborne import install
    from skyborne.server import App
    if REAL_CLAUDE_CONFIG_DIR:
        monkeypatch.setenv('CLAUDE_CONFIG_DIR', REAL_CLAUDE_CONFIG_DIR)
    else:
        monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    project = BASE / 'project'
    (project / '.claude').mkdir(parents=True, exist_ok=True)
    (project / '.claude' / 'settings.local.json').write_text(json.dumps({'enabledPlugins': {'skyborne@skills-dir': False}}))
    for f in project.glob('made-*.txt'):
        f.unlink()
    for f in BASE.glob('skyborne.db*'):
        f.unlink()
    app = App(port=0, db_path=BASE / 'skyborne.db')
    app.start()
    install.write_plugin(BASE / 'plugin', app.port)
    yield {'app': app, 'project': project}
    app.stop()


def start_claude(project, debug):
    s = Session(['claude', '--plugin-dir', str(BASE / 'plugin'), '--permission-mode', 'default', '--model', MODEL,
                 '--debug-file', str(BASE / debug)], cwd=str(project), env=clean_env())
    if s.wait_for(r'(?i)trust (the files in )?this folder|do you trust', 20):
        s.send('\r')
    time.sleep(6)
    return s


def answer(app, ask_id, decision):
    """As the page does it: the launch token and the server's own Origin."""
    req = urllib.request.Request(f'http://127.0.0.1:{app.port}/api/answer', method='POST',
                                 data=json.dumps({'id': ask_id, 'decision': decision}).encode(),
                                 headers={'Content-Type': 'application/json', 'X-Skyborne-Token': app.token,
                                          'Origin': f'http://127.0.0.1:{app.port}', 'Host': f'127.0.0.1:{app.port}'})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def card(app, check=lambda c: True, timeout=90):
    return wait_until(lambda: next((c for c in app.approvals.snapshot()['asks'] if check(c)), None), timeout, 0.05)


def gone(app, ask_id, timeout=5):
    return wait_until(lambda: all(c['id'] != ask_id for c in app.approvals.snapshot()['asks']), timeout)


class Answers:
    """Every `answer` the server broadcasts (Claude Code's word on an answer from the page)."""

    def __init__(self, app):
        self.got, q = [], app.hub.subscribe()

        def run():
            while True:
                msg = q.get().decode()
                if msg.startswith('event: answer'):
                    self.got.append(json.loads(msg.split('data: ', 1)[1]))
        threading.Thread(target=run, daemon=True).start()

    def of(self, ask_id, timeout):
        return wait_until(lambda: next((a for a in self.got if a['id'] == ask_id), None), timeout, 0.05)


def notices(s, mark):
    import re
    return [l.strip()[:160] for l in s.text(mark).splitlines() if re.search(NOTICE, l)]


def test_answering_real_permission_prompts(live):
    app, project = live['app'], live['project']
    made = lambda name: (project / name).exists()
    out = {'claude': subprocess.run(['claude', '--version'], capture_output=True, text=True).stdout.strip(), 'cases': {}}
    answers = Answers(app)
    try:
        s = start_claude(project, 'debug-1.txt')
        try:
            # 1) approve from Skyborne: the dialog closes and the command runs
            mark, asked = s.mark(), time.time()
            s.type_line('Run exactly this bash command and nothing else: touch made-1.txt')
            c = card(app, lambda c: 'made-1' in json.dumps(c['input']))
            assert c, 'no card for the first request'
            shown = s.wait_for(DIALOG, 20, since=mark)
            case = {'prompt -> card (s)': round(time.time() - asked, 2), 'dialog shown too': bool(shown), 'card agent': c['agent']}
            time.sleep(1)
            t = time.time()
            case['answer status'] = answer(app, c['id'], 'allow')
            case['card while waiting for Claude Code'] = next((x['state'] for x in app.approvals.snapshot()['asks'] if x['id'] == c['id']), 'gone')
            case['command ran'] = bool(wait_until(lambda: made('made-1.txt'), 15))
            case['approve -> command ran (s)'] = round(time.time() - t, 2)
            said = answers.of(c['id'], 5)
            case['Claude Code confirmed (applied)'] = said and said['applied']
            case['approve -> card closed as applied (s)'] = round(time.time() - t, 2) if said else None
            case['notices'] = notices(s, mark)
            out['cases']['1 approve from Skyborne'] = case
            assert case['answer status'] == 200 and case['command ran'] and not case['notices'] and case['Claude Code confirmed (applied)'] is True
            time.sleep(4)

            # 2) deny from Skyborne: Claude is told "Denied from Skyborne"
            mark = s.mark()
            s.type_line('Run exactly this bash command and nothing else: touch made-2.txt')
            c = card(app, lambda c: 'made-2' in json.dumps(c['input']))
            assert c
            s.wait_for(DIALOG, 20, since=mark)
            time.sleep(1)
            case = {'answer status': answer(app, c['id'], 'deny')}
            time.sleep(8)
            screen = s.text(mark)
            case['command ran'] = made('made-2.txt')
            case['Claude saw "Denied from Skyborne"'] = 'Denied from Skyborne' in screen or 'DeniedfromSkyborne' in screen
            case['notices'] = notices(s, mark)
            out['cases']['2 deny from Skyborne'] = case
            assert case['answer status'] == 200 and not case['command ran'] and case['Claude saw "Denied from Skyborne"']
            time.sleep(3)

            # 3) Yes typed in the terminal first: the card clears within 1 s
            mark = s.mark()
            s.type_line('Run exactly this bash command and nothing else: touch made-3.txt')
            c = card(app, lambda c: 'made-3' in json.dumps(c['input']))
            assert c and s.wait_for(DIALOG, 20, since=mark)
            time.sleep(1)
            s.send('1')
            t = time.time()
            cleared = gone(app, c['id'])
            case = {'Yes -> card cleared (s)': round(time.time() - t, 2) if cleared else None, 'command ran': bool(wait_until(lambda: made('made-3.txt'), 10)),
                    'a late page answer': answer(app, c['id'], 'deny')}
            out['cases']['3 Yes in the terminal first'] = case
            assert cleared and case['Yes -> card cleared (s)'] < 1 and case['command ran'] and case['a late page answer'] == 409
            time.sleep(4)

            # 3b) a click while the command runs after a Yes in the terminal: sent, then shown as too late
            mark = s.mark()
            s.type_line('Run exactly this bash command in the foreground (run_in_background false) and nothing else: '
                        "python3 -c 'import time; time.sleep(10)' && touch made-3b.txt")
            c = card(app, lambda c: 'made-3b' in json.dumps(c['input']))
            assert c and s.wait_for(DIALOG, 20, since=mark)
            time.sleep(1)
            s.send('1')
            time.sleep(2)
            case = {'click while it runs': answer(app, c['id'], 'allow')}
            said = answers.of(c['id'], 25)
            case['Claude Code said'] = said and {'applied': said['applied']}
            case['command ran'] = made('made-3b.txt')
            out['cases']['3b a click after a Yes, while the command runs'] = case
            assert case['click while it runs'] == 200 and said and said['applied'] is False and case['command ran']
            time.sleep(4)

            # 4) No typed in the terminal first: the card clears within 1 s
            mark = s.mark()
            s.type_line('Run exactly this bash command and nothing else: touch made-4.txt')
            c = card(app, lambda c: 'made-4' in json.dumps(c['input']))
            assert c and s.wait_for(DIALOG, 20, since=mark)
            time.sleep(1)
            s.send('3')
            t = time.time()
            cleared = gone(app, c['id'])
            case = {'No -> card cleared (s)': round(time.time() - t, 2) if cleared else None, 'a late page answer': answer(app, c['id'], 'allow')}
            time.sleep(3)
            case['command ran (must be False)'] = made('made-4.txt')
            out['cases']['4 No in the terminal first'] = case
            assert cleared and case['No -> card cleared (s)'] < 1 and not case['command ran (must be False)'] and case['a late page answer'] == 409

            # 5) a helper's request: the card names the helper, and approving it works
            s.type_line('Use the Agent tool once, with subagent_type general-purpose, and tell the helper exactly this: '
                        '"Run exactly this bash command and nothing else: touch made-5.txt". Do nothing else yourself.')
            c = card(app, lambda c: 'made-5' in json.dumps(c['input']), timeout=150)
            assert c, 'no card for the helper'
            time.sleep(1)
            helper = next((a for a in (app.hub.docs.get(c['session']) or {}).get('agents', []) if a['id'] == c['agent']), {})
            case = {'card agent is a helper': c['agent'] != 'main', 'helper in the session doc': helper.get('name'),
                    'answer status': answer(app, c['id'], 'allow')}
            case['command ran'] = bool(wait_until(lambda: made('made-5.txt'), 20))
            out['cases']['5 a helper asks'] = case
            assert case['card agent is a helper'] and case['answer status'] == 200 and case['command ran']
            time.sleep(10)
        finally:
            s.type_line('/exit')
            time.sleep(4)
            s.close()

        # 6) claude -p: never held, so it ends as quickly as without Skyborne
        t = time.time()
        r = subprocess.run(['claude', '-p', 'Run exactly this bash command and nothing else: touch made-6.txt',
                            '--plugin-dir', str(BASE / 'plugin'), '--permission-mode', 'default', '--model', MODEL],
                           cwd=str(project), env=clean_env(), capture_output=True, text=True, timeout=150)
        case = {'run took (s)': round(time.time() - t, 1), 'command ran': made('made-6.txt'), 'exit code': r.returncode}
        out['cases']['6 claude -p'] = case
        assert case['run took (s)'] < 40 and not case['command ran']

        # 7) Skyborne stopped: the terminal dialog works exactly as without it, and nothing shows
        app.stop()
        s = start_claude(project, 'debug-2.txt')
        try:
            mark = s.mark()
            s.type_line('Run exactly this bash command and nothing else: touch made-7.txt')
            shown = s.wait_for(DIALOG, 90, since=mark)
            time.sleep(2)
            case = {'dialog shown': bool(shown), 'notices': notices(s, mark)}
            s.send('1')
            case['Yes ran it'] = bool(wait_until(lambda: made('made-7.txt'), 15))
            out['cases']['7 Skyborne stopped'] = case
            assert shown and not case['notices'] and case['Yes ran it']
        finally:
            s.type_line('/exit')
            time.sleep(4)
            s.close()
    finally:
        try:
            out['decisions'] = [{k: r[k] for k in ('tool_name', 'agent_id', 'decision', 'answered_in', 'how', 'confirmed', 'waited_ms')}
                                for r in app.store.decisions()]
        except Exception as e:  # the results still get saved
            out['decisions'] = f'unreadable: {e}'
        BASE.mkdir(parents=True, exist_ok=True)
        (BASE / 'results.json').write_text(json.dumps(out, indent=2))
        print('\n' + json.dumps(out, indent=2))
