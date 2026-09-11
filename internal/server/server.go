// Package server runs the local HTTP server: it serves the UI's static
// assets and relays messages between the browser (over a WebSocket at
// /ws) and one kernel.Manager, per PLAN.md's Architecture diagram.
package server

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"

	"github.com/coder/websocket"

	"github.com/ajish-thomas/loupe/internal/kernel"
	"github.com/ajish-thomas/loupe/web"
)

// Server serves the UI and bridges its WebSocket to a kernel.
type Server struct {
	kernel *kernel.Manager
	mux    *http.ServeMux
}

// New builds a Server that serves embedded assets (or webDir when non-empty)
// and relays /ws traffic to km.
func New(webDir string, km *kernel.Manager) *Server {
	s := &Server{kernel: km, mux: http.NewServeMux()}
	var assets http.FileSystem = http.FS(web.Assets)
	if webDir != "" {
		assets = http.Dir(webDir)
	}
	s.mux.Handle("/", http.FileServer(assets))
	s.mux.HandleFunc("/ws", s.handleWS)
	s.mux.HandleFunc("/fs", s.handleFS)
	return s
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mux.ServeHTTP(w, r)
}

// browserRequest is a message the browser is allowed to send: either an
// execute_request (Code set) or a zoom_request (XRange set, possibly nil
// for a full-range reset) -- PLAN.md's "zoom-settle re-aggregation".
type browserRequest struct {
	Type   string    `json:"type"`
	MsgID  string    `json:"msg_id"`
	Code   string    `json:"code,omitempty"`
	XRange []float64 `json:"x_range,omitempty"`
}

func (s *Server) handleWS(w http.ResponseWriter, r *http.Request) {
	c, err := websocket.Accept(w, r, nil)
	if err != nil {
		return
	}
	defer c.CloseNow()

	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()
	// Keep reading during execution: reads detect disconnects and allow Stop
	// on the same WebSocket as the request that is stuck in user code.
	requests := make(chan []byte, 16)
	go func() {
		defer cancel()
		for {
			_, data, err := c.Read(ctx)
			if err != nil {
				return
			}
			var req browserRequest
			if json.Unmarshal(data, &req) == nil && req.Type == "interrupt_request" {
				s.kernel.Interrupt()
				continue
			}
			select {
			case requests <- data:
			case <-ctx.Done():
				return
			default:
				return // bound queued work from a flooding client
			}
		}
	}()
	for {
		var data []byte
		select {
		case data = <-requests:
		case <-ctx.Done():
			return
		}

		var req browserRequest
		if err := json.Unmarshal(data, &req); err != nil {
			if !s.writeError(ctx, c, "", fmt.Errorf("malformed message: %w", err)) {
				return
			}
			continue
		}

		var messages <-chan kernel.Message
		switch req.Type {
		case "execute_request":
			messages = s.kernel.Execute(ctx, req.MsgID, req.Code)
		case "zoom_request":
			messages = s.kernel.Zoom(ctx, req.MsgID, req.XRange)
		default:
			if !s.writeError(ctx, c, req.MsgID, fmt.Errorf("unknown message type %q", req.Type)) {
				return
			}
			continue
		}

		if !s.relay(ctx, c, messages) {
			return
		}
	}
}

// relay forwards a kernel response channel to the browser -- a figure's
// JSON header as a text frame immediately followed by its raw bytes as a
// binary frame, matching PLAN.md's wire format on both hops. Returns
// false if the connection should be closed.
func (s *Server) relay(ctx context.Context, c *websocket.Conn, messages <-chan kernel.Message) bool {
	for msg := range messages {
		header, err := json.Marshal(msg)
		if err != nil {
			return s.writeError(ctx, c, msg.MsgID, err)
		}
		if err := c.Write(ctx, websocket.MessageText, header); err != nil {
			return false
		}
		if msg.Type == "figure" {
			if err := c.Write(ctx, websocket.MessageBinary, msg.Payload); err != nil {
				return false
			}
		}
	}
	return true
}

// writeError sends a kernel.Message-shaped error frame so the browser
// renders protocol-level failures the same way it renders kernel errors.
// Returns false if the write itself failed (caller should stop serving).
func (s *Server) writeError(ctx context.Context, c *websocket.Conn, msgID string, cause error) bool {
	msg := kernel.Message{Type: "error", MsgID: msgID, EName: "ProtocolError", EValue: cause.Error()}
	data, err := json.Marshal(msg)
	if err != nil {
		return false
	}
	return c.Write(ctx, websocket.MessageText, data) == nil
}
