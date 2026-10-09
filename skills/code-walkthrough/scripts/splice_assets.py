#!/usr/bin/env python3
"""Inline the vendored JS a built page actually uses.

Each placeholder below is only filled when the page actually contains the marker it needs:
Mermaid is 3.2MB and hljs is 128K, so an explain-only or diagram-only page does not carry
either just because the template has both placeholders. An unfilled placeholder stays in
the file as an inert HTML comment, which is why it is safe to leave unfilled rather than
erroring.

Idempotent: a placeholder that is already gone is skipped, so re-splicing an existing page
does not double the payload.
"""
import argparse
import pathlib
import re
import sys

# placeholder -> (marker the page must contain to need it, asset filenames in load order)
ASSETS = {
    "<!-- MERMAID_JS -->": ('class="mermaid"', ["mermaid.min.js"]),
    "<!-- HLJS_JS -->": ('class="diff"', ["highlight.min.js"]),
}

# Nextcloud's HTML viewer rewrites every `<script` substring in the page to inject a CSP
# nonce, including one inside hljs's own XML-grammar regex literal. The nonce is random
# base64 and often contains "/", which can terminate that literal early with a SyntaxError,
# killing highlighting. \x73 is a valid "s" escape in both regex and string literals, so
# this stays JS-identical while removing the substring the rewrite matches on.
SCRIPT_TAG_RE = re.compile(r"<(s)cript", re.IGNORECASE)


def _escape_script_tag(match):
    s = match.group(1)
    hex_escape = r"\x53" if s == "S" else r"\x73"
    return "<" + hex_escape + match.group(0)[2:]


def mermaid_payload(skill_dir):
    js = (pathlib.Path(skill_dir) / "assets" / "mermaid.min.js").read_text()
    return SCRIPT_TAG_RE.sub(_escape_script_tag, js)


def splice(page_path, skill_dir):
    page = pathlib.Path(page_path)
    text = page.read_text()
    report = []
    for placeholder, (needle, filenames) in ASSETS.items():
        name = placeholder.strip("<!- >")
        if placeholder not in text:
            report.append(f"{name}: no placeholder, skipped")
            continue
        if needle not in text:
            report.append(f"{name}: page has no {needle}, left empty")
            continue
        missing = [f for f in filenames if not (skill_dir / "assets" / f).is_file()]
        if missing:
            return report, f"missing vendored asset(s): {', '.join(missing)}"
        js = "\n".join((skill_dir / "assets" / f).read_text() for f in filenames)
        js = SCRIPT_TAG_RE.sub(_escape_script_tag, js)
        text = text.replace(placeholder, js)
        report.append(f"{name}: spliced {len(js)} bytes from {', '.join(filenames)}")
    page.write_text(text)
    return report, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("page")
    parser.add_argument(
        "--skill",
        default=str(pathlib.Path(__file__).resolve().parent.parent),
        help="skill directory holding assets/ (defaults to this script's own skill)",
    )
    args = parser.parse_args()
    report, error = splice(args.page, pathlib.Path(args.skill))
    for line in report:
        print(line)
    if error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
