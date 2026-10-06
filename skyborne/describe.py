"""What a tool call is doing, as the city shows it: a kind it animates and one line of text.

Ported from Skyborne's earlier reporter (its describeTool and roleName), without describeTool's length cuts.
The kind always comes from the tool's name, never from any text.
"""
import re

ROLE_NAMES = {
    'explore': 'Scout',
    'plan': 'Architect',
    'general-purpose': 'Builder',
    'claude-code-guide': 'Guide',
    'statusline-setup': 'Tinker',
    'fork': 'Twin',
    'claude': 'Builder',
}


def _words(s: str):
    return [w for w in re.split(r'[^A-Za-z0-9]+', s) if w]


def role_name(agent_type: str) -> str:
    """Explore -> Scout, Plan -> Architect, code-reviewer -> Critic, else the type in CamelCase."""
    t = agent_type.lower()
    if t in ROLE_NAMES:
        return ROLE_NAMES[t]
    if 'review' in t:
        return 'Critic'
    if 'test' in t:
        return 'Tester'
    if 'research' in t or 'search' in t:
        return 'Scout'
    if 'design' in t:
        return 'Artist'
    return ''.join(w[0].upper() + w[1:] for w in _words(agent_type))[:10] or 'Helper'


def _base(path) -> str:
    if not isinstance(path, str):
        return ''
    parts = [p for p in re.split(r'[\\/]', path) if p]
    return parts[-1] if parts else path


def _str(v) -> str:
    return v if isinstance(v, str) else ''


def describe_tool(tool: str, tool_input) -> tuple[str, str]:
    """(kind, text) for one tool call."""
    i = tool_input if isinstance(tool_input, dict) else {}
    if tool in ('Bash', 'BashOutput'):
        return 'bash', '$ ' + _str(i.get('command'))
    if tool in ('Read', 'NotebookRead'):
        return 'read', 'Reading ' + _base(i.get('file_path'))
    if tool in ('Edit', 'MultiEdit', 'NotebookEdit'):
        return 'edit', 'Editing ' + _base(i.get('file_path') or i.get('notebook_path'))
    if tool == 'Write':
        return 'write', 'Writing ' + _base(i.get('file_path'))
    if tool in ('Grep', 'Glob'):
        return 'search', 'Searching ' + _str(i.get('pattern'))
    if tool == 'WebSearch':
        return 'web', 'Web search: ' + _str(i.get('query'))
    if tool == 'WebFetch':
        return 'web', 'Reading a web page'
    if tool in ('Agent', 'Task'):
        role = role_name(_str(i.get('subagent_type')) or 'general-purpose')
        return 'spawn', f"Sending out {'an' if role[:1] in 'AEIOU' else 'a'} {role}"
    if tool in ('TodoWrite', 'TaskCreate', 'TaskUpdate', 'TaskList'):
        return 'task', 'Updating the task list'
    # these two wait for the person, not for a yes or no (approvals.py TERMINAL_ONLY): words, not the tool's name
    if tool == 'AskUserQuestion':
        return 'tool', 'Asking you a question'
    if tool == 'ExitPlanMode':
        return 'tool', 'Sharing its plan'
    m = re.match(r'^mcp__(.+?)__(.+)$', tool)
    if m:
        return 'mcp', f"{m.group(1).replace('_', ' ')}: {m.group(2)}"
    return 'tool', tool
