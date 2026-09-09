//go:build loupe_embed

package extract

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestPythonRuntimeExtractsAndRunsViaTheBundledLoader(t *testing.T) {
	// Built with -tags loupe_embed against a real
	// internal/embedded/runtime.tar.zst (run scripts/generate-runtime.sh
	// first). See extract_devmode_test.go for the complementary case.
	//
	// Invokes Python the same way internal/kernel.Manager.command does --
	// via the bundled loader, not a direct exec -- since that's the whole
	// point: the extracted CPython and its native extensions (Polars'
	// among them) must not depend on the host's glibc.
	rt, err := PythonRuntime()
	if err != nil {
		t.Fatalf("PythonRuntime: %v", err)
	}
	if _, err := os.Stat(rt.Python); err != nil {
		t.Fatalf("extracted python not found at %s: %v", rt.Python, err)
	}
	if _, err := os.Stat(rt.Loader); err != nil {
		t.Fatalf("bundled loader not found at %s: %v", rt.Loader, err)
	}

	out, err := exec.Command(
		rt.Loader, "--library-path", rt.LibDir,
		rt.Python, "-c", "import numpy, polars, matplotlib, fastexcel, loupe_kernel; print('ok')",
	).CombinedOutput()
	if err != nil {
		t.Fatalf("extracted python (via bundled loader) failed to import the runtime deps: %v\n%s", err, out)
	}
	if strings.TrimSpace(string(out)) != "ok" {
		t.Fatalf("unexpected output: %s", out)
	}
}

func TestPythonRuntimeNeverFallsBackToHostLibraries(t *testing.T) {
	// The point of bundling glibc: every "calling init" line under
	// LD_DEBUG=libs must resolve inside the extraction root (our bundled
	// glibc, or a package's own self-contained .libs directory), never to
	// a host system path.
	rt, err := PythonRuntime()
	if err != nil {
		t.Fatalf("PythonRuntime: %v", err)
	}
	extractionRoot := filepath.Dir(rt.LibDir) // rt.LibDir is <root>/glibc

	cmd := exec.Command(
		rt.Loader, "--library-path", rt.LibDir,
		rt.Python, "-c", "import numpy, polars, matplotlib, fastexcel",
	)
	cmd.Env = append(os.Environ(), "LD_DEBUG=libs")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("run failed: %v\n%s", err, out)
	}

	for _, line := range strings.Split(string(out), "\n") {
		if !strings.Contains(line, "calling init") {
			continue
		}
		if !strings.Contains(line, extractionRoot) {
			t.Fatalf("a library was initialized from outside %s (fell back to a host path): %s", extractionRoot, line)
		}
	}
}

func TestPythonRuntimeSecondCallIsFast(t *testing.T) {
	if _, err := PythonRuntime(); err != nil {
		t.Fatalf("first PythonRuntime: %v", err)
	}
	// The second call should hit the cached-extraction fast path
	// (PLAN.md milestone 2: "second run is <200ms") -- verified for the
	// whole binary in main_embedded_test.go; here just confirm repeated
	// calls agree on the same paths, proving no re-extraction occurred.
	rt1, err := PythonRuntime()
	if err != nil {
		t.Fatalf("second PythonRuntime: %v", err)
	}
	rt2, err := PythonRuntime()
	if err != nil {
		t.Fatalf("third PythonRuntime: %v", err)
	}
	if rt1 != rt2 {
		t.Fatalf("rt1=%+v rt2=%+v, want the same cached paths", rt1, rt2)
	}
}
