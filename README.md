# loupe

A single-binary, air-gapped Python plotting workbench.

Python has the richest plotting ecosystem there is, but using it means having a
correct Python install with the right libraries on the machine in front of you.
`loupe` removes that requirement: it is **one executable** that carries its own
CPython runtime, its own data and plotting stack (Polars, NumPy, matplotlib),
and its own browser UI. Drop it on any x86-64 Linux box and plot — no Python, no
`pip`, **no network access at any point, including the first run**.

The name is the idea: a jeweller's loupe. Plot a hundred million points, zoom
in, and the detail re-resolves — the server re-aggregates for the new viewport
instead of just stretching the pixels already on screen.

```sh
./loupe                # serves on 127.0.0.1:<port>, opens your browser
```

---

## What it does

- **Plotting-focused REPL in the browser.** A persistent Python session with
  `np`, `pl`, and curated `plot()` / `scatter()` / `histogram()` commands ready
  to use. Output, tracebacks, and data previews render alongside the editor.
- **Lazy ingest.** `scan("data.csv")` returns a Polars `LazyFrame`; your
  `filter` / `select` / `group_by` extend a query plan that only executes when a
  plot or a bounded preview needs it. Large CSVs are streamed once to a session
  Parquet cache so every later slice and zoom is fast.
- **Interactive plots that stay smooth at scale.** A 1920 px chart can only show
  ~1920 distinct x positions, so `loupe` reduces the source to a few thousand
  points with [M4](http://www.vldb.org/pvldb/vol7/p797-jugel.pdf) (a
  provably error-free line-chart aggregation) before anything crosses the wire.
  Pan and zoom happen client-side at 60 fps; when the viewport settles, the
  kernel re-aggregates against the original source. Browser memory stays
  proportional to pixels, not to row count.
- **Survives your code.** User code that infinite-loops, segfaults inside NumPy,
  calls `sys.exit()`, or exhausts memory does not take the app down. **Stop and
  reset kernel** kills and restarts the interpreter; a crash auto-restarts it.

## Why it is built this way

| Decision | Reason |
|---|---|
| **Go host, Python as a subprocess** | Process isolation. A REPL runs hostile code; the supervisor has to outlive an interpreter crash and restart it. Rules out cgo/libpython and frozen-bundle approaches. |
| **Ship a real CPython tree, not a frozen bundle** | Dynamic imports, `help()`, and full tracebacks all work. A wheel can be side-loaded later without a rebuild. |
| **Browser UI on `127.0.0.1`, not a native webview** | Native webviews need WebKitGTK installed on Linux — a system dependency that defeats the point. |
| **Polars only — no pandas, no PyArrow, no seaborn** | One Arrow-native engine with lazy evaluation, predicate/projection pushdown, and larger-than-RAM streaming covers every job. Removes the single largest wheel from the bundle. |
| **Plot data travels as binary float32, never JSON** | 1M float64 points as JSON is ~20 MB of text and hundreds of ms in `JSON.parse`. float32 halves the bytes and no display can resolve the difference. |
| **uPlot for the built-in charts** | ~34 ms initial render, ~48 KB. A second renderer is added per chart type only when one is actually needed. |
| **Bundle glibc, invoke our own dynamic linker** | The extracted CPython resolves against shipped `libc`/`libm`/`libstdc++`/… instead of the host's, via `ld-linux-x86-64.so.2 --library-path <bundled>`. Not static linking (Python extensions need `dlopen`); the only remaining floor is a Linux ≥ 3.2.0 kernel. |

## Architecture

```
loupe (single Go binary)
├── embedded FS: CPython tree + site-packages + bundled glibc + vendored web assets
├── extractor      → $XDG_CACHE_HOME/loupe/<content-hash>/   (first run only)
├── kernel manager → supervises `python3 -m loupe_kernel` (restart on crash, kill on Stop)
├── HTTP server    → serves the vendored UI on 127.0.0.1
└── WebSocket hub  → browser ⟷ host ⟷ kernel
```

- **Kernel protocol:** newline-delimited JSON over a Unix domain socket —
  Jupyter-*shaped* (`execute_request`, `stream`, `execute_result`, `error`) but
  deliberately not the Jupyter/pyzmq protocol. The user's `stdout`/`stderr` are
  captured into `stream` messages; the socket itself is never on stdio.
- **Figures:** one JSON header naming each array's byte length, immediately
  followed by the raw `float32` bytes (x then y), dropped zero-copy into uPlot.
- **Zoom:** on zoom-settle the browser sends a `zoom_request` with the new
  x-range; the kernel replays the current figure's reduction for that range
  against the source. Because the kernel is a local process on a socket, this
  round trip is sub-millisecond — full fidelity at every zoom level.
- **Laziness is load-bearing.** A `filter()` written before `plot()` is pushed
  into the file scan (Polars skips Parquet row groups outright). Any code path
  that calls `.collect()` on an uncapped frame before `plot()` is a bug — it
  still works, just slowly and at ~10x the memory.

## Building

### Dev mode (default)

The default build shells out to `python/.venv` — no runtime generator needed for
day-to-day work.

```sh
# Python side first, so the venv exists
cd python && uv sync && uv run pytest && cd ..

# Go side, from the repo root
go build ./...
./loupe                 # serves using python/.venv

make test               # Go race tests + pytest + a real headless-Chromium smoke test
make vet
```

The browser test needs Node 22+ and a locally installed Chromium (`CHROMIUM`
overrides the executable). Individual targets: `make test-go`, `make
test-python`, `make test-web`.

### A real single, air-gapped binary

```sh
./scripts/generate-runtime.sh                          # downloads + assembles internal/embedded/runtime.tar.zst (~90 MB, needs network)
CGO_ENABLED=0 go build -tags loupe_embed -o loupe .
./loupe                                                # extracts to $XDG_CACHE_HOME/loupe/ on first run; no network, ever, after that
```

- `scripts/generate-runtime.sh` is pinned to a specific
  [`python-build-standalone`](https://github.com/astral-sh/python-build-standalone)
  release and reads exact dependency versions from `python/pyproject.toml` via
  `uv export`, so the venv and the embedded runtime never drift. Its output is
  gitignored and regenerated on demand, not committed.
- `CGO_ENABLED=0` matters: at Go's default (`1`, auto-detected when a C compiler
  is on `PATH`) the `net` package links a cgo DNS resolver against
  `libc`/`libresolv` for no reason this program needs — it only ever binds a
  literal `127.0.0.1`. With cgo off the Go binary is genuinely static.
- `go test -tags loupe_embed ./...` after generating verifies the real embedded
  runtime extracts and runs (these tests are inapplicable in the default build,
  and vice versa — see `internal/embedded/doc.go`).

## Using it

```python
df = scan("measurements.csv")          # reports the inferred schema; returns a LazyFrame
preview(df.filter(pl.col("x") > 0), n=20)
plot("x", "y", data=df)                 # M4 reduction is appended to the same lazy plan

# Correct a bad CSV inference without touching the original file:
df = scan("measurements.csv", schema_overrides={"x": pl.Float64})
```

- **Built-in commands:** `plot`, `scatter`, `histogram` — each takes either
  column names + `data=` (a LazyFrame/DataFrame) or array-likes directly.
  `scan`, `preview`, `np`, `pl` are in scope; `api` / `engine` / `ingest` are
  there as an escape hatch.
- **Examples menu** in the Input pane fills the editor with a starter snippet
  you then run yourself (Ctrl/⌘+Enter, or the Run button).
- **Ingest:** CSV, Parquet, NDJSON/JSONL, IPC/Arrow. CSVs ≥ 64 MiB stream to a
  session Parquet cache keyed by source metadata and parsing options; the cache
  is removed when the kernel resets or shuts down. `cache_threshold`,
  `cache_dir`, `infer_schema_length` (default 10,000), and `try_parse_dates`
  (default `True`) are `scan()` arguments.
- **Previews:** a trailing LazyFrame/DataFrame auto-displays up to 20 rows;
  `preview(df, n=…)` allows 1–100 and reports whether more exist. A preview
  collects `head(n+1)` only — though a user query such as a global sort can
  still make that scan the whole source.
- **Stop and reset kernel** stops stuck Python or native code and clears the
  session's variables and figures. Disconnecting a tab mid-execution also
  cancels its request. All browser tabs share one kernel.

## Performance

Targets the design is measured against (numbers from a 1e8-row, 1.2 GB Parquet
file through the full Go → kernel → engine stack on the dev machine):

| Metric | Target | Measured |
|---|---|---|
| Time to first plot, cold (incl. extraction) | < 2 s | ~1.3 s |
| Time to first plot, warm session | < 50 ms | 142 ms warm start; sub-50 ms per plot |
| Pan / zoom | 60 fps, zero server round-trips | ✅ client-side |
| Re-aggregation on zoom settle, narrow zoom | < 150 ms | 18–40 ms (pushdown skips most of the file) |
| Re-aggregation, full range (fully zoomed out) | < 150 ms | 1.5–1.8 s ⚠️ |
| Browser memory | O(pixels), independent of source size | ✅ |
| Kernel peak RSS, full-range 1e8-row query | well under 800 MB/column | ~1.0 GB ⚠️ |

Full-range views over 1e8 rows are the current weak spot: a batch-and-merge
streaming reduction brought kernel RSS down ~6–7× (from ~6–7 GB) and the query
still completes, but it is over target on both time and memory. Narrow zooms —
the common interactive case — are well within target because Parquet pushdown
does the work.

## Status and limitations

- **Linux x86-64 only.** No macOS, no Windows. A musl-only host (Alpine) is
  untested either way.
- **Tier 2 (paste arbitrary matplotlib code) is not built yet.** The built-in
  `plot`/`scatter`/`histogram` commands (tier 1) are. When tier 2 lands it will
  run verbatim matplotlib over NumPy/Polars via matplotlib's own WebAgg backend
  — not verbatim *any* script: no pandas or seaborn is bundled, and most pasted
  third-party code is pandas/seaborn-shaped. That trade is reversible at build
  time (one line in the generator's requirements), not an architectural
  commitment.
- **Large-CSV ingest RSS** peaks around 1.6 GiB on the dev machine for a 1e8-row
  conversion — above the few-hundred-MB goal.
- **Shared kernel state.** All tabs share one interpreter; Stop resets that
  shared namespace. Process isolation does not itself impose a memory quota.
- **`noexec` cache dir** breaks extraction and does not yet produce a clear
  error.

## Layout

```
main.go, internal/          Go host: extraction, subprocess supervision, HTTP/WS server
  internal/embedded/        //go:embed of the generated runtime (two build variants)
  internal/extract/         content-hashed extraction, pure-Go zstd, skip-if-cached
  internal/kernel/          subprocess supervision, socket protocol, restart/interrupt
  internal/server/          HTTP + WebSocket hub, static asset serving
python/src/loupe_kernel/    the embedded Python side (uv project)
  __init__.py               REPL loop, stdout/stderr capture, JSON socket protocol
  ingest.py                 lazy scans, streamed CSV→Parquet cache, capped previews
  api.py                    tier-1 curated commands: plot / scatter / histogram
  engine.py                 M4 viewport aggregation, batch-and-merge streaming
web/                        embedded UI + vendored uPlot; headless-Chromium smoke test
scripts/generate-runtime.sh assembles the embedded CPython runtime + bundled glibc
scripts/benchmark-ingest.py opt-in 100M-row CSV ingest / RSS check
AGENTS.md                   architecture, idioms, and test guidance for contributors
```

## License

MIT — see [LICENSE](LICENSE).

`loupe`'s own source is MIT. A release binary additionally bundles third-party
runtime components, unmodified, under their own licenses: CPython (PSF), Polars,
NumPy, matplotlib, fastexcel, and uPlot (all permissive), plus — in the
`-tags loupe_embed` build — glibc (LGPL-2.1), libstdc++ / libgcc_s (GPL-3.0 with
the GCC Runtime Library Exception, which permits this), and zlib. Redistributing
the binary carries those components' obligations; the MIT license covers this
repository's code.
