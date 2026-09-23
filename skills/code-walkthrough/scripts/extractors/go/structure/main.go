// Command structure_bin prints the declared classes/interfaces/functions of one Go source
// file (read from stdin) as JSON: {name: {kind, hash, member_hashes, extends, implements}}.
//
// Deliberately go/parser, not go/packages: structure.py calls this once per file per git ref
// on file *content*, not a real checkout, so there is no module to load and no need for the
// type information a whole-package load would cost. extends/implements stay empty here --
// Go has neither keyword, so structure.py's heritage list is TypeScript-only.
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"io"
	"os"
	"regexp"
	"strings"
)

type component struct {
	Kind         string            `json:"kind"`
	Hash         string            `json:"hash"`
	MemberHashes map[string]string `json:"member_hashes"`
	Extends      []string          `json:"extends"`
	Implements   []string          `json:"implements"`
}

var wsRe = regexp.MustCompile(`\s+`)

func bodyHash(fset *token.FileSet, start, end token.Pos, src []byte) string {
	text := string(src[fset.Position(start).Offset:fset.Position(end).Offset])
	normalized := strings.TrimSpace(wsRe.ReplaceAllString(text, " "))
	sum := sha256.Sum256([]byte(normalized))
	return hex.EncodeToString(sum[:])
}

// recvTypeName is the receiver's bare type name (`*Foo`, `Foo`, `*Box[T]` and `Box[K, V]` all
// give "Foo"/"Box"), or "" for a receiver this still can't name.
func recvTypeName(fl *ast.FieldList) string {
	if fl == nil || len(fl.List) == 0 {
		return ""
	}
	expr := fl.List[0].Type
	if star, ok := expr.(*ast.StarExpr); ok {
		expr = star.X
	}
	switch t := expr.(type) {
	case *ast.IndexExpr: // one type param: Box[T]
		expr = t.X
	case *ast.IndexListExpr: // two or more type params: Box[K, V]
		expr = t.X
	}
	if id, ok := expr.(*ast.Ident); ok {
		return id.Name
	}
	return ""
}

func main() {
	src, err := io.ReadAll(os.Stdin)
	if err != nil {
		os.Exit(1)
	}
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, "input.go", src, 0)
	if err != nil {
		os.Exit(1)
	}

	components := map[string]*component{}

	for _, decl := range file.Decls {
		gd, ok := decl.(*ast.GenDecl)
		if !ok || gd.Tok != token.TYPE {
			continue
		}
		for _, spec := range gd.Specs {
			ts, ok := spec.(*ast.TypeSpec)
			if !ok {
				continue
			}
			switch t := ts.Type.(type) {
			case *ast.StructType:
				components[ts.Name.Name] = &component{
					Kind: "struct", MemberHashes: map[string]string{},
					Extends: []string{}, Implements: []string{},
					Hash: bodyHash(fset, ts.Pos(), ts.End(), src),
				}
			case *ast.InterfaceType:
				members := map[string]string{}
				for _, m := range t.Methods.List {
					for _, n := range m.Names {
						members[n.Name] = bodyHash(fset, m.Pos(), m.End(), src)
					}
				}
				components[ts.Name.Name] = &component{
					Kind: "interface", MemberHashes: members,
					Extends: []string{}, Implements: []string{},
					Hash: bodyHash(fset, ts.Pos(), ts.End(), src),
				}
			}
		}
	}

	for _, decl := range file.Decls {
		fd, ok := decl.(*ast.FuncDecl)
		if !ok || fd.Body == nil {
			continue
		}
		hash := bodyHash(fset, fd.Pos(), fd.End(), src)
		if fd.Recv == nil {
			components[fd.Name.Name] = &component{
				Kind: "function", Hash: hash, MemberHashes: map[string]string{},
				Extends: []string{}, Implements: []string{},
			}
			continue
		}
		recv := recvTypeName(fd.Recv)
		if recv == "" {
			continue
		}
		owner, ok := components[recv]
		if !ok {
			// A method on a receiver type this file never declares as a struct/interface --
			// embedded elsewhere, or (`type Status int`) a named type over a non-struct base.
			// Synthesize a placeholder so the method itself isn't dropped; "type" rather than
			// "struct" since this file never actually said it's a struct.
			owner = &component{
				Kind: "type", MemberHashes: map[string]string{},
				Extends: []string{}, Implements: []string{},
			}
			components[recv] = owner
		}
		owner.MemberHashes[fd.Name.Name] = hash
	}

	json.NewEncoder(os.Stdout).Encode(components)
}
