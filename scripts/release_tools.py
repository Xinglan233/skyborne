"""Small helpers the release workflow (.github/workflows/release.yml) runs. Standard library only
(check-dist also needs readme-renderer, which the workflow installs).

    python scripts/release_tools.py check-tag v0.1.0        the tag must match the version in pyproject.toml
    python scripts/release_tools.py dev-version 17          stamp 0.1.0.dev17 into pyproject.toml and the package (a TestPyPI dry run)
    python scripts/release_tools.py pin-readme v0.1.0       print README.md with its relative links made absolute for that git ref
    python scripts/release_tools.py pin-readme v0.1.0 --write    the same, written back to README.md (the workflow's checkout only)
    python scripts/release_tools.py check-dist dist         the built files carry the page, and their description has no relative link

PyPI shows README.md as the package's description, where relative image and doc links break, so the
workflow pins them to the tag before building. The README in the repository is never changed.
"""
import glob
import pathlib
import re
import sys
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from html.parser import HTMLParser

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = 'ishraq21/skyborne'
IMAGES = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp')
PAGE_FILES = ['skyborne/web/index.html', 'skyborne/web/assets/three/three.module.min.js', 'skyborne/web/assets/icons/icon.svg']
NOT_RELATIVE = re.compile(r'^([a-z][a-z0-9+.-]*:|#|//)', re.I)


def project_version(root=ROOT):
    return tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']


def package_version(root=ROOT):
    m = re.search(r"^__version__ = '([^']+)'", (root / 'skyborne' / '__init__.py').read_text(encoding='utf-8'), re.M)
    return m.group(1) if m else None


def check_tag(tag, root=ROOT):
    """Returns a problem as text, or None when the tag is v<version> and both places that hold the version agree."""
    version = project_version(root)
    if tag != f'v{version}':
        return f'The tag {tag} does not match the version in pyproject.toml ({version}): it must be v{version}.'
    if package_version(root) != version:
        return f'skyborne/__init__.py says {package_version(root)} but pyproject.toml says {version}.'
    return None


def dev_version(number, root=ROOT):
    """Stamps <version>.dev<number> into pyproject.toml and skyborne/__init__.py; returns it."""
    version = project_version(root)
    dev = f'{version}.dev{int(number)}'
    for path, old, new in ((root / 'pyproject.toml', f'version = "{version}"', f'version = "{dev}"'),
                           (root / 'skyborne' / '__init__.py', f"__version__ = '{version}'", f"__version__ = '{dev}'")):
        text = path.read_text(encoding='utf-8')
        if old not in text:
            raise SystemExit(f'{path.name}: cannot find {old}')
        path.write_text(text.replace(old, new, 1), encoding='utf-8')
    return dev


def _absolute(target, ref, as_image, root):
    """The absolute URL for a relative link in the README, or the link itself if it is not relative."""
    if NOT_RELATIVE.match(target):
        return target
    path, hash_, fragment = target.partition('#')
    path = path[2:] if path.startswith('./') else path
    if not (root / path).exists():
        raise SystemExit(f'README.md links to {path}, which does not exist in the repository.')
    if as_image and path.lower().endswith(IMAGES):
        return f'https://raw.githubusercontent.com/{REPO}/{ref}/{path}'
    return f'https://github.com/{REPO}/blob/{ref}/{path}{hash_}{fragment}'


INLINE = re.compile(r'''\]\(\s*([^)\s]+)((?:\s+(?:"[^"]*"|'[^']*'))?\s*)\)''')  # ](target "optional title")
REFERENCE = re.compile(r'^ {0,3}\[[^\]]+\]:[ \t]*<?([^\s>]+)', re.M)             # [name]: target
HTML_ATTR = re.compile(r'''(\b(?:src|href)=)(["'])(.*?)\2''')


def _is_image(text, close):
    """Whether the ']' at text[close] ends the alt text of an image (![alt]), counting nested brackets."""
    depth = 0
    for i in range(close, -1, -1):
        depth += (text[i] == ']') - (text[i] == '[')
        if depth == 0:
            return i > 0 and text[i - 1] == '!'
    return False


def pin_readme(text, ref, root=ROOT):
    """README text with every relative link made absolute for the git ref: images (src, ![]()) point at
    raw.githubusercontent.com so they display, everything else at the file's page on GitHub."""
    for m in REFERENCE.finditer(text):
        if not NOT_RELATIVE.match(m[1]):
            raise SystemExit(f'README.md has a relative reference-style link ({m[1]}); use an inline link, which can be pinned.')
    out, last = [], 0
    for m in INLINE.finditer(text):
        out += [text[last:m.start(1)], _absolute(m[1], ref, _is_image(text, m.start()), root)]
        last = m.end(1)
    out.append(text[last:])
    text = ''.join(out)
    return HTML_ATTR.sub(lambda m: m[1] + m[2] + _absolute(m[3], ref, m[1].startswith('src'), root) + m[2], text)


class _Urls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        self.urls += [v for k, v in attrs if k in ('src', 'href') and v]


def relative_urls(html):
    """Every src and href in the HTML that is not absolute (and not a #anchor or mailto:)."""
    parser = _Urls()
    parser.feed(html)
    return [u for u in parser.urls if not NOT_RELATIVE.match(u)]


def render_markdown(text):
    """The HTML PyPI makes of a Markdown description (it uses readme-renderer)."""
    from readme_renderer.markdown import render
    html = render(text)
    if html is None:
        raise SystemExit('readme-renderer could not render the description')
    return html


def check_dist(dist_dir):
    """Problems with the built sdist and wheel, as a list: the page is in both, and the description PyPI will
    show has no relative link (checked on the rendered HTML, so every Markdown form counts)."""
    wheels, sdists = glob.glob(f'{dist_dir}/skyborne-*.whl'), glob.glob(f'{dist_dir}/skyborne-*.tar.gz')
    if len(wheels) != 1 or len(sdists) != 1:
        return [f'expected one wheel and one sdist in {dist_dir}, found {len(wheels)} and {len(sdists)}']
    wheel, sdist = zipfile.ZipFile(wheels[0]), tarfile.open(sdists[0])
    wnames, snames = wheel.namelist(), sdist.getnames()
    problems = [f'missing from the wheel: {n}' for n in PAGE_FILES if n not in wnames]
    problems += [f'missing from the sdist: {n}' for n in PAGE_FILES if not any(s.endswith('/' + n) for s in snames)]
    metadata = wheel.read(next(n for n in wnames if n.endswith('.dist-info/METADATA'))).decode('utf-8')
    message = Parser().parsestr(metadata)
    body = str(message.get_payload())
    if len(body.strip()) < 500:
        problems.append('the package has no description (the README did not get in)')
    problems += [f'the description still has a relative link: {u}' for u in relative_urls(render_markdown(body))]
    print(f"wheel: {len(wnames)} files, {sum('/web/' in n for n in wnames)} page files; sdist: {len(snames)} files; version {message['Version']}")
    return problems


def main(argv):
    cmd, args = (argv[0], argv[1:]) if argv else ('', [])
    if cmd == 'check-tag' and len(args) == 1:
        problem = check_tag(args[0])
        if problem:
            print(problem)
            return 1
        print(f'OK: {args[0]} matches version {project_version()}')
        return 0
    if cmd == 'dev-version' and len(args) == 1:
        print(dev_version(args[0]))
        return 0
    if cmd == 'pin-readme' and args and args[0] != '--write':
        readme = ROOT / 'README.md'
        pinned = pin_readme(readme.read_text(encoding='utf-8'), args[0])
        if '--write' in args[1:]:
            readme.write_text(pinned, encoding='utf-8')
        else:
            sys.stdout.write(pinned)
        return 0
    if cmd == 'check-dist' and len(args) == 1:
        problems = check_dist(args[0])
        print('\n'.join(problems) if problems else 'OK: the package is complete and its description has no relative links')
        return 1 if problems else 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
