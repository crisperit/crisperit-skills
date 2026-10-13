#!/usr/bin/env python3
"""Sanitiser for model-authored HTML/SVG pictures, plus the symbol digest the author reads.

Output is rebuilt only from our own parsed tree (allowlisted names, escaped values), so
nothing the model copied from untrusted PR text reaches the page unchecked. Any violation
is fatal for that fragment; the problem lines drive the author's repair loop.
"""
import argparse
import html as _html
import json
import re
import sys
from html.parser import HTMLParser

PR_MAX = 24 * 1024
GROUP_MAX = 12 * 1024
TOTAL_MAX = 120 * 1024
MAX_ELEMENTS = 400
MAX_DEPTH = 12
MAX_GROUPS = 12
MAX_PROBLEMS = 8
TITLE_MAX = 80

_PHRASING = frozenset("span strong em b i code small sub sup br mark kbd abbr del ins svg".split())
_FLOW = _PHRASING | frozenset(
    "div p h4 h5 ul ol dl table figure section header footer hr pre blockquote".split())
_SVG_KIDS = frozenset("g rect circle ellipse line polyline polygon path text title defs marker".split())
_SVG_SHAPES = frozenset("rect circle ellipse line polyline polygon path".split())
_VOID = frozenset({"br", "hr"}) | _SVG_SHAPES

_CHILDREN = {None: _FLOW, "figure": _FLOW | {"figcaption"}, "ul": {"li"}, "ol": {"li"},
             "dl": {"dt", "dd"}, "table": {"caption", "thead", "tbody", "tfoot", "tr"},
             "thead": {"tr"}, "tbody": {"tr"}, "tfoot": {"tr"}, "tr": {"th", "td"},
             "svg": _SVG_KIDS, "g": _SVG_KIDS, "defs": _SVG_KIDS,
             "marker": _SVG_KIDS - {"marker", "defs"}, "text": {"tspan"}, "tspan": {"tspan"},
             "title": set()}
for _t in _PHRASING - {"svg"}:
    _CHILDREN[_t] = _PHRASING
for _t in "p h4 h5 dt caption pre figcaption".split():
    _CHILDREN[_t] = _PHRASING
for _t in "div section header footer blockquote li dd td th".split():
    _CHILDREN[_t] = _FLOW
for _t in _SVG_SHAPES | {"br", "hr"}:
    _CHILDREN[_t] = set()
_ALLOWED_TAGS = frozenset(_CHILDREN) - {None}
_TEXT_OK = frozenset(
    (_PHRASING - {"svg"}) | set("p h4 h5 dt caption pre figcaption div section header footer blockquote li dd td th figure "
                    "text tspan title".split()))

_GLOBAL = {"style", "title", "aria-label", "aria-hidden", "role"}
_PRES = {"fill", "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap", "stroke-linejoin", "opacity"}
_TXT = {"text-anchor", "dominant-baseline", "font-size", "font-weight", "font-family"}
_MARK = {"marker-end", "marker-start"}
_ATTRS = {
    "svg": {"viewbox", "width", "height", "xmlns"}, "g": _PRES | _TXT,
    "rect": {"x", "y", "width", "height", "rx", "ry"} | _PRES,
    "circle": {"cx", "cy", "r"} | _PRES, "ellipse": {"cx", "cy", "rx", "ry"} | _PRES,
    "line": {"x1", "y1", "x2", "y2"} | _PRES | _MARK,
    "polyline": {"points"} | _PRES | _MARK, "polygon": {"points"} | _PRES,
    "path": {"d"} | _PRES | _MARK,
    "text": {"x", "y", "dx", "dy"} | _TXT | _PRES, "tspan": {"x", "y", "dx", "dy"} | _TXT | _PRES,
    "marker": {"id", "markerwidth", "markerheight", "refx", "refy", "orient", "markerunits", "viewbox"},
    "td": {"colspan", "rowspan", "scope"}, "th": {"colspan", "rowspan", "scope"},
}
_CANON = {"viewbox": "viewBox", "markerwidth": "markerWidth", "markerheight": "markerHeight",
          "refx": "refX", "refy": "refY", "markerunits": "markerUnits"}

VARS = frozenset(
    ["--background", "--foreground", "--card", "--border", "--muted-foreground", "--accent",
     "--font-mono", "--font-sans", "--radius", "--success", "--warning", "--destructive",
     "--destructive-surface", "--accent-surface", "--code-background"]
    + [f"--chart-{i}" for i in range(1, 7)])

_KEYWORDS = frozenset("""none auto flex block inline inline-block inline-flex grid inline-grid row column
row-reverse column-reverse wrap nowrap wrap-reverse start end center flex-start flex-end space-between
space-around space-evenly stretch baseline normal bold bolder lighter italic oblique left right justify
uppercase lowercase capitalize underline line-through overline solid dashed dotted double hidden visible
clip border-box content-box fit-content min-content max-content auto-fit auto-fill transparent currentcolor
in srgb sans-serif monospace serif system-ui pre pre-wrap pre-line break-word anywhere break-all keep-all
top middle bottom sub super text-top text-bottom tabular-nums lining-nums thin medium thick span dense
tabular-nums""".split())
_FUNCS = frozenset({"var", "calc", "min", "max", "clamp", "repeat", "minmax", "color-mix"})
_UNITS = frozenset({"", "px", "rem", "em", "%", "fr", "ch"})

_PROP_EXACT = frozenset("""display order gap row-gap column-gap place-items grid-template-columns
grid-template-rows grid-column grid-row width min-width max-width height min-height max-height box-sizing
border-radius color background background-color opacity fill stroke stroke-width font-size font-weight
font-style font-family line-height letter-spacing text-align text-transform text-decoration white-space
overflow-wrap word-break vertical-align font-variant-numeric overflow overflow-x overflow-y""".split())
_PROP_FAMILY = re.compile(
    r"(flex(-[a-z]+)*|align-[a-z]+|justify-[a-z]+|margin(-[a-z]+)*|padding(-[a-z]+)*|border(-[a-z]+)*)")
_CSS_BAD = re.compile(r"[\\@{}<>\"'!]|/\*|\*/|[^\x20-\x7e]")
_TOK = re.compile(r"\s*(?:(-?(?:\d+\.?\d*|\.\d+))([A-Za-z%]*)|(--[A-Za-z0-9_-]+)|([A-Za-z][A-Za-z0-9-]*)(\()?|([,/()+*-]))")

_MATH = frozenset({"calc", "min", "max", "clamp"})
_VAR_ARG = re.compile(r"\s*(--[A-Za-z0-9_-]+)\s*\)")
_NUM = re.compile(r"-?(?:[0-9]+\.?[0-9]*|\.[0-9]+)")
_PAINT_VAR = r"var\((--[a-z0-9-]+)\)"
_PAINT = re.compile(rf"none|currentcolor|{_PAINT_VAR}|color-mix\(in srgb, {_PAINT_VAR} (\d{{1,3}}(?:\.\d+)?)%, transparent\)",
                    re.I)
_ID = re.compile(r"[a-z][a-z0-9-]{0,24}")
_URL_REF = re.compile(r"url\(#([a-z0-9-]+)\)")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ROLES = {"img", "list", "listitem", "group", "note", "figure", "presentation", "none"}


def _esc(s):
    return repr(s[:30])[1:-1]


class _Bad(Exception):
    pass


def _number(v, lo=None, hi=2000, neg=False, pct=True):
    v = v.strip()
    m = _NUM.fullmatch(v[:-1] if pct and v.endswith("%") else v)
    if not m:
        raise _Bad(f"{v[:30]!r} is not a number")
    f = float(m.group())
    if (f < 0 and not neg) or abs(f) > hi or (lo is not None and f < lo):
        raise _Bad(f"{v[:30]!r} is out of range")
    return v


def _css_value(prop, value):
    """Validate one declaration value; returns it whitespace-collapsed."""
    pos, stack, toks, terms = 0, [], [], 0
    while pos < len(value):
        m = _TOK.match(value, pos)
        if not m:
            if not value[pos:].strip():
                break
            raise _Bad(f"unexpected character in {prop} value")
        pos = m.end()
        num, unit, var, ident, paren, punct = m.groups()
        if len(toks) > 100:
            raise _Bad(f"{prop} value is too long")
        if num is not None:
            if unit.lower() not in _UNITS:
                raise _Bad(f"unit {unit!r} is not allowed in {prop}")
            if num.startswith("-") or abs(float(num)) > 2000:
                raise _Bad(f"{num}{unit} is out of range in {prop}")
            if _MATH.intersection(stack):
                terms += 1
                if terms > 8:
                    raise _Bad(f"{prop} math has more than 8 numbers")
            toks.append((num, unit.lower()))
        elif var:
            raise _Bad(f"{var} is only allowed inside var()")
        elif ident and ident.lower() == "var" and paren:
            vm = _VAR_ARG.match(value, pos)
            if not vm or vm.group(1) not in VARS:
                raise _Bad(f"var() takes one allowed theme variable, not {value[pos:pos + 24].strip()!r}")
            if prop == "font-family" and vm.group(1) not in ("--font-mono", "--font-sans"):
                raise _Bad("font-family takes only --font-sans or --font-mono")
            pos = vm.end()
            toks.append(vm.group(1))
        elif ident:
            if paren:
                if ident.lower() not in _FUNCS:
                    raise _Bad(f"{ident}() is not allowed")
                stack.append(ident.lower())
            elif ident.lower() not in _KEYWORDS:
                raise _Bad(f"{ident!r} is not allowed in {prop}")
            toks.append(ident.lower())
        elif punct == "(":
            if not stack:
                raise _Bad("unexpected ( in value")
            stack.append("")
        elif punct == ")":
            if not stack:
                raise _Bad("unbalanced ) in value")
            stack.pop()
            toks.append(")")
        else:
            if punct in "*/" and _MATH.intersection(stack):
                raise _Bad(f"{punct} is not allowed inside calc, min, max or clamp in {prop}")
            toks.append(punct)
    if stack or not toks:
        raise _Bad(f"{prop} value is empty or unbalanced")
    if prop == "font-size":
        if len(toks) != 1 or not isinstance(toks[0], tuple):
            raise _Bad("font-size takes one number with a unit")
        n, u = float(toks[0][0]), toks[0][1]
        if not {"px": 11 <= n <= 32, "rem": .7 <= n <= 2, "em": .7 <= n <= 2, "%": 70 <= n <= 200}.get(u, False):
            raise _Bad("font-size must be 11-32px, 0.7-2rem or 70-200%")
    elif prop in ("overflow", "overflow-x", "overflow-y"):
        if len(toks) != 1 or toks[0] not in ("visible", "hidden", "auto", "clip"):
            raise _Bad("overflow must be visible, hidden, auto or clip")
    elif prop == "opacity":
        if len(toks) != 1 or not isinstance(toks[0], tuple) or toks[0][1] or float(toks[0][0]) > 1:
            raise _Bad("opacity must be 0..1")
    elif prop in ("fill", "stroke") and not _paint(value.strip()):
        raise _Bad(f"{prop} must be none, currentColor, var(--theme) or color-mix of one")
    return " ".join(value.split())


def _paint(v):
    m = _PAINT.fullmatch(v)
    if not m:
        return False
    names = [g for g in m.groups()[:2] if g]
    return all(n in VARS for n in names) and (m.group(3) is None or float(m.group(3)) <= 100)


def _style(v):
    if len(v) > 500:
        raise _Bad("style is longer than 500 characters")
    if _CSS_BAD.search(v):
        raise _Bad("style contains a forbidden character (\\ @ { } < > \" ' ! /* non-ASCII)")
    decls = [d for d in v.split(";") if d.strip()]
    if len(decls) > 20:
        raise _Bad("style has more than 20 declarations")
    out = []
    for d in decls:
        prop, sep, val = d.partition(":")
        prop = prop.strip().lower()
        if not sep or not val.strip():
            raise _Bad(f"malformed declaration {d.strip()[:30]!r}")
        if prop not in _PROP_EXACT and not _PROP_FAMILY.fullmatch(prop):
            raise _Bad(f"CSS property {prop[:30]!r} is not allowed")
        out.append(f"{prop}: {_css_value(prop, val)}")
    return "; ".join(out)


class _Node:
    __slots__ = ("tag", "attrs", "kids")

    def __init__(self, tag, attrs):
        self.tag, self.attrs, self.kids = tag, attrs, []


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node(None, [])
        self.stack = [self.root]
        self.count = 0
        self.errs = []

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > MAX_ELEMENTS:
            self.errs.append(f"more than {MAX_ELEMENTS} elements")
            return
        if len(self.stack) > MAX_DEPTH:
            self.errs.append(f"nesting deeper than {MAX_DEPTH} levels")
            return
        n = _Node(tag, attrs)
        self.stack[-1].kids.append(n)
        if tag not in _VOID:
            self.stack.append(n)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        data = _CTRL.sub("", data)
        kids = self.stack[-1].kids
        if kids and isinstance(kids[-1], str):
            kids[-1] += data
        else:
            kids.append(data)

    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass


class _Emit:
    def __init__(self, prefix):
        self.prefix, self.problems = prefix, []
        self.defined, self.refs = set(), []

    def bad(self, msg):
        if msg not in self.problems:
            self.problems.append(msg)

    def bare_id(self, v):
        return v[len(self.prefix):] if v.startswith(self.prefix) else v

    def attr(self, tag, name, val):
        if val is None:
            raise _Bad("needs a value")
        if _CTRL.search(val):
            raise _Bad("has control characters")
        if name not in _GLOBAL and name not in _ATTRS.get(tag, ()):
            raise _Bad("is not allowed")
        v = val.strip()
        if name == "style":
            return _style(v)
        if name in ("title", "aria-label"):
            if len(val) > 200:
                raise _Bad("is longer than 200 characters")
            return val
        if name == "aria-hidden":
            return self._one_of(name, v.lower(), {"true", "false"})
        if name == "role":
            return self._one_of(name, v.lower(), _ROLES)
        if name in ("x", "y", "dx", "dy"):
            return _number(v, neg=tag in ("text", "tspan"))
        if name in ("cx", "cy", "r", "rx", "ry", "x1", "y1", "x2", "y2", "width", "height",
                    "markerwidth", "markerheight", "refx", "refy"):
            return _number(v)
        if name == "viewbox":
            parts = [p for p in re.split(r"[\s,]+", v) if p]
            if len(parts) != 4:
                raise _Bad("viewBox needs four numbers")
            for i, p in enumerate(parts):
                _number(p, lo=1 if i > 1 else None, neg=True, pct=False)
            return " ".join(parts)
        if name == "xmlns":
            if v != "http://www.w3.org/2000/svg":
                raise _Bad("xmlns must be the SVG namespace")
            return None
        if name in ("points", "d"):
            return self._path(name, v)
        if name == "id":
            bare = self.bare_id(v)
            if not _ID.fullmatch(bare):
                raise _Bad(f"marker id {v[:30]!r} must match [a-z][a-z0-9-]*, 25 chars at most")
            if bare in self.defined:
                raise _Bad(f"marker id {bare!r} is defined twice")
            self.defined.add(bare)
            return self.prefix + bare
        if name in _MARK:
            m = _URL_REF.fullmatch(v)
            if not m or not _ID.fullmatch(self.bare_id(m.group(1))):
                raise _Bad(f"{name} must be url(#id) of a marker in this fragment")
            self.refs.append((name, self.bare_id(m.group(1))))
            return f"url(#{self.prefix}{self.bare_id(m.group(1))})"
        if name == "orient":
            return self._one_of(name, v, {"auto"})
        if name == "markerunits":
            return {"strokewidth": "strokeWidth", "userspaceonuse": "userSpaceOnUse"}[
                self._one_of(name, v.lower(), {"strokewidth", "userspaceonuse"})]
        if name in ("fill", "stroke"):
            if not _paint(v):
                raise _Bad(f"{name} must be none, currentColor, var(--theme) or color-mix of one")
            return "currentColor" if v.lower() == "currentcolor" else v
        if name == "stroke-width":
            return _number(v, hi=20, pct=False)
        if name == "stroke-dasharray":
            if not re.fullmatch(r"[0-9., ]{1,50}", v):
                raise _Bad("stroke-dasharray takes up to 50 chars of numbers")
            return v
        if name == "stroke-linecap":
            return self._one_of(name, v, {"butt", "round", "square"})
        if name == "stroke-linejoin":
            return self._one_of(name, v, {"miter", "round", "bevel"})
        if name == "opacity":
            return _number(v, hi=1, pct=False)
        if name == "text-anchor":
            return self._one_of(name, v, {"start", "middle", "end"})
        if name == "dominant-baseline":
            return self._one_of(name, v, {"auto", "middle", "central", "hanging", "alphabetic"})
        if name == "font-size":
            n = _number(v, lo=11, hi=32, pct=False)
            return n
        if name == "font-weight":
            return self._one_of(name, v, {"normal", "bold", "400", "500", "600", "700"})
        if name == "font-family":
            return self._one_of(name, v, {"var(--font-sans)", "var(--font-mono)"})
        if name in ("colspan", "rowspan"):
            if not v.isdigit() or not 1 <= int(v) <= 20:
                raise _Bad(f"{name} must be 1..20")
            return str(int(v))
        if name == "scope":
            return self._one_of(name, v, {"col", "row", "colgroup", "rowgroup"})
        raise _Bad("is not allowed")

    @staticmethod
    def _one_of(name, v, allowed):
        if v not in allowed:
            raise _Bad(f"{name} must be one of {sorted(allowed)}")
        return v

    @staticmethod
    def _path(name, v):
        charset = r"[0-9\s,.]+" if name == "points" else r"[MmLlHhVvCcSsQqTtAaZz0-9\s,.\-]+"
        if len(v) > 2000 or not re.fullmatch(charset, v):
            raise _Bad(f"{name} is too long or has characters outside its allowed set")
        if any(abs(float(n)) > 2000 for n in re.findall(r"-?\d*\.?\d+", v)):
            raise _Bad(f"{name} has a number over 2000")
        return " ".join(v.split())

    def element(self, n, parent):
        tag = n.tag
        if tag not in _ALLOWED_TAGS:
            self.bad(f"<{_esc(tag)}> is not allowed")
            return ""
        if tag not in _CHILDREN.get(parent, ()):
            self.bad(f"<{_esc(tag)}> is not allowed " + (f"inside <{parent}>" if parent else "at the top level"))
            return ""
        parts, seen = [], set()
        for name, val in n.attrs:
            if name in seen:
                self.bad(f"<{tag}> repeats attribute {name[:30]!r}")
                continue
            seen.add(name)
            try:
                out = self.attr(tag, name, val)
            except _Bad as e:
                self.bad(f"<{tag}> {_esc(name)}: {e}")
                continue
            if out is not None:
                parts.append(f' {_CANON.get(name, name)}="{_html.escape(out, quote=True)}"')
        if tag == "svg":
            a = dict(n.attrs)
            if "viewbox" not in a:
                self.bad("<svg> needs a viewBox")
            if not ((a.get("role", "") or "").strip().lower() == "img" and (a.get("aria-label") or "").strip()
                    or (a.get("aria-hidden", "") or "").strip().lower() == "true"):
                self.bad("<svg> needs role=img with aria-label, or aria-hidden=true")
        head = f"<{tag}{''.join(parts)}"
        if tag in _VOID:
            return head + (">" if tag in ("br", "hr") else "/>")
        body = []
        for kid in n.kids:
            if isinstance(kid, str):
                if tag in _TEXT_OK:
                    body.append(_html.escape(kid, quote=False))
                elif kid.strip():
                    self.bad(f"text is not allowed directly inside <{tag}>")
            else:
                body.append(self.element(kid, tag))
        return f"{head}>{''.join(body)}</{tag}>"


def _cap(problems):
    if len(problems) <= MAX_PROBLEMS:
        return problems
    return problems[:MAX_PROBLEMS] + [f"... and {len(problems) - MAX_PROBLEMS} more"]


def sanitize_fragment(html, prefix="cwv0-", max_bytes=PR_MAX):
    """Returns (clean_html | None, problems). None whenever any problem exists."""
    if not isinstance(html, str):
        return None, ["fragment must be a string"]
    if not html.strip():
        return None, ["fragment is empty"]
    size = len(html.encode("utf-8", "replace"))
    if size > max_bytes:
        return None, [f"fragment is {size // 1024} KB, over the {max_bytes // 1024} KB limit"]
    try:
        b = _Builder()
        b.feed(html)
        b.close()
        em = _Emit(prefix)
        em.problems.extend(dict.fromkeys(b.errs))
        out = "".join(
            em.element(k, None) if not isinstance(k, str) else _html.escape(k, quote=False) for k in b.root.kids)
        for name, ref in em.refs:
            if ref not in em.defined:
                em.bad(f"{name} references marker {ref!r}, which this fragment does not define")
    except Exception as e:
        return None, [f"could not parse the fragment ({type(e).__name__})"]
    if em.problems:
        return None, _cap(em.problems)
    if not out.strip():
        return None, ["fragment has no content"]
    return out, []


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def check_visuals(raw, n_groups):
    """raw = {"pr": {"title","html"}|None, "groups": [{"index","html"}]} -> (clean, problems)."""
    clean = {"pr": None, "groups": {}}
    if not isinstance(raw, dict):
        return clean, ['visuals: expected an object with "pr" and "groups"']
    n_groups = n_groups if _is_int(n_groups) else 0
    problems, total = [], 0

    def frag(label, html, prefix, cap):
        out, probs = sanitize_fragment(html, prefix, cap)
        problems.extend(f"{label}: {p}" for p in probs)
        return out

    pr = raw.get("pr")
    groups = raw.get("groups")
    if groups is None:
        groups = []
    if not isinstance(groups, list):
        problems.append("visuals.groups: expected a list")
        groups = []
    if len(groups) > MAX_GROUPS:
        problems.append(f"visuals.groups: at most {MAX_GROUPS} pictures, {len(groups) - MAX_GROUPS} dropped")
        groups = groups[:MAX_GROUPS]
    items = [("pr", pr)] + [(f"groups[{k}]", g) for k, g in enumerate(groups)]
    for _, it in items:
        h = it.get("html") if isinstance(it, dict) else None
        total += len(h.encode("utf-8", "replace")) if isinstance(h, str) else 0
    if total > TOTAL_MAX:
        return clean, [f"visuals: {total // 1024} KB in total, over the {TOTAL_MAX // 1024} KB limit"]

    if pr is not None:
        if not isinstance(pr, dict):
            problems.append("visuals.pr: expected an object with title and html or null")
        else:
            title = pr.get("title")
            title = " ".join(title.split()) if isinstance(title, str) else ""
            if not 1 <= len(title) <= TITLE_MAX:
                problems.append(f"visuals.pr: title must be plain text of 1..{TITLE_MAX} characters")
            else:
                out = frag("visuals.pr", pr.get("html"), "cwv0-", PR_MAX)
                if out:
                    clean["pr"] = {"title": title, "html": out}
    for k, g in enumerate(groups):
        label = f"visuals.groups[{k}]"
        if not isinstance(g, dict):
            problems.append(f"{label}: expected an object with index and html")
            continue
        idx = g.get("index")
        if not _is_int(idx) or not 0 <= idx < n_groups:
            problems.append(f"{label}: index {str(idx)[:20]} is not a group index (0..{n_groups - 1})")
            continue
        if idx in clean["groups"] or idx in {x.get("index") for x in groups[:k] if isinstance(x, dict)}:
            problems.append(f"{label}: index {idx} is used twice")
            continue
        out = frag(label, g.get("html"), f"cwv{idx + 1}-", GROUP_MAX)
        if out:
            clean["groups"][idx] = out
    return clean, problems


def symbol_digest(symdelta, complexity=None, max_symbols=150, max_edges=100):
    """One line per symbol and per symbol-to-symbol edge; '' when the delta has no symbols."""
    nodes = symdelta.get("nodes") if isinstance(symdelta, dict) else None
    syms = [n for n in nodes or [] if isinstance(n, dict) and n.get("kind") == "symbol"]
    if not syms:
        return ""
    files = (complexity or {}).get("files") if isinstance(complexity, dict) else None
    files = files if isinstance(files, dict) else {}
    lines = ["symbols (id | file:start-end | state | cx before->after):"]
    for n in syms[:max_symbols]:
        rng = n.get("range") if isinstance(n.get("range"), list) and len(n["range"]) == 2 else ["?", "?"]
        cx = "-"
        entry = files.get(n.get("file")) if isinstance(files.get(n.get("file")), dict) else {}
        for s in entry.get("symbols") or []:
            if isinstance(s, dict) and s.get("name") == n.get("label"):
                cx = f"cx {s.get('before')}->{s.get('after')}"
                break
        lines.append(f"{n.get('id')} | {n.get('file', '?')}:{rng[0]}-{rng[1]} | {n.get('state', '?')} | {cx}")
    if len(syms) > max_symbols:
        lines.append(f"... and {len(syms) - max_symbols} more symbols")
    ids = {n.get("id") for n in syms}
    edges = [e for e in symdelta.get("edges") or []
             if isinstance(e, dict) and e.get("source") in ids and e.get("target") in ids]
    if edges:
        lines.append("edges (caller -> callee | state):")
        lines += [f"{e['source']} -> {e['target']} | {e.get('state', '?')}" for e in edges[:max_edges]]
        if len(edges) > max_edges:
            lines.append(f"... and {len(edges) - max_edges} more edges")
    return "\n".join(lines)


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("digest")
    d.add_argument("--symdelta", required=True)
    d.add_argument("--complexity")
    c = sub.add_parser("check")
    c.add_argument("--visuals", required=True)
    c.add_argument("--groups", type=int, required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "digest":
            print(symbol_digest(_load(a.symdelta), _load(a.complexity) if a.complexity else None))
            return 0
        _, problems = check_visuals(_load(a.visuals), a.groups)
    except (OSError, ValueError) as e:
        print(f"cannot read input: {e}", file=sys.stderr)
        return 1
    print("\n".join(problems) if problems else "ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
