"""`skyborne doctor`: check the setup and explain any problem in plain words."""
import json
import os
import pathlib
import platform
import shutil
import sqlite3
import sys
import time
import urllib.request

from . import config, install

OK, WARN, FAIL = 'OK', 'Warning', 'Problem'


def _json(path):
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def managed_settings_path():
    system = platform.system()
    if system == 'Darwin':
        return pathlib.Path('/Library/Application Support/ClaudeCode/managed-settings.json')
    if system == 'Windows':
        return pathlib.Path(os.environ.get('ProgramData', r'C:\ProgramData')) / 'ClaudeCode' / 'managed-settings.json'
    return pathlib.Path('/etc/claude-code/managed-settings.json')


def health(port, timeout=1.0):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=timeout) as r:
            data = json.loads(r.read())
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def port_in_use(port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(('127.0.0.1', port)) == 0


def checks(port=config.DEFAULT_PORT):
    """Every check as (level, sentence)."""
    out = []
    v = sys.version_info
    out.append((OK, f'Python {v.major}.{v.minor}.') if v >= (3, 11) else (FAIL, f'Python {v.major}.{v.minor} is too old: Skyborne needs 3.11 or newer.'))

    out.append((OK, 'curl is installed (the plugin uses it to send events).') if shutil.which('curl')
               else (FAIL, 'curl is not on PATH, so the plugin cannot send events. Install curl and open a new terminal.'))

    h = health(port)
    if h and h.get('name') == 'skyborne':
        out.append((OK, f"The server is running on 127.0.0.1:{port} (version {h.get('version')})."))
        if h.get('lastEventAt'):
            ago = int(time.time() - h['lastEventAt'] / 1000)
            out.append((OK, f"Last event received {ago} seconds ago ({h.get('events')} stored)."))
        else:
            out.append((WARN, 'No events received yet. Start a new Claude Code session after installing the plugin.'))
    elif port_in_use(port):
        out.append((FAIL, f'Port {port} is taken by another program. Run `skyborne --port <other>` and `skyborne install --port <other>`.'))
    else:
        out.append((WARN, f'The server is not running. Start it with `skyborne`. Claude Code works normally without it.'))

    target = install.plugin_dir()
    marker = _json(target / install.MARKER)
    if not (target / '.claude-plugin' / 'plugin.json').exists():
        out.append((FAIL, f'The plugin is not installed. Run `skyborne install`.'))
    elif not marker:
        out.append((WARN, f'{target} exists but was not installed by Skyborne.'))
    else:
        out.append((OK, f'The plugin is installed in {target}.'))
        if marker.get('port') != port:
            out.append((WARN, f"The plugin sends to port {marker.get('port')}, not {port}. Run `skyborne install --port {port}` to change it."))
        installed = _json(target / 'hooks' / 'hooks.json')
        wanted_port = marker.get('port') if isinstance(marker.get('port'), int) else port
        if installed != install.hooks_json(wanted_port, config.approval_timeout()):
            older = installed != install.hooks_json(wanted_port, marker.get('approval_timeout') or 0)
            out.append((WARN, 'The plugin is from an older Skyborne, so approvals from the city are off. Run `skyborne install`.' if older
                        else 'approval_timeout_seconds changed since the plugin was installed. Run `skyborne install` to use it.'))

    user = _json(config.claude_dir() / 'settings.json')
    managed = _json(managed_settings_path())
    enabled = {**(user.get('enabledPlugins') or {}), **(managed.get('enabledPlugins') or {})}
    if enabled.get(f'{install.PLUGIN_NAME}@skills-dir') is False:
        out.append((FAIL, f'The plugin is turned off in settings (enabledPlugins "{install.PLUGIN_NAME}@skills-dir": false).'))
    for name, s in (('your settings', user), ('managed settings', managed)):
        if s.get('disableAllHooks') is True:
            out.append((FAIL, f'disableAllHooks is on in {name}, so no hooks run, Skyborne\'s included.'))
        strict = s.get('strictKnownMarketplaces')
        if isinstance(strict, list) and not any(isinstance(m, dict) and m.get('source') == 'skills-dir' for m in strict):
            out.append((FAIL, f'strictKnownMarketplaces in {name} blocks plugins in the skills folder. Add {{"source": "skills-dir"}} to it.'))
    if managed.get('allowManagedHooksOnly') is True:
        out.append((FAIL, 'Managed settings allow only managed hooks (allowManagedHooksOnly), so Skyborne\'s hooks will not run.'))

    command = ((user.get('statusLine') or {}) if isinstance(user.get('statusLine'), dict) else {}).get('command', '')
    if 'skyborne' in str(command) and 'statusline' in str(command):
        out.append((OK, 'The status line sends cost, context and rate limits to Skyborne.'))
    else:
        out.append((WARN, 'The status line is not installed (optional: it adds cost, context and rate limits). Run `skyborne install` to add it.'))

    try:
        config.ensure_home()
        with sqlite3.connect(config.db_path()) as db:
            db.execute('CREATE TABLE IF NOT EXISTS _doctor (x)')
            db.execute('DROP TABLE _doctor')
        out.append((OK, f'The database can be written: {config.db_path()}.'))
    except Exception as e:
        out.append((FAIL, f'The database at {config.db_path()} cannot be written ({type(e).__name__}).'))

    out.append((OK, 'Reminder: Claude Code runs hooks only after you accept the folder trust prompt.'))
    return out


def run(port=config.DEFAULT_PORT):
    results = checks(port)
    for level, sentence in results:
        print(f'{level:8} {sentence}')
    return 1 if any(level == FAIL for level, _ in results) else 0
