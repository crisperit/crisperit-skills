import pathlib
import re
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import cw_testlib  # noqa: E402
import splice_assets  # noqa: E402

SKILL_DIR = pathlib.Path(__file__).parent.parent


def fake_skill(tmp_path, **files):
    assets = tmp_path / "assets"
    assets.mkdir(parents=True)
    for name, body in files.items():
        (assets / name).write_text(body)
    return tmp_path


# (vendored file, its payload, placeholder, a page that needs it, a page that does not)
ASSETS = [
    ("mermaid.min.js", "MERMAIDJS", "<!-- MERMAID_JS -->",
     '<pre class="mermaid">flowchart TB</pre>', "<p>no diagram here</p>"),
    ("highlight.min.js", "HLJSJS", "<!-- HLJS_JS -->",
     '<pre class="diff">+x</pre>', "<p>explain mode only</p>"),
]


def _page(tmp_path, asset, body):
    skill = fake_skill(tmp_path / "skill", **{asset[0]: asset[1]})
    page = tmp_path / "page.html"
    page.write_text(f"{body}<script>{asset[2]}</script>")
    return skill, page


@pytest.mark.parametrize("asset", ASSETS, ids=[a[0] for a in ASSETS])
def test_splice_fills_the_placeholder_only_for_a_page_that_needs_it(tmp_path, asset):
    skill, page = _page(tmp_path, asset, asset[3])
    _report, error = splice_assets.splice(page, skill)
    assert error is None
    assert asset[1] in page.read_text()

    skill, page = _page(tmp_path / "other", asset, asset[4])
    _report, error = splice_assets.splice(page, skill)
    out = page.read_text()
    assert error is None
    assert asset[1] not in out
    assert asset[2] in out


@pytest.mark.parametrize("asset", ASSETS, ids=[a[0] for a in ASSETS])
def test_splice_is_idempotent(tmp_path, asset):
    skill, page = _page(tmp_path, asset, asset[3])
    splice_assets.splice(page, skill)
    first = page.read_text()
    splice_assets.splice(page, skill)
    assert page.read_text() == first


def test_missing_vendored_asset_reports_error_and_leaves_page_alone(tmp_path):
    skill = fake_skill(tmp_path / "skill")
    page = tmp_path / "page.html"
    original = '<pre class="mermaid">flowchart TB</pre><script><!-- MERMAID_JS --></script>'
    page.write_text(original)
    _report, error = splice_assets.splice(page, skill)
    assert error is not None
    assert "mermaid.min.js" in error
    assert page.read_text() == original


def test_splice_escapes_script_substring_leaked_from_vendored_js(tmp_path):
    # Nextcloud's viewer rewrites every "<script" substring in the page text, including
    # ones hiding inside the JS payload itself (hljs's XML grammar has exactly one).
    skill = fake_skill(tmp_path / "skill", **{"highlight.min.js": "begin:/<script(?=\\s|>)/"})
    page = tmp_path / "page.html"
    page.write_text('<pre class="diff">+x</pre><script><!-- HLJS_JS --></script>')
    _report, error = splice_assets.splice(page, skill)
    out = page.read_text()
    assert error is None
    # only the page's own real <script> tag should remain; none leaked from the spliced JS
    assert len(re.findall(r"<script", out, re.IGNORECASE)) == 1
    assert r"<\x73cript" in out


def test_rewritten_highlight_js_still_tokenizes_a_script_tag(tmp_path):
    cw_testlib.require_node()
    js = (SKILL_DIR / "assets" / "highlight.min.js").read_text()
    js = splice_assets.SCRIPT_TAG_RE.sub(splice_assets._escape_script_tag, js)
    bundle = tmp_path / "highlight.js"
    bundle.write_text(js)
    script = (
        f"const hljs = require({str(bundle)!r});"
        "const r = hljs.highlight('<script>x</script>', {language: 'xml'});"
        "if (!r.value.includes('hljs-name')) throw new Error('not tokenised: ' + r.value);"
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
