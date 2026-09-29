import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import splice_assets

SKILL_DIR = pathlib.Path(__file__).parent.parent


def fake_skill(tmp_path, **files):
    assets = tmp_path / "assets"
    assets.mkdir(parents=True)
    for name, body in files.items():
        (assets / name).write_text(body)
    return tmp_path


def test_fills_the_placeholder_the_page_needs(tmp_path):
    skill = fake_skill(tmp_path / "skill", **{"mermaid.min.js": "MERMAIDJS"})
    page = tmp_path / "page.html"
    page.write_text('<pre class="mermaid">flowchart TB</pre><script><!-- MERMAID_JS --></script>')
    _report, error = splice_assets.splice(page, skill)
    out = page.read_text()
    assert error is None
    assert "MERMAIDJS" in out


def test_page_with_no_mermaid_block_leaves_the_placeholder_alone(tmp_path):
    skill = fake_skill(tmp_path / "skill", **{"mermaid.min.js": "MERMAIDJS"})
    page = tmp_path / "page.html"
    page.write_text("<p>no diagram here</p><script><!-- MERMAID_JS --></script>")
    _report, error = splice_assets.splice(page, skill)
    out = page.read_text()
    assert error is None
    assert "MERMAIDJS" not in out
    assert "<!-- MERMAID_JS -->" in out


def test_resplicing_does_not_double_the_payload(tmp_path):
    skill = fake_skill(tmp_path / "skill", **{"mermaid.min.js": "MERMAIDJS"})
    page = tmp_path / "page.html"
    page.write_text('<pre class="mermaid">x</pre><script><!-- MERMAID_JS --></script>')
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


def test_fills_the_hljs_placeholder_on_a_diff_page(tmp_path):
    skill = fake_skill(tmp_path / "skill", **{"highlight.min.js": "HLJSJS"})
    page = tmp_path / "page.html"
    page.write_text('<pre class="diff">+x</pre><script><!-- HLJS_JS --></script>')
    _report, error = splice_assets.splice(page, skill)
    out = page.read_text()
    assert error is None
    assert "HLJSJS" in out


def test_page_with_no_diff_block_leaves_the_hljs_placeholder_alone(tmp_path):
    skill = fake_skill(tmp_path / "skill", **{"highlight.min.js": "HLJSJS"})
    page = tmp_path / "page.html"
    page.write_text("<p>explain mode only</p><script><!-- HLJS_JS --></script>")
    _report, error = splice_assets.splice(page, skill)
    out = page.read_text()
    assert error is None
    assert "HLJSJS" not in out
    assert "<!-- HLJS_JS -->" in out


def test_resplicing_hljs_does_not_double_the_payload(tmp_path):
    skill = fake_skill(tmp_path / "skill", **{"highlight.min.js": "HLJSJS"})
    page = tmp_path / "page.html"
    page.write_text('<pre class="diff">+x</pre><script><!-- HLJS_JS --></script>')
    splice_assets.splice(page, skill)
    first = page.read_text()
    splice_assets.splice(page, skill)
    assert page.read_text() == first


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
    node = shutil.which("node")
    if not node:
        print("skip (node not on PATH)")
        return
    js = (SKILL_DIR / "assets" / "highlight.min.js").read_text()
    js = splice_assets.SCRIPT_TAG_RE.sub(splice_assets._escape_script_tag, js)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
        bundle_path = f.name
    try:
        script = (
            f"const hljs = require({bundle_path!r});"
            "const r = hljs.highlight('<script>x</script>', {language: 'xml'});"
            "if (!r.value.includes('hljs-name')) throw new Error('not tokenised: ' + r.value);"
        )
        proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    finally:
        pathlib.Path(bundle_path).unlink()
    assert proc.returncode == 0, proc.stderr


if __name__ == "__main__":
    tests = [
        test_fills_the_placeholder_the_page_needs,
        test_page_with_no_mermaid_block_leaves_the_placeholder_alone,
        test_resplicing_does_not_double_the_payload,
        test_missing_vendored_asset_reports_error_and_leaves_page_alone,
        test_fills_the_hljs_placeholder_on_a_diff_page,
        test_page_with_no_diff_block_leaves_the_hljs_placeholder_alone,
        test_resplicing_hljs_does_not_double_the_payload,
        test_splice_escapes_script_substring_leaked_from_vendored_js,
        test_rewritten_highlight_js_still_tokenizes_a_script_tag,
    ]
    for test in tests:
        with tempfile.TemporaryDirectory() as tmp:
            test(pathlib.Path(tmp))
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
