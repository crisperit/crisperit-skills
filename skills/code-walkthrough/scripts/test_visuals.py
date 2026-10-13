import json
import subprocess
import sys
from pathlib import Path

import pytest

import visuals as v

SCRIPT = Path(__file__).with_name("visuals.py")
SVG_OPEN = '<svg viewBox="0 0 100 50" role="img" aria-label="x">'


def san(html, **kw):
    return v.sanitize_fragment(html, **kw)


def rejected(html, needle):
    out, probs = san(html)
    assert out is None, f"accepted: {html!r} -> {out!r}"
    assert any(needle.lower() in p.lower() for p in probs), probs


def styled(css):
    return f'<div style="{css}">x</div>'


@pytest.mark.parametrize("html,needle", [
    ("<script>alert(1)</script>", "<script>"),
    ("<style>body{}</style>", "<style>"),
    ('<iframe srcdoc="<script>1</script>"></iframe>', "<iframe>"),
    ('<img src=x onerror=alert(1)>', "<img>"),
    ('<a href="javascript:alert(1)">x</a>', "<a>"),
    ("<form><input><button>x</button></form>", "<form>"),
    ("<input>", "<input>"),
    ("<button>x</button>", "<button>"),
    ("<base href=//evil>", "<base>"),
    ('<meta http-equiv=refresh content="0;url=//x">', "<meta>"),
    ('<link rel=stylesheet href=//x>', "<link>"),
    ("<object data=x></object>", "<object>"),
    ("<embed src=x>", "<embed>"),
    ("<template><b>x</b></template>", "<template>"),
    ("<noscript>x</noscript>", "<noscript>"),
    ("<math><mi>x</mi></math>", "<math>"),
    ("<h1>x</h1>", "<h1>"),
    ("<h2>x</h2>", "<h2>"),
    ("<h3>x</h3>", "<h3>"),
])
def test_forbidden_html_tags(html, needle):
    rejected(html, needle)


@pytest.mark.parametrize("inner,needle", [
    ("<foreignObject><div>x</div></foreignObject>", "<foreignobject>"),
    ('<use href="#a"/>', "<use>"),
    ('<a xlink:href="javascript:1"><rect/></a>', "<a>"),
    ('<animate attributeName="x"/>', "<animate>"),
    ('<set attributeName="x" to="1"/>', "<set>"),
    ('<image href="x.png"/>', "<image>"),
    ('<filter id="f"></filter>', "<filter>"),
    ('<linearGradient id="g"></linearGradient>', "<lineargradient>"),
    ("<div>x</div>", "inside <svg>"),
    ("hello", "directly inside <svg>"),
    ("<g>hello</g>", "directly inside <g>"),
    ('<rect onload="1" width="5"/>', "onload"),
    ('<rect transform="rotate(5)"/>', "transform"),
    ('<rect width="-5"/>', "out of range"),
    ('<rect width="5000"/>', "out of range"),
    ('<rect fill="#ff0000"/>', "fill"),
    ('<rect fill="red"/>', "fill"),
    ('<rect fill="url(#x)"/>', "fill"),
    ('<path d="M0 0 L5 5 foo"/>', "path"),
    ('<polyline points="0,0 -5,5"/>', "points"),
    ('<path d="M0 0" id="p"/>', "id"),
    ('<text font-size="9">x</text>', "font-size: "),
    ('<text font-family="Arial">x</text>', "font-family"),
    ('<line x1="0" y1="0" x2="5" y2="5" marker-end="url(#nope)"/>', "does not define"),
    ('<line x1="0" y1="0" x2="5" y2="5" marker-end="url(https://x/y.svg#a)"/>', "marker-end"),
    ('<line x1="0" y1="0" x2="5" y2="5" marker-end="url(#a) url(#b)"/>', "marker-end"),
    ('<rect marker-end="url(#a)"/>', "marker-end"),
    ('<defs><marker id="a" markerWidth="4" markerHeight="4"/><marker id="a"/></defs>', "twice"),
    ('<defs><marker id="A b"/></defs>', "marker id"),
])
def test_svg_abuse(inner, needle):
    rejected(f"{SVG_OPEN}{inner}</svg>", needle)


def test_svg_requirements():
    rejected('<svg role="img" aria-label="x"><rect/></svg>', "viewBox")
    rejected('<svg viewBox="0 0 10 10"><rect/></svg>', "aria-hidden")
    rejected('<svg viewBox="0 0 10 10" role="img"><rect/></svg>', "aria-hidden")
    rejected('<svg viewBox="0 0 x 10" aria-hidden="true"></svg>', "not a number")
    rejected(f"{SVG_OPEN}<svg viewBox='0 0 1 1' aria-hidden='true'></svg></svg>", "inside <svg>")
    rejected("<rect width='5'/>", "top level")


def test_svg_text_and_arrowheads_allowed():
    html = (f'{SVG_OPEN}<defs><marker id="arrow" markerWidth="6" markerHeight="6" refX="5" refY="3" '
            'orient="auto" markerUnits="strokeWidth"><path d="M0 0 L6 3 L0 6 z" fill="var(--foreground)"/>'
            '</marker></defs><rect x="2" y="2" width="30" height="20" rx="4" fill="none" stroke="var(--border)"/>'
            '<line x1="32" y1="12" x2="60" y2="12" stroke="currentColor" stroke-width="1.5" marker-end="url(#arrow)"/>'
            '<text x="5" y="14" dx="-1" font-size="12" font-family="var(--font-mono)" text-anchor="start" '
            'fill="var(--foreground)">a <tspan font-weight="bold">b</tspan> &amp; c</text></svg>')
    out, probs = san(html, prefix="cwv3-")
    assert probs == []
    assert 'id="cwv3-arrow"' in out and 'marker-end="url(#cwv3-arrow)"' in out
    assert "markerWidth" in out and "viewBox" in out
    assert "a <tspan" in out and "&amp; c" in out


def test_xmlns_dropped_but_only_the_svg_namespace():
    out, probs = san('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 5 5" aria-hidden="true"></svg>')
    assert probs == [] and "xmlns" not in out
    rejected('<svg xmlns="http://evil" viewBox="0 0 5 5" aria-hidden="true"></svg>', "xmlns")


@pytest.mark.parametrize("html,needle", [
    ('<div onclick="x()">a</div>', "onclick"),
    ('<div ONMOUSEOVER="x()">a</div>', "onmouseover"),
    ('<div id="a">a</div>', "id"),
    ('<div class="cw-card">a</div>', "class"),
    ('<div name="a">a</div>', "name"),
    ('<div data-sym="a">a</div>', "data-sym"),
    ('<div aria-live="polite">a</div>', "aria-live"),
    ('<div aria-hidden>a</div>', "needs a value"),
    ("<div hidden>a</div>", "hidden"),
    ('<span title>a</span>', "needs a value"),
    ('<div role="button">a</div>', "role"),
    ('<div title="' + "x" * 201 + '">a</div>', "200"),
    ('<td colspan="99">a</td>', "td"),
    ('<div href="x">a</div>', "href"),
    ('<div srcdoc="x">a</div>', "srcdoc"),
    ('<div style="a:b" style="c:d">x</div>', "repeats"),
])
def test_attribute_abuse(html, needle):
    wrapped = f"<table><tr>{html}</tr></table>" if html.startswith("<td") else html
    rejected(wrapped, needle)


def test_title_quote_breakout_is_escaped():
    out, probs = san('<div title="a&quot; onclick=&quot;alert(1)">x</div>')
    assert probs == []
    assert 'title="a&quot; onclick=&quot;alert(1)"' in out
    assert ' onclick=' not in out.replace("&quot; onclick=", "")


@pytest.mark.parametrize("css,needle", [
    ("background:url(x.png)", "url"), ("background:URL(x.png)", "url"), ("background:u&#114;l(x.png)", "url"),
    ("background:u&#x72;l(x.png)", "url"), ("background:\\75 rl(x.png)", "style"),
    ("width:expression(alert(1))", "expression"), ("background:image-set(x 1x)", "image-set"),
    ("color:rgb(1,2,3)", "rgb"), ("color:hsl(1,2%,3%)", "hsl"), ("color:attr(x)", "attr"),
    ("width:env(safe-area-inset-top)", "env"),
])
def test_style_functions_rejected(css, needle):
    rejected(styled(css), needle)


@pytest.mark.parametrize("css,needle", [
    ("@import 'x'", "style"), ("behavior:url(x)", "behavior"), ("-moz-binding:url(x)", "-moz-binding"),
    ("position:fixed", "'position'"), ("position:absolute", "'position'"),
    ("top:0", "'top'"), ("inset:0", "'inset'"), ("z-index:9999", "'z-index'"),
    ("transform:scale(9)", "'transform'"), ("cursor:pointer", "'cursor'"), ("content:x", "'content'"),
    ("animation:x 1s", "'animation'"), ("transition:all 1s", "'transition'"), ("filter:blur(2px)", "'filter'"),
    ("--x:1", "--x"), ("pointer-events:none", "'pointer-events'"),
    ("color:red", "red"), ("color:#fff", "color"), ("color:#ffffff", "color"), ("background:blue", "blue"),
    ("color:var(--bg)", "var()"), ("color:var(--BACKGROUND)", "var()"),
    ("color:var(--foreground, red)", "var()"), ("margin:-4px", "out of range"),
    ("margin-left:-1px", "out of range"), ("width:3000px", "out of range"),
    ("font-size:10px", "font-size"), ("font-size:33px", "font-size"), ("font-size:0.5rem", "font-size"),
    ("font-size:300%", "font-size"), ("font-size:small", "font-size"),
    ("color:/**/red", "style"), ("width:5px/*c*/", "style"), ("width:5px!important", "style"),
    ("width:5px;;;color:red", "red"),
    ("font-family:Arial", "arial"), ("font-family:var(--font-mono),Arial", "arial"),
    ("overflow:scroll", "overflow"), ("opacity:2", "opacity"),
    ("width:5pt", "unit"), ("width:1e3px", "width"), ("width:calc(100% - var(--x))", "var()"),
    ("color:color-mix(in srgb, red 20%, transparent)", "color"),
    ("color: color-mix(in srgb, var(--accent) 20%, transparent", "color"),
    ("width", "style"), ("width:", "malformed"), (":5px", "style"),
    ("background:é", "style"), ("width:5px;" * 21, "style"),
    ("color:var(--foreground)" + ";" + "x" * 500, "500"),
    ("display:flex" + ";gap:1px" * 20, "style"),
    ("margin-top:calc(0px - 2000px * 2000)", "not allowed inside calc"),
    ("width:calc(2000px * 2000px)", "not allowed inside calc"),
    ("width:calc(100% / 0.0001)", "not allowed inside calc"),
    ("width:min(2000px * 2000, 5px)", "not allowed inside calc"),
    ("width:max(1px, 2000px * 2000)", "not allowed inside calc"),
    ("width:clamp(1px, 5px, 2000px * 2000)", "not allowed inside calc"),
    ("margin-top:calc(1px + 2px + 3px + 4px + 5px + 6px + 7px + 8px + 9px)", "more than 8"),
])
def test_style_abuse(css, needle):
    rejected(styled(css), needle)


@pytest.mark.parametrize("css", [
    "width:calc(100% - 20px)", "width:clamp(100px, 50%, 300px)", "width:min(100%, 320px)",
    "width:max(10%, 20px)", "gap:calc(1rem + 2px)",
])
def test_style_math_without_multiply_or_divide_passes(css):
    out, probs = san(styled(css))
    assert probs == [] and out


def test_style_problem_messages_escape_model_supplied_names():
    _, probs = san('<div style="x" \x1b[31m="1">a</div><\x1b[2Jb>c</div>')
    assert all("\x1b" not in p for p in probs)


def test_style_allowed_forms_are_normalised():
    out, probs = san(styled("DISPLAY:Grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));"
                            "gap:calc(1rem + 2px);padding:4px 8px;border:1px solid var(--border);"
                            "background:color-mix(in srgb, var(--chart-1) 15%, transparent);font-size:0.9rem;"
                            "margin:0 auto;width:min(100%, 320px);flex:1 1 0;grid-column:1 / 3"))
    assert probs == []
    assert out.startswith('<div style="display: Grid; grid-template-columns: repeat(auto-fit,minmax(120px,1fr));')
    assert "grid-column: 1 / 3" in out


def test_style_error_lines_name_the_problem():
    _, probs = san(styled("position:fixed;background:url(x)"))
    assert any("'position'" in p for p in probs)


@pytest.mark.parametrize("html,needle", [
    ("</div></section><script>alert(1)</script>", "<script>"),
    ("<div>a</div></div></section><iframe></iframe>", "<iframe>"),
    ("<table>text<tr><td>a</td></tr></table>", "directly inside <table>"),
    ("<ul>text<li>a</li></ul>", "directly inside <ul>"),
    ("<ul><div>a</div></ul>", "inside <ul>"),
    ("<table><td>a</td></table>", "inside <table>"),
    ("<tr><td>a</td></tr>", "top level"),
    ("<p>a<div>b</div></p>", "inside <p>"),
    ("<p>a<p>b</p></p>", "inside <p>"),
    ("<span><div>b</div></span>", "inside <span>"),
    ("<pre><ul><li>a</li></ul></pre>", "inside <pre>"),
    ("<dl><li>a</li></dl>", "inside <dl>"),
    ("<li>a</li>", "top level"),
])
def test_structure_violations(html, needle):
    rejected(html, needle)


def test_comments_doctype_pi_cdata_dropped_and_text_marker_escaped():
    out, probs = san("<!DOCTYPE html><?php x ?><!-- c --><div>a<![CDATA[ b ]]>c</div>"
                     "<div>&lt;!-- HR_STATE --&gt; <!-- HR_STATE --></div>")
    assert probs == []
    assert "<!--" not in out and "<?" not in out and "CDATA" not in out.upper().replace("&LT;", "")
    assert "&lt;!-- HR_STATE --&gt;" in out
    assert "<!-- HR_STATE -->" not in out


def test_stray_end_tags_ignored_and_unclosed_closed():
    out, probs = san("</div></span><div><p>a</b></p><ul><li>one</li><li>two</ul>")
    assert probs == []
    assert out == "<div><p>a</p><ul><li>one</li><li>two</li></ul></div>"
    assert out.count("<li>") == out.count("</li>") and out.count("<ul>") == out.count("</ul>")


def test_entities_decoded_then_checked_and_reescaped():
    out, probs = san("<p>&lt;script&gt;alert(1)&lt;/script&gt; &amp; &#60;b&#62; é</p>")
    assert probs == []
    assert "<script" not in out and "&lt;script&gt;" in out and "é" in out
    rejected('<div style="b&#x61;ckground:u&#114;l(x)">a</div>', "")


def test_text_with_control_characters_cleaned():
    out, probs = san("<p>a\x00b\x08c</p>")
    assert probs == [] and out == "<p>abc</p>"


def test_empty_and_non_string_fragments():
    for bad in ("", "   ", "<!-- only a comment -->", None, 5, ["<p>x</p>"], {"a": 1}):
        out, probs = san(bad)
        assert out is None and probs


def test_caps():
    assert san("<p>" + "x" * (24 * 1024) + "</p>")[0] is None
    assert san("<p>x</p>" * 3000)[0] is None
    out, probs = san("<span>x</span>" * 400)
    assert out and not probs
    rejected("<span>x</span>" * 401, "400 elements")
    deep = "<div>" * 12 + "x" + "</div>" * 12
    assert san(deep)[1] == []
    rejected("<div>" * 13 + "x", "deeper than 12")
    rejected(f"{SVG_OPEN}" + "<g>" * 20 + "</svg>", "deeper than 12")
    assert san("<p>" + "x" * 13 * 1024 + "</p>", max_bytes=v.GROUP_MAX)[0] is None


def test_problem_lines_capped_at_eight_plus_more():
    out, probs = san("".join(f"<t{i}>a</t{i}>" for i in range(20)))
    assert out is None
    assert len(probs) == 9 and probs[-1] == "... and 12 more"


def test_repeated_problem_listed_once():
    _, probs = san("<img><img><img>")
    assert probs == ["<img> is not allowed"]


@pytest.mark.parametrize("html", [
    "<p>a <b>b</b> &amp; &lt;c&gt; &quot;d&quot;</p>",
    '<div title="a&quot;b&#x27;c" style="display:flex;gap:4px"><span>x</span><br><hr></div>',
    "<ul><li>a</li>\n<li>b</li></ul><table> <thead><tr><th scope='col' colspan=2>h</th></tr></thead>"
    "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>",
    f'{SVG_OPEN}<defs><marker id="a1" markerWidth="4" markerHeight="4"><path d="M0 0L4 2L0 4z"/></marker></defs>'
    '<path d="M0 0 L10 10" marker-end="url(#a1)" stroke="var(--accent)"/><text x="1" y="9">t &amp; u</text></svg>',
    "<pre>  keep\n   spaces </pre><figure><div>x</div><figcaption>c</figcaption></figure>",
    "stray </b> text <span>unclosed",
])
def test_idempotent_and_balanced(html):
    once, probs = san(html)
    assert probs == [] and once
    twice, probs2 = san(once)
    assert probs2 == [] and twice == once
    assert san(once, prefix="cwv0-")[0] == once


def test_prefix_not_stacked_on_resanitise():
    html = (f'{SVG_OPEN}<defs><marker id="m"/></defs><line x1="0" y1="0" x2="5" y2="5" marker-end="url(#m)"/></svg>')
    once, _ = san(html, prefix="cwv2-")
    assert 'id="cwv2-m"' in once
    assert san(once, prefix="cwv2-")[0] == once


def test_realistic_pr_level_picture_passes():
    html = '''
<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:stretch">
  <section style="flex:1 1 280px;max-width:100%;padding:12px;border:1px solid var(--border);border-radius:var(--radius);background:var(--card)">
    <h4 style="margin:0 0 8px;color:var(--muted-foreground);text-transform:uppercase;font-size:0.75rem">Before</h4>
    <p style="margin:0">Each handler built its own <code style="background:var(--code-background)">Registry</code>.</p>
    <ul style="margin:8px 0 0;padding-left:16px"><li>3 copies</li><li>no shared cache</li></ul>
  </section>
  <section style="flex:1 1 280px;max-width:100%;padding:12px;border:1px solid var(--border);border-radius:var(--radius);background:color-mix(in srgb, var(--chart-2) 14%, transparent)">
    <h4 style="margin:0 0 8px;font-size:0.75rem">After</h4>
    <p style="margin:0">One <strong>shared registry</strong>, resolved once.</p>
  </section>
</div>
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-top:12px">
  <span style="padding:4px 8px;border-radius:999px;background:var(--accent-surface);color:var(--accent)">cx 12 &rarr; 9</span>
  <span style="padding:4px 8px;border-radius:999px;background:var(--destructive-surface);color:var(--destructive)">-2 files</span>
</div>
<svg viewBox="0 0 320 90" style="max-width:100%;height:auto" role="img" aria-label="request flows from handler to registry to store">
  <defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="userSpaceOnUse">
    <path d="M0 0 L8 4 L0 8 z" fill="var(--muted-foreground)"/></marker></defs>
  <rect x="4" y="25" width="80" height="40" rx="6" fill="color-mix(in srgb, var(--chart-1) 20%, transparent)" stroke="var(--chart-1)" stroke-width="1.5"/>
  <text x="44" y="50" text-anchor="middle" font-size="12" font-family="var(--font-mono)" fill="var(--foreground)">handler</text>
  <line x1="84" y1="45" x2="118" y2="45" stroke="var(--muted-foreground)" stroke-width="1.5" marker-end="url(#arrow)"/>
  <rect x="120" y="25" width="80" height="40" rx="6" fill="none" stroke="var(--chart-2)" stroke-dasharray="4 2"/>
  <text x="160" y="50" text-anchor="middle" font-size="12" fill="var(--foreground)">registry</text>
  <polyline points="200,45 230,45 230,70 250,70" fill="none" stroke="var(--muted-foreground)" marker-end="url(#arrow)"/>
  <circle cx="270" cy="70" r="16" fill="var(--card)" stroke="var(--chart-3)"/>
  <text x="270" y="74" text-anchor="middle" font-size="11" fill="var(--foreground)">store</text>
</svg>'''
    out, probs = san(html)
    assert probs == [], probs
    assert out.count("<svg") == 1 and 'marker-end="url(#cwv0-arrow)"' in out
    assert san(out)[0] == out


def raw(pr=None, groups=()):
    return {"pr": pr, "groups": list(groups)}


OK = "<p>ok</p>"


def test_check_visuals_happy_path():
    clean, probs = v.check_visuals(raw({"title": "  The  PR ", "html": OK},
                                       [{"index": 1, "html": OK}, {"index": 0, "html": "<div>b</div>"}]), 2)
    assert probs == []
    assert clean == {"pr": {"title": "The PR", "html": OK}, "groups": {1: OK, 0: "<div>b</div>"}}


def test_check_visuals_null_pr_and_empty_groups_are_fine():
    assert v.check_visuals({"pr": None, "groups": []}, 3) == ({"pr": None, "groups": {}}, [])
    assert v.check_visuals({}, 3) == ({"pr": None, "groups": {}}, [])


def test_check_visuals_group_index_handling():
    clean, probs = v.check_visuals(raw(None, [
        {"index": 0, "html": OK}, {"index": 0, "html": "<p>dup</p>"}, {"index": 5, "html": OK},
        {"index": -1, "html": OK}, {"index": "1", "html": OK}, {"index": True, "html": OK},
        {"index": 1.0, "html": OK}, {"html": OK}, "x", None,
    ]), 2)
    assert clean["groups"] == {0: OK}
    assert len(probs) == 9
    assert any("used twice" in p for p in probs) and any("out of range" in p or "not a group index" in p for p in probs)


def test_check_visuals_prefixes_ids_per_fragment():
    svg = (f'{SVG_OPEN}<defs><marker id="a"/></defs><path d="M0 0L5 5" marker-end="url(#a)"/></svg>')
    clean, probs = v.check_visuals(raw({"title": "t", "html": svg}, [{"index": 2, "html": svg}]), 3)
    assert probs == []
    assert 'id="cwv0-a"' in clean["pr"]["html"] and 'id="cwv3-a"' in clean["groups"][2]


def test_check_visuals_problem_lines_name_the_fragment_and_drop_it():
    clean, probs = v.check_visuals(raw({"title": "t", "html": "<img>"}, [
        {"index": 0, "html": "<script>"}, {"index": 1, "html": OK}]), 2)
    assert clean == {"pr": None, "groups": {1: OK}}
    assert "visuals.pr: <img> is not allowed" in probs
    assert "visuals.groups[0]: <script> is not allowed" in probs


@pytest.mark.parametrize("title", [None, "", "   ", "x" * 81, 5, ["a"]])
def test_check_visuals_bad_titles(title):
    clean, probs = v.check_visuals(raw({"title": title, "html": OK}), 1)
    assert clean["pr"] is None and any("title" in p for p in probs)


def test_check_visuals_title_kept_as_plain_text():
    clean, _ = v.check_visuals(raw({"title": "<b>x</b> & y", "html": OK}), 1)
    assert clean["pr"]["title"] == "<b>x</b> & y"


def test_check_visuals_size_caps():
    big = "<p>" + "x" * (13 * 1024) + "</p>"
    clean, probs = v.check_visuals(raw(None, [{"index": 0, "html": big}]), 1)
    assert clean["groups"] == {} and "limit" in probs[0]
    many = [{"index": i, "html": "<p>" + "y" * 11000 + "</p>"} for i in range(12)]
    clean, probs = v.check_visuals(raw({"title": "t", "html": OK}, many), 12)
    assert clean == {"pr": None, "groups": {}} and "in total" in probs[0]
    clean, probs = v.check_visuals(raw(None, [{"index": i, "html": OK} for i in range(13)]), 13)
    assert sorted(clean["groups"]) == list(range(12)) and any("at most 12" in p for p in probs)


@pytest.mark.parametrize("junk", [
    None, 5, "x", [], [1, 2], {"pr": 5}, {"pr": "x"}, {"pr": []}, {"pr": {"title": "t"}},
    {"pr": {"title": "t", "html": 5}}, {"groups": "x"}, {"groups": {"a": 1}}, {"groups": [[]]},
    {"groups": [{"index": [], "html": {}}]}, {"pr": {"title": {}, "html": []}, "groups": 7},
])
def test_check_visuals_never_raises_on_garbage(junk):
    clean, probs = v.check_visuals(junk, 2)
    assert clean == {"pr": None, "groups": {}} and probs
    clean, probs = v.check_visuals({"groups": [{"index": 0, "html": OK}]}, "n")
    assert clean["groups"] == {} and probs
    v.check_visuals(junk, None)


def sym(i, file="a.ts", state="new", rng=(1, 5), label=None):
    return {"id": f"p:{i}", "label": label or i, "kind": "symbol", "parent": "p", "state": state,
            "file": file, "range": list(rng)}


def test_digest_lines_and_complexity_match():
    sd = {"nodes": [{"id": "p", "label": "p", "kind": "pkg"}, sym("f"), sym("g", state="changed", rng=[7, 9])],
          "edges": [{"source": "p:f", "target": "p:g", "state": "new"},
                    {"source": "p:f", "target": "p", "state": "new"},
                    {"source": "p:zzz", "target": "p:g", "state": "gone"}]}
    cx = {"files": {"a.ts": {"symbols": [{"name": "f", "before": 3, "after": 5}, {"name": "other", "before": 1, "after": 1}]}}}
    d = v.symbol_digest(sd, cx).splitlines()
    assert "p:f | a.ts:1-5 | new | cx 3->5" in d
    assert "p:g | a.ts:7-9 | changed | -" in d
    assert "p:f -> p:g | new" in d
    assert not any("p:zzz" in x or "-> p |" in x for x in d)
    assert not any(x.startswith("p |") for x in d)
    assert v.symbol_digest(sd).splitlines()[1].endswith("| -")


def test_digest_empty_without_symbols_and_for_garbage():
    assert v.symbol_digest({"nodes": [{"id": "p", "kind": "pkg"}], "edges": []}, {}) == ""
    for junk in (None, 5, [], {}, {"nodes": "x"}, {"nodes": [None, 3]}):
        assert v.symbol_digest(junk, junk) == ""
    assert v.symbol_digest({"nodes": [sym("f", file=None)]}, {"files": []}) .count("\n") == 1


def test_digest_caps():
    nodes = [sym(f"s{i}") for i in range(200)]
    edges = [{"source": f"p:s{i}", "target": f"p:s{i + 1}", "state": "new"} for i in range(150)]
    d = v.symbol_digest({"nodes": nodes, "edges": edges}).splitlines()
    assert sum(1 for x in d if " | a.ts:" in x) == 150
    assert "... and 50 more symbols" in d
    assert sum(1 for x in d if " -> " in x and " | new" in x) == 100
    assert "... and 50 more edges" in d


def run_cli(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


def test_cli_digest_and_check(tmp_path):
    sd = tmp_path / "s.json"
    sd.write_text(json.dumps({"nodes": [sym("f")], "edges": []}))
    cx = tmp_path / "c.json"
    cx.write_text(json.dumps({"files": {"a.ts": {"symbols": [{"name": "f", "before": 1, "after": 2}]}}}))
    r = run_cli("digest", "--symdelta", str(sd), "--complexity", str(cx))
    assert r.returncode == 0 and "cx 1->2" in r.stdout
    good = tmp_path / "g.json"
    good.write_text(json.dumps(raw({"title": "t", "html": OK}, [{"index": 0, "html": OK}])))
    r = run_cli("check", "--visuals", str(good), "--groups", "1")
    assert r.returncode == 0 and r.stdout.strip() == "ok"
    bad = tmp_path / "b.json"
    bad.write_text(json.dumps(raw({"title": "t", "html": "<img>"})))
    r = run_cli("check", "--visuals", str(bad), "--groups", "1")
    assert r.returncode == 1 and "visuals.pr: <img> is not allowed" in r.stdout
    r = run_cli("check", "--visuals", str(tmp_path / "missing.json"), "--groups", "1")
    assert r.returncode == 1
    notjson = tmp_path / "n.json"
    notjson.write_text("{")
    assert run_cli("check", "--visuals", str(notjson), "--groups", "1").returncode == 1
