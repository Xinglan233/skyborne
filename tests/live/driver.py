"""Drive a real interactive `claude` session in a pseudo-terminal, for the opt-in live tests.

Standard library only; macOS and Linux (it needs `pty`). The screen is read as plain text with
the terminal's escape codes removed, so checks look for words, not positions.
"""
import fcntl
import os
import re
import select
import signal
import struct
import subprocess
import termios
import threading
import time

# Variables a Claude Code session sets for the processes it starts. A test session must not
# inherit them: CLAUDE_CODE_CHILD_SESSION turns transcript saving off (docs/FINDINGS.md).
PARENT_SESSION_VARS = (
    'CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CODE_SESSION_ID',
    'CLAUDE_CODE_SESSION_ATTENDED', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_EXECPATH',
    'CLAUDE_CODE_SSE_PORT', 'CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN',
)

ENTER, ESC, CTRL_C, CTRL_O = '\r', '\x1b', '\x03', '\x0f'

_ESCAPES = re.compile(
    rb'\x1b\[[0-?]*[ -/]*[@-~]'          # CSI: colours, cursor moves, clears
    rb'|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'  # OSC: titles, links
    rb'|\x1b[PX^_][^\x1b]*\x1b\\'         # DCS and friends
    rb'|\x1b[()][0-9A-Za-z]|\x1b[@-Z\\-_=>]'
)


def clean_env(extra=None):
    env = {k: v for k, v in os.environ.items() if k not in PARENT_SESSION_VARS}
    env.update(extra or {})
    return env


class Session:
    """One `claude` process on a pseudo-terminal. Everything it prints is kept in `raw`."""

    def __init__(self, args, cwd, env=None, cols=140, rows=45):
        self.raw = bytearray()
        self._lock = threading.Lock()
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

        def own_terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self.proc = subprocess.Popen(
            args, cwd=cwd, env=env if env is not None else clean_env(),
            stdin=slave, stdout=slave, stderr=slave, preexec_fn=own_terminal, close_fds=True,
        )
        os.close(slave)
        self.fd = master
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        while True:
            try:
                ready, _, _ = select.select([self.fd], [], [], 0.2)
                if not ready:
                    if self.proc.poll() is not None:
                        return
                    continue
                chunk = os.read(self.fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            # answer "where is the cursor?" so a program waiting on it carries on
            if b'\x1b[6n' in chunk:
                os.write(self.fd, b'\x1b[1;1R')
            with self._lock:
                self.raw += chunk

    def mark(self):
        with self._lock:
            return len(self.raw)

    def text(self, since=0):
        with self._lock:
            data = bytes(self.raw[since:])
        return _ESCAPES.sub(b'', data).decode('utf-8', 'replace').replace('\r', '')

    def wait_for(self, pattern, timeout=60, since=0):
        """Wait until `pattern` (a regex) shows up in the text after `since`; return the match or None."""
        rx = re.compile(pattern)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            m = rx.search(self.text(since))
            if m:
                return m
            if self.proc.poll() is not None:
                break
            time.sleep(0.2)
        return None

    def send(self, keys):
        os.write(self.fd, keys.encode())

    def type_line(self, line):
        # typed in pieces, then Enter on its own, the way a person would
        for i in range(0, len(line), 16):
            self.send(line[i:i + 16])
            time.sleep(0.05)
        time.sleep(0.3)
        self.send(ENTER)

    def close(self, timeout=10):
        if self.proc.poll() is None:
            self.send(CTRL_C)
            time.sleep(0.4)
            self.send(CTRL_C)
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(5)
        try:
            os.close(self.fd)
        except OSError:
            pass
