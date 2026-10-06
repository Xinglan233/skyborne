"""Reducer evals (tests/evals/*.json) and the rules behind them."""
import copy
import json
import pathlib
import random

import pytest

from skyborne.reducer import Session, reduce

EVALS = sorted((pathlib.Path(__file__).resolve().parent / 'evals').glob('*.json'))


def load(name):
    return json.loads((pathlib.Path(__file__).resolve().parent / 'evals' / f'{name}.json').read_text(encoding='utf-8'))


def doc_of(name, events=None, facts=None):
    e = load(name)
    [doc] = reduce(events if events is not None else e['events'], facts if facts is not None else e['facts'], e['now']).values()
    return doc


@pytest.mark.parametrize('path', EVALS, ids=lambda p: p.stem)
def test_eval_matches_saved_result(path):
    e = json.loads(path.read_text(encoding='utf-8'))
    assert reduce(e['events'], e['facts'], e['now']) == e['expect']


@pytest.mark.parametrize('path', EVALS, ids=lambda p: p.stem)
def test_arrival_order_does_not_matter(path):
    """Hooks run as separate background processes, so events can arrive in any order."""
    e = json.loads(path.read_text(encoding='utf-8'))
    rng = random.Random(path.stem)
    for _ in range(5):
        events, facts = copy.deepcopy(e['events']), copy.deepcopy(e['facts'])
        rng.shuffle(events)
        rng.shuffle(facts)
        assert reduce(events, facts, e['now']) == e['expect']


@pytest.mark.parametrize('path', EVALS, ids=lambda p: p.stem)
def test_live_updates_match_a_rebuild(path):
    """Feeding one session event by event (as the server does) ends where a rebuild does."""
    e = json.loads(path.read_text(encoding='utf-8'))
    [(sid, expect)] = e['expect'].items()
    s = Session(sid)
    mixed = [('e', x) for x in e['events']] + [('f', x) for x in e['facts']]
    mixed.sort(key=lambda kx: kx[1]['ts'])
    for kind, x in mixed:
        (s.add_event if kind == 'e' else s.add_fact)(copy.deepcopy(x))
        s.doc(e['now'])  # building the doc mid-way must not change anything
    assert s.doc(e['now']) == expect


# ---- the rules, one by one ----

def lead(doc):
    return doc['agents'][0]


def test_activity_kind_comes_from_the_tool_name():
    doc = doc_of('turn-with-tool')
    bash = next(f for f in doc['feed'] if f.get('tool') == 'Bash' and f['kind'] != 'wait')
    assert bash['kind'] == 'bash' and bash['text'] == '$ touch spike-1a.txt'


def test_duration_from_duration_ms_else_post_minus_pre():
    assert next(f for f in doc_of('turn-with-tool')['feed'] if f['kind'] == 'bash')['durationMs'] == 82
    assert doc_of('duration-fallback')['feed'][0]['durationMs'] == 450
    assert 'durationMs' not in next(f for f in doc_of('permission-denied')['feed'] if f['kind'] == 'bash')


def test_helper_linked_to_its_agent_call_and_done_at_subagent_stop():
    mid, done = doc_of('helpers-midway'), doc_of('helpers')
    scout = mid['agents'][1]
    assert (scout['name'], scout['type'], scout['parent'], scout['description']) == ('Scout 1', 'Explore', 'main', 'count alpha lines')
    assert scout['status'] == 'working'  # its Agent call already returned (async launch): not done yet
    assert done['agents'][1]['status'] == 'done' and done['agents'][1]['activity'] == '4'


def test_unknown_subagent_stop_is_ignored():
    assert len(doc_of('internal-agent-ignored')['agents']) == 1
    assert [a['name'] for a in doc_of('helpers')['agents']] == ['Skybot', 'Scout 1']


def test_task_notification_is_not_a_turn_or_headline():
    doc = doc_of('helpers')
    assert doc['turns'] == 1
    assert not any('<task-notification>' in f['text'] for f in doc['feed'])
    assert '<task-notification>' not in doc['headline']


def test_stop_with_helpers_running_is_not_idle():
    l = lead(doc_of('helpers-midway'))
    assert (l['status'], l['activity']) == ('working', 'Waiting for 2 helpers')
    assert lead(doc_of('helpers'))['status'] == 'done'


def test_waiting_matched_and_cleared():
    open_ = doc_of('waiting-open')
    assert open_['waiting'] == {'agent': 'main', 'tool': 'Bash', 'since': 3010}
    assert lead(open_)['kind'] == 'wait' and lead(open_)['waiting'] is True
    for name in ('turn-with-tool', 'waiting-cleared-by-terminal-no', 'waiting-cleared-by-next-prompt', 'waiting-cleared-by-session-end'):
        doc = doc_of(name)
        assert doc['waiting'] is None and lead(doc)['waiting'] is False, name
    assert lead(doc_of('waiting-cleared-by-terminal-no'))['activity'] == 'Waiting for a prompt'


def test_out_of_order_events_still_match():
    doc = doc_of('out-of-order-arrival')
    assert doc['waiting'] is None
    assert next(f for f in doc['feed'] if f['kind'] == 'bash')['durationMs'] == 82


def test_model_history_skips_synthetic_and_repeats():
    assert lead(doc_of('models'))['models'] == ['claude-haiku-4-5-20251001', 'claude-sonnet-5-5', 'claude-opus-5-5']
    assert lead(doc_of('models'))['model'] == 'claude-opus-5-5'


def test_tokens_count_each_message_once_and_include_helpers():
    doc = doc_of('tokens-from-transcripts')
    assert sum(lead(doc)['usage'].values()) == 426_143
    assert doc['tokens']['total'] == 517_687
    assert sorted(sum(h['usage'].values()) for h in doc['tokens']['helpers']) == [25_787, 25_827, 39_930]
    assert all(a['status'] == 'done' for a in doc['agents'][1:])  # their reports say they finished


def test_status_line_gives_cost_context_and_rate_limits():
    doc = doc_of('turn-with-tool')
    assert doc['cost'] == {'usd': 0.0608294}
    assert doc['context'] == {'tokens': 41130, 'window': 200000, 'percent': 21}
    assert set(doc['rateLimits']) == {'five_hour', 'seven_day'}
    assert doc['sessionName'] == 'spike-approval-b'


def test_errors_and_endings():
    assert (lead(doc_of('stop-failure'))['status'], doc_of('stop-failure')['headline']) == ('error', 'Hit a snag')
    assert lead(doc_of('idle-notification'))['status'] == 'idle'
    assert doc_of('permission-denied')['feed'][0]['text'] == 'Auto mode refused Bash: [Data Exfiltration]'
    assert doc_of('tool-failure')['feed'][0]['text'].startswith('Bash failed: Exit code 1')
    assert doc_of('waiting-cleared-by-session-end')['ended'] == {'at': 7000, 'reason': 'prompt_input_exit'}
    compact = doc_of('compaction')
    assert lead(compact)['status'] == 'idle' and sum(f['kind'] == 'join' for f in compact['feed']) == 1


def test_full_text_is_kept():
    long = 'x' * 50_000
    e = load('turn-with-tool')
    events = copy.deepcopy(e['events'])
    events[1]['payload']['prompt'] = long
    doc = doc_of('turn-with-tool', events=events)
    assert any(f['text'] == long for f in doc['feed'])


# ---- arrival a few milliseconds off, resumes and final ends ----

def _view(doc):
    """What must not depend on which of two close events reached the server first."""
    w = doc['waiting']
    return {'agents': {a['id']: (a['status'], a['kind'], a['waiting'], a['tools']) for a in doc['agents']},
            'waiting': (w['agent'], w['tool']) if w else None, 'turns': doc['turns'], 'tokens': doc['tokens']['total'],
            'ended': 'ended' in doc, 'feed': sorted(f['kind'] for f in doc['feed'])}


@pytest.mark.parametrize('path', EVALS, ids=lambda p: p.stem)
def test_close_events_swapped_in_time(path):
    """Each background hook is its own process, so two events a few ms apart can be stamped in either order.
    Helper numbering, activity text, the headline and feed order may follow the stamps; the state may not."""
    e = json.loads(path.read_text(encoding='utf-8'))
    expect = {sid: _view(d) for sid, d in reduce(e['events'], e['facts'], e['now']).items()}
    for seed in range(5):
        rng = random.Random(f'{path.stem}-{seed}')
        events = [{**ev, 'ts': ev['ts'] + rng.randint(0, 30)} for ev in e['events']]
        got = reduce(events, copy.deepcopy(e['facts']), e['now'])
        assert {sid: _view(d) for sid, d in got.items()} == expect, f'seed {seed}'


def _with(name, *extra):
    e = load(name)
    return reduce(e['events'] + [{'ts': ts, 'payload': p} for ts, p in extra], e['facts'], e['now'])


def test_a_resumed_session_can_wait_for_the_mayor_again():
    from conftest import payload
    e = load('waiting-cleared-by-session-end')
    sid = e['events'][0]['payload']['session_id']
    end = max(ev['ts'] for ev in e['events'])
    pre = payload('PreToolUse.json', session_id=sid, tool_use_id='toolu_resumed')
    ask = payload('PermissionRequest.json', session_id=sid, tool_input=pre['tool_input'], tool_name=pre['tool_name'])
    [doc] = _with('waiting-cleared-by-session-end', (end + 1000, payload('SessionStart-cmdhook.json', session_id=sid, source='resume')),
                  (end + 2000, pre), (end + 2010, ask)).values()
    assert 'ended' not in doc
    assert doc['waiting'] == {'agent': 'main', 'tool': pre['tool_name'], 'since': end + 2010}
    assert sum(f['kind'] == 'leave' for f in doc['feed']) == 1
    # without the resume, a request stamped just after the end is a late arrival: the session has left
    [doc] = _with('waiting-cleared-by-session-end', (end + 5, ask)).values()
    assert doc['waiting'] is None and doc['ended']['at'] == end and lead(doc)['kind'] == 'leave'
    # or stamped in the same millisecond as the end
    [doc] = _with('waiting-cleared-by-session-end', (end, ask)).values()
    assert doc['waiting'] is None and doc['ended']['at'] == end


def test_a_long_agent_result_still_links_its_helper(tmp_path):
    """A result over 20 KB is stored cut; the ids that link the call to its helper stay with it."""
    from conftest import payload
    from skyborne.store import Store, TRUNCATE_AT
    sid = '00000000-0000-4000-8000-0000000000aa'
    post = payload('PostToolUse-Agent-async-launch.json', session_id=sid)
    post['tool_response'] = {**post['tool_response'], 'content': [{'type': 'text', 'text': 'report ' * TRUNCATE_AT}]}
    pre = payload('PreToolUse.json', session_id=sid, tool_name='Agent', tool_use_id=post['tool_use_id'], tool_input=post['tool_input'])
    st = Store(tmp_path / 's.db')
    for i, p in enumerate([payload('SessionStart-cmdhook.json', session_id=sid), pre, post,
                           payload('SubagentStart.json', session_id=sid, agent_id=post['tool_response']['agentId'])]):
        st.add_event(1000 + i * 10, p)
    st.commit()
    events, facts = st.load([sid])
    assert events[2]['payload']['tool_response']['skyborneTruncated']
    helper = reduce(events, facts)[sid]['agents'][1]
    assert helper['description'] == post['tool_input']['description'] and helper['parent'] == 'main'


def test_a_helper_that_stopped_stays_done():
    from conftest import payload
    e = load('helpers')
    h = next(ev for ev in e['events'] if ev['payload'].get('hook_event_name') == 'SubagentStop' and ev['payload'].get('agent_type'))
    late = payload('PreToolUse-inside-helper.json', session_id=h['payload']['session_id'], agent_id=h['payload']['agent_id'],
                   tool_use_id='toolu_late')
    [doc] = _with('helpers', (h['ts'] + 5, late)).values()
    helper = next(a for a in doc['agents'] if a['id'] == h['payload']['agent_id'])
    before = next(a for a in doc_of('helpers')['agents'] if a['id'] == h['payload']['agent_id'])
    assert helper['status'] == 'done' and helper['activity'] == before['activity']
    assert helper['tools'] == before['tools'] + 1  # the late call still counts; it just doesn't wake the helper


def test_same_moment_ties_do_not_depend_on_arrival():
    from conftest import payload
    e = load('turn-with-tool')
    sid = e['events'][0]['payload']['session_id']
    t = max(ev['ts'] for ev in e['events']) + 100
    a = {'ts': t, 'payload': payload('Stop.json', session_id=sid, last_assistant_message='Alpha')}
    b = {'ts': t, 'payload': payload('Stop.json', session_id=sid, last_assistant_message='Beta')}
    one = reduce(e['events'] + [a, b], e['facts'], e['now'])
    two = reduce(e['events'] + [b, a], e['facts'], e['now'])
    assert one == two


def _ev(ts, name, **p):
    return {'ts': ts, 'payload': {'session_id': 's', 'hook_event_name': name, **p}}


def _bash(ts, name, tuid=None, cmd='ls', **p):
    return _ev(ts, name, tool_name='Bash', tool_input={'command': cmd}, **({'tool_use_id': tuid} if tuid else {}), **p)


def test_a_question_or_a_plan_isnt_called_an_approval():
    # (tool, the step before the request, the wait, its log line); every other tool keeps "Needs approval"
    for tool, step, activity, line in (('AskUserQuestion', 'Asking you a question', 'Has a question for you', 'Asked you a question'),
                                       ('ExitPlanMode', 'Sharing its plan', 'Has a plan for you to review', 'Asked you to review its plan'),
                                       ('Bash', 'Bash', 'Needs approval: Bash', 'Needs approval for Bash')):
        for who in ('main', 'h1'):  # the lead, and a helper asking
            s = Session('s')
            helper = {} if who == 'main' else {'agent_id': who, 'agent_type': 'Explore'}
            if helper:
                s.add_event(_ev(1, 'SubagentStart', **helper))
            s.add_event(_ev(5, 'PreToolUse', tool_name=tool, tool_input={'x': 1}, tool_use_id='T1', **helper))
            if tool != 'Bash':  # (a Bash step shows its command)
                assert next(a for a in s.doc(now=5)['agents'] if a['id'] == who)['activity'] == step, (tool, who)
            s.add_event(_ev(6, 'PermissionRequest', tool_name=tool, tool_input={'x': 1}, **helper))
            d = s.doc(now=10)
            bot = next(a for a in d['agents'] if a['id'] == who)
            assert bot['waiting'] and bot['activity'] == activity, (tool, who)
            assert [f['text'] for f in d['feed'] if f['kind'] == 'wait'] == [line], (tool, who)


def test_request_state_says_why_a_request_is_over():
    s = Session('s')
    assert s.request_state(10, 'main', '{"command":"ls"}') == ('unseen', '')
    s.add_event(_bash(5, 'PreToolUse', 'T1'))
    s.add_event(_bash(10, 'PermissionRequest'))
    assert s.request_state(10, 'main', '{"command":"ls"}') == ('open', 'T1')
    s.add_event(_bash(50, 'PostToolUse', 'T1'))
    assert s.request_state(10, 'main', '{"command":"ls"}') == ('ran', 'T1')
    s.add_fact({'ts': 60, 'session_id': 's', 'kind': 'tool_result', 'tool_use_id': 'T2', 'is_error': True})
    s.add_event(_bash(70, 'PreToolUse', 'T2', 'rm x'))
    s.add_event(_bash(71, 'PermissionRequest', cmd='rm x'))
    assert s.request_state(71, 'main', '{"command":"rm x"}') == ('refused', 'T2')
    s.add_event(_bash(80, 'PreToolUse', 'T3', 'pwd'))
    s.add_event(_bash(81, 'PermissionRequest', cmd='pwd'))
    s.add_event(_ev(90, 'SessionEnd', reason='other'))
    assert s.request_state(81, 'main', '{"command":"pwd"}') == ('ended', 'T3')


def test_a_request_with_a_tool_use_id_matches_that_call_exactly():
    # the docs list tool_use_id on PermissionRequest (Claude Code 2.1.289 sends none): when present, it wins
    s = Session('s')
    s.add_event(_bash(5, 'PreToolUse', 'T1'))
    s.add_event(_bash(9, 'PreToolUse', 'T2'))
    s.add_event(_bash(6, 'PermissionRequest', 'T2'))  # nearer in time to T1, but names T2
    assert s.request_state(6, 'main', '{"command":"ls"}') == ('open', 'T2')
    s.add_event(_bash(20, 'PostToolUse', 'T1'))
    assert s.request_state(6, 'main', '{"command":"ls"}')[0] == 'open'
    s.add_event(_bash(30, 'PostToolUse', 'T2'))
    assert s.request_state(6, 'main', '{"command":"ls"}') == ('ran', 'T2')


def test_two_identical_requests_in_one_millisecond_are_told_apart_by_the_servers_id():
    s = Session('s')
    s.add_event(_bash(5, 'PreToolUse', 'T1'))
    s.add_event(_bash(5, 'PreToolUse', 'T2'))
    s.add_event({**_bash(6, 'PermissionRequest'), 'ask': 'a1'})
    s.add_event({**_bash(6, 'PermissionRequest'), 'ask': 'a2'})
    s.add_event(_bash(20, 'PostToolUse', 'T1'))
    key = '{"command":"ls"}'
    assert sorted(s.request_state(6, 'main', key, a)[0] for a in ('a1', 'a2')) == ['open', 'ran']
    assert s.doc()['waiting'] is not None


def titled(*titles):
    """A one-prompt session given transcript titles: (title, custom, seq), read long after it went quiet."""
    s = Session('t')
    s.add_event({'ts': 1000, 'payload': {'session_id': 't', 'hook_event_name': 'UserPromptSubmit', 'prompt': 'Hi'}})
    for title, custom, seq in titles:
        s.add_fact({'ts': 9_000_000, 'session_id': 't', 'kind': 'title', 'title': title, 'custom': custom, 'seq': seq})
    return s.doc(9_000_000)


def test_a_title_names_the_session_without_making_it_look_active():
    doc = titled(('first-title', False, 10))
    assert doc['sessionName'] == 'first-title' and doc['updatedAt'] == 1000 and doc['startedAt'] == 1000


def test_the_newest_title_wins_whatever_order_the_lines_arrive_in():
    assert titled(('old', False, 10), ('new', False, 900))['sessionName'] == 'new'
    assert titled(('new', False, 900), ('old', False, 10))['sessionName'] == 'new'
    assert titled(('stored', False, -1), ('read-again', False, 40))['sessionName'] == 'read-again'
    # a name given with /rename beats Claude's own title, wherever it is in the file
    assert titled(('Mine', True, 10), ('claude-title', False, 900))['sessionName'] == 'Mine'


def test_an_esc_settles_a_request_left_open():
    s = Session('e')
    for ts, name, extra in ((1000, 'UserPromptSubmit', {'prompt': 'Go'}),
                            (2000, 'PermissionRequest', {'tool_name': 'Bash', 'tool_input': {'command': 'ls'}})):
        s.add_event({'ts': ts, 'payload': {'session_id': 'e', 'hook_event_name': name, **extra}})
    assert s.doc(3000)['waiting'] is not None
    s.add_fact({'ts': 2500, 'session_id': 'e', 'kind': 'interrupt'})
    doc = s.doc(3000)
    assert doc['waiting'] is None and lead(doc)['activity'] == 'Interrupted'


def test_lines_a_fork_copied_neither_end_it_nor_show_in_its_log():
    """A fork's transcript copies the conversation it came from. A hand-over line pointing at itself can only be a
    copy (even before its SessionStart is in), and an Esc older than the fork happened in the other session."""
    s = Session('f')
    s.add_event({'ts': 30_000, 'payload': {'session_id': 'f', 'hook_event_name': 'SessionEnd', 'reason': 'continued', 'continued_in': 'f'},
                 'source': 'transcript'})
    s.add_event({'ts': 51_000, 'payload': {'session_id': 'f', 'hook_event_name': 'PreToolUse', 'tool_name': 'Bash',
                                           'tool_use_id': 'T1', 'tool_input': {'command': 'ls'}}})
    s.add_fact({'ts': 10_000, 'session_id': 'f', 'kind': 'interrupt'})
    assert 'ended' not in s.doc(60_000)  # before its SessionStart arrives
    s.add_event({'ts': 50_000, 'payload': {'session_id': 'f', 'hook_event_name': 'SessionStart', 'source': 'fork'}})
    doc = s.doc(60_000)
    assert 'ended' not in doc and lead(doc)['status'] == 'working'
    assert [f['text'] for f in doc['feed'] if f['kind'] == 'idle'] == []


def test_a_hand_over_wins_over_the_exit_after_it_unless_the_session_was_resumed():
    moved = doc_of('continued-then-exit')
    assert moved['ended'] == {'at': 15400, 'reason': 'continued', 'continuedIn': '00000000-0000-4000-8000-0000000000bb'}
    assert lead(moved)['activity'] == 'Continued in another session'
    assert [f['text'] for f in moved['feed'] if f['kind'] == 'leave'] == ['Continued in another session']
    s = Session('r')  # handed over, resumed later, then quit: it ended by quitting
    for ts, name, extra in ((1000, 'SessionStart', {'source': 'startup'}),
                            (2000, 'SessionEnd', {'reason': 'continued', 'continued_in': 'x'}),
                            (9000, 'SessionStart', {'source': 'resume'}), (12000, 'SessionEnd', {'reason': 'prompt_input_exit'})):
        s.add_event({'ts': ts, 'payload': {'session_id': 'r', 'hook_event_name': name, **extra}})
    assert s.doc(13000)['ended'] == {'at': 12000, 'reason': 'prompt_input_exit'}


def test_a_prompt_after_a_hand_over_means_it_was_resumed():
    """If the resume's SessionStart never arrived (Skyborne was down, or the hook failed), a new prompt still
    shows the session going on, and a later exit is how it ended."""
    s = Session('p')
    for ts, name, extra in ((1000, 'SessionStart', {'source': 'startup'}),
                            (2000, 'SessionEnd', {'reason': 'continued', 'continued_in': 'x'}),
                            (9000, 'UserPromptSubmit', {'prompt': 'Back again'})):
        s.add_event({'ts': ts, 'payload': {'session_id': 'p', 'hook_event_name': name, **extra}})
    doc = s.doc(9500)
    assert 'ended' not in doc and lead(doc)['status'] == 'working'
    s.add_event({'ts': 12000, 'payload': {'session_id': 'p', 'hook_event_name': 'SessionEnd', 'reason': 'prompt_input_exit'}})
    assert s.doc(13000)['ended'] == {'at': 12000, 'reason': 'prompt_input_exit'}
