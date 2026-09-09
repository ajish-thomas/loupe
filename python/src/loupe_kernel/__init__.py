"""loupe_kernel: the embedded Python side of the loupe workbench.

Per PLAN.md, "Architecture": the Go host supervises this as a
`python3 -m loupe_kernel <socket_path>` subprocess and speaks a
newline-delimited JSON protocol over a Unix domain socket -- deliberately
not the Jupyter/pyzmq protocol, but Jupyter-*shaped* (`execute_request`,
`stream`, `execute_result`, `error`) so migrating later stays open.

The Go host owns the socket (creates, binds, listens); this process
connects to it as a client. That keeps lifecycle and cleanup on the
supervisor's side, matching `internal/kernel`'s role of restarting this
process on crash.

Two things the protocol is deliberately NOT:

- **Not on stdio.** PLAN.md: "do not put the protocol on stdio -- user
  code calling print() would corrupt the stream." `sys.stdout`/`sys.stderr`
  are redirected to `stream` messages on the *same* socket only for the
  duration of executing one request; the process's real stdio is left
  alone.
- **Not JSON for plot data.** PLAN.md: "Plot data never travels as JSON
  ... a small JSON header followed by a Float32Array payload." A `Figure`
  is sent as one JSON header line naming the byte length of each array,
  immediately followed by that many raw bytes -- `x` then `y` -- with no
  further framing, since the header already says how much to read.

See ../../PLAN.md for the rest of the architecture this package implements.
"""

from __future__ import annotations

import ast
import contextlib
import json
import socket
import sys
import traceback
from typing import IO, Any

import numpy as np
import polars as pl

from loupe_kernel import api, engine, ingest

__version__ = "0.1.0"

# What sys.exit() in user code is allowed to do: propagate all the way out
# of serve() and end this process. PLAN.md, "Decisions and why": "User code
# in a REPL will ... call sys.exit() ... The host must survive that and
# restart the interpreter." So SystemExit is deliberately NOT in this tuple
# -- only ordinary errors and Ctrl-C are caught and reported without
# ending the process.
_CAUGHT = (Exception, KeyboardInterrupt)


def main(argv: list[str]) -> int:
    """Entry point for `python3 -m loupe_kernel <socket_path>`."""
    if len(argv) != 1:
        print("usage: loupe_kernel <socket_path>", file=sys.stderr)
        return 2

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(argv[0])
    try:
        serve(sock)
    finally:
        sock.close()
    return 0


def serve(sock: socket.socket) -> None:
    """Run the REPL loop against an already-connected socket.

    Reads one request at a time and executes it against a namespace that
    persists across requests, like any REPL. Returns cleanly on EOF (the
    host closing the socket is a normal shutdown). A `SystemExit` raised
    by user code is deliberately left to propagate -- see the module
    docstring.

    Two request types: `execute_request` runs code, as always. A
    `zoom_request` -- `{"x_range": [min, max] | null}` -- is PLAN.md's
    "zoom-settle re-aggregation": it re-runs the *currently displayed*
    figure's reduction for a new viewport by calling its `replot` closure
    (`api.Figure.replot`), rather than re-executing any code, so a browser
    pan/zoom re-aggregates against the original source instead of the
    already-reduced points on screen. `last_figure` tracks whichever
    Figure was most recently auto-displayed, across both request types.
    """
    f = sock.makefile("rwb")
    namespace = _new_namespace()
    last_figure: api.Figure | None = None

    try:
        while True:
            request = _recv_json(f)
            if request is None:
                return  # EOF: the host closed the socket.

            msg_type = request.get("type")
            msg_id = request.get("msg_id")

            if msg_type == "execute_request":
                value = _execute(f, msg_id, request["code"], namespace)
                if isinstance(value, api.Figure):
                    last_figure = value
            elif msg_type == "zoom_request":
                last_figure = _zoom(f, msg_id, request.get("x_range"), last_figure)
            else:
                raise ValueError(f"unknown message type {msg_type!r}")
    finally:
        f.close()


def _new_namespace() -> dict[str, Any]:
    """The REPL's persistent globals: plotting-focused, per PLAN.md's
    framing of this as "a Python interpreter focused on plotting" --
    the tier-1 commands and `np`/`pl` are available unqualified, with the
    full `api`/`engine`/`ingest` modules also present as an escape hatch.
    """
    return {
        "__name__": "__main__",
        "np": np,
        "pl": pl,
        "api": api,
        "engine": engine,
        "ingest": ingest,
        "plot": api.plot,
        "scatter": api.scatter,
        "histogram": api.histogram,
        "scan": ingest.scan,
        "preview": ingest.preview,
    }


def _execute(f: IO[bytes], msg_id: str | None, code: str, namespace: dict[str, Any]) -> Any:
    """Run one request's code, send its outcome messages, and return
    whatever value was displayed (a `Figure`, some other repr'd value, or
    None) so `serve` can track it as the figure a later zoom applies to.

    Mirrors a REPL's "auto-print the last expression" behaviour (the same
    ast-splitting `code.InteractiveConsole`/IPython use): if `code`'s last
    statement is a bare expression, its value -- not just its side effects
    -- is what gets reported. A `Figure` value is sent as binary; any other
    non-None value is sent as its repr.
    """
    stdout = _Stream(f, msg_id, "stdout")
    stderr = _Stream(f, msg_id, "stderr")

    try:
        exec_code, eval_code = _compile_repl(code)
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            ingest.report_scans(lambda path, schema: _send_json(f, {
                "type": "data_schema", "msg_id": msg_id, "path": str(path),
                "columns": _schema_columns(schema),
            })),
        ):
            exec(exec_code, namespace)
            value = eval(eval_code, namespace) if eval_code is not None else None
            if isinstance(value, api.Figure):
                _send_figure(f, msg_id, value)
            elif isinstance(value, (pl.LazyFrame, pl.DataFrame, ingest.Preview)):
                result = value if isinstance(value, ingest.Preview) else ingest.preview(value)
                _send_json(f, {
                    "type": "data_preview", "msg_id": msg_id,
                    "columns": _schema_columns(result.data.schema),
                    "rows": [[str(cell) if cell is not None else "null" for cell in row]
                             for row in result.data.iter_rows()],
                    "limit": result.limit, "has_more": result.has_more,
                })
            elif value is not None:
                _send_json(f, {"type": "execute_result", "msg_id": msg_id, "text": repr(value)})

    except _CAUGHT as exc:
        _send_error(f, msg_id, type(exc).__name__, str(exc), exc)
        _send_json(f, {"type": "execute_reply", "msg_id": msg_id, "status": "error"})
        return None

    _send_json(f, {"type": "execute_reply", "msg_id": msg_id, "status": "ok"})
    return value


def _zoom(
    f: IO[bytes],
    msg_id: str | None,
    x_range: list[float] | None,
    last_figure: api.Figure | None,
) -> api.Figure | None:
    """Handle one zoom_request: re-run `last_figure`'s reduction for a new
    viewport. Returns the figure now on screen -- the new one on success,
    or the unchanged `last_figure` if the zoom could not be served, so a
    failed zoom leaves the next zoom attempt with something to retry.
    """
    if last_figure is None or last_figure.replot is None:
        _send_error(f, msg_id, "NoFigure", "nothing plotted yet to zoom")
        _send_json(f, {"type": "execute_reply", "msg_id": msg_id, "status": "error"})
        return last_figure

    try:
        new_range = tuple(x_range) if x_range is not None else None
        figure = last_figure.replot(new_range)
    except _CAUGHT as exc:
        _send_error(f, msg_id, type(exc).__name__, str(exc), exc)
        _send_json(f, {"type": "execute_reply", "msg_id": msg_id, "status": "error"})
        return last_figure

    _send_figure(f, msg_id, figure)
    _send_json(f, {"type": "execute_reply", "msg_id": msg_id, "status": "ok"})
    return figure


def _compile_repl(code: str, filename: str = "<loupe>"):
    """Split `code` into an exec part and an optional eval part for its
    trailing expression, if it has one. Returns `(exec_code, eval_code)`,
    with `eval_code` None when the last statement isn't a bare expression.
    """
    tree = ast.parse(code, filename, mode="exec")
    eval_code = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = tree.body.pop()
        eval_code = compile(ast.Expression(last.value), filename, "eval")
    exec_code = compile(tree, filename, "exec")
    return exec_code, eval_code


class _Stream:
    """A file-like object that turns writes into `stream` messages, for
    `contextlib.redirect_stdout`/`redirect_stderr` to point at. Empty
    writes (print() often issues one for its default end/sep handling)
    are not worth a message and are dropped.
    """

    def __init__(self, f: IO[bytes], msg_id: str | None, name: str) -> None:
        self._f = f
        self._msg_id = msg_id
        self._name = name

    def write(self, text: str) -> int:
        if text:
            _send_json(self._f, {"type": "stream", "msg_id": self._msg_id, "name": self._name, "text": text})
        return len(text)

    def flush(self) -> None:
        self._f.flush()


def _send_error(
    f: IO[bytes], msg_id: str | None, ename: str, evalue: str, exc: BaseException | None = None
) -> None:
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__) if exc is not None else []
    _send_json(f, {"type": "error", "msg_id": msg_id, "ename": ename, "evalue": evalue, "traceback": tb})


def _send_json(f: IO[bytes], msg: dict[str, Any]) -> None:
    f.write(json.dumps(msg).encode("utf-8"))
    f.write(b"\n")
    f.flush()


def _recv_json(f: IO[bytes]) -> dict[str, Any] | None:
    line = f.readline()
    if not line:
        return None
    return json.loads(line)


def _send_figure(f: IO[bytes], msg_id: str | None, fig: api.Figure) -> None:
    """Send a Figure as PLAN.md describes: one JSON header naming each
    array's byte length, then the raw float32 bytes back to back -- x,
    then y -- with no further framing needed.
    """
    x = np.ascontiguousarray(fig.x, dtype=np.float32)
    y = np.ascontiguousarray(fig.y, dtype=np.float32)
    _send_json(
        f,
        {
            "type": "figure",
            "msg_id": msg_id,
            "kind": fig.kind,
            "title": fig.title,
            "dtype": "float32",
            "byte_lengths": [x.nbytes, y.nbytes],
        },
    )
    f.write(x.tobytes())
    f.write(y.tobytes())
    f.flush()


def _schema_columns(schema: pl.Schema) -> list[dict[str, str]]:
    return [{"name": name, "dtype": str(dtype)} for name, dtype in schema.items()]
