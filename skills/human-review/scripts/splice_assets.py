#!/usr/bin/env python3
"""Inline the vendored JS a built page actually uses.

One placeholder. Mermaid is 3.2MB, so a page with no `class="mermaid"` block (a markdown-only
run never even builds one) should not carry it. An unused placeholder stays in the file as an
inert HTML comment, which is why it is safe to leave unfilled rather than erroring.

Idempotent: a placeholder that is already gone is skipped, so re-splicing an existing page
does not double the payload.
"""
import argparse
import pathlib
import sys

# placeholder -> (marker the page must contain to need it, asset filenames in load order)
ASSETS = {
    "<!-- MERMAID_JS -->": ('class="mermaid"', ["mermaid.min.js"]),
}


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
