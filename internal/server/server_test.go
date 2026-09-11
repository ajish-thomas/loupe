package server

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/ajish-thomas/loupe/internal/kernel"
)

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

// testServer starts a real kernel and wraps it in an httptest.Server.
func testServer(t *testing.T) (*httptest.Server, string) {
	t.Helper()
	km, err := kernel.Start(kernel.Runtime{Python: testPython(t)})
	if err != nil {
		t.Fatalf("kernel.Start: %v", err)
	}
	t.Cleanup(func() { km.Close() })

	webDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(webDir, "index.html"), []byte("hello from web dir"), 0o644); err != nil {
		t.Fatalf("writing test asset: %v", err)
	}

	srv := httptest.NewServer(New(webDir, km))
	t.Cleanup(srv.Close)

	wsURL := "ws" + srv.URL[len("http"):] + "/ws"
	return srv, wsURL
}

func TestServesStaticAssets(t *testing.T) {
	srv, _ := testServer(t)

	resp, err := http.Get(srv.URL + "/index.html")
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
}

func TestWSExecuteResultRoundTrip(t *testing.T) {
	_, wsURL := testServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	c, _, err := websocket.Dial(ctx, wsURL, nil)
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer c.Close(websocket.StatusNormalClosure, "")

	send(t, ctx, c, browserRequest{Type: "execute_request", MsgID: "1", Code: "1 + 1"})

	msg := recvText(t, ctx, c)
	if msg.Type != "execute_result" || msg.Text != "2" {
		t.Fatalf("msg = %+v, want execute_result 2", msg)
	}
	reply := recvText(t, ctx, c)
	if reply.Type != "execute_reply" || reply.Status != "ok" {
		t.Fatalf("reply = %+v, want execute_reply ok", reply)
	}
}

func TestWSFigureArrivesAsHeaderThenBinaryPayload(t *testing.T) {
	_, wsURL := testServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	c, _, err := websocket.Dial(ctx, wsURL, nil)
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer c.Close(websocket.StatusNormalClosure, "")

	send(t, ctx, c, browserRequest{Type: "execute_request", MsgID: "1", Code: "plot([1.0, 2.0], [3.0, 4.0])"})

	header := recvText(t, ctx, c)
	if header.Type != "figure" || header.Kind != "line" {
		t.Fatalf("header = %+v, want a line figure", header)
	}

	typ, payload, err := c.Read(ctx)
	if err != nil {
		t.Fatalf("reading payload frame: %v", err)
	}
	if typ != websocket.MessageBinary {
		t.Fatalf("payload frame type = %v, want binary", typ)
	}
	wantLen := header.ByteLengths[0] + header.ByteLengths[1]
	if len(payload) != wantLen {
		t.Fatalf("len(payload) = %d, want %d", len(payload), wantLen)
	}

	reply := recvText(t, ctx, c)
	if reply.Type != "execute_reply" {
		t.Fatalf("reply = %+v, want execute_reply", reply)
	}
}

func TestWSUnknownMessageTypeGetsAnErrorFrame(t *testing.T) {
	_, wsURL := testServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	c, _, err := websocket.Dial(ctx, wsURL, nil)
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer c.Close(websocket.StatusNormalClosure, "")

	send(t, ctx, c, browserRequest{Type: "not_a_real_type", MsgID: "1"})

	msg := recvText(t, ctx, c)
	if msg.Type != "error" || msg.EName != "ProtocolError" {
		t.Fatalf("msg = %+v, want a ProtocolError", msg)
	}
}

func TestWSConnectionSurvivesAKernelError(t *testing.T) {
	_, wsURL := testServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	c, _, err := websocket.Dial(ctx, wsURL, nil)
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer c.Close(websocket.StatusNormalClosure, "")

	send(t, ctx, c, browserRequest{Type: "execute_request", MsgID: "1", Code: "1 / 0"})
	errMsg := recvText(t, ctx, c)
	if errMsg.Type != "error" || errMsg.EName != "ZeroDivisionError" {
		t.Fatalf("errMsg = %+v, want ZeroDivisionError", errMsg)
	}
	recvText(t, ctx, c) // execute_reply

	// the same WS connection (and kernel) must still work afterwards
	send(t, ctx, c, browserRequest{Type: "execute_request", MsgID: "2", Code: "1 + 1"})
	msg := recvText(t, ctx, c)
	if msg.Type != "execute_result" || msg.Text != "2" {
		t.Fatalf("msg = %+v, want execute_result 2", msg)
	}
}

func TestWSConnectionSurvivesAKernelCrash(t *testing.T) {
	_, wsURL := testServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	c, _, err := websocket.Dial(ctx, wsURL, nil)
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer c.Close(websocket.StatusNormalClosure, "")

	send(t, ctx, c, browserRequest{Type: "execute_request", MsgID: "1", Code: "import os; os._exit(1)"})

	var sawError, sawRestarted bool
	for {
		msg := recvText(t, ctx, c)
		if msg.Type == "error" {
			sawError = true
		}
		if msg.Type == "kernel_restarted" {
			sawRestarted = true
			break
		}
	}
	if !sawError || !sawRestarted {
		t.Fatalf("sawError=%v sawRestarted=%v, want both true", sawError, sawRestarted)
	}

	// the same WS connection must still work against the restarted kernel
	send(t, ctx, c, browserRequest{Type: "execute_request", MsgID: "2", Code: "1 + 1"})
	msg := recvText(t, ctx, c)
	if msg.Type != "execute_result" || msg.Text != "2" {
		t.Fatalf("msg = %+v, want execute_result 2", msg)
	}
}

func TestWSZoomRequestReaggregatesTheLastFigure(t *testing.T) {
	_, wsURL := testServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	c, _, err := websocket.Dial(ctx, wsURL, nil)
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer c.Close(websocket.StatusNormalClosure, "")

	send(t, ctx, c, browserRequest{
		Type: "execute_request", MsgID: "1",
		Code: "plot(list(range(1000)), list(range(1000)))",
	})
	drainReply(t, ctx, c) // figure header, binary payload, execute_reply

	send(t, ctx, c, browserRequest{Type: "zoom_request", MsgID: "2", XRange: []float64{100, 200}})

	header := recvText(t, ctx, c)
	if header.Type != "figure" || header.Kind != "line" {
		t.Fatalf("header = %+v, want a line figure", header)
	}
	typ, payload, err := c.Read(ctx)
	if err != nil {
		t.Fatalf("reading payload frame: %v", err)
	}
	if typ != websocket.MessageBinary {
		t.Fatalf("payload frame type = %v, want binary", typ)
	}
	n := header.ByteLengths[0] / 4
	for i := 0; i < n; i++ {
		v := decodeFloat32(payload[i*4 : i*4+4])
		if v < 100 || v > 200 {
			t.Fatalf("x contains %v, want everything within [100, 200]", v)
		}
	}
	reply := recvText(t, ctx, c)
	if reply.Type != "execute_reply" || reply.Status != "ok" {
		t.Fatalf("reply = %+v, want execute_reply ok", reply)
	}
}

func TestWSZoomRequestBeforeAnyFigureIsAnError(t *testing.T) {
	_, wsURL := testServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	c, _, err := websocket.Dial(ctx, wsURL, nil)
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer c.Close(websocket.StatusNormalClosure, "")

	send(t, ctx, c, browserRequest{Type: "zoom_request", MsgID: "1", XRange: []float64{0, 1}})

	msg := recvText(t, ctx, c)
	if msg.Type != "error" || msg.EName != "NoFigure" {
		t.Fatalf("msg = %+v, want a NoFigure error", msg)
	}
}

func drainReply(t *testing.T, ctx context.Context, c *websocket.Conn) {
	t.Helper()
	for {
		typ, data, err := c.Read(ctx)
		if err != nil {
			t.Fatalf("read: %v", err)
		}
		if typ != websocket.MessageText {
			continue // a figure's binary payload frame
		}
		var msg kernel.Message
		if err := json.Unmarshal(data, &msg); err != nil {
			t.Fatalf("unmarshal %q: %v", data, err)
		}
		if msg.Type == "execute_reply" {
			return
		}
	}
}

func decodeFloat32(b []byte) float32 {
	return math.Float32frombits(binary.LittleEndian.Uint32(b))
}

func send(t *testing.T, ctx context.Context, c *websocket.Conn, req browserRequest) {
	t.Helper()
	data, err := json.Marshal(req)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if err := c.Write(ctx, websocket.MessageText, data); err != nil {
		t.Fatalf("write: %v", err)
	}
}

func recvText(t *testing.T, ctx context.Context, c *websocket.Conn) kernel.Message {
	t.Helper()
	typ, data, err := c.Read(ctx)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if typ != websocket.MessageText {
		t.Fatalf("frame type = %v, want text", typ)
	}
	var msg kernel.Message
	if err := json.Unmarshal(data, &msg); err != nil {
		t.Fatalf("unmarshal %q: %v", data, err)
	}
	return msg
}

func TestWSInterruptOnExecutingConnection(t *testing.T) {
	_, url := testServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	c, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer c.CloseNow()
	send(t, ctx, c, browserRequest{Type: "execute_request", MsgID: "loop", Code: "print('ready', flush=True)\nwhile True: pass"})
	if msg := recvText(t, ctx, c); msg.Type != "stream" {
		t.Fatalf("not ready: %+v", msg)
	}
	send(t, ctx, c, browserRequest{Type: "interrupt_request"})
	for recvText(t, ctx, c).Type != "kernel_restarted" {
	}
	send(t, ctx, c, browserRequest{Type: "execute_request", MsgID: "next", Code: "42"})
	if msg := recvText(t, ctx, c); msg.Text != "42" {
		t.Fatalf("not recovered: %+v", msg)
	}
	recvText(t, ctx, c)
}

func TestWSDisconnectUnblocksOtherConnections(t *testing.T) {
	_, url := testServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	c, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	send(t, ctx, c, browserRequest{Type: "execute_request", MsgID: "loop", Code: "print('ready', flush=True)\nwhile True: pass"})
	recvText(t, ctx, c)
	c.CloseNow()
	other, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer other.CloseNow()
	send(t, ctx, other, browserRequest{Type: "execute_request", MsgID: "next", Code: "42"})
	if msg := recvText(t, ctx, other); msg.Text != "42" {
		t.Fatalf("other tab stuck: %+v", msg)
	}
	recvText(t, ctx, other)
}

func TestEmbeddedUIAssets(t *testing.T) {
	// No disk web directory and no kernel needed to serve the offline UI.
	s := New("", nil)
	for _, path := range []string{"/", "/app.js", "/vendor/uPlot.iife.min.js", "/vendor/uPlot.min.css"} {
		t.Run(path, func(t *testing.T) {
			r := httptest.NewRecorder()
			s.ServeHTTP(r, httptest.NewRequest(http.MethodGet, path, nil))
			if r.Code != http.StatusOK || r.Body.Len() == 0 {
				t.Fatalf("asset missing: %s: %d", path, r.Code)
			}
		})
	}
}

func TestWSIngestSchemaAndPreview(t *testing.T) {
	_, url := testServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	c, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer c.CloseNow()
	path := filepath.Join(t.TempDir(), "data.csv")
	if err := os.WriteFile(path, []byte("x,y\n1,2\n3,4\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	quoted, _ := json.Marshal(path)
	send(t, ctx, c, browserRequest{Type: "execute_request", MsgID: "scan", Code: "df = scan(" + string(quoted) + ", cache_threshold=0)\ndf"})
	schema := recvText(t, ctx, c)
	if schema.Type != "data_schema" || len(schema.Columns) != 2 || schema.Path != path {
		t.Fatalf("schema lost during relay: %+v", schema)
	}
	preview := recvText(t, ctx, c)
	if preview.Type != "data_preview" || len(preview.Rows) != 2 || preview.Rows[0][0] != "1" || preview.Limit != 20 {
		t.Fatalf("preview lost during relay: %+v", preview)
	}
	if reply := recvText(t, ctx, c); reply.Status != "ok" {
		t.Fatalf("failed: %+v", reply)
	}
}

func TestWSHeatmapAndZoomCarryColorArray(t *testing.T) {
	_, wsURL := testServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	c, _, err := websocket.Dial(ctx, wsURL, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer c.CloseNow()
	for _, request := range []browserRequest{
		{Type: "execute_request", MsgID: "heat", Code: `heatmap([3,1,2], [6,2,4], [30,10,20], cmap="plasma", bins=4)`},
		{Type: "zoom_request", MsgID: "zoom", XRange: []float64{1, 2}},
	} {
		send(t, ctx, c, request)
		header := recvText(t, ctx, c)
		if header.Kind != "heatmap" || header.Cmap != "plasma" || header.ColorBins != 4 || len(header.ColorRange) != 2 || header.ColorRange[0] != 10 || header.ColorRange[1] != 30 || len(header.ByteLengths) != 3 {
			t.Fatalf("bad heatmap header: %+v", header)
		}
		typ, payload, err := c.Read(ctx)
		if err != nil {
			t.Fatal(err)
		}
		n := header.ByteLengths[0]
		if typ != websocket.MessageBinary || len(payload) != n*3 {
			t.Fatalf("bad payload: %v %d", typ, len(payload))
		}
		for i := 0; i < n; i += 4 {
			x := math.Float32frombits(binary.LittleEndian.Uint32(payload[i:]))
			color := math.Float32frombits(binary.LittleEndian.Uint32(payload[2*n+i:]))
			if color != x*10 {
				t.Fatalf("misaligned color: %v %v", x, color)
			}
		}
		if reply := recvText(t, ctx, c); reply.Status != "ok" {
			t.Fatalf("%+v", reply)
		}
	}
}
