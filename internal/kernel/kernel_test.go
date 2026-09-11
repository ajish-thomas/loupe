package kernel

import (
	"bufio"
	"context"
	"encoding/binary"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// testPython locates the project's dev venv interpreter (python/.venv),
// which has loupe_kernel installed. Tests that need a real kernel skip
// cleanly if it hasn't been set up (`cd python && uv sync`).
func testPython(t *testing.T) string {
	t.Helper()
	path, err := filepath.Abs("../../python/.venv/bin/python3")
	if err != nil {
		t.Fatalf("resolving venv path: %v", err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Skipf("dev venv not found at %s (run `cd python && uv sync`): %v", path, err)
	}
	return path
}

func startTestKernel(t *testing.T) *Manager {
	t.Helper()
	m, err := Start(Runtime{Python: testPython(t)})
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	t.Cleanup(func() { m.Close() })
	return m
}

func drain(ctx context.Context, ch <-chan Message) []Message {
	var msgs []Message
	for msg := range ch {
		msgs = append(msgs, msg)
	}
	return msgs
}

func TestStartRejectsUnknownPythonExecutable(t *testing.T) {
	_, err := Start(Runtime{Python: "/no/such/python/executable"})
	if err == nil {
		t.Fatal("expected an error for a nonexistent python executable")
	}
}

func TestExecuteResultForTrailingExpression(t *testing.T) {
	m := startTestKernel(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	msgs := drain(ctx, m.Execute(ctx, "1", "1 + 1"))

	if len(msgs) != 2 {
		t.Fatalf("got %d messages, want 2: %+v", len(msgs), msgs)
	}
	if msgs[0].Type != "execute_result" || msgs[0].Text != "2" {
		t.Errorf("msgs[0] = %+v, want execute_result with text 2", msgs[0])
	}
	if msgs[1].Type != "execute_reply" || msgs[1].Status != "ok" {
		t.Errorf("msgs[1] = %+v, want execute_reply with status ok", msgs[1])
	}
}

func TestExecuteStreamsPrintOutput(t *testing.T) {
	m := startTestKernel(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	msgs := drain(ctx, m.Execute(ctx, "1", "print('hi')"))

	var text string
	for _, msg := range msgs {
		if msg.Type == "stream" {
			if msg.Name != "stdout" {
				t.Errorf("stream name = %q, want stdout", msg.Name)
			}
			text += msg.Text
		}
	}
	if text != "hi\n" {
		t.Errorf("streamed text = %q, want %q", text, "hi\n")
	}
}

func TestExecuteReportsErrorsWithoutKillingTheKernel(t *testing.T) {
	m := startTestKernel(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	msgs := drain(ctx, m.Execute(ctx, "1", "1 / 0"))
	if msgs[0].Type != "error" || msgs[0].EName != "ZeroDivisionError" {
		t.Fatalf("msgs[0] = %+v, want a ZeroDivisionError", msgs[0])
	}

	// the kernel must still be usable afterwards
	msgs2 := drain(ctx, m.Execute(ctx, "2", "1 + 1"))
	if msgs2[0].Type != "execute_result" || msgs2[0].Text != "2" {
		t.Fatalf("msgs2[0] = %+v, want execute_result 2", msgs2[0])
	}
}

func TestExecuteNamespacePersistsAcrossCalls(t *testing.T) {
	m := startTestKernel(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	drain(ctx, m.Execute(ctx, "1", "a = 40"))
	msgs := drain(ctx, m.Execute(ctx, "2", "a + 2"))

	if msgs[0].Text != "42" {
		t.Fatalf("msgs[0].Text = %q, want 42", msgs[0].Text)
	}
}

func TestExecuteFigureDecodesToCorrectFloat32Bytes(t *testing.T) {
	m := startTestKernel(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
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
	if fig.Kind != "line" {
		t.Errorf("Kind = %q, want line", fig.Kind)
	}
	if len(fig.ByteLengths) != 2 || fig.ByteLengths[0] != 12 || fig.ByteLengths[1] != 12 {
		t.Fatalf("ByteLengths = %v, want [12 12] (3 float32s each)", fig.ByteLengths)
	}
	if len(fig.Payload) != 24 {
		t.Fatalf("len(Payload) = %d, want 24", len(fig.Payload))
	}

	x := decodeFloat32s(fig.Payload[:12])
	y := decodeFloat32s(fig.Payload[12:])
	wantX := []float32{1, 2, 3}
	wantY := []float32{4, 5, 6}
	for i := range wantX {
		if x[i] != wantX[i] || y[i] != wantY[i] {
			t.Fatalf("decoded x=%v y=%v, want x=%v y=%v", x, y, wantX, wantY)
		}
	}
}

func decodeFloat32s(b []byte) []float32 {
	out := make([]float32, len(b)/4)
	for i := range out {
		bits := binary.LittleEndian.Uint32(b[i*4 : i*4+4])
		out[i] = math.Float32frombits(bits)
	}
	return out
}

func TestZoomReaggregatesTheLastFigure(t *testing.T) {
	m := startTestKernel(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	drain(ctx, m.Execute(ctx, "1", "plot(list(range(1000)), list(range(1000)))"))
	msgs := drain(ctx, m.Zoom(ctx, "2", []float64{100, 200}))

	var fig *Message
	for i := range msgs {
		if msgs[i].Type == "figure" {
			fig = &msgs[i]
		}
	}
	if fig == nil {
		t.Fatalf("no figure message in %+v", msgs)
	}
	x := decodeFloat32s(fig.Payload[:fig.ByteLengths[0]])
	for _, v := range x {
		if v < 100 || v > 200 {
			t.Fatalf("x contains %v, want everything within [100, 200]", v)
		}
	}
	if msgs[len(msgs)-1].Status != "ok" {
		t.Fatalf("final status = %q, want ok", msgs[len(msgs)-1].Status)
	}
}

func TestZoomWithNilRangeRestoresFullView(t *testing.T) {
	m := startTestKernel(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	drain(ctx, m.Execute(ctx, "1", "plot(list(range(1000)), list(range(1000)))"))
	drain(ctx, m.Zoom(ctx, "2", []float64{100, 200}))
	msgs := drain(ctx, m.Zoom(ctx, "3", nil))

	var fig *Message
	for i := range msgs {
		if msgs[i].Type == "figure" {
			fig = &msgs[i]
		}
	}
	if fig == nil {
		t.Fatalf("no figure message in %+v", msgs)
	}
	x := decodeFloat32s(fig.Payload[:fig.ByteLengths[0]])
	if x[0] != 0 || x[len(x)-1] != 999 {
		t.Fatalf("x = %v, want it to span the full [0, 999] range", x)
	}
}

func TestZoomBeforeAnyFigureIsAnError(t *testing.T) {
	m := startTestKernel(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	msgs := drain(ctx, m.Zoom(ctx, "1", []float64{0, 1}))

	if msgs[0].Type != "error" || msgs[0].EName != "NoFigure" {
		t.Fatalf("msgs[0] = %+v, want a NoFigure error", msgs[0])
	}

	// the kernel must still be usable afterwards
	msgs2 := drain(ctx, m.Execute(ctx, "2", "1 + 1"))
	if msgs2[0].Text != "2" {
		t.Fatalf("msgs2[0] = %+v, want execute_result 2", msgs2[0])
	}
}

func TestKernelAutoRestartsAfterOsExit(t *testing.T) {
	m := startTestKernel(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	msgs := drain(ctx, m.Execute(ctx, "1", "import os; os._exit(1)"))

	var sawError, sawRestarted bool
	for _, msg := range msgs {
		if msg.Type == "error" {
			sawError = true
		}
		if msg.Type == "kernel_restarted" {
			sawRestarted = true
		}
	}
	if !sawError {
		t.Fatalf("expected an error reporting the crash, got %+v", msgs)
	}
	if !sawRestarted {
		t.Fatalf("expected a kernel_restarted message, got %+v", msgs)
	}

	// the next request must reach a genuinely new, working kernel
	msgs2 := drain(ctx, m.Execute(ctx, "2", "1 + 1"))
	if msgs2[0].Type != "execute_result" || msgs2[0].Text != "2" {
		t.Fatalf("kernel not usable after restart: %+v", msgs2)
	}
}

func TestKernelRestartStartsWithAFreshNamespace(t *testing.T) {
	m := startTestKernel(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	drain(ctx, m.Execute(ctx, "1", "a = 99"))
	drain(ctx, m.Execute(ctx, "2", "import os; os._exit(1)"))

	msgs := drain(ctx, m.Execute(ctx, "3", "a"))

	if msgs[0].Type != "error" || msgs[0].EName != "NameError" {
		t.Fatalf("msgs[0] = %+v, want a NameError -- 'a' should not survive a restart", msgs[0])
	}
}

func TestKernelRestartRecoversTheLastFigureToo(t *testing.T) {
	// Not just the namespace: last_figure (what a subsequent zoom_request
	// applies to) must also be gone after a restart.
	m := startTestKernel(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	drain(ctx, m.Execute(ctx, "1", "plot([1.0, 2.0], [3.0, 4.0])"))
	drain(ctx, m.Execute(ctx, "2", "import os; os._exit(1)"))

	msgs := drain(ctx, m.Zoom(ctx, "3", []float64{0, 1}))

	if msgs[0].Type != "error" || msgs[0].EName != "NoFigure" {
		t.Fatalf("msgs[0] = %+v, want NoFigure -- the old figure should not survive a restart", msgs[0])
	}
}

func TestCloseTerminatesTheSubprocess(t *testing.T) {
	m, err := Start(Runtime{Python: testPython(t)})
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	if err := m.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	if _, err := os.Stat(m.sockDir); !os.IsNotExist(err) {
		t.Errorf("socket dir %s was not removed", m.sockDir)
	}
}

func TestInterruptRecoversFromInfiniteLoop(t *testing.T) {
	m := startTestKernel(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	ch := m.Execute(ctx, "loop", "print('ready', flush=True)\nwhile True: pass")
	select {
	case msg := <-ch:
		if msg.Type != "stream" {
			t.Fatalf("expected readiness, got %+v", msg)
		}
	case <-ctx.Done():
		t.Fatal("kernel never started loop")
	}
	m.Interrupt()
	msgs := drain(ctx, ch)
	if len(msgs) == 0 || msgs[len(msgs)-1].Type != "kernel_restarted" {
		t.Fatalf("missing restart: %+v", msgs)
	}
	msgs = drain(ctx, m.Execute(ctx, "next", "1 + 1"))
	if len(msgs) != 2 || msgs[0].Text != "2" {
		t.Fatalf("unusable after interrupt: %+v", msgs)
	}
}

func TestCancelledRequestDoesNotPoisonNextReply(t *testing.T) {
	m := startTestKernel(t)
	ctx, cancel := context.WithCancel(context.Background())
	ch := m.Execute(ctx, "cancelled", "print('ready', flush=True)\nwhile True: pass")
	<-ch
	cancel() // stop consuming the response channel, as a disconnected client does
	nextCtx, nextCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer nextCancel()
	msgs := drain(nextCtx, m.Execute(nextCtx, "next", "42"))
	if len(msgs) != 2 || msgs[0].MsgID != "next" || msgs[0].Text != "42" {
		t.Fatalf("stale reply or blocked recovery: %+v", msgs)
	}
}

func TestCancelledQueuedRequestReturnsWithoutExecuting(t *testing.T) {
	m := startTestKernel(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	loop := m.Execute(ctx, "loop", "print('ready', flush=True)\nwhile True: pass")
	<-loop
	queuedCtx, queuedCancel := context.WithCancel(ctx)
	queued := m.Execute(queuedCtx, "queued", "raise AssertionError('must not execute')")
	queuedCancel()
	select {
	case _, ok := <-queued:
		if ok {
			t.Fatal("cancelled queued request returned a message")
		}
	case <-ctx.Done():
		t.Fatal("queued cancellation blocked on active request")
	}
	m.Interrupt()
	drain(ctx, loop)
}

func TestCloseDuringExecutionAndRepeatedClose(t *testing.T) {
	m := startTestKernel(t)
	ch := m.Execute(context.Background(), "loop", "print('ready', flush=True)\nwhile True: pass")
	<-ch
	closed := make(chan error, 1)
	go func() { closed <- m.Close() }()
	select {
	case err := <-closed:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Close blocked on execution")
	}
	if err := m.Close(); err != nil {
		t.Fatal(err)
	}
	for range m.Execute(context.Background(), "after-close", "1") {
		t.Fatal("execution after Close")
	}
}

func TestRestartFailureCanBeRetried(t *testing.T) {
	m := startTestKernel(t)
	python := m.runtime.Python
	m.runtime.Python = "/no/such/python"
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	msgs := drain(ctx, m.Execute(ctx, "crash", "import os; os._exit(1)"))
	if len(msgs) != 2 || msgs[1].Type != "error" {
		t.Fatalf("expected restart failure: %+v", msgs)
	}
	m.runtime.Python = python
	msgs = drain(ctx, m.Execute(ctx, "retry", "42"))
	if len(msgs) != 2 || msgs[0].Text != "42" {
		t.Fatalf("failed retry: %+v", msgs)
	}
}

func TestRequestDeadlineStopsInfiniteLoop(t *testing.T) {
	m := startTestKernel(t)
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	drain(ctx, m.Execute(ctx, "deadline", "while True: pass"))
	nextCtx, nextCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer nextCancel()
	msgs := drain(nextCtx, m.Execute(nextCtx, "next", "42"))
	if len(msgs) != 2 || msgs[0].Text != "42" {
		t.Fatalf("deadline did not restore kernel: %+v", msgs)
	}
}

func TestMalformedKernelFramesAreRejected(t *testing.T) {
	for _, input := range []string{
		`{"type":"figure","byte_lengths":[-4,4]}`,
		`{"type":"figure","byte_lengths":[1,4]}`,
		`{"type":"figure","byte_lengths":[67108864,4]}`,
		`{"type":"figure","byte_lengths":[]}`,
		`{"type":"figure","kind":"heatmap","byte_lengths":[4,4]}`,
		`{"type":"figure","kind":"heatmap","byte_lengths":[4,4,8]}`,
		`{"type":"figure","kind":"heatmap","byte_lengths":[-4,-4,-4]}`,
		`{"type":"figure","kind":"heatmap","byte_lengths":[67108864,67108864,67108864]}`,
		strings.Repeat("x", (1<<20)+1),
	} {
		m := &Manager{reader: bufio.NewReader(strings.NewReader(input + "\n"))}
		if _, err := m.readMessage(); err == nil {
			t.Fatal("accepted invalid frame")
		}
	}
}
