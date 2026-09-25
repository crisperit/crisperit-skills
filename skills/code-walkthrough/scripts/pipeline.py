#!/usr/bin/env python3
"""One driver for the steps after the analysis: gate, graphs, links, walkthrough, state and
render. Runs each existing script as a subprocess with the same argv docs/demo/build.sh already
exercises in CI, so behaviour and flags match what CI runs, not a parallel in-process path.

Usage:
  pipeline.py early --dir D --repo R --base B --head H
  pipeline.py prepare --dir D --repo R --base B --head H [--pr N] [--paths P ...]
      [--explain] [--prior] [--links-cached]
  pipeline.py render --dir D --slug S [--title T]
  pipeline.py all --dir D --repo R --base B --head H --slug S [--title T] [--pr N]
      [--paths P ...] [--explain] [--prior] [--links-cached]

All three read raw.diff, analysis.json and, if present, symdelta.json from --dir. symdelta.py
itself stays outside this driver: it runs alongside the fan-out, before this is invoked.

Stdout is a few plain lines: the gate result, each graph section's language or null reason,
links on/off, a warning when analysis.json's verdict is blank, and (from render) the page path.
It never prints hunk text or notes.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent


def _script(name):
    return str(SCRIPTS_DIR / name)


def run(name, *args, capture=False):
    return subprocess.run(
        [sys.executable, _script(name), *args],
        check=False, capture_output=capture, text=True, errors="replace",
    )


def _run_to_file(name, args, out_path):
    """Run a script whose real output is on stdout, saving it to out_path on success."""
    result = run(name, *args, capture=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        return False
    out_path.write_text(result.stdout)
    return True


def _run_ok(name, args):
    """Run a script that writes its own --out file, forwarding stderr on failure."""
    result = run(name, *args, capture=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        return False
    return True


def _repo_toplevel(repo):
    result = subprocess.run(["git", "-C", repo, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def cmd_early(args):
    d = Path(args.dir)
    ok = _run_to_file(
        "complexity.py",
        ["--repo", args.repo, "--base", args.base, "--head", args.head,
         "--diff", str(d / "raw.diff")],
        d / "complexity.json",
    )
    return 0 if ok else 1


def cmd_prepare(args):
    d = Path(args.dir)
    toplevel = _repo_toplevel(args.repo)
    if toplevel is None:
        print(f"--repo {args.repo} does not look like a git repo "
              "(git rev-parse --show-toplevel failed)", file=sys.stderr)
        return 1
    resolved_dir = d.resolve()
    if resolved_dir == toplevel or toplevel in resolved_dir.parents:
        print(f"--dir {d} is inside the repo ({toplevel}); output must stay outside "
              "the working tree", file=sys.stderr)
        return 1

    raw_diff = d / "raw.diff"
    analysis_path = d / "analysis.json"

    gate = run("validate_analysis.py", "--diff", str(raw_diff), "--analysis", str(analysis_path))
    if gate.returncode != 0:
        return 1

    for stale in d.glob("section-*.html"):
        stale.unlink()

    complexity_path = d / "complexity.json"
    if not complexity_path.exists():
        print("complexity: no early/regen hit, ran now")
        if not _run_to_file(
            "complexity.py",
            ["--repo", args.repo, "--base", args.base, "--head", args.head,
             "--diff", str(raw_diff)],
            complexity_path,
        ):
            return 1

    symdelta_path = d / "symdelta.json"
    if symdelta_path.exists():
        structure_json = d / "structure.json"
        structure_cmd = ["--repo", args.repo, "--base", args.base, "--head", args.head,
                          "--symdelta", str(symdelta_path), "--analysis", str(analysis_path),
                          "--out", str(structure_json)]
        if args.paths:
            structure_cmd += ["--paths", *args.paths]
        if not _run_ok("structure.py", structure_cmd):
            return 1

        structure_sections = ["--kind", "structure", "--data", str(structure_json),
                               "--format", "html"]
        if args.explain:
            structure_sections.append("--explain")
        if not _run_to_file("sections.py", structure_sections, d / "section-structure.html"):
            return 1

        structure_lang = json.loads(structure_json.read_text()).get("language")
        symdelta_lang = json.loads(symdelta_path.read_text()).get("language")
        print(f"structure: {structure_lang or 'null'}")
        print(f"symbols: {symdelta_lang or 'null'}")
    else:
        print("sections: skipped, no symdelta.json")

    links_path = d / "links.json"
    if not args.links_cached:
        links_cmd = ["--repo", args.repo, "--diff", str(raw_diff), "--head", args.head]
        if args.pr:
            links_cmd += ["--pr", args.pr]
        if not _run_to_file("links.py", links_cmd, links_path):
            return 1
    elif not links_path.exists():
        print(f"{links_path}: missing, but --links-cached was passed", file=sys.stderr)
        return 1
    links_data = json.loads(links_path.read_text())
    links_ok = bool(links_data.get("head_pushed")) and bool(links_data.get("repo_url"))
    print(f"links: {'on' if links_ok else 'off'}")

    walkthrough_cmd = ["--analysis", str(analysis_path), "--diff", str(raw_diff),
                        "--format", "html"]
    if symdelta_path.exists():
        walkthrough_cmd += ["--symdelta", str(symdelta_path)]
    if complexity_path.exists():
        walkthrough_cmd += ["--complexity", str(complexity_path)]
    if args.explain:
        walkthrough_cmd.append("--explain")
    if not _run_to_file("walkthrough.py", walkthrough_cmd, d / "section-walkthrough.html"):
        return 1

    state_path = d / "state.json"
    state_cmd = ["--analysis", str(analysis_path), "--diff", str(raw_diff), "--out", str(state_path)]
    if links_ok:
        state_cmd += ["--links", str(links_path)]
    if args.prior and state_path.exists():
        state_cmd += ["--prior", str(state_path)]
    if not _run_ok("state.py", state_cmd):
        return 1

    analysis = json.loads(analysis_path.read_text())
    if not (analysis.get("verdict") or "").strip():
        print("warning: verdict is blank")

    (d / "pipeline.json").write_text(json.dumps({"explain": args.explain, "links_ok": links_ok},
                                                 indent=2))
    return 0


def cmd_render(args):
    d = Path(args.dir)
    pipeline_path = d / "pipeline.json"
    if not pipeline_path.exists():
        print(f"{pipeline_path}: missing, run prepare first", file=sys.stderr)
        return 1
    pipeline = json.loads(pipeline_path.read_text())

    analysis_path = d / "analysis.json"
    raw_diff = d / "raw.diff"

    render_cmd = ["--analysis", str(analysis_path), "--diff", str(raw_diff), "--format", "html",
                  "--template", str(SKILL_DIR / "assets" / "diff-review-template.html"),
                  "--walkthrough", str(d / "section-walkthrough.html"),
                  "--state", str(d / "state.json")]
    if pipeline.get("explain"):
        render_cmd.append("--explain")
    if pipeline.get("links_ok"):
        render_cmd += ["--links", str(d / "links.json")]
    structure_html = d / "section-structure.html"
    if structure_html.exists():
        render_cmd += ["--structure", str(structure_html)]
    if args.title:
        render_cmd += ["--title", args.title]

    page = d / f"{args.slug}.html"
    if not _run_to_file("render.py", render_cmd, page):
        return 1

    section_files = sorted(str(p) for p in d.glob("section-*.html"))
    gate = run("validate_analysis.py", "--diff", str(raw_diff), "--analysis", str(analysis_path),
               "--rendered", str(page), "--sections", *section_files)
    if gate.returncode != 0:
        return 1

    splice = run("splice_assets.py", str(page), "--skill", str(SKILL_DIR), capture=True)
    if splice.returncode != 0:
        sys.stderr.write(splice.stderr or splice.stdout)
        return 1

    print(str(page))
    return 0


def cmd_all(args):
    rc = cmd_prepare(args)
    if rc != 0:
        return rc
    return cmd_render(args)


def build_parser():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    early = sub.add_parser("early")
    early.add_argument("--dir", required=True)
    early.add_argument("--repo", required=True)
    early.add_argument("--base", required=True)
    early.add_argument("--head", required=True)
    early.set_defaults(func=cmd_early)

    def add_prepare_args(sp):
        sp.add_argument("--dir", required=True)
        sp.add_argument("--repo", required=True)
        sp.add_argument("--base", required=True)
        sp.add_argument("--head", required=True)
        sp.add_argument("--pr")
        sp.add_argument("--paths", nargs="*", default=[])
        sp.add_argument("--explain", action="store_true")
        sp.add_argument("--prior", action="store_true")
        sp.add_argument("--links-cached", action="store_true")

    def add_render_args(sp):
        sp.add_argument("--slug", required=True)
        sp.add_argument("--title")

    prepare = sub.add_parser("prepare")
    add_prepare_args(prepare)
    prepare.set_defaults(func=cmd_prepare)

    render = sub.add_parser("render")
    render.add_argument("--dir", required=True)
    add_render_args(render)
    render.set_defaults(func=cmd_render)

    all_ = sub.add_parser("all")
    add_prepare_args(all_)
    add_render_args(all_)
    all_.set_defaults(func=cmd_all)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
