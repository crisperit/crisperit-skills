import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import splice_assets


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


if __name__ == "__main__":
    import tempfile

    tests = [
        test_fills_the_placeholder_the_page_needs,
        test_page_with_no_mermaid_block_leaves_the_placeholder_alone,
        test_resplicing_does_not_double_the_payload,
        test_missing_vendored_asset_reports_error_and_leaves_page_alone,
    ]
    for test in tests:
        with tempfile.TemporaryDirectory() as tmp:
            test(pathlib.Path(tmp))
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
