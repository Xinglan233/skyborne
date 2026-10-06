"""scripts/release_tools.py: what the release workflow relies on."""
import importlib.util
import io
import pathlib
import re
import tarfile
import zipfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('release_tools', ROOT / 'scripts' / 'release_tools.py')
assert spec and spec.loader
rt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rt)


def fake_repo(tmp_path, version='1.2.3', package_version=None):
    (tmp_path / 'skyborne').mkdir()
    (tmp_path / 'pyproject.toml').write_text(f'[project]\nname = "skyborne"\nversion = "{version}"\n')
    (tmp_path / 'skyborne' / '__init__.py').write_text(f"__version__ = '{package_version or version}'\n")
    (tmp_path / 'docs' / 'images').mkdir(parents=True)
    for name in ('docs/GUIDE.md', 'docs/images/a.gif', 'docs/images/b.jpg', 'LICENSE'):
        (tmp_path / name).write_text('x')
    return tmp_path


def test_the_tag_must_be_v_plus_the_version(tmp_path):
    root = fake_repo(tmp_path)
    assert rt.check_tag('v1.2.3', root) is None
    for bad in ('1.2.3', 'v1.2.4', 'v1.2', 'vfoo', 'v1.2.3.dev1'):
        assert 'does not match' in rt.check_tag(bad, root), bad


def test_the_two_places_that_hold_the_version_must_agree(tmp_path):
    root = fake_repo(tmp_path, '1.2.3', package_version='1.2.2')
    assert '1.2.2' in rt.check_tag('v1.2.3', root)


def test_this_repository_is_consistent():
    assert rt.package_version() == rt.project_version()
    assert rt.check_tag('v' + rt.project_version()) is None


def test_a_dev_version_is_stamped_in_both_places(tmp_path):
    root = fake_repo(tmp_path)
    assert rt.dev_version(17, root) == '1.2.3.dev17'
    assert rt.project_version(root) == rt.package_version(root) == '1.2.3.dev17'


def test_relative_links_become_absolute_for_the_ref(tmp_path):
    root = fake_repo(tmp_path)
    text = ('<a href="docs/images/b.jpg"><img src="docs/images/b.jpg" alt="x"></a>\n'
            '![alt](docs/images/a.gif) [guide](docs/GUIDE.md#platforms) [license](./LICENSE)\n'
            '<a href="https://example.com/x"><img src="https://img.shields.io/badge/a-b-blue"></a> [top](#why) <a href="mailto:a@b.c">m</a>')
    out = rt.pin_readme(text, 'v9.9.9', root)
    raw, blob = 'https://raw.githubusercontent.com/ishraq21/skyborne/v9.9.9/', 'https://github.com/ishraq21/skyborne/blob/v9.9.9/'
    assert f'<img src="{raw}docs/images/b.jpg"' in out and f'<a href="{blob}docs/images/b.jpg">' in out
    assert f'![alt]({raw}docs/images/a.gif)' in out
    assert f'[guide]({blob}docs/GUIDE.md#platforms)' in out and f'[license]({blob}LICENSE)' in out
    assert 'https://example.com/x' in out and 'https://img.shields.io/badge/a-b-blue' in out and '[top](#why)' in out and 'mailto:a@b.c' in out
    assert rt.pin_readme(out, 'v9.9.9', root) == out  # nothing relative is left, so a second pass changes nothing


def test_a_link_to_a_missing_file_stops_the_release(tmp_path):
    root = fake_repo(tmp_path)
    with pytest.raises(SystemExit, match='does not exist'):
        rt.pin_readme('[x](docs/NOPE.md)', 'v1', root)


def test_the_real_readme_pins_cleanly():
    """Every relative link in README.md points at a file that exists, and none is left relative after pinning."""
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    out = rt.pin_readme(readme, 'v0.1.0')
    assert 'raw.githubusercontent.com/ishraq21/skyborne/v0.1.0/docs/images/' in out
    assert 'github.com/ishraq21/skyborne/blob/v0.1.0/CONTRIBUTING.md' in out
    leftovers = re.findall(r'(?:src|href)="(?!https?:|mailto:|#)([^"]+)"|\]\((?!https?:|mailto:|#)([^)\s]+)\)', out)
    assert not leftovers, leftovers


def test_titles_single_quotes_and_nested_alt_text(tmp_path):
    root = fake_repo(tmp_path)
    raw, blob = 'https://raw.githubusercontent.com/ishraq21/skyborne/v1/', 'https://github.com/ishraq21/skyborne/blob/v1/'
    out = rt.pin_readme('[g](docs/GUIDE.md "the guide") [h](docs/GUIDE.md \'single\') <img src=\'docs/images/a.gif\'>\n'
                        '![a [b] c](docs/images/a.gif) [![badge](docs/images/b.jpg)](docs/GUIDE.md)', 'v1', root)
    assert f'[g]({blob}docs/GUIDE.md "the guide")' in out and f"[h]({blob}docs/GUIDE.md 'single')" in out
    assert f"<img src='{raw}docs/images/a.gif'>" in out
    assert f'![a [b] c]({raw}docs/images/a.gif)' in out            # an image, though its alt text has brackets
    assert f'[![badge]({raw}docs/images/b.jpg)]({blob}docs/GUIDE.md)' in out  # an image inside a link


def test_a_relative_reference_style_link_stops_the_release(tmp_path):
    root = fake_repo(tmp_path)
    with pytest.raises(SystemExit, match='reference-style'):
        rt.pin_readme('[guide][1]\n\n[1]: docs/GUIDE.md\n', 'v1', root)
    assert rt.pin_readme('[x][1]\n\n[1]: https://example.com/\n', 'v1', root).endswith('https://example.com/\n')


def test_relative_urls_in_rendered_html():
    html = '<a href="https://x.y/z">a</a><a href="docs/GUIDE.md">b</a><img src="a.gif"><a href="#top">c</a><a href="mailto:a@b.c">d</a><a href="//cdn/x">e</a>'
    assert rt.relative_urls(html) == ['docs/GUIDE.md', 'a.gif']


def make_dist(directory, description, page=rt.PAGE_FILES):
    directory.mkdir(parents=True, exist_ok=True)
    metadata = 'Metadata-Version: 2.4\nName: skyborne\nVersion: 1.2.3\n\n' + description
    with zipfile.ZipFile(directory / 'skyborne-1.2.3-py3-none-any.whl', 'w') as z:
        z.writestr('skyborne-1.2.3.dist-info/METADATA', metadata)
        for name in page:
            z.writestr(name, 'x')
    with tarfile.open(directory / 'skyborne-1.2.3.tar.gz', 'w:gz') as t:
        for name in page:
            info = tarfile.TarInfo('skyborne-1.2.3/' + name)
            info.size = 1
            t.addfile(info, io.BytesIO(b'x'))


def test_check_dist(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'render_markdown', lambda text: text)  # the real renderer (readme-renderer) runs in the workflow
    good = '<p><a href="https://github.com/x">ok</a> ' + 'words ' * 100 + '</p>'
    make_dist(tmp_path / 'good', good)
    assert rt.check_dist(str(tmp_path / 'good')) == []
    make_dist(tmp_path / 'rel', good + '<img src="docs/images/a.gif">')
    assert any('relative link: docs/images/a.gif' in p for p in rt.check_dist(str(tmp_path / 'rel')))
    make_dist(tmp_path / 'nopage', good, page=rt.PAGE_FILES[1:])
    problems = rt.check_dist(str(tmp_path / 'nopage'))
    assert any('missing from the wheel' in p for p in problems) and any('missing from the sdist' in p for p in problems)
    make_dist(tmp_path / 'empty', 'short')
    assert any('no description' in p for p in rt.check_dist(str(tmp_path / 'empty')))
    assert 'expected one wheel' in rt.check_dist(str(tmp_path / 'nothing'))[0]
