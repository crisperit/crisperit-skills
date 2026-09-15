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

// modulePath reads the `module <path>` directive from root/go.mod. Packages under this prefix
// belong to the module being analysed; everything else is a dependency.
func modulePath(root string) (string, error) {
	f, err := os.Open(filepath.Join(root, "go.mod"))
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
	return "", fmt.Errorf("no module directive found in %s", filepath.Join(root, "go.mod"))
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
	mod, err := modulePath(root)
	if err != nil {
		fmt.Fprintln(os.Stderr, "extractor:", err)
		os.Exit(1)
	}
	cfg := &packages.Config{
		Mode: packages.NeedName | packages.NeedFiles | packages.NeedSyntax |
			packages.NeedTypes | packages.NeedTypesInfo | packages.NeedDeps | packages.NeedImports,
		Dir:   root,
		Tests: true,
		Env:   append(os.Environ(), "GOPROXY=off", "GOFLAGS=-mod=mod"),
	}
	pkgs, err := packages.Load(cfg, "./...")
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
	packages.Visit(pkgs, nil, func(p *packages.Package) {
		if p.TypesInfo == nil || (p.PkgPath != mod && !strings.HasPrefix(p.PkgPath, mod+"/")) {
			return
		}
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
}
