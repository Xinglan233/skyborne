"""`skyborne import`: past transcripts become the events and usage a live recording would have, with
their own times, never "now"; re-running is safe; sessions recorded live are left alone."""
import io
import json
import os
import pathlib
import shutil
import sqlite3
import sys

import pytest

from skyborne import importer
from skyborne.reducer import reduce
from skyborne.store import Store
from skyborne.transcripts import to_ms

FIX = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'transcripts'
FULL_SID = '00000000-0000-4000-8000-000000000001'
S2_SID = '00000000-0000-4000-8000-0000000000c2'


def projects():
    return pathlib.Path(os.environ['CLAUDE_CONFIG_DIR']) / 'projects'


def place_full():
    """The real-shape fixture, where Claude Code keeps transcripts."""
    dest = projects() / '-home-user-project'
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIX / 'import-session' / f'{FULL_SID}.jsonl', dest)
    shutil.copytree(FIX / 'import-session' / FULL_SID, dest / FULL_SID)
    return dest / f'{FULL_SID}.jsonl'


def place_s2():
    """The slim spike session (structure and usage only)."""
    dest = projects() / '-home-user-spike'
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIX / 's2' / 'session.jsonl', dest / f'{S2_SID}.jsonl')
    shutil.copytree(FIX / 's2' / 'subagents', dest / S2_SID / 'subagents')
    return dest / f'{S2_SID}.jsonl'


def doc_from_db(db, sid):
    return reduce(*Store(db).load([sid]))[sid]


def lines_of(path):
    return [json.loads(l) for l in path.read_text(encoding='utf-8').splitlines() if l.strip()]


def test_a_real_shaped_session_comes_back_with_its_own_times(tmp_path):
    main = place_full()
    result = importer.run(7, db_path=tmp_path / 's.db', out=lambda *a: None)
    assert result['summary']['imported'] == 1
    doc = doc_from_db(tmp_path / 's.db', FULL_SID)

    every = lines_of(main) + [d for h in sorted((main.parent / FULL_SID / 'subagents').glob('*.jsonl')) for d in lines_of(h)]
    stamps = [to_ms(d['timestamp'], None) for d in every if d.get('timestamp')]
    assert lines_of(main)[0].get('timestamp') is None  # the file starts with untimed header lines
    assert (doc['startedAt'], doc['updatedAt']) == (min(stamps), max(stamps))  # never the time of the import
    assert doc['title'] == 'project' and doc['sessionName'] == 'Import fixture' and doc['imported'] is True
    assert doc['turns'] == 3  # the three prompts; /exit, its output and Claude Code's notes are not turns
    assert doc['ended']['reason'] == 'imported' and doc['waiting'] is None

    lead, helpers = doc['agents'][0], doc['agents'][1:]
    assert lead['kind'] == 'leave' and lead['tools'] == 4  # two Agent calls, two Bash calls
    assert sorted(h['description'] for h in helpers) == ['Count lines in alpha.txt', 'Find secret word in beta.txt']
    assert all(h['status'] == 'done' and h['parent'] == 'main' and h['tools'] == 1 and h['type'] == 'Explore' for h in helpers)
    assert sorted(h['activity'] for h in helpers) == ['5', 'pineapple']  # their final answers

    feed = doc['feed']
    assert any(f['kind'] == 'answer' and f['text'].startswith('Both agents have completed') for f in feed)
    assert any(f['kind'] == 'error' and f['text'] == "Bash didn't run" for f in feed)  # the "No" typed in the terminal
    assert not any('<command-name>' in f['text'] or '<task-notification>' in f['text'] for f in feed)
    assert result['skipped lines']['refused calls'] == 1 and result['skipped lines']['commands and notes'] >= 3

    # tokens: every message counted once, from its last line, helpers included
    last = {}
    for d in every:
        m = d.get('message') or {}
        if d.get('type') == 'assistant' and m.get('usage'):
            last[m['id']] = sum(m['usage'].get(k) or 0 for k in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))
    assert doc['tokens']['total'] == sum(last.values()) == result['summary']['tokens']


def test_the_spike_sessions_tokens_match_the_hand_count(tmp_path):
    place_s2()
    importer.run(7, db_path=tmp_path / 's.db', out=lambda *a: None)
    assert doc_from_db(tmp_path / 's.db', S2_SID)['tokens']['total'] == 517_687


def test_running_it_again_imports_nothing_twice(tmp_path):
    place_full()
    db = tmp_path / 's.db'
    first = importer.run(7, db_path=db, out=lambda *a: None)['summary']
    again = importer.run(7, db_path=db, out=lambda *a: None)['summary']
    assert first['imported'] == 1 and again.get('imported', 0) == 0 and again['imported before'] == 1
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM events').fetchone()[0] == first['events']


def test_two_imports_at_once_add_a_session_once(tmp_path, monkeypatch):
    """Both runs pass the first check; the one that writes second sees the other's import and skips."""
    place_full()
    db = tmp_path / 's.db'
    build, other = importer.build, {}

    def build_while_another_import_runs(path):
        if not other:
            other['running'] = True
            other.update(importer.run(7, db_path=db, out=lambda *a: None)['summary'])
        return build(path)
    monkeypatch.setattr(importer, 'build', build_while_another_import_runs)
    first = importer.run(7, db_path=db, out=lambda *a: None)['summary']
    assert other['imported'] == 1 and first == {'imported before': 1}
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM events').fetchone()[0] == other['events']
    assert doc_from_db(db, FULL_SID)['turns'] == 3


def test_a_session_still_open_when_imported_goes_on_live(tmp_path):
    """The import closes it; the hooks that follow (Skyborne started mid-session) open it again."""
    from conftest import payload
    place_full()
    db = tmp_path / 's.db'
    importer.run(7, db_path=db, out=lambda *a: None)
    end = doc_from_db(db, FULL_SID)['ended']['at']
    pre = payload('PreToolUse.json', session_id=FULL_SID, tool_use_id='toolu_live1')
    ask = payload('PermissionRequest.json', session_id=FULL_SID, tool_input=pre['tool_input'], tool_name=pre['tool_name'])
    st = Store(db)
    for i, p in enumerate([payload('UserPromptSubmit.json', session_id=FULL_SID, prompt='Still here'), pre, ask]):
        st.add_event(end + 1000 + i * 10, p)
    st.commit()
    doc = doc_from_db(db, FULL_SID)
    assert 'ended' not in doc and doc['turns'] == 4
    assert doc['waiting']['tool'] == pre['tool_name'] and doc['agents'][0]['kind'] == 'wait'
    assert not any(f['kind'] == 'leave' for f in doc['feed'])


def test_a_session_recorded_live_is_left_alone(tmp_path):
    place_full()
    st = Store(tmp_path / 's.db')
    st.add_event(1, {'session_id': FULL_SID, 'hook_event_name': 'SessionStart'})
    st.commit()
    summary = importer.run(7, db_path=tmp_path / 's.db', out=lambda *a: None)['summary']
    assert summary == {'recorded live': 1}


def test_only_the_last_days_are_read(tmp_path):
    main = place_full()
    old = main.stat().st_mtime - 10 * 86400
    os.utime(main, (old, old))
    assert importer.transcripts(7) == [] and importer.transcripts(11) == [main]


def test_the_first_run_offers_to_import_once(tmp_path, monkeypatch, capsys):
    from skyborne import __main__ as cli, config
    from skyborne.server import App
    place_full()
    app = App(port=0, db_path=tmp_path / 's.db')
    try:
        monkeypatch.setattr(sys, 'stdin', io.StringIO(''))  # not a terminal: a tip, and ask next time
        cli._first_run(app)
        assert 'skyborne import' in capsys.readouterr().out and config.load_state() == {}
        stdin = io.StringIO('\n')  # Enter takes the default: yes
        stdin.isatty = lambda: True
        monkeypatch.setattr(sys, 'stdin', stdin)
        cli._first_run(app)
        assert 'Imported 1 session' in capsys.readouterr().out and config.load_state()['imported'] is True
        cli._first_run(app)  # asked once only
        assert capsys.readouterr().out == ''
    finally:
        app.httpd.server_close()
