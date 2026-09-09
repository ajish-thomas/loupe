package main

import (
	"bytes"
	"context"
	"io"
	"net"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"
)

// syncBuffer is a bytes.Buffer safe for one writer goroutine and one
// reader goroutine, needed because serveHTTP writes to w from the test's
// background goroutine while the test reads it.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

func TestRunRejectsUnknownFlags(t *testing.T) {
	var buf bytes.Buffer
	err := run(context.Background(), []string{"-not-a-real-flag"}, &buf)
	if err == nil {
		t.Fatal("expected an error for an unknown flag")
	}
}

func TestRunReportsKernelStartFailure(t *testing.T) {
	var buf bytes.Buffer
	err := run(context.Background(), []string{"-python", "/no/such/python"}, &buf)
	if err == nil {
		t.Fatal("expected an error for a nonexistent -python executable")
	}
	if !strings.Contains(err.Error(), "starting kernel") {
		t.Errorf("error = %q, want it to mention kernel startup", err)
	}
}

func TestServeHTTPServesAndShutsDownOnCancel(t *testing.T) {
	listener := newLocalListener(t)
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, "ok")
	})

	ctx, cancel := context.WithCancel(context.Background())
	var out syncBuffer
	done := make(chan error, 1)
	go func() { done <- serveHTTP(ctx, listener, handler, &out) }()

	resp, err := http.Get("http://" + listener.Addr().String())
	if err != nil {
		t.Fatalf("GET before cancel: %v", err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if string(body) != "ok" {
		t.Fatalf("body = %q, want %q", body, "ok")
	}
	if !strings.Contains(out.String(), "serving on http://") {
		t.Errorf("output = %q, want it to report the listening address", out.String())
	}

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("serveHTTP returned %v after cancel, want nil", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("serveHTTP did not return within 5s of cancel")
	}
}

func TestServeHTTPReturnsListenerErrors(t *testing.T) {
	listener := newLocalListener(t)
	listener.Close() // Serve on a closed listener must fail immediately

	var out syncBuffer
	err := serveHTTP(context.Background(), listener, http.NotFoundHandler(), &out)
	if err == nil {
		t.Fatal("expected an error from a closed listener")
	}
}

func newLocalListener(t *testing.T) *net.TCPListener {
	t.Helper()
	l, err := net.ListenTCP("tcp", &net.TCPAddr{IP: net.ParseIP("127.0.0.1")})
	if err != nil {
		t.Fatalf("ListenTCP: %v", err)
	}
	t.Cleanup(func() { l.Close() })
	return l
}

func TestDefaultPythonExeIsNeverEmpty(t *testing.T) {
	if defaultPythonExe() == "" {
		t.Fatal("defaultPythonExe() returned an empty string")
	}
}
