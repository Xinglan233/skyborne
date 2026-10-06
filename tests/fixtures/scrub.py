"""Turn real recorded data into committable test fixtures.

    python tests/fixtures/scrub.py <raw samples dir> <session transcript .jsonl>
    python tests/fixtures/scrub.py --recording <recording.json> <name>
    python tests/fixtures/scrub.py --full <session transcript .jsonl> <name>

The second form scrubs a live test's recording (tmp/live/recording.json: the events Skyborne
stored) into tests/fixtures/recordings/<name>.json, and the session's transcripts into
tests/fixtures/transcripts/<name>/.

Writes tests/fixtures/payloads/ (one hook payload per file), tests/fixtures/timelines/ and
tests/fixtures/transcripts/s2/ (the session transcript, its helper transcripts and their .meta.json).
Every id (sessions, prompts, tool calls, messages, requests, helpers) is swapped for a stable fake,
consistently across all files so the links between them survive. Home paths, temp paths and the
recording folder's name are replaced. Transcripts keep only structure and numbers: message ids,
models, usage, stop reasons and block types; no prompt, answer, thinking or tool text.

The third form keeps a whole transcript (every line and field, the shape `skyborne import` reads),
with its helpers, for a session whose content is harmless (a live test's). Scrubbing is
skyborne.scrub's; a fixture with anything its leak check finds is not written.
"""
import json
import os
import pathlib
import re
import sys

OUT = pathlib.Path(__file__).resolve().parent
REPO = str(OUT.parents[1])
sys.path.insert(0, REPO)
from skyborne.scrub import Scrubber, leaks  # noqa: E402

_dashed = lambda path: re.sub(r'[^A-Za-z0-9]', '-', path)  # how Claude Code names a project's folders
SCRUB = Scrubber(replace=[(REPO + '/tmp/live/project', '/home/user/project'), (REPO, '/home/user/skyborne'),
                          (_dashed(REPO + '/tmp/live/project'), '-home-user-project'), (_dashed(REPO), '-home-user-skyborne'),
                          ('skyborne-spike', 'project')])
scrub_text, scrub_value = SCRUB.text, SCRUB.value
ids = SCRUB.ids


TAGS = ('task-id', 'tool-use-id', 'status')


def slim_line(d):
    """One transcript line, cut down to what Skyborne reads."""
    t = d.get('type')
    keep = {k: d[k] for k in ('type', 'isSidechain', 'agentId', 'timestamp', 'sessionId', 'origin') if k in d}
    if t == 'assistant':
        m = d.get('message') or {}
        blocks = []
        for b in m.get('content') or []:
            if isinstance(b, dict):
                blocks.append({k: b[k] for k in ('type', 'id', 'name') if k in b})
        keep['message'] = {k: m[k] for k in ('id', 'model', 'stop_reason', 'usage') if k in m}
        keep['message']['content'] = blocks
        return keep
    if t == 'user':
        m = d.get('message') or {}
        c = m.get('content')
        if isinstance(c, str):
            if c.startswith('<task-notification>'):
                parts = [f'<{tag}>{x.group(1)}</{tag}>' for tag in TAGS for x in [re.search(f'<{tag}>(.*?)</{tag}>', c, re.S)] if x]
                c = '<task-notification>\n' + '\n'.join(parts) + '\n</task-notification>'
            else:
                c = '[prompt]'
        elif isinstance(c, list):
            c = [{k: b[k] for k in ('type', 'tool_use_id', 'is_error') if k in b} for b in c if isinstance(b, dict)]
        keep['message'] = {'role': 'user', 'content': c}
        if isinstance(d.get('toolUseResult'), str):
            keep['toolUseResult'] = d['toolUseResult']
        return keep
    if t == 'cost-state':
        return {k: d[k] for k in ('type', 'sessionId', 'totalCostUSD', 'modelUsage') if k in d}
    return None


def slim_transcript(src, dst):
    lines = []
    for raw in open(src, encoding='utf-8'):
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        s = slim_line(d)
        if s is not None:
            lines.append(json.dumps(scrub_value(s), separators=(',', ':')))
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return len(lines)


def main(raw_dir, transcript):
    raw_dir, transcript = pathlib.Path(raw_dir), pathlib.Path(transcript)
    (OUT / 'payloads').mkdir(exist_ok=True)
    (OUT / 'timelines').mkdir(exist_ok=True)
    for f in sorted(raw_dir.iterdir()):
        if f.suffix == '.json' and not f.name.startswith('subagent-agent-') and not f.name.startswith('transcript-'):
            data = scrub_value(json.loads(f.read_text(encoding='utf-8')))
            (OUT / 'payloads' / f.name).write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
        elif f.name.startswith('timeline-'):
            text = re.sub(r'id=\.\.\w+', 'id=..', scrub_text(f.read_text(encoding='utf-8')))
            (OUT / 'timelines' / f.name).write_text(text, encoding='utf-8')
    out = OUT / 'transcripts' / 's2'
    n = slim_transcript(transcript, out / 'session.jsonl')
    print(f'session.jsonl: {n} lines')
    helpers = transcript.with_suffix('') / 'subagents'
    for f in sorted(helpers.glob('agent-*')):
        name = scrub_text(f.name)
        if f.suffix == '.jsonl':
            print(f'{name}: {slim_transcript(f, out / "subagents" / name)} lines')
        elif f.name.endswith('.meta.json'):
            (out / 'subagents').mkdir(parents=True, exist_ok=True)
            (out / 'subagents' / name).write_text(json.dumps(scrub_value(json.loads(f.read_text(encoding='utf-8'))), indent=2) + '\n', encoding='utf-8')
    print(f'{len(ids)} ids replaced')


def recording(path, name):
    rec = json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    events = rec['events']
    transcript = next(e['payload']['transcript_path'] for e in events if e['payload'].get('transcript_path'))
    out = {'events': scrub_value(events), 'counts': rec.get('counts'), 'timings': rec.get('timings')}
    (OUT / 'recordings').mkdir(exist_ok=True)
    (OUT / 'recordings' / f'{name}.json').write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    base = OUT / 'transcripts' / name
    print(f'session.jsonl: {slim_transcript(transcript, base / "session.jsonl")} lines')
    helpers = pathlib.Path(transcript).with_suffix('') / 'subagents'
    for f in sorted(helpers.glob('agent-*')):
        target = base / 'subagents' / scrub_text(f.name)
        if f.suffix == '.jsonl':
            print(f'{target.name}: {slim_transcript(f, target)} lines')
        elif f.name.endswith('.meta.json'):
            target.write_text(json.dumps(scrub_value(json.loads(f.read_text(encoding='utf-8'))), indent=2) + '\n', encoding='utf-8')
    print(f'{len(events)} events, {len(ids)} ids replaced')


def bare(d):
    """A line without what Claude Code loaded for the session (instructions, memory, tool lists, file
    snapshots) or Claude's private reasoning: the importer needs only their place and time."""
    if d.get('type') == 'attachment':
        a = d.get('attachment') if isinstance(d.get('attachment'), dict) else {}
        return {**{k: d[k] for k in ('type', 'timestamp', 'uuid', 'parentUuid', 'sessionId', 'cwd', 'isSidechain', 'agentId') if k in d},
                'attachment': {'type': a.get('type', '')}}
    if d.get('type') == 'file-history-snapshot':
        return {'type': d['type'], 'messageId': d.get('messageId', ''), 'snapshot': {}}
    m = d.get('message')
    if d.get('type') == 'assistant' and isinstance(m, dict) and isinstance(m.get('content'), list):
        d = {**d, 'message': {**m, 'content': [{**b, 'thinking': '(Thinking)'} if isinstance(b, dict) and b.get('type') == 'thinking' else b
                                              for b in m['content']]}}
    return d


def full(transcript, name):
    src = pathlib.Path(transcript)
    lines = [bare(json.loads(l)) for l in open(src, encoding='utf-8') if l.strip()]
    # the name a person gives a session is an untimed header line too; the test session had none
    lines.insert(1, {'type': 'custom-title', 'customTitle': 'Import fixture', 'sessionId': src.stem})
    files = {f'{scrub_text(src.stem)}.jsonl': '\n'.join(json.dumps(scrub_value(d), ensure_ascii=False) for d in lines) + '\n'}
    for f in sorted((src.with_suffix('') / 'subagents').glob('agent-*')):
        body = f.read_text(encoding='utf-8')
        if f.name.endswith('.meta.json'):  # one JSON object (possibly over several lines), not JSON lines
            rows = [scrub_value(json.loads(body))]
        else:
            rows = [scrub_value(bare(json.loads(l))) for l in body.splitlines() if l.strip()]
        files[f'{scrub_text(src.stem)}/subagents/{scrub_text(f.name)}'] = (
            json.dumps(rows[0], indent=2) + '\n' if f.name.endswith('.meta.json') else '\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n')
    found = [hit for text in files.values() for hit in leaks(text, SCRUB.identity)]
    if found:
        sys.exit(f'not written: the leak check found {found}')
    base = OUT / 'transcripts' / name
    for rel, text in files.items():
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_text(text, encoding='utf-8')
        print(f'{rel}: {text.count(chr(10))} lines')
    print('scrubbed:', SCRUB.report())


if __name__ == '__main__':
    if sys.argv[1:2] == ['--recording']:
        recording(*sys.argv[2:4])
    elif sys.argv[1:2] == ['--full']:
        full(*sys.argv[2:4])
    else:
        main(*sys.argv[1:3])
