// Package kernel supervises the `python3 -m loupe_kernel <socket_path>`
// subprocess and speaks the wire protocol described in PLAN.md,
// "Architecture": newline-delimited JSON control messages over a Unix
// domain socket, with a "figure" message's raw float32 bytes (x then y,
// per its byte_lengths) immediately following its JSON header rather than
// being encoded as JSON.
//
// This process (Go) owns the socket: it creates, binds, and listens on it,
// then spawns the kernel with the path as an argument and accepts its
// connection. That keeps lifecycle and cleanup on the supervisor's side --
// including restarting the kernel if it dies (PLAN.md, "Decisions and
// why": a crash or `os._exit()` in user code must not take the whole
// process down with it).
//
// How Python itself is invoked is described by a Runtime: a plain
// interpreter path in dev mode, or (a real -tags loupe_embed build) a
// bundled dynamic linker pointed at bundled glibc, so the interpreter
// never depends on the host's -- see Runtime's doc comment.
package kernel

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"time"
)

// Column describes a Polars column in schema and bounded preview messages.
type Column struct {
	Name  string `json:"name"`
	Dtype string `json:"dtype"`
}

// Message is one wire-protocol message from the kernel. Not every field is
// set on every message; see PLAN.md's message shapes (execute_reply,
// stream, execute_result, error, figure), plus this package's own
// synthetic "kernel_restarted" (see roundTrip).
type Message struct {
	Type  string `json:"type"`
	MsgID string `json:"msg_id,omitempty"`

	// stream
	Name string `json:"name,omitempty"`
	Text string `json:"text,omitempty"`

	// error
	EName     string   `json:"ename,omitempty"`
	EValue    string   `json:"evalue,omitempty"`
	Traceback []string `json:"traceback,omitempty"`

	// execute_reply
	Status string `json:"status,omitempty"`

	// figure
	Kind        string `json:"kind,omitempty"`
	Title       string `json:"title,omitempty"`
	Dtype       string `json:"dtype,omitempty"`
	ByteLengths []int  `json:"byte_lengths,omitempty"`

	// data_schema / data_preview
	Path    string     `json:"path,omitempty"`
	Columns []Column   `json:"columns,omitempty"`
	Rows    [][]string `json:"rows,omitempty"`
	Limit   int        `json:"limit,omitempty"`
	HasMore bool       `json:"has_more,omitempty"`

	// Payload holds a figure message's raw bytes. It is never part of the
	// JSON itself -- see the package doc comment.
	Payload []byte `json:"-"`
}

// connectTimeout bounds how long a (re)spawn waits for the subprocess to
// connect, so a broken python invocation fails loudly instead of hanging.
const connectTimeout = 10 * time.Second

// Runtime describes how to invoke the Python interpreter. Loader and
// LibDir are empty for a plain interpreter -- dev mode, where Python is
// already correctly linked against whatever's on the host (python/.venv
// or PATH) and should just be exec'd directly. When they're set (a real
// -tags loupe_embed build; see internal/extract.PythonRuntime), Start
// invokes Python via that bundled dynamic linker instead of executing it
// directly, so the interpreter and its native extensions never depend on
// the host's glibc -- see scripts/generate-runtime.sh's "Bundled glibc".
type Runtime struct {
	Python string
	Loader string
	LibDir string
}

// Manager supervises one kernel subprocess and its socket connection,
// transparently replacing both if the subprocess dies.
type Manager struct {
	runtime Runtime

	gate           chan struct{} // serializes requests and lifecycle operations
	done           chan struct{}
	shutdown       context.Context
	shutdownCancel context.CancelFunc
	closeOnce      sync.Once
	stateMu        sync.Mutex // guards activeCancel, never held during socket I/O
	activeCancel   context.CancelFunc
	cmd            *exec.Cmd
	conn           *net.UnixConn
	listener       *net.UnixListener
	sockDir        string
	reader         *bufio.Reader
}

// Start spawns `python3 -m loupe_kernel <socket_path>` per rt (see
// Runtime), waits for it to connect, and returns a Manager ready for
// Execute. The interpreter must be able to `import loupe_kernel`.
func Start(rt Runtime) (*Manager, error) {
	shutdown, shutdownCancel := context.WithCancel(context.Background())
	m := &Manager{runtime: rt, gate: make(chan struct{}, 1), done: make(chan struct{}), shutdown: shutdown, shutdownCancel: shutdownCancel}
	if err := m.spawn(); err != nil {
		shutdownCancel()
		return nil, err
	}
	return m, nil
}

// spawn starts a fresh kernel subprocess and socket, storing it on m.
// Callers must hold m.gate and, if replacing a previous kernel, have
// already torn it down (see roundTrip's crash-recovery path).
func (m *Manager) spawn() error {
	sockDir, err := os.MkdirTemp("", "loupe-kernel-")
	if err != nil {
		return fmt.Errorf("kernel: creating socket dir: %w", err)
	}
	sockPath := filepath.Join(sockDir, "kernel.sock")

	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: sockPath, Net: "unix"})
	if err != nil {
		os.RemoveAll(sockDir)
		return fmt.Errorf("kernel: listening on %s: %w", sockPath, err)
	}

	cmd := m.command(sockPath)
	cmd.Env = append(os.Environ(), "LOUPE_SESSION_CACHE="+filepath.Join(sockDir, "data"))
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		listener.Close()
		os.RemoveAll(sockDir)
		return fmt.Errorf("kernel: starting %s: %w", m.runtime.Python, err)
	}

	if err := listener.SetDeadline(time.Now().Add(connectTimeout)); err != nil {
		cmd.Process.Kill()
		cmd.Wait()
		listener.Close()
		os.RemoveAll(sockDir)
		return err
	}
	conn, err := listener.AcceptUnix()
	if err != nil {
		cmd.Process.Kill()
		cmd.Wait()
		listener.Close()
		os.RemoveAll(sockDir)
		return fmt.Errorf("kernel: subprocess did not connect within %s: %w", connectTimeout, err)
	}
	listener.SetDeadline(time.Time{})

	m.cmd = cmd
	m.conn = conn
	m.listener = listener
	m.sockDir = sockDir
	m.reader = bufio.NewReader(conn)
	return nil
}

// command builds the kernel subprocess invocation for sockPath. With a
// bundled loader (Runtime.Loader set), the loader itself is the program,
// told via --library-path to resolve against the bundled glibc instead
// of the host's -- see the Runtime doc comment. Without one (dev mode),
// Python is exec'd directly.
func (m *Manager) command(sockPath string) *exec.Cmd {
	if m.runtime.Loader == "" {
		return exec.Command(m.runtime.Python, "-m", "loupe_kernel", sockPath)
	}
	return exec.Command(
		m.runtime.Loader, "--library-path", m.runtime.LibDir,
		m.runtime.Python, "-m", "loupe_kernel", sockPath,
	)
}

// teardown releases and clears the current kernel's process and socket
// resources. Callers must hold m.gate.
func (m *Manager) teardown() error {
	if m.conn != nil {
		m.conn.Close()
		m.conn = nil
	}
	if m.listener != nil {
		m.listener.Close()
		m.listener = nil
	}
	if m.cmd != nil {
		m.cmd.Process.Kill()
		m.cmd.Wait()
		m.cmd = nil
	}
	return os.RemoveAll(m.sockDir)
}

// Interrupt stops the active request by cancelling its socket operation. The
// request owner kills and replaces the interpreter before accepting more work.
// This also stops native code or Python that catches KeyboardInterrupt.
// Session variables are lost, just as with crash recovery. An idle call is a no-op.
func (m *Manager) Interrupt() {
	m.stateMu.Lock()
	defer m.stateMu.Unlock()
	if m.activeCancel != nil {
		m.activeCancel()
	}
}

// Execute sends one execute_request and streams the kernel's responses on
// the returned channel until execute_reply, or kernel_restarted after a forced
// stop/crash. Cancellation can close the channel without a terminal message. It blocks other Execute or
// Zoom calls on this Manager until that reply arrives, matching the
// kernel's own one-request-at-a-time protocol.
func (m *Manager) Execute(ctx context.Context, msgID, code string) <-chan Message {
	req, err := json.Marshal(map[string]string{
		"type": "execute_request", "msg_id": msgID, "code": code,
	})
	return m.roundTrip(ctx, msgID, req, err)
}

// Zoom sends one zoom_request -- PLAN.md's "zoom-settle re-aggregation":
// re-run the currently displayed figure's reduction for a new viewport,
// not any code. xRange is nil for a full-range reset, which marshals to
// JSON null, matching what the kernel's zoom_request handler expects.
// Streams responses exactly like Execute.
func (m *Manager) Zoom(ctx context.Context, msgID string, xRange []float64) <-chan Message {
	req, err := json.Marshal(map[string]any{
		"type": "zoom_request", "msg_id": msgID, "x_range": xRange,
	})
	return m.roundTrip(ctx, msgID, req, err)
}

// roundTrip sends one already-encoded request line and streams the
// kernel's responses until execute_reply. Shared by Execute and Zoom so
// the locking and read loop exist in exactly one place.
//
// If the kernel dies mid-request -- a crash, or user code calling
// os._exit() -- the write or read fails; roundTrip reports that as an
// error for the in-flight request, then transparently respawns a fresh
// kernel so the *next* request has something to talk to, finishing with a
// synthetic "kernel_restarted" message so the browser can tell the user
// their session state (variables, the last figure) was lost. A failed
// respawn is reported as a second error instead.
func (m *Manager) roundTrip(ctx context.Context, msgID string, request []byte, marshalErr error) <-chan Message {
	ch := make(chan Message)
	go func() {
		defer close(ch)
		if marshalErr != nil {
			m.send(ctx, ch, errorMessage(msgID, marshalErr))
			return
		}
		select {
		case m.gate <- struct{}{}:
		case <-ctx.Done():
			return
		case <-m.done:
			return
		}
		defer func() { <-m.gate }()
		select {
		case <-m.done:
			return
		default:
		}
		if ctx.Err() != nil {
			return
		}
		if m.conn == nil {
			if err := m.spawn(); err != nil {
				m.send(ctx, ch, errorMessage(msgID, err))
				return
			}
		}

		workCtx, cancel := context.WithCancel(ctx)
		m.stateMu.Lock()
		m.activeCancel = cancel
		m.stateMu.Unlock()
		conn := m.conn // callback must never close a replacement connection
		stopCancel := context.AfterFunc(workCtx, func() { conn.Close() })
		stopClose := context.AfterFunc(m.shutdown, cancel)
		defer func() {
			stopClose()
			stopCancel()
			cancel()
			m.stateMu.Lock()
			m.activeCancel = nil
			m.stateMu.Unlock()
		}()

		if _, err := conn.Write(append(request, '\n')); err != nil {
			m.recoverFromCrash(ctx, ch, msgID, err)
			return
		}
		for {
			msg, err := m.readMessage()
			if err != nil {
				m.recoverFromCrash(ctx, ch, msgID, err)
				return
			}
			if msg.Type == "execute_reply" {
				m.stateMu.Lock()
				m.activeCancel = nil
				stopped := stopCancel()
				m.stateMu.Unlock()
				if !stopped {
					m.recoverFromCrash(ctx, ch, msgID, workCtx.Err())
					return
				}
			}
			select {
			case ch <- msg:
			case <-workCtx.Done():
				m.recoverFromCrash(ctx, ch, msgID, workCtx.Err())
				return
			}
			if msg.Type == "execute_reply" {
				return
			}
		}
	}()
	return ch
}

// send cannot strand recovery when its client disconnects or the host closes.
func (m *Manager) send(ctx context.Context, ch chan<- Message, msg Message) {
	select {
	case ch <- msg:
	case <-ctx.Done():
	case <-m.done:
	}
}

// recoverFromCrash restores the transport before attempting to notify a client.
// Callers must hold m.gate; a failed spawn leaves nil resources for a later retry.
func (m *Manager) recoverFromCrash(ctx context.Context, ch chan<- Message, msgID string, cause error) {
	m.teardown()
	select {
	case <-m.done:
		return
	default:
	}
	err := m.spawn()
	m.send(ctx, ch, errorMessage(msgID, fmt.Errorf("kernel stopped: %w", cause)))
	if err != nil {
		m.send(ctx, ch, errorMessage(msgID, fmt.Errorf("restarting kernel: %w", err)))
		return
	}
	m.send(ctx, ch, Message{Type: "kernel_restarted", MsgID: msgID})
}

func errorMessage(msgID string, err error) Message {
	return Message{Type: "error", MsgID: msgID, EName: "KernelError", EValue: err.Error()}
}

func (m *Manager) readMessage() (Message, error) {
	// A bad kernel must not make the Go host allocate unbounded control data.
	var line []byte
	for {
		fragment, more, err := m.reader.ReadLine()
		if err != nil {
			return Message{}, err
		}
		if len(line)+len(fragment) > 1<<20 {
			return Message{}, fmt.Errorf("kernel: control message exceeds 1 MiB")
		}
		line = append(line, fragment...)
		if !more {
			break
		}
	}

	var msg Message
	if err := json.Unmarshal(line, &msg); err != nil {
		return Message{}, fmt.Errorf("kernel: decoding message: %w", err)
	}

	if msg.Type == "figure" {
		if len(msg.ByteLengths) != 2 {
			return Message{}, fmt.Errorf("kernel: figure requires two byte lengths")
		}
		total := 0
		for _, n := range msg.ByteLengths {
			if n < 0 || n%4 != 0 || n > 64<<20-total {
				return Message{}, fmt.Errorf("kernel: invalid figure byte lengths %v", msg.ByteLengths)
			}
			total += n
		}
		payload := make([]byte, total)
		if _, err := io.ReadFull(m.reader, payload); err != nil {
			return Message{}, fmt.Errorf("kernel: reading figure payload: %w", err)
		}
		msg.Payload = payload
	}

	return msg, nil
}

// Close terminates the kernel subprocess and releases its socket resources.
func (m *Manager) Close() error {
	m.closeOnce.Do(func() { close(m.done); m.shutdownCancel() })
	m.gate <- struct{}{}
	defer func() { <-m.gate }()
	return m.teardown()
}
