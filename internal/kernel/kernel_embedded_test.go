//go:build loupe_embed

package kernel

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"testing"

	"github.com/ajish-thomas/loupe/internal/extract"
)

// TestManagerUsesTheBundledLoaderForARealEmbeddedRuntime is the one test
// that actually exercises Manager.command's Loader branch: every other
// kernel_test.go test uses testPython(t) (the dev venv, Loader == ""),
// which passes under -tags loupe_embed too but never touches this
// path. internal/extract's own embedded tests verify extraction and the
// bundled loader in isolation; this verifies kernel.Start actually wires
// a real extract.Runtime through Manager.command correctly end to end.
func TestManagerUsesTheBundledLoaderForARealEmbeddedRuntime(t *testing.T) {
	rt, err := extract.PythonRuntime()
	if err != nil {
		t.Fatalf("extract.PythonRuntime: %v", err)
	}
	if rt.Loader == "" {
		t.Fatal("extract.PythonRuntime returned no Loader -- test setup is wrong")
	}
	// Building Go alone does not refresh the Python archive. Fail release tests
	// if the embedded sources differ from the code exercised by dev tests.
	packages, err := filepath.Glob(filepath.Join(filepath.Dir(filepath.Dir(rt.Python)), "lib", "python*", "site-packages", "loupe_kernel"))
	if err != nil || len(packages) != 1 {
		t.Fatalf("locating embedded package: %v, %v", packages, err)
	}
	sources, err := filepath.Glob("../../python/src/loupe_kernel/*.py")
	if err != nil || len(sources) == 0 {
		t.Fatalf("locating source package: %v", err)
	}
	for _, source := range sources {
		want, err := os.ReadFile(source)
		if err != nil {
			t.Fatal(err)
		}
		got, err := os.ReadFile(filepath.Join(packages[0], filepath.Base(source)))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(got, want) {
			t.Fatalf("stale embedded %s: regenerate the runtime with make release", filepath.Base(source))
		}
	}

	m, err := Start(Runtime{Python: rt.Python, Loader: rt.Loader, LibDir: rt.LibDir})
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	t.Cleanup(func() { m.Close() })

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	msgs := drain(ctx, m.Execute(ctx, "1", "plot([1.0, 2.0, 3.0], [4.0, 5.0, 6.0])"))

	var fig *Message
	for i := range msgs {
		if msgs[i].Type == "figure" {
			fig = &msgs[i]
		}
	}
	if fig == nil {
		t.Fatalf("no figure message in %+v", msgs)
	}
	if fig.Kind != "line" || len(fig.Payload) != 24 {
		t.Fatalf("fig = %+v, want a line figure with 24 payload bytes", fig)
	}
	path := filepath.Join(t.TempDir(), "data.csv")
	if err := os.WriteFile(path, []byte("x,y\n1,2\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	msgs = drain(ctx, m.Execute(ctx, "ingest", fmt.Sprintf("df = scan(%q, cache_threshold=0)\ndf", path)))
	if len(msgs) != 3 || msgs[0].Type != "data_schema" || msgs[1].Type != "data_preview" {
		t.Fatalf("embedded ingest failed (regenerate runtime if stale): %+v", msgs)
	}
	loop := m.Execute(ctx, "loop", "print('ready', flush=True)\nwhile True: pass")
	<-loop
	m.Interrupt()
	msgs = drain(ctx, loop)
	if len(msgs) == 0 || msgs[len(msgs)-1].Type != "kernel_restarted" {
		t.Fatalf("embedded interrupt failed: %+v", msgs)
	}
}
