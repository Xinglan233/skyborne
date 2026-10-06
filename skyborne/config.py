"""Where Skyborne keeps its files, and the few settings it reads."""
import json
import os
import pathlib

DEFAULT_PORT = 7317
DEFAULT_RETENTION_DAYS = 30
DEFAULT_APPROVAL_TIMEOUT = 600   # seconds the PermissionRequest hook waits for an answer from the page
MIN_APPROVAL_TIMEOUT = 30
MAX_APPROVAL_TIMEOUT = 3600


def home() -> pathlib.Path:
    """Skyborne's own folder: ~/.skyborne, or $SKYBORNE_HOME (tests use a temporary one)."""
    return pathlib.Path(os.environ.get('SKYBORNE_HOME') or pathlib.Path.home() / '.skyborne')


def ensure_home() -> pathlib.Path:
    """Create Skyborne's folder if needed, private to its owner: it holds prompts, tool output and settings backups."""
    h = home()
    h.mkdir(mode=0o700, parents=True, exist_ok=True)
    return h


def claude_dir() -> pathlib.Path:
    """Claude Code's configuration folder: ~/.claude, or $CLAUDE_CONFIG_DIR when it's set."""
    return pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR') or pathlib.Path.home() / '.claude')


def db_path() -> pathlib.Path:
    return home() / 'skyborne.db'


def load() -> dict:
    """~/.skyborne/config.json, if there is one. Unknown keys are ignored."""
    try:
        data = json.loads((home() / 'config.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def retention_days() -> int:
    days = load().get('retention_days', DEFAULT_RETENTION_DAYS)
    return days if isinstance(days, int) and not isinstance(days, bool) and days > 0 else DEFAULT_RETENTION_DAYS


def approval_timeout() -> int:
    """`approval_timeout_seconds` from config.json: how long a permission request waits for the page (kept
    within 30 to 3600 s, default 600). `skyborne install` writes it into the plugin, so a change needs a
    re-install."""
    v = load().get('approval_timeout_seconds', DEFAULT_APPROVAL_TIMEOUT)
    if not isinstance(v, int) or isinstance(v, bool):
        return DEFAULT_APPROVAL_TIMEOUT
    return min(max(v, MIN_APPROVAL_TIMEOUT), MAX_APPROVAL_TIMEOUT)


def load_state() -> dict:
    """~/.skyborne/state.json: what Skyborne remembers between runs (e.g. that it asked about importing)."""
    try:
        data = json.loads((home() / 'state.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict):
    path = ensure_home() / 'state.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, path)
