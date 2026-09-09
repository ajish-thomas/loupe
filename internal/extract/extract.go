// Package extract unpacks the embedded CPython runtime (internal/embedded)
// to a content-hashed cache directory on first run, and skips the work on
// later runs -- PLAN.md milestone 2: "extracts the tree ... second run is
// <200ms".
//
// PLAN.md originally described this as "verify a size manifest per file
// and skip rewriting". Naming the extraction directory after a hash of
// the whole embedded blob achieves the same goal more simply: any change
// to the embedded runtime gets a new directory automatically, so the
// "should I re-extract" check is a single os.Stat rather than an
// enumerate-and-compare over every file.
package extract

import (
	"archive/tar"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/klauspost/compress/zstd"

	"github.com/ajish-thomas/loupe/internal/embedded"
)

// ErrNoEmbeddedRuntime is returned by PythonRuntime when this binary was
// built without -tags loupe_embed (the default dev-mode build) -- see
// internal/embedded's doc comment.
var ErrNoEmbeddedRuntime = errors.New("extract: no embedded runtime in this build (missing -tags loupe_embed)")

// Runtime describes how to invoke the extracted Python interpreter: not
// by executing it directly, but via its bundled dynamic linker pointed
// at bundled glibc/libstdc++/libgcc_s/libz (scripts/generate-runtime.sh,
// "Bundled glibc"), so the interpreter and its native extensions --
// Polars' Rust runtime among them -- never depend on whatever glibc
// happens to be installed on the host. Verified end to end (LD_DEBUG
// showing zero fallback to host libraries, a full
// numpy/polars/matplotlib/fastexcel import, and a real loupe_kernel
// socket round trip) before this became how the embedded build invokes
// Python -- see internal/kernel.Start, which is the intended caller.
type Runtime struct {
	Python string // path to python3
	Loader string // path to the bundled ld-linux-x86-64.so.2
	LibDir string // --library-path argument for Loader
}

// PythonRuntime extracts the embedded runtime, if this binary was built
// with -tags loupe_embed, into a content-hashed directory under the
// user's cache directory, and returns how to invoke its Python.
func PythonRuntime() (Runtime, error) {
	if len(embedded.RuntimeTarZst) == 0 {
		return Runtime{}, ErrNoEmbeddedRuntime
	}
	base, err := os.UserCacheDir()
	if err != nil {
		return Runtime{}, fmt.Errorf("extract: finding cache dir: %w", err)
	}
	baseDir := filepath.Join(base, "loupe")
	if err := os.MkdirAll(baseDir, 0o755); err != nil {
		return Runtime{}, fmt.Errorf("extract: creating %s: %w", baseDir, err)
	}

	dir, err := Extract(embedded.RuntimeTarZst, baseDir)
	if err != nil {
		return Runtime{}, err
	}

	glibcDir := filepath.Join(dir, "glibc")
	return Runtime{
		Python: filepath.Join(dir, "python", "bin", "python3"),
		Loader: filepath.Join(glibcDir, "ld-linux-x86-64.so.2"),
		LibDir: glibcDir,
	}, nil
}

// Extract decompresses and untars a zstd-compressed tar (tarZst) into
// <baseDir>/<hash of tarZst>/, skipping the work if that directory
// already exists, and returns its path.
//
// Takes tarZst and baseDir as parameters, rather than reading
// internal/embedded directly, so tests can exercise it against a small
// synthetic archive instead of the real ~90 MB one.
func Extract(tarZst []byte, baseDir string) (string, error) {
	hash := sha256.Sum256(tarZst)
	dir := filepath.Join(baseDir, hex.EncodeToString(hash[:])[:16])

	if info, err := os.Stat(dir); err == nil && info.IsDir() {
		return dir, nil // already extracted
	}

	// Extract into a sibling temp directory first, then atomically rename
	// into place, so a process killed mid-extraction can never leave
	// behind a directory that looks complete (the Stat above) but isn't.
	tmpDir, err := os.MkdirTemp(baseDir, ".extract-*")
	if err != nil {
		return "", fmt.Errorf("extract: creating temp dir: %w", err)
	}
	defer os.RemoveAll(tmpDir) // no-op once the rename below succeeds

	if err := untar(tarZst, tmpDir); err != nil {
		return "", err
	}

	if err := os.Rename(tmpDir, dir); err != nil {
		// Another process may have already finished extracting the same
		// content and won the race; that's fine, use what's there.
		if info, statErr := os.Stat(dir); statErr == nil && info.IsDir() {
			return dir, nil
		}
		return "", fmt.Errorf("extract: finalizing %s: %w", dir, err)
	}

	return dir, nil
}

func untar(tarZst []byte, dest string) error {
	zr, err := zstd.NewReader(bytes.NewReader(tarZst))
	if err != nil {
		return fmt.Errorf("extract: opening zstd stream: %w", err)
	}
	defer zr.Close()

	cleanDest := filepath.Clean(dest)
	tr := tar.NewReader(zr)
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return fmt.Errorf("extract: reading tar: %w", err)
		}

		target := filepath.Join(dest, hdr.Name)
		if target != cleanDest && !strings.HasPrefix(target, cleanDest+string(os.PathSeparator)) {
			return fmt.Errorf("extract: tar entry %q escapes destination", hdr.Name)
		}

		switch hdr.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
		case tar.TypeReg:
			if err := writeFile(target, tr, os.FileMode(hdr.Mode)); err != nil {
				return err
			}
		case tar.TypeSymlink:
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			if err := os.Symlink(hdr.Linkname, target); err != nil {
				return err
			}
		case tar.TypeLink:
			linkTarget := filepath.Join(dest, hdr.Linkname)
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			if err := os.Link(linkTarget, target); err != nil {
				return err
			}
		}
	}
}

func writeFile(target string, r io.Reader, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return err
	}
	f, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode)
	if err != nil {
		return err
	}
	if _, err := io.Copy(f, r); err != nil {
		f.Close()
		return fmt.Errorf("extract: writing %s: %w", target, err)
	}
	return f.Close()
}
