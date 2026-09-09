package extract

import (
	"archive/tar"
	"bytes"
	"os"
	"path/filepath"
	"testing"

	"github.com/klauspost/compress/zstd"
)

// fakeRuntime builds a small tar.zst archive shaped like the real one
// (python/bin/python3 plus a lib file) so tests exercise the real
// untar/extract logic without needing the actual ~90 MB runtime.
type tarEntry struct {
	name    string
	content string
	mode    int64
	symlink string // if set, a symlink to this target instead of a regular file
}

func fakeRuntime(t *testing.T, entries []tarEntry) []byte {
	t.Helper()
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	for _, e := range entries {
		if e.symlink != "" {
			if err := tw.WriteHeader(&tar.Header{
				Name:     e.name,
				Typeflag: tar.TypeSymlink,
				Linkname: e.symlink,
				Mode:     0o777,
			}); err != nil {
				t.Fatalf("tar header: %v", err)
			}
			continue
		}
		mode := e.mode
		if mode == 0 {
			mode = 0o644
		}
		if err := tw.WriteHeader(&tar.Header{
			Name: e.name, Size: int64(len(e.content)), Mode: mode,
		}); err != nil {
			t.Fatalf("tar header: %v", err)
		}
		if _, err := tw.Write([]byte(e.content)); err != nil {
			t.Fatalf("tar write: %v", err)
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatalf("tar close: %v", err)
	}

	var zbuf bytes.Buffer
	zw, err := zstd.NewWriter(&zbuf)
	if err != nil {
		t.Fatalf("zstd writer: %v", err)
	}
	if _, err := zw.Write(buf.Bytes()); err != nil {
		t.Fatalf("zstd write: %v", err)
	}
	if err := zw.Close(); err != nil {
		t.Fatalf("zstd close: %v", err)
	}
	return zbuf.Bytes()
}

func aRuntime(t *testing.T) []byte {
	t.Helper()
	return fakeRuntime(t, []tarEntry{
		{name: "python/bin/python3", content: "#!/bin/sh\necho fake python\n", mode: 0o755},
		{name: "python/lib/marker.txt", content: "hello"},
	})
}

func TestExtractReturnsTheExtractionDirectory(t *testing.T) {
	baseDir := t.TempDir()
	tarZst := aRuntime(t)

	dir, err := Extract(tarZst, baseDir)
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}

	if filepath.Dir(dir) != baseDir {
		t.Fatalf("dir = %s, want it directly under baseDir %s", dir, baseDir)
	}
	data, err := os.ReadFile(filepath.Join(dir, "python", "bin", "python3"))
	if err != nil {
		t.Fatalf("reading extracted file: %v", err)
	}
	if string(data) != "#!/bin/sh\necho fake python\n" {
		t.Fatalf("extracted content = %q, wrong", data)
	}
}

func TestExtractPreservesExecutableBit(t *testing.T) {
	baseDir := t.TempDir()
	dir, err := Extract(aRuntime(t), baseDir)
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}

	info, err := os.Stat(filepath.Join(dir, "python", "bin", "python3"))
	if err != nil {
		t.Fatalf("Stat: %v", err)
	}
	if info.Mode()&0o111 == 0 {
		t.Fatalf("mode = %v, want the executable bit set", info.Mode())
	}
}

func TestExtractIsIdempotentAndSkipsReextraction(t *testing.T) {
	baseDir := t.TempDir()
	tarZst := aRuntime(t)

	path1, err := Extract(tarZst, baseDir)
	if err != nil {
		t.Fatalf("first Extract: %v", err)
	}
	entriesAfterFirst, _ := os.ReadDir(baseDir)

	path2, err := Extract(tarZst, baseDir)
	if err != nil {
		t.Fatalf("second Extract: %v", err)
	}
	entriesAfterSecond, _ := os.ReadDir(baseDir)

	if path1 != path2 {
		t.Fatalf("path1=%s path2=%s, want the same path", path1, path2)
	}
	// The fast path must not even attempt a new extraction: no extra
	// (or leftover .extract-*) entries should appear under baseDir.
	if len(entriesAfterSecond) != len(entriesAfterFirst) {
		t.Fatalf("baseDir entries grew from %d to %d on a cached hit",
			len(entriesAfterFirst), len(entriesAfterSecond))
	}
}

func TestExtractDifferentContentUsesDifferentDirectories(t *testing.T) {
	baseDir := t.TempDir()

	path1, err := Extract(aRuntime(t), baseDir)
	if err != nil {
		t.Fatalf("Extract 1: %v", err)
	}
	differentRuntime := fakeRuntime(t, []tarEntry{
		{name: "python/bin/python3", content: "different content entirely", mode: 0o755},
	})
	path2, err := Extract(differentRuntime, baseDir)
	if err != nil {
		t.Fatalf("Extract 2: %v", err)
	}

	if path1 == path2 {
		t.Fatalf("different content produced the same path %s", path1)
	}
}

func TestExtractHandlesSymlinks(t *testing.T) {
	baseDir := t.TempDir()
	tarZst := fakeRuntime(t, []tarEntry{
		{name: "python/bin/python3.13", content: "the real binary", mode: 0o755},
		{name: "python/bin/python3", symlink: "python3.13"},
	})

	dir, err := Extract(tarZst, baseDir)
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}

	data, err := os.ReadFile(filepath.Join(dir, "python", "bin", "python3")) // follows the symlink
	if err != nil {
		t.Fatalf("reading through symlink: %v", err)
	}
	if string(data) != "the real binary" {
		t.Fatalf("content = %q, want the target's content", data)
	}
}

func TestExtractRejectsPathTraversal(t *testing.T) {
	baseDir := t.TempDir()
	tarZst := fakeRuntime(t, []tarEntry{
		{name: "../../etc/passwd", content: "malicious"},
	})

	if _, err := Extract(tarZst, baseDir); err == nil {
		t.Fatal("expected an error for a path-traversal tar entry")
	}
}

func TestExtractRejectsGarbageInput(t *testing.T) {
	baseDir := t.TempDir()
	if _, err := Extract([]byte("not a zstd stream"), baseDir); err == nil {
		t.Fatal("expected an error for non-zstd input")
	}
}
