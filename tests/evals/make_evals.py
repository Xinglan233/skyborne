"""Build the reducer evals in this folder from the scrubbed real payloads in tests/fixtures/.

    python tests/evals/make_evals.py

Each eval is a list of hook events (real payload samples, re-timed and moved into one session),
optional transcript/status-line facts, and the document the reducer must produce. Expected
documents are written by the reducer itself, so only re-run this on purpose, and read the diff:
test_reducer.py checks each rule by hand as well.
"""
import copy
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from skyborne.reducer import Session, reduce  # noqa: E402
from skyborne.transcripts import facts_from_line, meta_fact  # noqa: E402

FIX = ROOT / 'tests' / 'fixtures'
OUT = pathlib.Path(__file__).resolve().parent
SID = '00000000-0000-4000-8000-0000000000aa'
NOW = 2_000_000


def sample(name):
    p = json.loads((FIX / 'payloads' / name).read_text(encoding='utf-8'))
    p.pop('_source', None)
    return p


def ev(ts, name, **changes):
    p = sample(name)
    p.update(changes)
    p['session_id'] = SID
    for k in [k for k, v in changes.items() if v is None]:
        del p[k]
    return {'ts': ts, 'payload': p}


def raw(ts, **payload):
    return {'ts': ts, 'payload': {'session_id': SID, 'cwd': '/home/user/project', **payload}}


def usage(ts, mid, agent='main', model='claude-haiku-4-5-20251001', i=10, o=100, cw=50, cr=1000):
    return {'ts': ts, 'session_id': SID, 'kind': 'usage', 'message_id': mid, 'agent': agent, 'model': model, 'final': True,
            'usage': {'in': i, 'out': o, 'cw': cw, 'cr': cr}}


# the Agent launch sample names the helper and the call that launched it
LAUNCH = sample('PostToolUse-Agent-async-launch.json')
HELPER, AGENT_CALL = LAUNCH['tool_response']['agentId'], LAUNCH['tool_use_id']
INSIDE = sample('PreToolUse-inside-helper.json')
BG = sample('Stop-with-background-helpers.json')['background_tasks']
HELPER2 = BG[1]['id']
NOTE = sample('UserPromptSubmit-task-notification.json')['prompt']
T1 = sample('PreToolUse.json')['tool_use_id']

TURN = [
    ev(1000, 'SessionStart-cmdhook.json'),
    ev(2000, 'UserPromptSubmit.json'),
    ev(3000, 'PreToolUse.json'),
    ev(3010, 'PermissionRequest.json'),
    ev(9000, 'PostToolUse.json'),
    ev(9500, 'Stop.json'),
]
STATUS = {'ts': 9600, 'session_id': SID, 'kind': 'statusline', 'payload': sample('statusLine-with-rate-limits.json')}
WAITING = TURN[:4]

HELPERS = [
    ev(1000, 'SessionStart-cmdhook.json'),
    ev(1500, 'UserPromptSubmit.json', prompt='Count the lines in alpha.txt with a helper'),
    ev(5430, 'PreToolUse.json', tool_name='Agent', tool_input=LAUNCH['tool_input'], tool_use_id=AGENT_CALL),
    ev(5440, 'SubagentStart.json'),
    ev(5460, 'PostToolUse-Agent-async-launch.json'),
    ev(7330, 'PreToolUse-inside-helper.json'),
    ev(7340, 'PostToolUse.json', tool_name='Read', tool_input=INSIDE['tool_input'], tool_use_id=INSIDE['tool_use_id'],
       agent_id=HELPER, agent_type='Explore', duration_ms=2, tool_response={'type': 'text'}),
    ev(8200, 'Stop-with-background-helpers.json'),
    ev(8760, 'SubagentStop.json', agent_id=HELPER, agent_type='Explore', last_assistant_message='4'),
    ev(8780, 'UserPromptSubmit-task-notification.json'),
    ev(9070, 'SubagentStop.json', agent_id=HELPER2, agent_type='Explore', last_assistant_message='pineapple'),
    ev(10190, 'Stop.json', last_assistant_message='Alpha.txt has 4 lines.'),
]


def transcript_facts(name='s2', sid=SID):
    facts = []
    base = FIX / 'transcripts' / name
    for f, agent in [(base / 'session.jsonl', 'main')] + [(p, p.name[6:-6]) for p in sorted((base / 'subagents').glob('*.jsonl'))]:
        for line in f.read_text(encoding='utf-8').splitlines():
            facts += facts_from_line(json.loads(line), sid, agent, 0)
    for m in sorted((base / 'subagents').glob('*.meta.json')):
        fact = meta_fact(m, sid)
        fact['ts'] = 0  # file times differ per checkout
        facts.append(fact)
    return facts


NEW_SID = '00000000-0000-4000-8000-0000000000bb'


def iso(ts):
    """A transcript line's time for `ts` ms."""
    return datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def line_facts(lines, read_at=0):
    """Facts from transcript lines (the real shapes, scrubbed), as the server's tailer makes them: `seq` is the
    line's place in the file, `read_at` stands for when it was read."""
    out = []
    for seq, line in enumerate(lines):
        out += facts_from_line({'sessionId': SID, **line}, SID, 'main', read_at, seq)
    return out


# what the server stores when a transcript says the conversation went on in another session
def continued_end(ts, new=NEW_SID):
    return {'ts': ts, 'payload': {'session_id': SID, 'hook_event_name': 'SessionEnd', 'reason': 'continued', 'continued_in': new},
            'source': 'transcript'}


EVALS = {
    'turn-with-tool': ('A whole turn: prompt, an approved Bash call, answer. Duration from duration_ms; tokens and cost from facts.',
                       TURN, [usage(9400, 'm1'), STATUS]),
    'waiting-open': ('A PermissionRequest matched to its PreToolUse puts the lead in "needs you".', WAITING, []),
    'waiting-cleared-by-terminal-no': ('A terminal "No" fires no hook; the transcript\'s error tool_result ends the wait.',
                                       WAITING, [{'ts': 5000, 'session_id': SID, 'kind': 'tool_result', 'tool_use_id': T1, 'is_error': True}]),
    'waiting-cleared-by-next-prompt': ('The next real prompt ends any wait.', WAITING + [ev(6000, 'UserPromptSubmit.json', prompt='Never mind')], []),
    'waiting-cleared-by-session-end': ('SessionEnd ends any wait.', WAITING + [ev(7000, 'SessionEnd.json')], []),
    'duration-fallback': ('Without duration_ms, the duration is PostToolUse time minus PreToolUse time.',
                          [ev(1000, 'PreToolUse.json'), ev(1450, 'PostToolUse.json', duration_ms=None)], []),
    'helpers': ('Helpers: linked to their Agent call, done at SubagentStop, task notifications are not turns, '
                'an unknown SubagentStop is ignored.', HELPERS, []),
    'helpers-midway': ('A Stop while helpers still run leaves the lead working.', HELPERS[:8], []),
    'internal-agent-ignored': ('A SubagentStop with no SubagentStart is one of Claude Code\'s own agents: ignored.',
                               [ev(1000, 'SessionStart-cmdhook.json'), ev(2000, 'SubagentStop-internal-agent.json')], []),
    'out-of-order-arrival': ('Events that arrive out of order: PostToolUse before PreToolUse, PermissionRequest before PreToolUse.',
                             [ev(1000, 'UserPromptSubmit.json'), ev(2995, 'PermissionRequest.json'), ev(3000, 'PostToolUse.json'),
                              ev(3004, 'PreToolUse.json')], []),
    'models': ('Model history per agent: SessionStart, then transcript models; <synthetic> skipped, repeats collapsed.',
               [ev(1000, 'SessionStart-cmdhook.json')],
               [usage(2000, 'm1', model='claude-sonnet-5-5'), usage(3000, 'm2', model='<synthetic>', i=0, o=0, cw=0, cr=0),
                usage(4000, 'm3', model='claude-sonnet-5-5'), usage(5000, 'm4', model='claude-opus-5-5')]),
    'stop-failure': ('A turn that ends on an API error.',
                     [ev(1000, 'UserPromptSubmit.json'), raw(2000, hook_event_name='StopFailure', error='rate_limit')], []),
    'idle-notification': ('Notification idle_prompt puts the lead back to idle.',
                          TURN + [ev(70000, 'Notification.json', notification_type='idle_prompt', message='Claude is waiting for your input')], []),
    'permission-denied': ('Auto mode refusing a call.',
                          [ev(1000, 'PreToolUse.json', tool_input=sample('PermissionDenied.json')['tool_input'],
                              tool_use_id=sample('PermissionDenied.json')['tool_use_id']), ev(1980, 'PermissionDenied.json')], []),
    'tool-failure': ('A failed tool call.',
                     [ev(1000, 'PreToolUse.json', tool_input=sample('PostToolUseFailure.json')['tool_input'],
                         tool_use_id=sample('PostToolUseFailure.json')['tool_use_id']), ev(1025, 'PostToolUseFailure.json')], []),
    'compaction': ('/compact: summarising, then idle; the compact SessionStart is not a new arrival.',
                   [ev(1000, 'SessionStart-cmdhook.json'), ev(2000, 'PreCompact.json'), ev(17000, 'PostCompact.json'),
                    ev(17001, 'SessionStart-cmdhook.json', source='compact')], []),
    'files-edited': ('Edits: two Edits of one file (one failed), a Write, a NotebookEdit; a Read is not an edit; a call '
                     'still open when the session ended stops there.',
                     [ev(1000, 'SessionStart-cmdhook.json'), ev(1500, 'UserPromptSubmit.json', prompt='Tidy the notes'),
                      ev(2000, 'PreToolUse.json', tool_name='Edit', tool_input={'file_path': '/home/user/project/notes.md', 'old_string': 'a', 'new_string': 'b'}, tool_use_id='E1'),
                      ev(2100, 'PostToolUse.json', tool_name='Edit', tool_input={'file_path': '/home/user/project/notes.md', 'old_string': 'a', 'new_string': 'b'}, tool_use_id='E1', duration_ms=40, tool_response={'filePath': '/home/user/project/notes.md'}),
                      ev(3000, 'PreToolUse.json', tool_name='Edit', tool_input={'file_path': '/home/user/project/notes.md', 'old_string': 'x', 'new_string': 'y'}, tool_use_id='E2'),
                      ev(3050, 'PostToolUseFailure.json', tool_name='Edit', tool_input={'file_path': '/home/user/project/notes.md', 'old_string': 'x', 'new_string': 'y'}, tool_use_id='E2', error='String to replace not found in file.'),
                      ev(4000, 'PreToolUse.json', tool_name='Write', tool_input={'file_path': '/home/user/project/todo.md', 'content': '- tea'}, tool_use_id='W1'),
                      ev(4200, 'PostToolUse.json', tool_name='Write', tool_input={'file_path': '/home/user/project/todo.md', 'content': '- tea'}, tool_use_id='W1', duration_ms=None, tool_response={'type': 'create'}),
                      ev(5000, 'PreToolUse.json', tool_name='NotebookEdit', tool_input={'notebook_path': '/home/user/project/a.ipynb', 'new_source': 'print(1)'}, tool_use_id='N1'),
                      ev(5300, 'PostToolUse.json', tool_name='NotebookEdit', tool_input={'notebook_path': '/home/user/project/a.ipynb', 'new_source': 'print(1)'}, tool_use_id='N1', duration_ms=12, tool_response={}),
                      ev(6000, 'PreToolUse.json', tool_name='Read', tool_input={'file_path': '/home/user/project/notes.md'}, tool_use_id='R1'),
                      ev(6100, 'PostToolUse.json', tool_name='Read', tool_input={'file_path': '/home/user/project/notes.md'}, tool_use_id='R1', duration_ms=3, tool_response={'type': 'text'}),
                      ev(7000, 'PreToolUse.json', tool_use_id='B1'),
                      ev(9000, 'SessionEnd.json')], []),
    'continued-elsewhere': ('Moved to the background: the transcript\'s continued-in line (stored as a SessionEnd) ends the '
                            'session, and the hook that came a moment later doesn\'t wake it.',
                            [ev(1000, 'SessionStart-cmdhook.json'), ev(2000, 'UserPromptSubmit.json', prompt='Make the film'),
                             ev(3000, 'PreToolUse.json'), ev(3500, 'PostToolUse.json'),
                             ev(6000, 'UserPromptSubmit.json', prompt='Love it, now add the voice'),
                             continued_end(15400), ev(16100, 'Notification.json')], []),
    'continued-then-exit': ('/bg typed in the terminal: Claude Code writes continued-in, then half a second later sends '
                            'SessionEnd (prompt_input_exit) and quits. The hand-over is how it ended.',
                            [ev(1000, 'SessionStart-cmdhook.json'), ev(2000, 'UserPromptSubmit.json', prompt='Make the film'),
                             ev(4000, 'Stop.json', last_assistant_message='Done'), continued_end(15400),
                             ev(15886, 'SessionEnd.json', reason='prompt_input_exit')], []),
    'fork-counts-own-work': ('A fork (here, a conversation moved to the background) starts with a copy of the conversation, '
                             'old times and all: only what it did after its SessionStart counts, and a copied continued-in '
                             'line doesn\'t end it. Its name is Claude\'s newest title.',
                             [ev(50000, 'SessionStart-cmdhook.json', source='fork', session_title=None), continued_end(30000, new=SID),
                              ev(51000, 'PreToolUse.json'), ev(52000, 'PostToolUse.json')],
                             [usage(10000, 'msg_copied_1', o=5000), usage(20000, 'msg_copied_2', o=7000),
                              usage(51500, 'msg_own_1'), usage(52500, 'msg_own_2')]
                             + line_facts([{'type': 'ai-title', 'aiTitle': 'film-voice'}, {'type': 'ai-title', 'aiTitle': 'film-voice-retime'}],
                                          read_at=90000)),
    'interrupted-turn': ('Esc during a command: the call fails as interrupted, and the transcript\'s interrupt line ends the '
                         'turn (no Stop fires). A name set with /rename beats Claude\'s own title.',
                         [ev(1000, 'SessionStart-cmdhook.json'), ev(2000, 'UserPromptSubmit.json'), ev(3000, 'PreToolUse.json'),
                          ev(4000, 'PostToolUseFailure.json', tool_use_id=T1, tool_input=sample('PreToolUse.json')['tool_input'],
                             is_interrupt=True, error='[Request interrupted by user for tool use]')],
                         line_facts([{'type': 'custom-title', 'customTitle': 'Tidy notes'}, {'type': 'ai-title', 'aiTitle': 'notes-cleanup'},
                                     {'type': 'user', 'timestamp': iso(4100), 'isSidechain': False,
                                      'message': {'role': 'user', 'content': [{'type': 'text', 'text': '[Request interrupted by user for tool use]'}]}}],
                                    read_at=90000)),
    'tokens-from-transcripts': ('Tokens from a real session\'s transcripts, each message counted once: 426,143 for the lead '
                                'and 517,687 with its helpers.', [ev(1000, 'SessionStart-cmdhook.json')], None),
}


def recorded(name):
    """A session recorded by the live tests (scrubbed): every event as Skyborne stored it, plus its transcripts."""
    events = json.loads((FIX / 'recordings' / f'{name}.json').read_text(encoding='utf-8'))['events']
    sid = events[0]['payload']['session_id']
    return events, transcript_facts(name, sid)


def main():
    EVALS['recorded-live-session'] = ('A real session recorded by tests/live: two helpers in parallel, an approved command '
                                      'and one refused with a "No" in the terminal.', *recorded('live-session'))
    for name, (description, events, facts) in EVALS.items():
        facts = transcript_facts() if facts is None else facts
        events, facts = copy.deepcopy(events), copy.deepcopy(facts)
        expect = reduce(events, facts, NOW)
        data = {'name': name, 'description': description, 'now': NOW, 'events': events, 'facts': facts, 'expect': expect}
        (OUT / f'{name}.json').write_text(json.dumps(data, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
        print(f'{name}: {len(events)} events, {len(facts)} facts')
        # the session detail (GET /api/session) of the same events, in a subfolder test_reducer.py doesn't read
        (OUT / 'detail').mkdir(exist_ok=True)
        detail = {'name': name, 'expect': detail_of(events, facts)}
        (OUT / 'detail' / f'{name}.json').write_text(json.dumps(detail, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')


def detail_of(events, facts):
    """Session.detail() of an eval's one session, fed as the server feeds it."""
    [sid] = reduce(events, facts, NOW)
    s = Session(sid)
    for e in copy.deepcopy(events):
        s.add_event(e)
    for f in copy.deepcopy(facts):
        s.add_fact(f)
    return s.detail()


if __name__ == '__main__':
    main()
