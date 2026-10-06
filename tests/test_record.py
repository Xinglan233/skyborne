"""`skyborne record`: a scrubbed, self-contained file the page can play; planted secrets never survive;
stand-ins hide what was said and made; nothing is written if the leak check finds anything."""
import json

from skyborne import importer, record
from skyborne.reducer import reduce
from skyborne.store import Store
from test_import import FULL_SID, place_full
from test_scrub import ADA, PLANTED, PERSONAL, SESSION, planted_session


def imported_fixture(tmp_path):
    place_full()
    importer.run(7, db_path=tmp_path / 's.db', out=lambda *a: None)
    return tmp_path / 's.db'


def run(tmp_path, session, stand_ins=False, identity=ADA, db=None):
    lines = []
    out = tmp_path / 'demo.json'
    code = record.main(session, str(out), stand_ins, db_path=db or tmp_path / 's.db', identity=identity, say=lines.append)
    return code, (json.loads(out.read_text(encoding='utf-8')) if out.exists() else None), lines


def test_planted_secrets_never_reach_the_file(tmp_path):
    st = Store(tmp_path / 's.db')
    for i, p in enumerate(planted_session()):
        st.add_event(1_800_000_000_000 + i * 1000, p)
    st.commit()
    code, rec, lines = run(tmp_path, SESSION[:8])
    assert code == 0, lines
    text = (tmp_path / 'demo.json').read_text(encoding='utf-8')
    for name, secret in PLANTED.items():
        assert secret not in text, name
    for p in PERSONAL:
        assert p.lower() not in text.lower(), p
    assert SESSION not in text and '1800000000' not in text  # no real ids, no real times
    assert any(l.startswith('Scrubbed:') and 'Leak check passed' in l for l in lines)


def test_a_recording_is_relative_in_time_and_matches_a_rebuild(tmp_path):
    db = imported_fixture(tmp_path)
    code, rec, _ = run(tmp_path, FULL_SID, db=db)
    assert code == 0 and rec['format'] == 'skyborne-recording' and rec['version'] == 1
    assert rec['events'][0]['t'] == 0 and all(0 <= f['t'] <= rec['duration'] for f in rec['frames'])
    ts = [f['t'] for f in rec['frames']]
    assert ts == sorted(ts) and all(b - a >= record.FRAME_MS for a, b in zip(ts, ts[1:]))
    last = rec['frames'][-1]['doc']
    [rebuilt] = reduce([{'ts': e['t'], 'payload': e['payload']} for e in rec['events']],
                       [{**f, 'ts': f['t'], 'session_id': rec['session']['id']} for f in rec['facts']]).values()
    rebuilt.pop('rateLimits', None)
    assert last == {**rebuilt, 'feed': rebuilt['feed'][:record.FEED]}
    assert last['turns'] == 3 and len(last['agents']) == 3 and last['startedAt'] == 0


def test_stand_ins_hide_what_was_said_and_made(tmp_path):
    db = imported_fixture(tmp_path)
    code, rec, lines = run(tmp_path, FULL_SID, stand_ins=True, db=db)
    assert code == 0
    text = json.dumps(rec)
    for said in ('Run exactly this bash command', 'In ONE message', 'pineapple', 'Both agents have completed',
                 'Count lines in alpha.txt', 'Use the Read tool only', 'Import fixture', 'The secret word'):
        assert said not in text, said
    last = rec['frames'][-1]['doc']
    assert last['title'] == 'project' and last['sessionName'] == 'Project'
    assert sorted(a.get('description') for a in last['agents'][1:]) == ['Helper task 1', 'Helper task 2']
    assert all(a['activity'].startswith('Answer ') for a in last['agents'][1:])  # the helpers' final answers
    # what the city shows of a tool call stays: a command, a file's name
    assert any(f['text'] == '$ touch made-by-test.txt' for f in last['feed'])
    for e in rec['events']:
        p = e['payload']
        assert set(p.get('tool_input', {})) <= set(record.SHOWN.get(p.get('tool_name'), ()))
        r = p.get('tool_response')
        assert r is None or r == '(Output hidden)' or set(r) <= set(record.LINKS)
    assert rec['report']['stand-ins'] > 10
    visible = lines[lines.index(next(l for l in lines if l.startswith('The page will show'))) + 1:]
    assert visible and not any('pineapple' in l for l in visible)


def test_stand_ins_cover_every_free_text_field(tmp_path):
    """Fields only some events carry: a turn's still-running helpers, a long (cut) error, a compaction's
    summary, the scratchpad folder, and the project's folder inside paths and commands."""
    from skyborne.store import TRUNCATE_AT
    st = Store(tmp_path / 's.db')
    base = {'session_id': SESSION, 'cwd': '/work/orchid-ledger', 'scratchpad_dir': '/private/tmp/claude-501/-work-orchid-ledger/x/scratchpad'}
    for i, p in enumerate([
        {**base, 'hook_event_name': 'SessionStart', 'source': 'startup'},
        {**base, 'hook_event_name': 'PreToolUse', 'tool_name': 'Read', 'tool_use_id': 'toolu_01ReadReadReadRead',
         'tool_input': {'file_path': '/work/orchid-ledger/payroll.csv'}},
        {**base, 'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 'toolu_01BashBashBashBash',
         'tool_input': {'command': 'cd /work/orchid-ledger && make'}},
        {**base, 'hook_event_name': 'PostToolUseFailure', 'tool_name': 'Bash', 'tool_use_id': 'toolu_01BashBashBashBash',
         'error': 'quarterly figures ' * (TRUNCATE_AT // 10)},
        {**base, 'hook_event_name': 'Stop', 'last_assistant_message': 'Started them.',
         'background_tasks': [{'id': 'a0123456789abcdef', 'type': 'subagent', 'status': 'running', 'description': 'audit the bonus pool'}]},
        {**base, 'hook_event_name': 'PostCompact', 'trigger': 'auto', 'compact_summary': 'We discussed the merger.'},
        {**base, 'hook_event_name': 'StopFailure', 'error': 'billing_error', 'error_details': 'Card for account 4471 declined'},
    ]):
        st.add_event(1_800_000_000_000 + i * 1000, p)
    st.commit()
    code, rec, lines = run(tmp_path, SESSION[:8], stand_ins=True)
    assert code == 0, lines
    text = json.dumps(rec)
    for said in ('orchid', 'quarterly figures', 'bonus pool', 'merger', 'claude-501', 'account 4471'):
        assert said not in text, said
    assert '/home/user/project/payroll.csv' in text and 'cd /home/user/project && make' in text


def test_nothing_is_written_when_the_leak_check_finds_something(tmp_path, monkeypatch):
    st = Store(tmp_path / 's.db')
    for i, p in enumerate(planted_session()):
        st.add_event(1_800_000_000_000 + i * 1000, p)
    st.commit()
    monkeypatch.setattr(record.Scrubber, 'text', lambda self, s: s)  # a scrubber that misses everything
    code, rec, lines = run(tmp_path, SESSION[:8])
    assert code == 1 and rec is None and not (tmp_path / 'demo.json').exists()
    assert 'Not written' in lines[0] and not any(secret in ' '.join(lines) for secret in PLANTED.values())


def test_an_unknown_session_lists_the_recent_ones(tmp_path):
    db = imported_fixture(tmp_path)
    code, rec, lines = run(tmp_path, 'zzz', db=db)
    assert code == 1 and rec is None and lines[0] == 'No session starts with that.'
    assert any(FULL_SID[:8] in l for l in lines[2:])


def test_a_forked_session_records_only_its_own_work(tmp_path):
    """A fork's transcript starts with a copy of the conversation (old times, counted in the session it came from);
    titles carry the time they were read; the session it continued in isn't part of this recording."""
    t, sid = 1_800_000_000_000, '00000000-0000-4000-8000-0000000000f1'
    base = {'session_id': sid, 'cwd': '/home/user/project'}
    events = [{'ts': t, 'payload': {**base, 'hook_event_name': 'SessionStart', 'source': 'fork'}},
              {'ts': t + 1000, 'payload': {**base, 'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 'toolu_1',
                                           'tool_input': {'command': 'ls'}}},
              {'ts': t + 5000, 'payload': {**base, 'hook_event_name': 'SessionEnd', 'reason': 'continued',
                                           'continued_in': 'next-session-id-123'}, 'source': 'transcript'}]
    usage = lambda ts, mid: {'ts': ts, 'session_id': sid, 'kind': 'usage', 'message_id': mid, 'agent': 'main', 'model': 'm',
                             'final': True, 'usage': {'in': 1, 'out': 2, 'cw': 3, 'cr': 4}}
    facts = [usage(t - 3_600_000, 'msg_copied'), usage(t + 2000, 'msg_own'),
             {'ts': t - 4_000_000, 'session_id': sid, 'kind': 'tool_result', 'tool_use_id': 'toolu_copied', 'is_error': True},
             {'ts': t + 9_000_000, 'session_id': sid, 'kind': 'title', 'title': 'film-voice-retime', 'custom': False, 'seq': 40}]
    for stand_ins in (False, True):
        rec, _ = record.build(sid, events, facts, stand_ins=stand_ins, identity=ADA)
        text = json.dumps(rec)
        assert rec['events'][0]['t'] == 0 and rec['duration'] < 10_000  # not stretched by the copy or the title's read time
        assert [f['message_id'] for f in rec['facts'] if f['kind'] == 'usage'] == ['msg_own']
        assert not [f for f in rec['facts'] if f['kind'] == 'tool_result']  # copied too
        assert 'next-session-id-123' not in text
        last = rec['frames'][-1]['doc']
        assert last['tokens']['total'] == 10 and last['ended']['reason'] == 'continued'
        assert last['sessionName'] == ('Project' if stand_ins else 'film-voice-retime')
        assert ('film-voice-retime' in text) is not stand_ins
