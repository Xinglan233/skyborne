"""Take personal and secret data out of recorded sessions before they leave this machine
(`skyborne record`, and the test fixtures made from real sessions).

A Scrubber rewrites every string it is given (values and dict keys, at any depth):
1. ids: session and prompt UUIDs, `toolu_`/`msg_`/`req_` ids and helper ids become stable fakes, the
   same real id always the same fake, so the links between events survive;
2. secrets: private-key blocks, passwords in URLs, keys and tokens with a known prefix, JWTs, bearer tokens, the value of
   anything named like a secret (`PASSWORD=…`, `"token": "…"`, `--password …`, a `password` key), whatever
   it looks like, and long random-looking strings become `[secret]`;
3. the person: their home folder (posix, Windows, JSON-escaped, URL-encoded and the dash-encoded
   form Claude Code uses for project folders), login name, full name, git name and email, and the
   computer's name;
4. emails become `user@example.com`.
It counts what it replaced (the scrub report). `leaks()` then searches the finished output for anything
that should be gone; `skyborne record` refuses to write a file with a single hit.
"""
import collections
import dataclasses
import getpass
import math
import os
import re
import socket
import subprocess

UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
PREFIXED = re.compile(r'\b(toolu|msg|req)_[A-Za-z0-9]{10,}')
AGENT = re.compile(r'\ba[0-9a-f]{16}\b')
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}')
SAFE_EMAIL = 'user@example.com'

# names that say their value is a secret (NAME=value, "name": "value", --name value, a dict key)
_NAMES = r'api[_-]?key|access[_-]?key|secret(?:[_-]?key)?|token|passw(?:or)?d|pwd|passphrase|authorization|auth|credentials?|private[_-]?key'
SECRET_KEY = re.compile(r'(?i)(?:' + _NAMES + r')$')
# the value after such a name, whatever its length or letters: a quoted phrase (JSON-escaped quotes too) or
# a bare word. Left alone: the `[secret]` placeholder itself, and in JSON an object or list, a literal
# (null, true…) or the second `=` of a comparison
_VALUE = (r'(?:(?P<e>\\?)(?P<q>["\']))?'
          r'(?(q)(?!\[secret\](?P=e)(?P=q))(?:\\[^\n"\']|(?!(?P=q))[^\n\\])+(?P=e)(?P=q)'
          r'|(?!\[secret\](?![^\s"\',;]))(?![\\=])(?![\[{](?:[\s"\'\[\]{}]|$))(?!(?:null|true|false|none)\b)[^\s"\',;]+)')


def _hide(m):
    q = (m.group('e') or '') + (m.group('q') or '')
    return m.group('pre') + q + '[secret]' + q


# (name, pattern, replacement): the order matters (a key block before the long-string rule)
SECRETS = [
    ('private key', re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)', re.S), '[secret]'),
    ('url password', re.compile(r'(?i)(?P<pre>\b[a-z][a-z0-9+.-]*://[^\s:/@"\']+:)(?!\[secret\]@)[^\s/@"\']+(?=@)'), r'\g<pre>[secret]'),
    ('api key', re.compile(r'\bsk-ant-[A-Za-z0-9_-]{16,}'), '[secret]'),
    ('api key', re.compile(r'\bsk-(?:proj-|svcacct-|live_|test_)?[A-Za-z0-9_-]{20,}'), '[secret]'),
    ('token', re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}'), '[secret]'),
    ('token', re.compile(r'\bxox[abposr]-[A-Za-z0-9-]{10,}'), '[secret]'),
    ('token', re.compile(r'\bglpat-[A-Za-z0-9_-]{20,}|\bnpm_[A-Za-z0-9]{30,}|\bAIza[0-9A-Za-z_-]{35}'), '[secret]'),
    ('access key', re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'), '[secret]'),
    ('token', re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'), '[secret]'),
    ('token', re.compile(r'(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}'), r'\1 [secret]'),
    # an Authorization header's credential, however short (the generic rule below takes only its first word)
    ('token', re.compile(r'(?i)(?P<pre>\b(?:proxy-)?authorization(?:\\?["\'])?[ \t]*[:=][ \t]*(?:\\?["\'])?'
                         r'(?:bearer|basic|token|digest|negotiate)[ \t]+)(?!\[secret\])[^\s"\',;\\]+'), r'\g<pre>[secret]'),
    # NAME=value and "name": "value" where the name ends in a secret word, and --password value flags
    ('secret value', re.compile(r'(?i)(?P<pre>\b[A-Za-z0-9_.-]*?(?:' + _NAMES + r')(?:\\?["\'])?[ \t]*[:=][ \t]*)' + _VALUE), _hide),
    ('secret value', re.compile(r'(?i)(?P<pre>(?<![A-Za-z0-9])--?(?:api[_-]?key|access[_-]?key|secret|token|password|passwd|pwd|auth[_-]?token'
                                r'|credentials?)[ =])' + _VALUE), _hide),
]
LONG = re.compile(r'[A-Za-z0-9+/=_-]{32,}')


def _random_looking(s):
    """A long run of mixed letters and digits with high entropy: a key, a token, a hash in base64."""
    if not (re.search(r'[a-z]', s) and re.search(r'[A-Z]', s) and re.search(r'[0-9]', s)):
        return False
    if sum(s.count(ch) for ch in '/-_') > len(s) / 8:  # paths and slugs, not keys (base64 has few separators)
        return False
    counts = collections.Counter(s)
    entropy = -sum(n / len(s) * math.log2(n / len(s)) for n in counts.values())
    return entropy >= 4.0


@dataclasses.dataclass
class Identity:
    """The strings that point at this person and this computer."""
    home: str = ''
    user: str = ''
    names: tuple = ()
    emails: tuple = ()
    hosts: tuple = ()


def local_identity() -> Identity:
    home = os.path.expanduser('~')
    try:
        user = getpass.getuser()
    except Exception:
        user = os.path.basename(home)
    names, emails = [], []
    try:
        import pwd
        gecos = pwd.getpwuid(os.getuid()).pw_gecos.split(',')[0].strip()
        if gecos:
            names.append(gecos)
    except (ImportError, KeyError, AttributeError):
        pass
    for key, out in (('user.name', names), ('user.email', emails)):
        try:
            v = subprocess.run(['git', 'config', '--global', key], capture_output=True, text=True, timeout=3).stdout.strip()
            if v:
                out.append(v)
        except (OSError, subprocess.SubprocessError):
            pass
    host = socket.gethostname()
    hosts = tuple(h for h in {host, host.split('.')[0]} if len(h) >= 3)
    return Identity(home=home, user=user, names=tuple(dict.fromkeys(names)), emails=tuple(dict.fromkeys(emails)), hosts=hosts)


def _words(identity: Identity):
    """Names to replace inside any text: login, full names and each part of 4+ letters, hosts."""
    out = []
    if len(identity.user) >= 4:
        out.append(('username', identity.user))
    for n in identity.names:
        out.append(('name', n))
        out += [('name', part) for part in re.split(r'\s+', n) if len(part) >= 4]
    out += [('computer name', h) for h in identity.hosts]
    return sorted(set(out), key=lambda kv: -len(kv[1]))


class Scrubber:
    def __init__(self, identity: Identity = None, replace=()):
        """`replace`: extra (text, replacement) pairs, applied first (e.g. a repository's own path)."""
        self.identity = identity if identity is not None else local_identity()
        self.replace = list(replace)
        self.ids = {}
        self.counts = collections.Counter()
        self._homes = self._home_forms()
        self._words = [(kind, re.compile(r'(?<![A-Za-z0-9])' + re.escape(w) + r'(?![A-Za-z0-9])', re.I)) for kind, w in _words(self.identity)]

    def _home_forms(self):
        home, user = self.identity.home.rstrip('/\\'), self.identity.user
        forms = []
        if '\\' in home:  # Windows: C:\Users\name, also as JSON writes it and with forward slashes
            forms += [(home, 'C:\\Users\\user'), (home.replace('\\', '\\\\'), 'C:\\\\Users\\\\user'), (home.replace('\\', '/'), 'C:/Users/user')]
        elif home:
            forms += [(home, '/home/user'), (home.replace('/', '%2F'), '%2Fhome%2Fuser')]
        if home:
            forms.append((re.sub(r'[^A-Za-z0-9]', '-', home), '-home-user'))
        if user:
            forms += [(f'-Users-{user}-', '-home-user-'), (f'-home-{user}-', '-home-user-')]
        return [(re.compile(re.escape(a), re.I), b) for a, b in sorted(set(forms), key=lambda kv: -len(kv[0])) if a]

    def fake(self, real, kind):
        if real not in self.ids:
            n = sum(1 for k, _ in self.ids.values() if k == kind) + 1
            value = (f'00000000-0000-4000-8000-{n:012d}' if kind == 'uuid' else f'a{n:016x}' if kind == 'agent' else f'{kind}_{n:024d}')
            self.ids[real] = (kind, value)
            self.counts['ids'] += 1
        return self.ids[real][1]

    def _sub(self, pattern, repl, s, count_as):
        new, n = pattern.subn(repl, s)
        if n:
            self.counts[count_as] += n
        return new

    def text(self, s: str) -> str:
        for a, b in self.replace:
            if a and a != b and a in s:
                self.counts['paths'] += s.count(a)
                s = s.replace(a, b)
        for name, pattern, repl in SECRETS:
            s = self._sub(pattern, repl, s, 'secrets')
        s = UUID.sub(lambda m: self.fake(m.group(0).lower(), 'uuid'), s)
        s = PREFIXED.sub(lambda m: self.fake(m.group(0), m.group(1)), s)
        s = AGENT.sub(lambda m: self.fake(m.group(0), 'agent'), s)
        for pattern, repl in self._homes:
            s = self._sub(pattern, lambda m, r=repl: r, s, 'home paths')
        # any other home folder (another user, another machine): /Users/x, /home/x, C:\Users\x
        s = self._sub(re.compile(r'(?<![A-Za-z0-9])(/Users/|/home/)(?!user(?![A-Za-z0-9._-]))[A-Za-z0-9._-]+'), r'\1user', s, 'home paths')
        s = self._sub(re.compile(r'(?i)\b([A-Z]:(?:\\\\|\\)Users(?:\\\\|\\))(?!user\b)[A-Za-z0-9._ -]+?(?=\\|"|$)'), r'\1user', s, 'home paths')
        s = self._sub(re.compile(r'(/private)?/(tmp|var/folders/[^/\s]+/[^/\s]+/T)/claude-\d+/'), '/tmp/claude/', s, 'home paths')
        for kind, pattern in self._words:
            s = self._sub(pattern, 'User' if kind == 'name' else 'host' if kind == 'computer name' else 'user', s,
                          'names' if kind == 'name' else 'usernames' if kind == 'username' else 'computer names')
        s = self._sub(EMAIL, lambda m: m.group(0) if m.group(0) == SAFE_EMAIL else SAFE_EMAIL, s, 'emails')
        def secret(m):
            if not _random_looking(m.group(0)):
                return m.group(0)
            self.counts['secrets'] += 1
            return '[secret]'
        return LONG.sub(secret, s)

    def value(self, v, key=''):
        """Any JSON value; under a key named like a secret (`password`, `apiKey`…) a string or number goes whole."""
        if key and SECRET_KEY.search(key) and (isinstance(v, (int, float)) and not isinstance(v, bool) or isinstance(v, str) and v and v != '[secret]'):
            self.counts['secrets'] += 1
            return '[secret]'
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, list):
            return [self.value(x, key) for x in v]
        if isinstance(v, dict):
            return {self.text(k): self.value(x, k) for k, x in v.items()}
        return v

    def report(self) -> dict:
        return {k: v for k, v in sorted(self.counts.items()) if v > 0}


def leaks(text: str, identity: Identity) -> list:
    """What's still personal or secret in finished output: (kind, short masked sample) for each hit."""
    found = []

    def hit(kind, m):
        sample = m if isinstance(m, str) else m.group(0)
        found.append((kind, sample[:3] + '…' if len(sample) > 3 else '…'))

    low = text.lower()
    for kind, w in _words(identity):
        if re.search(r'(?<![a-z0-9])' + re.escape(w.lower()) + r'(?![a-z0-9])', low):
            hit(kind, w)
    for e in identity.emails:
        if e.lower() in low:
            hit('email', e)
    if identity.home and identity.home.lower() in low:
        hit('home path', identity.home)
    for m in EMAIL.finditer(text):
        if m.group(0) != SAFE_EMAIL:
            hit('email', m)
    for name, pattern, _ in SECRETS:
        for m in pattern.finditer(text):
            if '[secret]' not in m.group(0):
                hit(name, m)
    for m in LONG.finditer(text):
        if _random_looking(m.group(0)):
            hit('random-looking string', m)
    for m in re.finditer(r'(?<![A-Za-z0-9])(?:/Users/|/home/)(?!user(?![A-Za-z0-9._-]))[A-Za-z0-9._-]+', text):
        hit('home path', m)
    return found
