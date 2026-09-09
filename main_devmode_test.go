//go:build !loupe_embed

package main

import (
	"testing"

	"github.com/ajish-thomas/loupe/internal/kernel"
)

func TestResolvePythonRuntimeFallsBackToDevModeWithoutAnEmbeddedRuntime(t *testing.T) {
	// This binary is built without -tags loupe_embed (how `go test`
	// runs throughout this repo by default), so extract.PythonRuntime()
	// always returns ErrNoEmbeddedRuntime and resolvePythonRuntime must
	// fall back to a plain interpreter (no bundled loader). See
	// main_embedded_test.go for the complementary case.
	got, err := resolvePythonRuntime()
	if err != nil {
		t.Fatalf("resolvePythonRuntime: %v", err)
	}
	want := kernel.Runtime{Python: defaultPythonExe()}
	if got != want {
		t.Fatalf("got %+v, want %+v", got, want)
	}
}
