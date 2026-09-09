//go:build !loupe_embed

package extract

import "testing"

func TestPythonRuntimeReturnsErrNoEmbeddedRuntimeInDevBuild(t *testing.T) {
	// This binary is built without -tags loupe_embed (how `go test`
	// runs throughout this repo by default), so embedded.RuntimeTarZst is
	// nil. See extract_embedded_test.go for the complementary case.
	_, err := PythonRuntime()
	if err != ErrNoEmbeddedRuntime {
		t.Fatalf("err = %v, want ErrNoEmbeddedRuntime", err)
	}
}
