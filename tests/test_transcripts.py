"""The transcript tailer: counting each message once, partial lines, truncated files, helper sidecars."""
import json
import shutil

from conftest import FIX
from skyborne.reducer import Session
from skyborne.transcripts import Tailer

S2 = FIX / 'transcripts' / 's2'


def tokens(u):
    return u['in'] + u['out'] + u['cw'] + u['cr']


def tail(tmp_path, sid='s2'):
    """A tailer following a copy of the recorded session (as Claude Code would have left it)."""
    copy = tmp_path / 'project'
    copy.mkdir()
    shutil.copy(S2 / 'session.jsonl', copy / 'session.jsonl')
    shutil.copytree(S2 / 'subagents', copy / 'session' / 'subagents')
    got = []
    t = Tailer(got.extend)
    t.watch(sid, str(copy / 'session.jsonl'))
    t.poll()
    return t, got, copy


def test_each_message_counted_once_from_its_last_line(tmp_path):
    _, facts, _ = tail(tmp_path)
    main = [f for f in facts if f['kind'] == 'usage' and f['agent'] == 'main']
    assert sum(tokens(f['usage']) for f in main) == 889_376  # what summing every line gives
    last = {}
    for f in main:
        last[f['message_id']] = f['usage']
    assert sum(tokens(u) for u in last.values()) == 426_143
    s = Session('s2')
    for f in facts:
        s.add_fact(f)
    doc = s.doc()
    assert sum(doc['agents'][0]['usage'].values()) == 426_143
    assert doc['tokens']['total'] == 517_687  # with the three helpers' own transcripts


def test_helper_sidecars_become_meta_facts(tmp_path):
    _, facts, _ = tail(tmp_path)
    meta = sorted((f['agent_id'], f['description']) for f in facts if f['kind'] == 'meta')
    assert [d for _, d in meta] == sorted(['count alpha lines', 'find beta word', 'list sample files'])
    assert all(f['tool_use_id'].startswith('toolu_') for f in facts if f['kind'] == 'meta')


def test_task_notifications_and_tool_results(tmp_path):
    _, facts, _ = tail(tmp_path)
    notes = [f for f in facts if f['kind'] == 'task_notification']
    assert notes and all(n['status'] == 'completed' and n['task_id'] for n in notes)
    assert any(f['kind'] == 'tool_result' for f in facts)


def test_a_partial_line_waits_for_the_rest(tmp_path):
    path = tmp_path / 't.jsonl'
    line = json.dumps({'type': 'assistant', 'timestamp': '2026-10-03T03:30:44.251Z',
                       'message': {'id': 'm1', 'model': 'x', 'stop_reason': 'end_turn',
                                   'usage': {'input_tokens': 1, 'output_tokens': 2}}}) + '\n'
    path.write_text(line[:30])
    got = []
    t = Tailer(got.extend)
    t.watch('s', str(path))
    t.poll()
    assert got == []
    with open(path, 'a') as f:
        f.write(line[30:])
    t.poll()
    assert [g['message_id'] for g in got] == ['m1']
    t.poll()
    assert len(got) == 1  # nothing read twice


def test_broken_lines_are_skipped_and_truncation_starts_over(tmp_path):
    path = tmp_path / 't.jsonl'
    good = json.dumps({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_x', 'is_error': True}]}})
    path.write_text('{not json\n' + good + '\n')
    got = []
    t = Tailer(got.extend)
    t.watch('s', str(path))
    t.poll()
    assert [(g['kind'], g['tool_use_id'], g['is_error']) for g in got] == [('tool_result', 'toolu_x', True)]
    path.write_text(good + '\n')  # shorter than before: replaced
    t.poll()
    assert len(got) == 2


def test_missing_files_are_harmless(tmp_path):
    got = []
    t = Tailer(got.extend)
    t.watch('s', str(tmp_path / 'nope.jsonl'))
    t.poll()
    assert got == []


def test_a_hook_decision_line_becomes_a_hook_decision_fact():
    from skyborne.transcripts import facts_from_line
    line = {'type': 'attachment', 'timestamp': '2026-10-03T10:00:00.000Z', 'sessionId': 's',
            'attachment': {'type': 'hook_permission_decision', 'decision': 'allow', 'toolUseID': 'toolu_1',
                           'hookEvent': 'PermissionRequest'}}
    [f] = facts_from_line(line, 's', 'main', 0)
    assert f == {'ts': 1791021600000, 'session_id': 's', 'kind': 'hook_decision', 'tool_use_id': 'toolu_1', 'decision': 'allow'}
    for broken in ({'decision': 'maybe'}, {'toolUseID': None}, {'hookEvent': 'PreToolUse'}, {'type': 'hook_success'}):
        assert facts_from_line({**line, 'attachment': {**line['attachment'], **broken}}, 's', 'main', 0) == []


# the line shapes below are the real ones (seen 2026-10-04, Claude Code 2.1.289), with made-up values
CONTINUED = {'type': 'continued-in', 'timestamp': '2026-10-04T23:49:09.297Z', 'sessionId': 'old', 'continuedInSessionId': 'new'}
INTERRUPTS = ['[Request interrupted by user]', '[Request interrupted by user for tool use]']


def user_text(text, **extra):
    return {'type': 'user', 'timestamp': '2026-10-04T10:00:00.000Z', 'sessionId': 's', 'isSidechain': False,
            'message': {'role': 'user', 'content': [{'type': 'text', 'text': text}]}, **extra}


def test_a_hand_over_a_title_and_an_esc_become_facts():
    from skyborne.transcripts import facts_from_line
    assert facts_from_line(CONTINUED, 'old', 'main', 0) == [
        {'ts': 1791157749297, 'session_id': 'old', 'kind': 'continued', 'continued_in': 'new'}]
    assert facts_from_line({'type': 'custom-title', 'customTitle': ' Tidy notes ', 'sessionId': 's'}, 's', 'main', 5, 120) == [
        {'ts': 5, 'session_id': 's', 'kind': 'title', 'title': 'Tidy notes', 'custom': True, 'seq': 120}]
    assert facts_from_line({'type': 'ai-title', 'aiTitle': 'notes-cleanup', 'sessionId': 's'}, 's', 'main', 5)[0]['custom'] is False
    for text in INTERRUPTS:
        assert facts_from_line(user_text(text), 's', 'main', 0) == [{'ts': 1791108000000, 'session_id': 's', 'kind': 'interrupt'}]


def test_lines_that_are_not_those_stay_quiet():
    from skyborne.transcripts import facts_from_line
    assert facts_from_line({**CONTINUED, 'continuedInSessionId': ''}, 'old', 'main', 0) == []
    # without a time of their own they'd take the read time, which changes with every restart
    assert facts_from_line({k: v for k, v in CONTINUED.items() if k != 'timestamp'}, 'old', 'main', 5) == []
    assert facts_from_line({k: v for k, v in user_text(INTERRUPTS[0]).items() if k != 'timestamp'}, 's', 'main', 5) == []
    assert facts_from_line({'type': 'ai-title', 'aiTitle': '  '}, 's', 'main', 0) == []
    assert facts_from_line({'type': 'agent-name', 'agentName': 'x'}, 's', 'main', 0) == []
    # a helper's own Esc line or titles (its transcript, or a helper line in the lead's) aren't the lead's
    assert facts_from_line(user_text(INTERRUPTS[0]), 's', 'agent-1', 0) == []
    assert facts_from_line(user_text(INTERRUPTS[0], isSidechain=True, agentId='agent-1'), 's', 'main', 0) == []
    assert facts_from_line({'type': 'ai-title', 'aiTitle': 'x'}, 's', 'agent-1', 0) == []
    # a prompt that only quotes the marker, or says more, is a prompt
    assert facts_from_line(user_text('Why did it say [Request interrupted by user]?'), 's', 'main', 0) == []
    two = user_text(INTERRUPTS[0])
    two['message']['content'].append({'type': 'text', 'text': 'more'})
    assert facts_from_line(two, 's', 'main', 0) == []


def test_title_lines_are_numbered_by_their_place_in_the_file(tmp_path):
    f = tmp_path / 'session.jsonl'
    first = json.dumps({'type': 'ai-title', 'aiTitle': 'first'}) + '\n'
    second = json.dumps({'type': 'ai-title', 'aiTitle': 'second'})
    f.write_bytes((first + second[:10]).encode())  # bytes, as Claude Code writes them (no \r\n on Windows); the second line is still being written
    got = []
    t = Tailer(got.extend)
    t.watch('s', str(f))
    t.poll()
    with f.open('ab') as fh:
        fh.write((second[10:] + '\n').encode())
    t.poll()
    assert [(x['title'], x['seq']) for x in got] == [('first', 0), ('second', len(first.encode()))]
