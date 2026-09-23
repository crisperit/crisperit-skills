package main

import (
	"os"
	"path/filepath"
	"slices"
	"testing"
)

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestModulePathsSingleModuleWithNoGoWork(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "go.mod"), "module example.com/single\n\ngo 1.21\n")

	mods, patterns, err := modulePaths(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(mods) != 1 || mods[0] != "example.com/single" {
		t.Fatalf("got %v, want [example.com/single]", mods)
	}
	if len(patterns) != 1 || patterns[0] != "./..." {
		t.Fatalf("got %v, want [./...]", patterns)
	}
}

func TestModulePathsWorkspaceBlockForm(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "go.work"), "go 1.21\n\nuse (\n\t./a\n\t./b\n)\n")
	writeFile(t, filepath.Join(root, "a", "go.mod"), "module example.com/a\n\ngo 1.21\n")
	writeFile(t, filepath.Join(root, "b", "go.mod"), "module example.com/b\n\ngo 1.21\n")

	mods, patterns, err := modulePaths(root)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(mods, "example.com/a") || !slices.Contains(mods, "example.com/b") || len(mods) != 2 {
		t.Fatalf("got %v, want [example.com/a example.com/b]", mods)
	}
	if !slices.Contains(patterns, "./a/...") || !slices.Contains(patterns, "./b/...") || len(patterns) != 2 {
		t.Fatalf("got %v, want [./a/... ./b/...]", patterns)
	}
}

func TestModulePathsWorkspaceSingleLineForm(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "go.work"), "go 1.21\n\nuse ./a\nuse ./b\n")
	writeFile(t, filepath.Join(root, "a", "go.mod"), "module example.com/a\n\ngo 1.21\n")
	writeFile(t, filepath.Join(root, "b", "go.mod"), "module example.com/b\n\ngo 1.21\n")

	mods, patterns, err := modulePaths(root)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(mods, "example.com/a") || !slices.Contains(mods, "example.com/b") || len(mods) != 2 {
		t.Fatalf("got %v, want [example.com/a example.com/b]", mods)
	}
	if !slices.Contains(patterns, "./a/...") || !slices.Contains(patterns, "./b/...") || len(patterns) != 2 {
		t.Fatalf("got %v, want [./a/... ./b/...]", patterns)
	}
}

func TestModulePathsSkipsAStaleUseEntryWithNoGoMod(t *testing.T) {
	root := t.TempDir()
	// "./gone" is a use directive with no go.mod behind it -- a stale workspace entry must not
	// fail the whole run.
	writeFile(t, filepath.Join(root, "go.work"), "go 1.21\n\nuse (\n\t./a\n\t./gone\n)\n")
	writeFile(t, filepath.Join(root, "a", "go.mod"), "module example.com/a\n\ngo 1.21\n")

	mods, patterns, err := modulePaths(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(mods) != 1 || mods[0] != "example.com/a" {
		t.Fatalf("got %v, want [example.com/a]", mods)
	}
	if len(patterns) != 1 || patterns[0] != "./a/..." {
		t.Fatalf("got %v, want [./a/...], the stale entry must not get a pattern either", patterns)
	}
}

func TestModulePathsWorkspaceRootNotItselfAUseEntry(t *testing.T) {
	// The root's own go.mod is deliberately not a `use` entry: a bare "./..." pattern from the
	// workspace root resolves nothing, since it only reaches directories listed in `use`.
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "go.mod"), "module example.com/root\n\ngo 1.21\n")
	writeFile(t, filepath.Join(root, "go.work"), "go 1.21\n\nuse (\n\t./a\n\t./b\n)\n")
	writeFile(t, filepath.Join(root, "a", "go.mod"), "module example.com/a\n\ngo 1.21\n")
	writeFile(t, filepath.Join(root, "b", "go.mod"), "module example.com/b\n\ngo 1.21\n")

	mods, patterns, err := modulePaths(root)
	if err != nil {
		t.Fatal(err)
	}
	if slices.Contains(mods, "example.com/root") {
		t.Fatalf("got %v, root module must not appear: it is not a use entry", mods)
	}
	if slices.Contains(patterns, "./...") {
		t.Fatalf("got %v, must not contain the bare ./... pattern in workspace mode", patterns)
	}
	want := []string{"./a/...", "./b/..."}
	slices.Sort(patterns)
	if !slices.Equal(patterns, want) {
		t.Fatalf("got %v, want %v", patterns, want)
	}
}
