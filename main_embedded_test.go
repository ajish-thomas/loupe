//go:build loupe_embed

package main

import (
	"os"
	"os/exec"
	"strings"
	"testing"
)

func TestResolvePythonRuntimeUsesTheRealEmbeddedRuntime(t *testing.T) {
	// Built with -tags loupe_embed against a real
	// internal/embedded/runtime.tar.zst (run scripts/generate-runtime.sh
	// first): resolvePythonRuntime must extract and use it, complete with
	// its bundled loader, not fall back to dev mode. See
	// main_devmode_test.go for the complementary case.
	got, err := resolvePythonRuntime()
	if err != nil {
		t.Fatalf("resolvePythonRuntime: %v", err)
	}
	if got.Python == defaultPythonExe() {
		t.Fatalf("got the dev-mode path %q, want the extracted embedded runtime", got.Python)
	}
	if got.Loader == "" || got.LibDir == "" {
		t.Fatalf("got %+v, want a bundled Loader/LibDir set", got)
	}

	cacheDir, err := os.UserCacheDir()
	if err != nil {
		t.Fatalf("UserCacheDir: %v", err)
	}
	if !strings.HasPrefix(got.Python, cacheDir) {
		t.Fatalf("got %q, want a path under the user cache dir %q", got.Python, cacheDir)
	}

	// Invoked the same way internal/kernel.Manager.command does.
	out, err := exec.Command(
		got.Loader, "--library-path", got.LibDir,
		got.Python, "-c", "import numpy, polars, matplotlib, fastexcel, loupe_kernel; print('ok')",
	).CombinedOutput()
	if err != nil {
		t.Fatalf("the extracted python (via bundled loader) failed to import the runtime deps: %v\n%s", err, out)
	}
	if strings.TrimSpace(string(out)) != "ok" {
		t.Fatalf("unexpected output: %s", out)
	}
}
