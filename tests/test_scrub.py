"""The scrubber: planted secrets and personal details, in every form a session can carry them, never
survive; ids stay linked; ordinary text stays readable."""
import json

from skyborne.scrub import Identity, Scrubber, leaks

ADA = Identity(home='/Users/adalove', user='adalove', names=('Ada Lovelace',), emails=('ada@analytical.engine',),
               hosts=('Adas-MacBook-Pro.local', 'Adas-MacBook-Pro'))
SESSION = '6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b'

# made-up secrets; the provider-shaped ones are split across two string literals so secret scanners don't flag them
PLANTED = {
    'anthropic key': 'sk-ant-' 'api03-Zx8vQ2mN7pL4kR9tY6wE3uI1oP5aS0dF-GhJkL_ZxCvBnM2qW8eR4tY6uI0oPAA',
    'openai key': 'sk-proj-' 'Q2w3E4r5T6y7U8i9O0pAsDfGhJkLzXcVbNm1234',
    'github token': 'ghp_' '1A2b3C4d5E6f7G8h9I0jK1lM2nO3pQ4rS5tU',
    'github pat': 'github_pat_' '11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyzABCDEFGHIJ012345',
    'slack token': 'xoxb-' '1234567890-0987654321-AbCdEfGhIjKlMnOpQrStUvWx',
    'aws key id': 'AKIA' 'IOSFODNN7EXAMPLE',
    'aws secret': 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
    'google key': 'AIza' 'SyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY',
    'gitlab token': 'glpat-' 'xYz12AbC34dEf56GhI78jK',
    'npm token': 'npm_' 'AbCdEfGhIjKlMnOpQrStUvWxYz0123456789',
    'jwt': 'eyJhbG' 'ciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkFkYSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c',
    'bearer': 'Bearer 7f3aK9mQ2xZ8vL4nR6tY1wE5uI0oP3aS',
    'password': 'hunter2Secret!9',
    'db url password': 'p4ssW0rd_xyz',
    'private key body': 'MIIE' 'vQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7',
    # named as secrets but shaped like ordinary words: no digits, short, or with spaces
    'plain password': 'correcthorsebatterystaple',
    'passphrase': 'purple monkey dishwasher',
    'short token': 'xyzzy',
    'key in a dict': 'plainwordkey',
    'bracketed, quoted': '[abc]defgh',
    'bracketed, bare': '[xyz]uvwqrs',
    'short bearer': 'zq9shrt',
    'short basic': 'YTpiYw==',
    'url password': 'r0ses!red',
}
PERSONAL = ['adalove', 'Ada Lovelace', 'Lovelace', 'ada@analytical.engine', 'someone.else@company.co.uk', 'Adas-MacBook-Pro']


def planted_session():
    """One session's worth of hook payloads with every secret and personal detail planted somewhere."""
    k = PLANTED
    return [
        {'session_id': SESSION, 'hook_event_name': 'SessionStart', 'cwd': '/Users/adalove/projects/engine',
         'transcript_path': f'/Users/adalove/.claude/projects/-Users-adalove-projects-engine/{SESSION}.jsonl'},
        {'session_id': SESSION, 'hook_event_name': 'UserPromptSubmit',
         'prompt': f"Hi, I'm Ada Lovelace (ada@analytical.engine, cc someone.else@company.co.uk). Send Authorization: Bearer "
                   f"{k['short bearer']} and Proxy-Authorization: Basic {k['short basic']}. Use {k['anthropic key']} "
                   f"and {k['openai key']}. My laptop is Adas-MacBook-Pro.local and I log in as adalove."},
        {'session_id': SESSION, 'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 'toolu_01AbCdEfGhIjKlMnOpQrStUv',
         'tool_input': {'command': f"export GITHUB_TOKEN={k['github token']} && curl -H 'Authorization: {k['bearer']}' "
                                   f"postgres://ada:{k['db url password']}@db.internal/x --password {k['password']} && "
                                   f"export DATABASE_PASSWORD={k['plain password']} DB_PASSPHRASE=\"{k['passphrase']}\" "
                                   f"&& curl -d '{{\"token\": \"{k['short token']}\"}}' "
                                   f"&& API_SECRET=\"{k['bracketed, quoted']}\" APP_TOKEN={k['bracketed, bare']} ./run "
                                   f"&& psql postgres://alice:{k['url password']}@localhost/app"}},
        {'session_id': SESSION, 'hook_event_name': 'PostToolUse', 'tool_name': 'Read', 'tool_use_id': 'toolu_01AbCdEfGhIjKlMnOpQrStUv',
         'tool_input': {'file_path': 'C:\\Users\\adalove\\secrets\\.env'},
         'tool_response': {'file': {'content': f"AWS_ACCESS_KEY_ID={k['aws key id']}\nAWS_SECRET_ACCESS_KEY={k['aws secret']}\n"
                                               f"GOOGLE_API_KEY={k['google key']}\nGITLAB={k['gitlab token']}\nNPM_TOKEN={k['npm token']}\n"
                                               f"SLACK={k['slack token']}\nPAT={k['github pat']}\nsession={k['jwt']}\n"
                                               "-----BEGIN RSA " f"PRIVATE KEY-----\n{k['private key body']}\n-----END RSA " "PRIVATE KEY-----\n"}}},
        {'session_id': SESSION, 'hook_event_name': 'PostToolUseFailure', 'tool_use_id': 'toolu_01ZyXwVuTsRqPoNmLkJiHgFe',
         'error': 'ls: C:\\\\Users\\\\adalove\\\\Desktop: denied; see %2FUsers%2Fadalove%2Fnotes and /home/adalove/x',
         # a path as a key, and a value deep in a list
         'extra': {'/Users/adalove/projects/engine/src': [{'by': 'adalove'}, ['Lovelace']]},
         'config': {'apiKey': k['key in a dict'], 'auth': True, 'retries': 3}},
    ]


def test_no_planted_secret_or_personal_detail_survives():
    s = Scrubber(ADA)
    out = json.dumps(s.value(planted_session()))
    for name, secret in PLANTED.items():
        assert secret not in out, name
    for p in PERSONAL:
        assert p.lower() not in out.lower(), p
    assert '/Users/' not in out and 'Users\\\\adalove' not in out and '%2FUsers%2Fadalove' not in out
    assert leaks(out, ADA) == []
    r = s.report()
    assert r['secrets'] >= len(PLANTED) - 2 and r['home paths'] >= 5 and r['emails'] >= 2 and r['usernames'] >= 2 and r['names'] >= 2


def test_ids_become_stable_fakes_and_stay_linked():
    s = Scrubber(ADA)
    events = s.value(planted_session())
    sids = {e['session_id'] for e in events}
    assert len(sids) == 1 and SESSION not in sids
    assert events[2]['tool_use_id'] == events[3]['tool_use_id'] != events[4]['tool_use_id']
    assert sids.pop() in events[0]['transcript_path']  # the same id inside a path becomes the same fake


def test_ordinary_text_stays_readable():
    s = Scrubber(ADA)
    for text in ('$ npm test -- --watch=false', 'Reading src/components/Button.tsx', 'The secret word is pineapple.',
                 'Fixed 3 failing tests in /home/user/project', 'input_tokens: 123456', 'Rename the district to Rate limits',
                 'max_tokens=4096', 'auth: none'):
        assert s.text(text) == text, text


def test_the_leak_check_catches_what_was_not_scrubbed():
    raw = json.dumps(planted_session())
    kinds = {k for k, _ in leaks(raw, ADA)}
    assert {'username', 'name', 'email', 'home path', 'api key', 'token', 'access key', 'private key'} <= kinds
    assert all(sample.endswith('…') and len(sample) <= 4 for _, sample in leaks(raw, ADA))  # never prints the secret itself


def test_a_windows_home_is_scrubbed_too():
    win = Identity(home='C:\\Users\\Ada', user='Ada', names=(), emails=(), hosts=())
    s = Scrubber(win)
    out = json.dumps(s.value({'cwd': 'C:\\Users\\Ada\\code\\engine', 'p': 'C:/Users/Ada/code', 'j': 'C:\\\\Users\\\\Ada\\\\code'}))
    assert 'Ada' not in out
