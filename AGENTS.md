# Project guidance

Read `README.md` for the architecture, the design rationale, measured results,
and the current limitations. Fix correctness bugs before adding scope. Keep the
README honest when behavior changes; distinguish implemented, tested, and
measured claims. Ask when requirements conflict or a product decision is needed.

## Architecture

- Go (`main.go`, `internal/`) owns extraction, subprocess supervision, and the
  loopback HTTP/WebSocket server. Python (`python/src/loupe_kernel/`) owns the
  persistent REPL, Polars ingest, and plotting/aggregation.
- Keep Python out of process. Do not introduce cgo/libpython or runtime downloads.
  Release builds must include every runtime and web asset and work offline.
- Control messages use newline-delimited JSON on a Unix socket, never stdio.
  Plot arrays travel as binary float32 payloads. Keep both protocol hops aligned.
- Keep ingest and user transformations lazy through aggregation. Collect only
  bounded previews or reduced results. Stream CSV cache writes to Parquet.
- Use Polars and NumPy; do not add pandas, seaborn, or PyArrow without a design
  decision. Defer matplotlib imports until tier 2 needs them.

## Implementation style

- Write idiomatic Go: gofmt, small cohesive packages, explicit error handling
  with wrapped causes, context-aware blocking operations, and clear ownership
  of goroutines, locks, subprocesses, and sockets. Avoid locks across operations
  needed to interrupt or shut down the kernel. Reap every started process.
- Write idiomatic Python: type annotations on public interfaces, pathlib,
  context managers, focused functions, and specific exceptions. Preserve
  tracebacks and keep execution/display failures inside the REPL error boundary.
- Follow existing browser JavaScript conventions. Use safe DOM text rendering,
  vendored assets, and same-origin communication. Keep protocol/UI behavior tested.
- Use the existing Go modules and Python uv lockfile. Do not edit generated
  runtime archives, vendored libraries, or virtual environments as source fixes.

## Testing

Use Go's standard `testing` framework and pytest for Python. Add meaningful
regressions for bugs and tests for new public behavior; cover invalid input,
cancellation, cleanup, and failure recovery as well as success. Prefer real
local socket/subprocess integration tests for protocol and lifecycle behavior.
Use pytest fixtures, parametrization, and temporary directories for data tests.
Keep unit tests small; make expensive performance checks explicit and repeatable.

Development setup: `cd python && uv sync --locked`. From the repository root:

```sh
python/.venv/bin/python -m pytest python/tests
go test -race ./...
go vet ./...
node --test web/browser.test.mjs
```

`make test` runs Go race tests, pytest, and the Chromium smoke test (Node 22+
and a local Chromium executable required; set `CHROMIUM` to override its path).
Run both complete Go/Python suites for changes spanning the Go/Python boundary. Ensure
the dev venv exists so integration tests do not silently skip. If a sandbox
blocks local sockets or build caches, report that separately from test failures.
After runtime generation, also run `go test -tags loupe_embed ./...` and build
with `CGO_ENABLED=0 go build -tags loupe_embed`. A pre-existing runtime archive
does not contain new Python changes; regenerate it before release validation.
Record benchmark inputs, time, peak RSS, and any unmet performance targets.
Never call a performance claim verified solely because its unit tests pass.
