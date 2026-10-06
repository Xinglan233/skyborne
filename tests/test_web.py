"""The built city page shipped in the package (skyborne/web/, made by `cd page && node build.js`):
complete and self-contained, on every OS. CI's page job also checks it matches its sources."""
import json
import pathlib
import re

WEB = pathlib.Path(__file__).resolve().parents[1] / 'skyborne' / 'web'


def page():
    return (WEB / 'index.html').read_text(encoding='utf-8')


def test_the_page_has_one_token_slot():
    assert page().count('__SKYBORNE_TOKEN__') == 1


def test_every_local_file_the_page_names_exists():
    html = page()
    imports = json.loads(re.search(r'<script type="importmap">(.*?)</script>', html, re.S).group(1))['imports']
    assert (WEB / imports['three']).is_file()
    addons = WEB / imports['three/addons/']
    for rel in re.findall(r"from 'three/addons/([^']+)'", html):
        assert (addons / rel).is_file(), rel
    fonts = re.findall(r'url\((\./assets/fonts/[^)]+)\)', html)
    icons = re.findall(r'<link rel="(?:icon|apple-touch-icon)" href="(\./assets/icons/[^"]+)"', html)
    assert len(icons) == 3 and all((WEB / i).is_file() for i in icons)
    assert len(fonts) == 8 and all((WEB / f).is_file() for f in fonts)


def test_nothing_is_loaded_from_elsewhere():
    html = page()
    # the footer's credit, "Follow on X" and GitHub button are plain links, opened only by a click
    assert not re.search(r'(src|href)\s*=\s*["\']?https?://(?!my-space\.io"|x\.com/myspaceio"|github\.com/ishraq21/skyborne")', html)
    assert not re.search(r"(import|from)\s*\(?['\"]https?://", html)
    assert 'fonts.googleapis' not in html and 'jsdelivr' not in html


def test_the_author_credit_is_the_footer_and_the_reel_watermark():
    # the footer: the credit, then "Follow on X", then the GitHub button. Reels carry a watermark: the credit and skyborne.dev.
    # Not on the loading screen
    html = page()
    assert html.count('Mirza') == 2
    assert re.search(r'<footer[^>]*><span class="credit glass">Skyborne by <a href="https://my-space\.io"[^>]*>Mirza Ishraq</a></span>'
                     r'<a class="follow" href="https://x\.com/myspaceio"[^>]*>.*?</a>'
                     r'<a class="gh" href="https://github\.com/ishraq21/skyborne"[^>]*>.*?</a></footer>', html)
    assert '<div class="r-foot"><span>Skyborne by Mirza Ishraq</span><span>skyborne.dev</span></div>' in html
    boot = re.search(r'<div class="boot".*?</div></div>', html, re.S).group(0)
    assert 'Mirza' not in boot


def test_the_only_links_out_are_the_credit_follow_on_x_and_github():
    # all three open in a new tab, tell the other site nothing about this page, and load nothing until clicked
    links = re.findall(r'<a [^>]*href="https?://[^"]*"[^>]*>', page())
    assert len(links) == 3
    assert any('href="https://my-space.io"' in a for a in links)
    assert any('href="https://x.com/myspaceio"' in a and 'aria-label="Follow on X"' in a for a in links)
    assert any('href="https://github.com/ishraq21/skyborne"' in a and 'aria-label="Skyborne on GitHub"' in a for a in links)
    assert all('target="_blank"' in a and 'rel="noopener' in a for a in links)


def test_the_skybot_logo_is_drawn_once_and_shown_three_times():
    # at the top left, on the reel overlay and on the loading screen
    html = page()
    assert html.count('<symbol id="skybot"') == 1
    for cls in ('mark', 'r-mark', 'b-mark'):
        assert f'<svg class="{cls}" aria-hidden="true"><use href="#skybot"/></svg>' in html, cls


def test_licenses_ship_with_the_bundled_files():
    names = {p.name for p in (WEB / 'assets' / 'licenses').iterdir()}
    assert names == {'three.js-LICENSE.txt', 'Inter-OFL.txt', 'Unbounded-OFL.txt', 'JetBrainsMono-OFL.txt', 'MartianMono-OFL.txt'}
