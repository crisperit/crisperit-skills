#!/usr/bin/env python3
"""Module and layer map: the whole repo's shape, with this change's modules marked.

Usage:
  python3 layers.py --repo <path> --head <ref> [--coupling coupling.json]

coupling.py and structure.py both answer "what did this diff move", which cannot show whether
a change respects the architecture, because the architecture is mostly the files the diff did
not touch. This walks every code file at one ref, folds files into modules, folds modules into
layers, and marks the modules the diff touched.

A layer is guessed from path segments against the usual hexagonal and DDD names. Dependencies
are expected to point inward, from ui and adapters and infrastructure, through application, to
domain. An edge pointing the other way is reported as an inversion: a fact about direction,
for a human to judge.

Reads files from the working tree when they are present and the ref is HEAD, since a subprocess
per file is slow on a large repo. Falls back to `git show` otherwise, so an older ref or a file
missing from disk still resolves.

Stdlib only, no network.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from coupling import (  # noqa: E402  one owner for import parsing and module resolution
    SKIP_PREFIX_SEGMENTS,
    detect_lang,
    git_show,
    go_module_prefix_at,
    list_repo_files,
    module_of,
    module_of_node,
    parse_edges,
    run_git,
)

# Outer to inner. A dependency is expected to point from a lower rank to a higher one.
# infrastructure sits at the outside with the adapters: it is a detail the core does not know.
LAYER_RANK = {
    "entrypoint": 0,
    "ui": 0,
    "adapters": 1,
    "infrastructure": 1,
    "application": 2,
    "domain": 3,
}
LAYER_KEYWORDS = {
    "ui": ("ui", "web", "views", "components", "presentation", "frontend", "pages"),
    "adapters": (
        "adapters", "adapter", "api", "controllers", "handlers", "http", "rest", "graphql",
        "cli", "transport", "interfaces", "ports", "delivery",
    ),
    "infrastructure": (
        "infrastructure", "infra", "persistence", "repositories", "repository", "db",
        "database", "clients", "gateways", "external", "config", "bootstrap", "platform",
    ),
    "application": (
        "application", "app", "usecases", "use_cases", "services", "service", "commands",
        "queries", "handlers_app", "workflows", "orchestrator",
    ),
    "domain": ("domain", "core", "entities", "model", "models", "aggregates", "value_objects"),
    "shared": ("shared", "common", "utils", "util", "lib", "helpers", "types", "vendor"),
    "test": ("test", "tests", "spec", "specs", "e2e", "__tests__", "fixtures"),
}


def layer_of(path):
    segments = path.split("/")[:-1]
    # A module that is just the source root holds the composition root, main.ts and its
    # wiring. That is the outermost thing in the build, not an unknown.
    if len(segments) == 1 and segments[0].lower() in SKIP_PREFIX_SEGMENTS:
        return "entrypoint"
    for segment in segments:
        for layer, keywords in LAYER_KEYWORDS.items():
            if segment.lower() in keywords:
                return layer
    basename = path.rsplit("/", 1)[-1].lower()
    if any(marker in basename for marker in (".test.", ".spec.", "_test.", "test_")):
        return "test"
    return "unknown"


def read_file(repo, ref, path, from_disk):
    if from_disk:
        candidate = Path(repo) / path
        if candidate.is_file():
            try:
                return candidate.read_text(errors="replace")
            except OSError:
                pass
    return git_show(repo, ref, path)


def file_edges_at(repo, ref):
    """Every in-repo import edge at one ref, as (importer node, imported node); a node is a
    file path for most languages but a package directory for Go (see coupling.graph_node)."""
    files_set = list_repo_files(repo, ref)
    go_prefix = go_module_prefix_at(repo, ref)
    head_sha = run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()
    ref_sha = run_git(repo, ["rev-parse", ref]).stdout.strip()
    dirty = bool(run_git(repo, ["status", "--porcelain"]).stdout.strip())
    from_disk = ref_sha == head_sha and not dirty

    edges = set()
    parsed = 0
    for path in sorted(files_set):
        if detect_lang(path) is None:
            continue
        content = read_file(repo, ref, path, from_disk)
        if content is None:
            continue
        result = parse_edges(detect_lang(path), path, content, files_set, go_prefix)
        if result is None:
            continue
        parsed += 1
        edges |= result
    return edges, parsed, from_disk


def aggregate(edges):
    """Edge ends are graph nodes, not always file paths: a Go edge's ends are package
    directories (coupling.graph_node), so they must go through module_of_node, not module_of,
    or a directory's own last segment gets read as a filename and stripped, folding a real
    package into its parent."""
    counts = {}
    for importer, imported in edges:
        src, dst = module_of_node(importer), module_of_node(imported)
        if src == dst:
            continue  # inside one module is not module coupling
        counts[(src, dst)] = counts.get((src, dst), 0) + 1
    return counts


def inversions_in(counts):
    """Edges pointing outward, from a more inner layer to a more outer one."""
    found = []
    for (src, dst), count in sorted(counts.items()):
        src_rank = LAYER_RANK.get(layer_of(src + "/x"))
        dst_rank = LAYER_RANK.get(layer_of(dst + "/x"))
        if src_rank is None or dst_rank is None or src_rank <= dst_rank:
            continue
        found.append({"from": src, "to": dst, "count": count})
    return found


# The whole-repo map runs to 477 module edges on a large Go repo, past mermaid's own 500-edge
# ceiling and far past what anyone reads. Modules this change touched are kept first, so the
# map stays an orientation aid for *this* diff rather than a picture of the entire repo.
MAX_LAYER_EDGES = 60

# Suppress the layer grouping unless it says something real, judged only on the modules being
# drawn. Measured on a large Go repo: 2 of 10 sampled modules have a guessable layer at all,
# clearing MIN_DISTINCT_LAYERS but at a fifth of the sample, under MIN_KNOWN_LAYER_SHARE.
MIN_DISTINCT_LAYERS = 2
MIN_KNOWN_LAYER_SHARE = 0.5


def layering_is_real(modules):
    """Whether grouping `modules` by LAYER_KEYWORDS says anything real, judged only on the
    modules being drawn, not the whole repo."""
    layers = [layer_of(module + "/x") for module in modules]
    known = [layer for layer in layers if layer != "unknown"]
    if len(set(known)) < MIN_DISTINCT_LAYERS:
        return False
    return len(known) >= len(layers) * MIN_KNOWN_LAYER_SHARE


def cap_layer_edges(counts, touched, max_edges=MAX_LAYER_EDGES):
    """(counts, truncated) keeping the edges that touch a changed module first, then the
    heaviest, so the cap drops the parts of the repo this diff never went near."""
    if len(counts) <= max_edges:
        return counts, False
    ranked = sorted(
        counts.items(),
        key=lambda kv: (-((kv[0][0] in touched) + (kv[0][1] in touched)), -kv[1], kv[0]),
    )
    return dict(ranked[:max_edges]), True


def build_mermaid(counts, modules, touched, new_pairs, layered=True):
    if not counts:
        return "", {}

    node_id = {module: f"M{i}" for i, module in enumerate(sorted(modules))}

    def node_lines(names):
        rendered = []
        for module in names:
            mark = " *" if module in touched else ""
            count = modules[module]
            unit = "file" if count == 1 else "files"
            rendered.append(f'    {node_id[module]}["{module}{mark}<br/>{count} {unit}"]')
        return rendered

    lines = ["flowchart LR"]
    if layered:
        by_layer = {}
        for module in sorted(modules):
            by_layer.setdefault(layer_of(module + "/x"), []).append(module)
        order = sorted(by_layer, key=lambda name: (LAYER_RANK.get(name, 9), name))
        for i, layer in enumerate(order):
            lines.append(f'  subgraph L{i}["{layer}"]')
            lines.extend(node_lines(by_layer[layer]))
            lines.append("  end")
    else:
        lines.extend(node_lines(sorted(modules)))

    for (src, dst), count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        arrow = "==>" if (src, dst) in new_pairs else "-->"
        lines.append(f"  {node_id[src]} {arrow}|{count}| {node_id[dst]}")
    ids = {node_id[module]: module for module in modules}
    return "\n".join(lines), ids


def analyse(repo, head, coupling_path):
    if run_git(repo, ["rev-parse", "--verify", f"{head}^{{commit}}"]).returncode != 0:
        raise RuntimeError(f"bad ref: {head!r}")

    edges, parsed, from_disk = file_edges_at(repo, head)
    counts = aggregate(edges)

    modules = {}
    for path in sorted(list_repo_files(repo, head)):
        if detect_lang(path) is None:
            continue
        module = module_of(path)
        modules[module] = modules.get(module, 0) + 1

    touched, new_pairs = set(), set()
    if coupling_path:
        data = json.loads(Path(coupling_path).read_text())
        # A module is touched when the diff changed something in it. Reading both ends of every
        # coupling edge instead marks each untouched neighbour as touched too, which on a
        # large Go repo made 27 of 38 modules look changed and left the star meaning nothing.
        changed_nodes = data.get("changed_nodes")
        if changed_nodes is None:  # coupling.json from before that field existed
            changed_nodes = [
                end for state in ("added", "removed", "unchanged")
                for pair in data.get(state) or [] for end in pair
            ]
        touched.update(module_of_node(node) for node in changed_nodes)
        for pair in data.get("added") or []:
            new_pairs.add(
                (module_of_node(pair[0]), module_of_node(pair[1]))
            )

    # Only the modules this change touched, and only the dependencies between them. The map is
    # built from the whole repo so the module and file counts are real, but drawing all of it
    # meant 86 boxes and 477 arrows on a large Go repo, a picture of the repo rather than of the
    # change. Falls back to the full map when the diff touched nothing recognisable, so the
    # level still shows something rather than going blank.
    scoped = {pair: c for pair, c in counts.items() if pair[0] in touched and pair[1] in touched}
    # Narrow to touched modules only when the full map is too dense to read, the same rule
    # coupling.touched_only() applies to the file graph. A repo whose whole map already fits is
    # more useful drawn whole.
    scoped_to_touched = bool(scoped) and len(counts) > MAX_LAYER_EDGES
    drawn_counts, truncated = cap_layer_edges(scoped if scoped_to_touched else counts, touched)

    # Only modules that carry an edge go in the diagram; a module nothing imports and that
    # imports nothing adds a box and no information.
    connected = {m for pair in drawn_counts for m in pair}
    drawn = {m: modules.get(m, 0) for m in connected}

    layered = layering_is_real(drawn)

    note = (
        f"Whole-repo map at {head}: {parsed} code files folded into {len(modules)} modules, "
        f"{len(drawn)} of them with dependencies to draw."
    )
    if not from_disk:
        note += " Read from git rather than the working tree."
    if scoped_to_touched:
        note += (
            f" Drawing the {len(drawn_counts)} dependencies between the {len(drawn)} modules "
            f"this change touched, of {len(counts)} in the repo. Click a module for its "
            "untouched neighbours."
        )
    if truncated:
        note += f" Capped at {MAX_LAYER_EDGES} dependencies."

    mermaid, ids = build_mermaid(drawn_counts, drawn, touched, new_pairs, layered)

    return {
        "modules": modules,
        "touched": sorted(touched & connected),
        "edges": [{"from": s, "to": d, "count": c} for (s, d), c in sorted(counts.items())],
        # inversions_in() ranks by the same guessed layer; a guess too thin to draw is too thin
        # to accuse a diff of pointing the wrong way.
        "inversions": inversions_in(counts) if layered else [],
        "unknown_layer": sorted(m for m in drawn if layer_of(m + "/x") == "unknown"),
        "mermaid": mermaid,
        "ids": ids,
        "note": note,
        "layered": layered,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--coupling")
    args = parser.parse_args()

    try:
        print(json.dumps(analyse(args.repo, args.head, args.coupling)))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
