"""The small commands Claude Code runs: `skyborne statusline` and `skyborne hook`.

Both read Claude Code's JSON from stdin, pass it to the local server and never fail loudly: if
Skyborne isn't running they carry on as if it was never installed. They import only what they
need, so each run starts fast.
"""
import sys


def _post(path, data: bytes, port: int, timeout: float) -> bool:
    import urllib.request
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=data, method='POST',
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def hook(port: int) -> int:
    """Forward one hook event. Always exits 0 with no output, so Claude Code never shows an error."""
    try:
        _post('/hook', sys.stdin.buffer.read(), port, 0.5)
    except BaseException:
        pass
    return 0


def _line(data: bytes) -> str:
    import json
    try:
        s = json.loads(data)
    except ValueError:
        return 'Skyborne'
    if not isinstance(s, dict):
        return 'Skyborne'
    parts = ['Skyborne']
    model = s.get('model') if isinstance(s.get('model'), dict) else {}
    if isinstance(model.get('display_name'), str):
        parts.append(model['display_name'])
    cost = s.get('cost') if isinstance(s.get('cost'), dict) else {}
    if isinstance(cost.get('total_cost_usd'), (int, float)):
        parts.append(f"${cost['total_cost_usd']:.2f}")
    ctx = s.get('context_window') if isinstance(s.get('context_window'), dict) else {}
    if isinstance(ctx.get('used_percentage'), (int, float)):
        parts.append(f"Context {round(ctx['used_percentage'])}%")
    return ' · '.join(parts)


def statusline(port: int) -> int:
    """Forward the status line's JSON, then print a line: the earlier status line's, if there was one."""
    try:
        data = sys.stdin.buffer.read()
    except BaseException:
        return 0
    _post('/statusline', data, port, 0.3)
    from .install import previous_statusline_command
    previous = previous_statusline_command()
    if previous:
        import subprocess
        try:
            r = subprocess.run(previous, shell=True, input=data, capture_output=True, timeout=5)
            sys.stdout.buffer.write(r.stdout)
            return 0
        except Exception:
            pass
    sys.stdout.buffer.write((_line(data) + '\n').encode('utf-8'))  # UTF-8 on every platform
    sys.stdout.flush()
    return 0
