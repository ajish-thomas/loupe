// Command loupe is the single-binary entry point for the plotting
// workbench described in PLAN.md.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/ajish-thomas/loupe/internal/extract"
	"github.com/ajish-thomas/loupe/internal/kernel"
	"github.com/ajish-thomas/loupe/internal/server"
	"github.com/ajish-thomas/loupe/internal/version"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if err := run(ctx, os.Args[1:], os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

// run parses flags, starts the kernel and HTTP server, and blocks until
// ctx is canceled (Ctrl-C) or the server fails, shutting down cleanly.
//
// -python, when unset, resolves via resolvePythonRuntime: the embedded
// runtime if this binary was built with -tags loupe_embed (a real,
// air-gapped release build -- PLAN.md's milestones 1-2), else the
// project's dev venv or PATH. An explicit -python is used as a plain
// interpreter (no bundled loader), matching how it always behaved.
func run(ctx context.Context, args []string, w io.Writer) error {
	fs := flag.NewFlagSet("loupe", flag.ContinueOnError)
	pythonExe := fs.String("python", "", "python executable with the loupe_kernel package installed "+
		"(default: the embedded runtime if this binary has one, else python/.venv or PATH)")
	addr := fs.String("addr", "127.0.0.1:0", "address to serve on")
	webDir := fs.String("web", "", "override the embedded UI assets with a directory")
	if err := fs.Parse(args); err != nil {
		return err
	}

	fmt.Fprintf(w, "loupe %s\n", version.String())

	rt := kernel.Runtime{Python: *pythonExe}
	if rt.Python == "" {
		var err error
		rt, err = resolvePythonRuntime()
		if err != nil {
			return fmt.Errorf("resolving python executable (see -python): %w", err)
		}
	}

	km, err := kernel.Start(rt)
	if err != nil {
		return fmt.Errorf("starting kernel (see -python): %w", err)
	}
	defer km.Close()

	listener, err := net.Listen("tcp", *addr)
	if err != nil {
		return fmt.Errorf("listening on %s: %w", *addr, err)
	}

	return serveHTTP(ctx, listener, server.New(*webDir, km), w)
}

// serveHTTP runs handler on listener until ctx is canceled, then shuts
// down gracefully. Split out from run so the serve/shutdown lifecycle is
// testable without a real kernel subprocess.
func serveHTTP(ctx context.Context, listener net.Listener, handler http.Handler, w io.Writer) error {
	httpServer := &http.Server{Handler: handler}
	fmt.Fprintf(w, "serving on http://%s\n", listener.Addr())

	errCh := make(chan error, 1)
	go func() { errCh <- httpServer.Serve(listener) }()

	select {
	case err := <-errCh:
		return err
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return httpServer.Shutdown(shutdownCtx)
	}
}

// resolvePythonRuntime prefers the embedded runtime internal/extract can
// produce (a real -tags loupe_embed release build) and falls back to
// the dev venv or PATH otherwise -- see internal/embedded's doc comment
// for what selects between the two.
func resolvePythonRuntime() (kernel.Runtime, error) {
	rt, err := extract.PythonRuntime()
	if err == nil {
		return kernel.Runtime{Python: rt.Python, Loader: rt.Loader, LibDir: rt.LibDir}, nil
	}
	if !errors.Is(err, extract.ErrNoEmbeddedRuntime) {
		return kernel.Runtime{}, err
	}
	return kernel.Runtime{Python: defaultPythonExe()}, nil
}

// defaultPythonExe looks for the project's dev venv relative to the
// current working directory -- the natural `go run .` workflow from the
// repo root. Falls back to whatever "python3" resolves to on PATH.
func defaultPythonExe() string {
	if path, err := filepath.Abs("python/.venv/bin/python3"); err == nil {
		if _, err := os.Stat(path); err == nil {
			return path
		}
	}
	return "python3"
}
