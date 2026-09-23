package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"go/ast"
	"go/token"
	"go/types"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/tools/go/packages"
)

// readModule reads the `module <path>` directive from dir/go.mod.
func readModule(dir string) (string, error) {
	f, err := os.Open(filepath.Join(dir, "go.mod"))
	if err != nil {
		return "", fmt.Errorf("reading go.mod: %w", err)
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) >= 2 && fields[0] == "module" {
			return strings.Trim(fields[1], "\"`"), nil
		}
	}
	if err := scanner.Err(); err != nil {
		return "", fmt.Errorf("reading go.mod: %w", err)
	}
	return "", fmt.Errorf("no module directive found in %s", filepath.Join(dir, "go.mod"))
}

// modulePaths returns the module path of every module rooted in this repo, paired with the
// packages.Load pattern needed to reach it. When root/go.work exists, that is one
// "./<dir>/..." pattern per directory named by a `use` directive (block or single-line form)
// that has a readable go.mod; a stale entry is skipped from both lists rather than failing
// the run. A single "./..." from the workspace root itself is not enough: it only resolves
// modules that are themselves `use` entries, which the root usually is not. Outside
// workspace mode there is just root's own go.mod and the single "./..." pattern. Packages
// under any of the returned module prefixes belong to the repo being analysed; everything
// else is a dependency.
func modulePaths(root string) (mods, patterns []string, err error) {
	workPath := filepath.Join(root, "go.work")
	if _, err := os.Stat(workPath); err != nil {
		mod, err := readModule(root)
		if err != nil {
			return nil, nil, err
		}
		return []string{mod}, []string{"./..."}, nil
	}
	dirs, err := workUseDirs(workPath)
	if err != nil {
		return nil, nil, err
	}
	for _, dir := range dirs {
		mod, err := readModule(filepath.Join(root, dir))
		if err != nil {
			continue
		}
		mods = append(mods, mod)
		patterns = append(patterns, "./"+filepath.ToSlash(filepath.Clean(dir))+"/...")
	}
	return mods, patterns, nil
}

// workUseDirs parses go.work's `use` directives (block or single-line form) and returns the
// listed directories verbatim; it does not check whether each has a go.mod.
func workUseDirs(workPath string) ([]string, error) {
	f, err := os.Open(workPath)
	if err != nil {
		return nil, fmt.Errorf("reading go.work: %w", err)
	}
	defer f.Close()
	var dirs []string
	inBlock := false
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if i := strings.Index(line, "//"); i >= 0 {
			line = strings.TrimSpace(line[:i])
		}
		switch {
		case line == "":
			continue
		case line == "use (":
			inBlock = true
		case inBlock && line == ")":
			inBlock = false
		case inBlock:
			dirs = append(dirs, line)
		case strings.HasPrefix(line, "use "):
			dirs = append(dirs, strings.TrimSpace(strings.TrimPrefix(line, "use ")))
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("reading go.work: %w", err)
	}
	return dirs, nil
}

type Edge struct {
	FromFile, FromSym, ToFile, ToSym string
}

func symName(fn *types.Func) string {
	sig, _ := fn.Type().(*types.Signature)
	if sig != nil && sig.Recv() != nil {
		t := sig.Recv().Type()
		if p, ok := t.(*types.Pointer); ok {
			t = p.Elem()
		}
		if n, ok := t.(*types.Named); ok {
			return n.Obj().Name() + "." + fn.Name()
		}
	}
	return fn.Name()
}

func main() {
	root := os.Args[1]
	mods, patterns, err := modulePaths(root)
	if err != nil {
		fmt.Fprintln(os.Stderr, "extractor:", err)
		os.Exit(1)
	}
	inRepo := func(pkgPath string) bool {
		for _, m := range mods {
			if pkgPath == m || strings.HasPrefix(pkgPath, m+"/") {
				return true
			}
		}
		return false
	}
	env := append(os.Environ(), "GOPROXY=off")
	// -mod may only be readonly or vendor in workspace mode: "go: -mod may only be set to
	// readonly or vendor when in workspace mode".
	if _, err := os.Stat(filepath.Join(root, "go.work")); err != nil {
		env = append(env, "GOFLAGS=-mod=mod")
	}
	cfg := &packages.Config{
		Mode: packages.NeedName | packages.NeedFiles | packages.NeedSyntax |
			packages.NeedTypes | packages.NeedTypesInfo | packages.NeedDeps | packages.NeedImports,
		Dir:   root,
		Tests: true,
		Env:   env,
	}
	pkgs, err := packages.Load(cfg, patterns...)
	if err != nil {
		panic(err)
	}
	rel := func(p string) string {
		if i := strings.Index(p, root); i == 0 {
			return strings.TrimPrefix(p[len(root):], "/")
		}
		return p
	}
	seen := map[Edge]bool{}
	out := json.NewEncoder(os.Stdout)
	matched := 0
	var loadErrs []packages.Error
	packages.Visit(pkgs, nil, func(p *packages.Package) {
		loadErrs = append(loadErrs, p.Errors...)
		if p.TypesInfo == nil || !inRepo(p.PkgPath) {
			return
		}
		matched++
		for _, f := range p.Syntax {
			fpath := rel(p.Fset.Position(f.Pos()).Filename)
			if fpath == "" || strings.HasPrefix(fpath, "/") {
				continue
			}
			ast.Inspect(f, func(n ast.Node) bool {
				fd, ok := n.(*ast.FuncDecl)
				if !ok {
					return true
				}
				def, _ := p.TypesInfo.Defs[fd.Name].(*types.Func)
				if def == nil {
					return true
				}
				if fd.Body == nil {
					// Assembly-backed or //go:linkname declarations have no Go body to inspect.
					return true
				}
				from := symName(def)
				ast.Inspect(fd.Body, func(m ast.Node) bool {
					ce, ok := m.(*ast.CallExpr)
					if !ok {
						return true
					}
					var id *ast.Ident
					switch fn := ce.Fun.(type) {
					case *ast.Ident:
						id = fn
					case *ast.SelectorExpr:
						id = fn.Sel
					default:
						return true
					}
					obj, _ := p.TypesInfo.Uses[id].(*types.Func)
					if obj == nil || obj.Pkg() == nil {
						return true
					}
					pos := p.Fset.Position(obj.Pos())
					tf := rel(pos.Filename)
					if tf == "" || strings.HasPrefix(tf, "/") || tf == "-" {
						return true
					}
					e := Edge{fpath, from, tf, symName(obj)}
					if !seen[e] {
						seen[e] = true
						out.Encode(e)
					}
					return true
				})
				return true
			})
			_ = token.NoPos
		}
	})
	if matched == 0 {
		fmt.Fprintf(os.Stderr, "extractor: loaded %d packages, 0 in-repo with type info\n", len(pkgs))
		for i, e := range loadErrs {
			if i >= 5 {
				break
			}
			fmt.Fprintln(os.Stderr, "extractor:", e)
		}
		os.Exit(1)
	}
}
