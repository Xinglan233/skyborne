"""Install and uninstall on a fake home: the plugin, and settings.json put back byte for byte."""
import hashlib
import json
import os
import pathlib
import subprocess
import sys

import pytest

from skyborne import config, install

ROOT = pathlib.Path(__file__).resolve().parents[1]


def settings():
    return config.claude_dir() / 'settings.json'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_settings(data, raw=None):
    settings().parent.mkdir(parents=True, exist_ok=True)
    settings().write_bytes(raw if raw is not None else json.dumps(data, indent=4).encode())


def test_repo_plugin_matches_what_install_writes(tmp_path):
    install.write_plugin(tmp_path / 'p')
    for rel in ('.claude-plugin/plugin.json', 'hooks/hooks.json'):
        assert (ROOT / 'plugin' / rel).read_text() == (tmp_path / 'p' / rel).read_text(), rel


def test_every_hook_but_permission_request_is_a_background_curl_with_no_shell():
    hooks = install.hooks_json(7317)['hooks']
    assert 'MessageDisplay' not in hooks
    for event, groups in hooks.items():
        if event == 'PermissionRequest':
            continue
        [h] = groups[0]['hooks']
        assert h['type'] == 'command' and h['command'] == 'curl' and h['async'] is True, event
        assert h['args'][-1] == 'http://127.0.0.1:7317/hook'


def test_permission_request_is_the_one_hook_that_waits():
    [h] = install.hooks_json(7317, 120)['hooks']['PermissionRequest'][0]['hooks']
    assert h['type'] == 'command' and 'async' not in h and 'args' not in h  # shell form, so `exit 0` can follow
    assert h['timeout'] == 120
    assert h['command'].startswith('curl -sf -m 120 ') and h['command'].endswith(' http://127.0.0.1:7317/permission; exit 0')
    assert "-H 'X-Skyborne-Wait: 115'" in h['command'] and "--data-binary '@-'" in h['command']
    assert '-H "X-Skyborne-Entrypoint: $CLAUDE_CODE_ENTRYPOINT"' in h['command']
    assert install.hooks_json()['hooks']['PermissionRequest'][0]['hooks'][0]['timeout'] == 600


@pytest.mark.parametrize('value, expected', [(None, 600), (120, 120), (29, 30), (5000, 3600), ('90', 600), (True, 600)])
def test_approval_timeout_setting(value, expected):
    if value is not None:
        config.ensure_home()
        (config.home() / 'config.json').write_text(json.dumps({'approval_timeout_seconds': value}))
    assert config.approval_timeout() == expected


def test_install_writes_the_configured_approval_timeout():
    config.ensure_home()
    (config.home() / 'config.json').write_text(json.dumps({'approval_timeout_seconds': 90}))
    target = install.install_plugin(7317)
    [h] = json.loads((target / 'hooks' / 'hooks.json').read_text())['hooks']['PermissionRequest'][0]['hooks']
    assert h['timeout'] == 90 and json.loads((target / install.MARKER).read_text())['approval_timeout'] == 90


def test_install_plugin_with_marker_and_port():
    target = install.install_plugin(9001)
    assert target == config.claude_dir() / 'skills' / 'skyborne'
    assert json.loads((target / install.MARKER).read_text())['port'] == 9001
    assert '127.0.0.1:9001/hook' in (target / 'hooks' / 'hooks.json').read_text()
    assert json.loads((target / '.claude-plugin' / 'plugin.json').read_text())['name'] == 'skyborne'
    install.install_plugin(7317)  # reinstalling over our own copy is fine
    assert install.uninstall_plugin().startswith('Removed')
    assert not target.exists()


def test_a_folder_that_is_not_ours_is_never_touched():
    target = config.claude_dir() / 'skills' / 'skyborne'
    target.mkdir(parents=True)
    (target / 'mine.txt').write_text('keep me')
    with pytest.raises(install.InstallError):
        install.install_plugin()
    assert 'not made by Skyborne' in install.uninstall_plugin()
    assert (target / 'mine.txt').read_text() == 'keep me'


def test_no_settings_file_then_uninstall_removes_it_again():
    assert not settings().exists()
    install.install_statusline()
    assert json.loads(settings().read_text())['statusLine']['command'].endswith('-m skyborne statusline')
    install.uninstall_statusline()
    assert not settings().exists()


def test_other_keys_kept_and_bytes_restored_exactly():
    raw = b'{\n    "model": "opus",\n    "enabledPlugins": {"x@y": true},\n    "env": {"A": "1"}\n}\n'
    write_settings(None, raw)
    if os.name != 'nt':
        os.chmod(settings(), 0o600)
    before = sha(settings())
    install.install_statusline()
    data = json.loads(settings().read_text())
    assert data['model'] == 'opus' and data['env'] == {'A': '1'} and 'statusLine' in data
    if os.name != 'nt':
        assert (settings().stat().st_mode & 0o777) == 0o600  # permissions kept
    backups = list((config.home() / 'backups').iterdir())
    assert len(backups) == 1 and backups[0].read_bytes() == raw
    assert 'exactly' in install.uninstall_statusline()
    assert sha(settings()) == before


def test_an_existing_status_line_is_remembered_and_still_shown(capsys, monkeypatch):
    write_settings({'statusLine': {'type': 'command', 'command': 'echo mine'}, 'theme': 'dark'})
    before = sha(settings())
    assert install.install_statusline() == {'type': 'command', 'command': 'echo mine'}
    assert install.previous_statusline_command() == 'echo mine'
    from skyborne import forward
    monkeypatch.setattr(sys, 'stdin', type('S', (), {'buffer': __import__('io').BytesIO(b'{}')})())
    forward.statusline(1)  # nothing listens on port 1: the forward fails quietly
    assert capsys.readouterr().out.strip() == 'mine'
    install.uninstall_statusline()
    assert sha(settings()) == before


def test_edits_after_install_are_kept_and_only_status_line_restored():
    write_settings({'statusLine': {'type': 'command', 'command': 'echo mine'}, 'theme': 'dark'})
    install.install_statusline()
    data = json.loads(settings().read_text())
    data['theme'] = 'light'  # the person changed something since
    settings().write_text(json.dumps(data))
    assert 'only the statusLine key' in install.uninstall_statusline()
    after = json.loads(settings().read_text())
    assert after == {'statusLine': {'type': 'command', 'command': 'echo mine'}, 'theme': 'light'}


def test_no_status_line_before_means_none_after_a_partial_restore():
    write_settings({'theme': 'dark'})
    install.install_statusline()
    data = json.loads(settings().read_text())
    data['theme'] = 'light'
    settings().write_text(json.dumps(data))
    install.uninstall_statusline()
    assert json.loads(settings().read_text()) == {'theme': 'light'}


def test_broken_settings_are_left_alone():
    write_settings(None, b'{not json')
    with pytest.raises(install.InstallError):
        install.install_statusline()
    assert settings().read_bytes() == b'{not json'


def test_cli_install_and_uninstall_round_trip():
    write_settings({'theme': 'dark'})
    before = sha(settings())
    env = {**os.environ}
    run = lambda *a: subprocess.run([sys.executable, '-m', 'skyborne', *a], env=env, capture_output=True, text=True, timeout=60)
    r = run('install', '--statusline')
    assert r.returncode == 0, r.stdout + r.stderr
    assert (config.claude_dir() / 'skills' / 'skyborne' / install.MARKER).exists()
    r = run('uninstall')
    assert r.returncode == 0, r.stdout + r.stderr
    assert sha(settings()) == before
    assert not (config.claude_dir() / 'skills' / 'skyborne').exists()


def test_skyborne_folder_and_backups_are_private():
    if os.name == 'nt':
        pytest.skip('POSIX permissions')
    write_settings({'theme': 'dark'})
    install.install_statusline()
    assert (config.home().stat().st_mode & 0o777) == 0o700
    assert ((config.home() / 'backups').stat().st_mode & 0o777) == 0o700


def test_changing_the_port_moves_the_status_line_too_and_uninstall_still_restores_exactly():
    raw = b'{\n    "model": "opus"\n}\n'
    write_settings(None, raw)
    before = sha(settings())
    install.install_statusline()
    command = lambda: json.loads(settings().read_text())['statusLine']['command']
    assert install.statusline_port(command()) == config.DEFAULT_PORT and '--port' not in command()
    assert install.set_statusline_port(7400) == 'The status line now sends to port 7400 (it sent to port 7317).'
    assert command().endswith('-m skyborne statusline --port 7400') and install.statusline_port(command()) == 7400
    assert install.set_statusline_port(7400) is None  # already there
    assert 'port 7401 (it sent to port 7400)' in install.set_statusline_port(7401)
    assert command().count('--port') == 1  # replaced, not stacked
    install.set_statusline_port(config.DEFAULT_PORT)
    assert '--port' not in command()
    install.set_statusline_port(7400)
    assert json.loads(settings().read_text())['model'] == 'opus'
    assert 'exactly' in install.uninstall_statusline()
    assert sha(settings()) == before


def test_changing_the_port_after_the_person_edited_settings_keeps_their_edit():
    write_settings({'theme': 'dark'})
    install.install_statusline()
    data = json.loads(settings().read_text())
    data['theme'] = 'light'
    settings().write_text(json.dumps(data))
    install.set_statusline_port(7400)
    now = json.loads(settings().read_text())
    assert now['theme'] == 'light' and now['statusLine']['command'].endswith('--port 7400')
    assert 'only the statusLine key' in install.uninstall_statusline()
    assert json.loads(settings().read_text()) == {'theme': 'light'}


def test_changing_the_port_leaves_a_status_line_that_is_not_ours_alone():
    write_settings({'theme': 'dark'})
    install.install_statusline()
    data = json.loads(settings().read_text())
    data['statusLine']['command'] = 'echo theirs'
    settings().write_text(json.dumps(data))
    before = settings().read_bytes()
    assert 'not Skyborne' in install.set_statusline_port(7400)
    assert settings().read_bytes() == before


def test_changing_the_port_without_a_status_line_does_nothing():
    assert install.set_statusline_port(7400) is None
    assert not settings().exists()


@pytest.mark.parametrize('command, port', [
    ('/x/python -m skyborne statusline', 7317), ('/x/python -m skyborne statusline --port 7400', 7400),
    ('"C:\\Program Files\\Python\\python.exe" -m skyborne statusline --port 8000', 8000),
    ("'/Users/me/My Dir/python' -m skyborne statusline --port 7400", 7400),
    ('echo mine', None), (None, None), ({'a': 1}, None),
    # anything but the exact command Skyborne writes is somebody's own, and is never rewritten
    ('/x/python -m skyborne statusline --port 7400 | head -1', None), ('/x/python -m skyborne statusline 2>/dev/null', None),
    ('/x/python -m skyborne statusline; true', None), ('/x/python -m skyborne statusline --port=7400', None),
    ('echo hi; /x/python -m skyborne statusline', None), ('/x/python -m skyborne statusline --port 7400 --other', None)])
def test_statusline_port_reads_the_port_from_the_command(command, port):
    assert install.statusline_port(command) == port


def test_a_wrapped_status_line_is_never_rewritten():
    install.install_statusline()
    data = json.loads(settings().read_text())
    data['statusLine']['command'] = '/x/python -m skyborne statusline --port 7400 | head -1'  # the person wrapped it
    settings().write_text(json.dumps(data))
    before = settings().read_bytes()
    assert 'not Skyborne' in install.set_statusline_port(7317)
    assert settings().read_bytes() == before


def test_a_leftover_skyborne_status_line_is_not_remembered_as_the_previous_one():
    """Running our own command as 'the previous status line' would make it run itself again and again."""
    ours = install.statusline_command(7400)
    write_settings({'statusLine': {'type': 'command', 'command': ours}})
    original = sha(settings())
    assert install.install_statusline() is None
    assert install.previous_statusline_command() is None
    install.uninstall_statusline()
    assert sha(settings()) == original  # put back as it was, not as a "previous status line" to run
    # and a state written by an older build, with our own command as "previous", is ignored too
    write_settings({'theme': 'dark'})
    install.install_statusline()
    state = install._load_state()
    state['statusline']['previous'] = {'type': 'command', 'command': ours}
    install._save_state(state)
    assert install.previous_statusline_command() is None


def test_changing_the_port_when_settings_json_is_gone_is_not_an_error():
    install.install_statusline()
    settings().unlink()
    note = install.set_statusline_port(7400)
    assert 'no longer exists' in note and 'skyborne uninstall' in note
    assert not settings().exists()


def test_changing_the_port_when_settings_json_is_broken_is_an_error_and_leaves_it_alone():
    install.install_statusline()
    settings().write_text('{not json')
    with pytest.raises(install.InstallError):
        install.set_statusline_port(7400)
    assert settings().read_text() == '{not json'


@pytest.mark.skipif(os.name == 'nt', reason='file modes')
def test_changing_the_port_keeps_the_file_permissions():
    write_settings({'theme': 'dark'})
    os.chmod(settings(), 0o600)
    install.install_statusline()
    install.set_statusline_port(7400)
    assert (settings().stat().st_mode & 0o777) == 0o600


def test_a_windows_style_command_is_moved_too():
    install.install_statusline()
    command = '"C:\\Program Files\\Python 3.12\\python.exe" -m skyborne statusline'  # how list2cmdline writes it on Windows
    data = json.loads(settings().read_text())
    data['statusLine']['command'] = command
    settings().write_text(json.dumps(data))
    assert install.statusline_port(command) == config.DEFAULT_PORT
    install.set_statusline_port(7400)
    assert json.loads(settings().read_text())['statusLine']['command'] == command + ' --port 7400'
    install.set_statusline_port(config.DEFAULT_PORT)
    assert json.loads(settings().read_text())['statusLine']['command'] == command
