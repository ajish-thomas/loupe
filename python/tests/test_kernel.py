"""Tests for loupe_kernel's REPL loop and wire protocol (__init__.py)."""

import json
import socket
import threading
from typing import Any

import numpy as np
import pytest

from loupe_kernel import _compile_repl, main, serve


class KernelClient:
    """Test double for the Go host: drives one `serve()` instance over a
    socketpair and decodes its messages."""

    def __init__(self, host: socket.socket) -> None:
        self._f = host.makefile("rwb")

    def send_raw(self, msg: dict[str, Any]) -> None:
        self._f.write((json.dumps(msg) + "\n").encode())
        self._f.flush()

    def recv_until_reply(self) -> list[dict[str, Any]]:
        messages = []
        while True:
            line = self._f.readline()
            if not line:
                raise EOFError("kernel closed the connection unexpectedly")
            msg = json.loads(line)
            if msg["type"] == "figure":
                n = sum(msg["byte_lengths"])
                msg["_raw"] = self._f.read(n)
            messages.append(msg)
            if msg["type"] == "execute_reply":
                return messages

    def run(self, msg_id: str, code: str) -> list[dict[str, Any]]:
        self.send_raw({"type": "execute_request", "msg_id": msg_id, "code": code})
        return self.recv_until_reply()

    def zoom(self, msg_id: str, x_range: list[float] | None) -> list[dict[str, Any]]:
        self.send_raw({"type": "zoom_request", "msg_id": msg_id, "x_range": x_range})
        return self.recv_until_reply()

    def close(self) -> None:
        self._f.close()


def _figure_arrays(msg: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    n_x = msg["byte_lengths"][0]
    raw = msg["_raw"]
    return np.frombuffer(raw[:n_x], dtype=np.float32), np.frombuffer(raw[n_x:], dtype=np.float32)


@pytest.fixture
def kernel():
    host, kernel_sock = socket.socketpair()
    thread = threading.Thread(target=serve, args=(kernel_sock,), daemon=True)
    thread.start()
    client = KernelClient(host)
    yield client
    client.close()
    host.close()
    thread.join(timeout=2)


# ---------------------------------------------------------------------------
# _compile_repl: the auto-display-last-expression split, in isolation
# ---------------------------------------------------------------------------


def test_compile_repl_splits_trailing_expression() -> None:
    exec_code, eval_code = _compile_repl("x = 1\nx + 1")
    ns: dict[str, Any] = {}
    exec(exec_code, ns)
    assert eval_code is not None
    assert eval(eval_code, ns) == 2


def test_compile_repl_statement_only_has_no_eval_code() -> None:
    _exec_code, eval_code = _compile_repl("x = 1")
    assert eval_code is None


def test_compile_repl_empty_code_is_a_no_op() -> None:
    exec_code, eval_code = _compile_repl("")
    assert eval_code is None
    exec(exec_code, {})  # must not raise


# ---------------------------------------------------------------------------
# serve(): basic execute/reply cycle and REPL semantics
# ---------------------------------------------------------------------------


def test_execute_result_for_trailing_expression(kernel: KernelClient) -> None:
    msgs = kernel.run("1", "1 + 1")
    assert msgs[0] == {"type": "execute_result", "msg_id": "1", "text": "2"}
    assert msgs[-1] == {"type": "execute_reply", "msg_id": "1", "status": "ok"}


def test_statement_only_code_has_no_execute_result(kernel: KernelClient) -> None:
    msgs = kernel.run("1", "x = 1")
    assert [m["type"] for m in msgs] == ["execute_reply"]


def test_empty_code_is_a_no_op(kernel: KernelClient) -> None:
    msgs = kernel.run("1", "")
    assert [m["type"] for m in msgs] == ["execute_reply"]
    assert msgs[0]["status"] == "ok"


def test_namespace_persists_across_requests(kernel: KernelClient) -> None:
    kernel.run("1", "a = 10")
    msgs = kernel.run("2", "a * 2")
    assert msgs[0]["text"] == "20"


def test_namespace_prelude_bindings(kernel: KernelClient) -> None:
    code = (
        "all(name in globals() for name in "
        "('np', 'pl', 'api', 'engine', 'ingest', 'plot', 'scatter', 'histogram', 'scan'))"
    )
    msgs = kernel.run("1", code)
    assert msgs[0]["text"] == "True"


# ---------------------------------------------------------------------------
# stdout/stderr redirection
# ---------------------------------------------------------------------------


def test_print_produces_stdout_stream_messages(kernel: KernelClient) -> None:
    msgs = kernel.run("1", "print('hi')")
    stream_msgs = [m for m in msgs if m["type"] == "stream"]
    assert stream_msgs
    assert all(m["name"] == "stdout" for m in stream_msgs)
    assert "".join(m["text"] for m in stream_msgs) == "hi\n"


def test_stderr_write_produces_stderr_stream_message(kernel: KernelClient) -> None:
    msgs = kernel.run("1", "import sys; sys.stderr.write('oops')")
    stream_msgs = [m for m in msgs if m["type"] == "stream"]
    assert stream_msgs == [{"type": "stream", "msg_id": "1", "name": "stderr", "text": "oops"}]


def test_empty_writes_produce_no_stream_message(kernel: KernelClient) -> None:
    # print()'s own sep/end writes can be empty strings; those aren't worth a
    # message. Assign the call's result so it isn't itself auto-displayed
    # (write() returning 0 would otherwise legitimately show up as a result,
    # exactly as a normal Python REPL echoes it).
    msgs = kernel.run("1", "import sys\n_ = sys.stdout.write('')")
    assert [m["type"] for m in msgs] == ["execute_reply"]


# ---------------------------------------------------------------------------
# Figure auto-display
# ---------------------------------------------------------------------------


def test_plot_is_auto_displayed_as_a_figure_message(kernel: KernelClient) -> None:
    msgs = kernel.run("1", "plot([1.0, 2.0, 3.0], [4.0, 5.0, 6.0])")
    fig_msgs = [m for m in msgs if m["type"] == "figure"]
    assert len(fig_msgs) == 1

    fig = fig_msgs[0]
    assert fig["kind"] == "line"
    assert fig["dtype"] == "float32"
    x, y = _figure_arrays(fig)
    np.testing.assert_array_equal(x, [1.0, 2.0, 3.0])
    np.testing.assert_array_equal(y, [4.0, 5.0, 6.0])


def test_scatter_is_auto_displayed_as_a_figure_message(kernel: KernelClient) -> None:
    msgs = kernel.run("1", "scatter([1.0, 2.0], [3.0, 4.0])")
    fig = next(m for m in msgs if m["type"] == "figure")
    assert fig["kind"] == "scatter"


def test_histogram_is_auto_displayed_as_a_figure_message(kernel: KernelClient) -> None:
    msgs = kernel.run("1", "histogram([1.0, 2.0, 3.0, 4.0], bins=2)")
    fig = next(m for m in msgs if m["type"] == "figure")
    assert fig["kind"] == "histogram"
    x, y = _figure_arrays(fig)
    assert len(x) == len(y) + 1


def test_figure_title_is_carried_through(kernel: KernelClient) -> None:
    msgs = kernel.run("1", "plot([1.0], [2.0], title='hello')")
    fig = next(m for m in msgs if m["type"] == "figure")
    assert fig["title"] == "hello"


def test_assigning_a_plot_does_not_auto_display(kernel: KernelClient) -> None:
    # Only a bare trailing expression auto-displays -- an assignment shouldn't.
    msgs = kernel.run("1", "fig = plot([1.0], [2.0])")
    assert [m["type"] for m in msgs] == ["execute_reply"]


# ---------------------------------------------------------------------------
# Error handling: the kernel survives ordinary faults
# ---------------------------------------------------------------------------


def test_exception_is_reported_as_error_and_kernel_keeps_running(kernel: KernelClient) -> None:
    msgs = kernel.run("1", "1 / 0")
    assert msgs[0]["type"] == "error"
    assert msgs[0]["ename"] == "ZeroDivisionError"
    assert len(msgs[0]["traceback"]) > 0
    assert msgs[-1] == {"type": "execute_reply", "msg_id": "1", "status": "error"}

    msgs2 = kernel.run("2", "1 + 1")
    assert msgs2[0]["text"] == "2"


def test_syntax_error_is_reported_as_error_and_kernel_keeps_running(kernel: KernelClient) -> None:
    msgs = kernel.run("1", "def (:")
    assert msgs[0]["type"] == "error"
    assert msgs[0]["ename"] == "SyntaxError"

    msgs2 = kernel.run("2", "1 + 1")
    assert msgs2[0]["text"] == "2"


def test_keyboard_interrupt_in_user_code_is_reported_not_fatal(kernel: KernelClient) -> None:
    msgs = kernel.run("1", "raise KeyboardInterrupt")
    assert msgs[0]["type"] == "error"
    assert msgs[0]["ename"] == "KeyboardInterrupt"

    msgs2 = kernel.run("2", "1 + 1")
    assert msgs2[0]["text"] == "2"


def test_namespace_survives_a_failed_request(kernel: KernelClient) -> None:
    kernel.run("1", "b = 99")
    kernel.run("2", "1 / 0")
    msgs = kernel.run("3", "b")
    assert msgs[0]["text"] == "99"


# ---------------------------------------------------------------------------
# zoom_request: server-side re-aggregation for the currently displayed figure
# ---------------------------------------------------------------------------


def test_zoom_request_reaggregates_the_last_figure(kernel: KernelClient) -> None:
    kernel.run("1", "plot(list(range(1000)), list(range(1000)))")

    msgs = kernel.zoom("2", [100.0, 200.0])

    fig = next(m for m in msgs if m["type"] == "figure")
    x, _ = _figure_arrays(fig)
    assert x.min() >= 100.0
    assert x.max() <= 200.0
    assert msgs[-1] == {"type": "execute_reply", "msg_id": "2", "status": "ok"}


def test_zoom_request_with_null_range_restores_full_view(kernel: KernelClient) -> None:
    kernel.run("1", "plot(list(range(1000)), list(range(1000)))")
    kernel.zoom("2", [100.0, 200.0])

    msgs = kernel.zoom("3", None)

    fig = next(m for m in msgs if m["type"] == "figure")
    x, _ = _figure_arrays(fig)
    assert x.min() == 0.0
    assert x.max() == 999.0


def test_zoom_request_works_for_scatter_and_histogram(kernel: KernelClient) -> None:
    kernel.run("1", "scatter(list(range(1000)), list(range(1000)))")
    msgs = kernel.zoom("2", [100.0, 200.0])
    fig = next(m for m in msgs if m["type"] == "figure")
    assert fig["kind"] == "scatter"

    kernel.run("3", "histogram(list(range(1000)))")
    msgs = kernel.zoom("4", [100.0, 200.0])
    fig = next(m for m in msgs if m["type"] == "figure")
    assert fig["kind"] == "histogram"


def test_zoom_request_before_any_figure_is_an_error(kernel: KernelClient) -> None:
    msgs = kernel.zoom("1", [0.0, 1.0])
    assert msgs[0]["type"] == "error"
    assert msgs[0]["ename"] == "NoFigure"
    assert msgs[-1]["status"] == "error"


def test_zoom_request_after_a_non_figure_result_still_zooms_prior_figure(
    kernel: KernelClient,
) -> None:
    kernel.run("1", "plot(list(range(1000)), list(range(1000)))")
    kernel.run("2", "1 + 1")  # no new figure -- the plot is still what's on screen

    msgs = kernel.zoom("3", [100.0, 200.0])

    fig = next(m for m in msgs if m["type"] == "figure")
    x, _ = _figure_arrays(fig)
    assert x.min() >= 100.0


def test_zoom_request_kernel_survives_and_a_later_zoom_still_works(kernel: KernelClient) -> None:
    kernel.zoom("1", [0.0, 1.0])  # errors: nothing plotted yet
    kernel.run("2", "plot(list(range(1000)), list(range(1000)))")

    msgs = kernel.zoom("3", [100.0, 200.0])

    fig = next(m for m in msgs if m["type"] == "figure")
    assert fig["kind"] == "line"


# ---------------------------------------------------------------------------
# Contract: SystemExit propagates (host restarts), EOF is a clean shutdown,
# an unrecognized message is a protocol violation -- tested without the
# `kernel` fixture, since these need to observe serve() itself, synchronously.
# ---------------------------------------------------------------------------


def test_system_exit_in_user_code_propagates_out_of_serve() -> None:
    host, kernel_sock = socket.socketpair()
    f = host.makefile("wb")
    f.write((json.dumps({"type": "execute_request", "msg_id": "1", "code": "import sys; sys.exit(3)"}) + "\n").encode())
    f.flush()

    with pytest.raises(SystemExit) as exc_info:
        serve(kernel_sock)
    assert exc_info.value.code == 3

    host.close()


def test_serve_returns_cleanly_on_eof() -> None:
    host, kernel_sock = socket.socketpair()
    host.close()

    serve(kernel_sock)  # must return, not raise

    kernel_sock.close()


def test_serve_raises_on_unknown_message_type() -> None:
    host, kernel_sock = socket.socketpair()
    f = host.makefile("wb")
    f.write((json.dumps({"type": "not_a_real_type"}) + "\n").encode())
    f.flush()

    with pytest.raises(ValueError, match="unknown message type"):
        serve(kernel_sock)

    host.close()


# ---------------------------------------------------------------------------
# main(): argv handling and the real `-m loupe_kernel <socket_path>` path
# ---------------------------------------------------------------------------


def test_main_requires_exactly_one_argument() -> None:
    assert main([]) == 2
    assert main(["a", "b"]) == 2


def test_main_connects_to_the_given_socket_and_serves(tmp_path) -> None:
    sock_path = str(tmp_path / "loupe.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(sock_path)
    listener.listen(1)

    result: dict[str, int] = {}
    thread = threading.Thread(target=lambda: result.__setitem__("code", main([sock_path])), daemon=True)
    thread.start()

    conn, _ = listener.accept()
    f = conn.makefile("rwb")
    f.write((json.dumps({"type": "execute_request", "msg_id": "1", "code": "2 + 2"}) + "\n").encode())
    f.flush()
    assert json.loads(f.readline()) == {"type": "execute_result", "msg_id": "1", "text": "4"}
    assert json.loads(f.readline()) == {"type": "execute_reply", "msg_id": "1", "status": "ok"}

    # makefile() dup()s the fd, so closing conn alone leaves it open and the
    # kernel never sees EOF -- close the file wrapper too.
    f.close()
    conn.close()  # EOF from the kernel's perspective: a clean shutdown
    thread.join(timeout=2)
    assert result["code"] == 0

    listener.close()


def test_repr_failure_is_reported_without_losing_namespace(kernel: KernelClient) -> None:
    messages = kernel.run("repr", "class Broken:\n    def __repr__(self):\n        raise ValueError('bad repr')\nkept = 42\nBroken()")
    assert messages[0]["ename"] == "ValueError"
    assert messages[-1]["status"] == "error"
    assert kernel.run("next", "kept")[0]["text"] == "42"


def test_lazyframe_result_is_a_bounded_preview(kernel: KernelClient) -> None:
    messages = kernel.run("preview", "pl.LazyFrame({'x': range(1000)})")
    result = messages[0]
    assert result["type"] == "data_preview"
    assert result["columns"] == [{"name": "x", "dtype": "Int64"}]
    assert len(result["rows"]) == 20
    assert result["has_more"] is True
    assert messages[-1]["status"] == "ok"


def test_explicit_preview_limit(kernel: KernelClient) -> None:
    result = kernel.run("preview", "preview(pl.LazyFrame({'x': range(1000)}), n=50)")[0]
    assert len(result["rows"]) == 50
    assert result["limit"] == 50


def test_scan_assignment_surfaces_schema(kernel: KernelClient, tmp_path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("x,y\n1,2\n")
    messages = kernel.run("scan", f"df = scan({str(path)!r})")
    assert messages[0]["type"] == "data_schema"
    assert messages[0]["path"] == str(path)
    assert messages[0]["columns"] == [{"name": "x", "dtype": "Int64"}, {"name": "y", "dtype": "Int64"}]
    assert messages[-1]["status"] == "ok"
    assert kernel.run("lazy", "isinstance(df, pl.LazyFrame)")[0]["text"] == "True"


def test_preview_failure_is_caught(kernel: KernelClient) -> None:
    messages = kernel.run("bad", "pl.LazyFrame({'x': ['bad']}).select(pl.col('x').cast(pl.Int64))")
    assert messages[0]["type"] == "error"
    assert messages[-1]["status"] == "error"
    assert kernel.run("next", "42")[0]["text"] == "42"


def test_heatmap_wire_and_execution_error_recovery(kernel):
    messages = kernel.run('h', 'heatmap([2,1], [4,2], [20,10], bins=4)')
    fig = messages[0]
    assert fig['kind'] == 'heatmap'
    assert fig['byte_lengths'] == [8,8,8]
    assert fig['color_range'] == [10,20]
    np.testing.assert_array_equal(np.frombuffer(fig['_raw'], dtype=np.float32), [1,2,2,4,10,20])
    assert kernel.run('bad', 'heatmap([1],[2],[3], cmap="nope")')[-1]['status'] == 'error'
    assert kernel.run('ok', '42')[0]['text'] == '42'
