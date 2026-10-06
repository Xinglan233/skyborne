"""Install and uninstall Skyborne's Claude Code plugin, and (only with the person's OK) its status line.

The plugin is generated here, so the repository's plugin/ folder and an installed copy can't drift
apart (a test compares them). It goes into Claude Code's skills folder, which loads any plugin
there as `skyborne@skills-dir`. A marker file says the folder is ours; nothing else is ever removed.

A plugin can't set the status line (only `agent` and `subagentStatusLine` take effect from a
plugin), so that one key is written into Claude Code's settings.json: after a backup, touching
only `statusLine`, written atomically. Uninstall puts back the exact original bytes when the file
is still as we left it, and otherwise restores just that key.
"""
import hashlib
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from . import __version__, config

PLUGIN_NAME = 'skyborne'
MARKER = '.skyborne-install.json'
# every hook event Skyborne records (no MessageDisplay: a slow answer would freeze the terminal)
HOOK_EVENTS = (
    'SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure',
    'PermissionRequest', 'PermissionDenied', 'Notification', 'SubagentStart', 'SubagentStop',
    'Stop', 'StopFailure', 'PreCompact', 'PostCompact', 'SessionEnd',
)


def manifest():
    return {'name': PLUGIN_NAME, 'version': __version__, 'author': {'name': 'Mirza Ishraq Yeahia'},
            'description': "Sends this session's Claude Code events to the local Skyborne server."}


def permission_command(port, timeout):
    """The one hook that waits: curl holds the request open until the page answers, the person answers in
    the terminal, or the time runs out, and prints the server's answer (the decision JSON, or nothing).
    A shell runs it so `exit 0` can follow: if Skyborne isn't running (or is an older version without
    /permission, which `-f` keeps quiet), the hook still ends with no output and no error, and the dialog
    in the terminal works as usual (docs/FINDINGS.md). `'@-'` is quoted for PowerShell's sake.
    The entrypoint tells the server whether anyone is at a terminal: `claude -p` and the Agent SDK
    (`sdk-*`) are never held, so scripts aren't kept waiting."""
    return (f"curl -sf -m {timeout} -H 'Content-Type: application/json' -H 'Expect:' "
            f"-H 'X-Skyborne-Wait: {max(1, timeout - 5)}' -H \"X-Skyborne-Entrypoint: $CLAUDE_CODE_ENTRYPOINT\" "
            f"--data-binary '@-' http://127.0.0.1:{port}/permission; exit 0")


def hooks_json(port=config.DEFAULT_PORT, timeout=config.DEFAULT_APPROVAL_TIMEOUT):
    """Each event is posted by curl, run directly (no shell) as a background hook: it never blocks
    Claude Code, and if Skyborne isn't running nothing shows in the terminal (docs/FINDINGS.md).
    The exception is PermissionRequest, which waits up to `timeout` seconds for an answer from the page."""
    handler = {'type': 'command', 'command': 'curl',
               'args': ['-s', '-m', '2', '-H', 'Content-Type: application/json', '--data-binary', '@-',
                        f'http://127.0.0.1:{port}/hook'],
               'async': True}
    hooks = {event: [{'hooks': [handler]}] for event in HOOK_EVENTS}
    hooks['PermissionRequest'] = [{'hooks': [{'type': 'command', 'command': permission_command(port, timeout),
                                              'timeout': timeout}]}]
    return {'hooks': hooks}


def write_plugin(target: pathlib.Path, port=config.DEFAULT_PORT, timeout=config.DEFAULT_APPROVAL_TIMEOUT):
    (target / '.claude-plugin').mkdir(parents=True, exist_ok=True)
    (target / 'hooks').mkdir(parents=True, exist_ok=True)
    (target / '.claude-plugin' / 'plugin.json').write_text(json.dumps(manifest(), indent=2) + '\n', encoding='utf-8')
    (target / 'hooks' / 'hooks.json').write_text(json.dumps(hooks_json(port, timeout), indent=2) + '\n', encoding='utf-8')


def plugin_dir():
    return config.claude_dir() / 'skills' / PLUGIN_NAME


def state_path():
    return config.home() / 'install.json'


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def statusline_command(port):
    """The command Claude Code runs for the status line: this Python, so it works without PATH."""
    args = [sys.executable, '-m', 'skyborne', 'statusline']
    if port != config.DEFAULT_PORT:
        args += ['--port', str(port)]
    return subprocess.list2cmdline(args) if os.name == 'nt' else shlex.join(args)


def _atomic_write(path: pathlib.Path, data: bytes, mode=None):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_state():
    try:
        data = json.loads(state_path().read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state):
    config.ensure_home()
    _atomic_write(state_path(), (json.dumps(state, indent=2) + '\n').encode('utf-8'))


class InstallError(Exception):
    pass


def install_plugin(port=config.DEFAULT_PORT):
    target = plugin_dir()
    if target.exists() and not (target / MARKER).exists():
        raise InstallError(f'{target} already exists and was not made by Skyborne, so it was left alone.')
    if target.exists():
        shutil.rmtree(target)
    timeout = config.approval_timeout()
    write_plugin(target, port, timeout)
    (target / MARKER).write_text(json.dumps({'version': __version__, 'port': port, 'approval_timeout': timeout,
                                             'at': int(time.time())}) + '\n', encoding='utf-8')
    return target


def install_statusline(port=config.DEFAULT_PORT):
    """Point Claude Code's status line at Skyborne. Returns what was there before (None if nothing)."""
    settings = config.claude_dir() / 'settings.json'
    state = _load_state()
    if state.get('statusline'):
        raise InstallError('The status line is already installed. Run `skyborne uninstall` first to change it.')
    existed = settings.exists()
    raw = settings.read_bytes() if existed else b''
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise InstallError(f'{settings} is not valid JSON, so it was left alone.')
    if not isinstance(data, dict):
        raise InstallError(f'{settings} does not hold a JSON object, so it was left alone.')
    backups = config.ensure_home() / 'backups'
    backups.mkdir(mode=0o700, exist_ok=True)
    backup = backups / f'settings-{time.strftime("%Y%m%d-%H%M%S")}.json'
    if existed:
        backup.write_bytes(raw)
    previous, had_key = data.get('statusLine'), 'statusLine' in data
    data['statusLine'] = {'type': 'command', 'command': statusline_command(port)}
    new = (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    settings.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(settings, new, (settings.stat().st_mode & 0o7777) if existed else None)
    state['statusline'] = {'settings': str(settings), 'existed': existed, 'backup': str(backup) if existed else None,
                           'previous': previous, 'had_key': had_key,
                           'written_sha256': _sha(new)}
    _save_state(state)
    return previous


def uninstall_statusline():
    """Undo install_statusline. Returns a sentence saying what was done."""
    state = _load_state()
    s = state.get('statusline')
    if not s:
        return 'The status line was not installed by Skyborne; nothing to undo.'
    settings = pathlib.Path(s['settings'])
    current = settings.read_bytes() if settings.exists() else None
    if current is not None and _sha(current) == s['written_sha256']:
        # untouched since we wrote it: put back exactly what was there
        if s['existed']:
            _atomic_write(settings, pathlib.Path(s['backup']).read_bytes(), settings.stat().st_mode & 0o7777)
        else:
            settings.unlink()
        msg = 'Restored settings.json exactly as it was before Skyborne.'
    elif current is not None:
        try:
            data = json.loads(current)
        except ValueError:
            raise InstallError(f'{settings} changed since install and is not valid JSON now; restore statusLine by hand '
                               f'(the original file is at {s["backup"]}).')
        if s['had_key']:
            data['statusLine'] = s['previous']
        else:
            data.pop('statusLine', None)
        _atomic_write(settings, (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode('utf-8'), settings.stat().st_mode & 0o7777)
        msg = 'settings.json changed since install, so only the statusLine key was put back; everything else was kept.'
    else:
        msg = 'settings.json no longer exists; nothing to restore.'
    del state['statusline']
    _save_state(state)
    return msg


def uninstall_plugin():
    target = plugin_dir()
    if not target.exists():
        return 'The plugin was not installed.'
    if not (target / MARKER).exists():
        return f'{target} was not made by Skyborne, so it was left alone.'
    shutil.rmtree(target)
    return f'Removed {target}.'


def previous_statusline_command():
    """The status line command that was there before ours, if any (ours runs it too)."""
    prev = (_load_state().get('statusline') or {}).get('previous')
    return prev.get('command') if isinstance(prev, dict) and isinstance(prev.get('command'), str) else None
