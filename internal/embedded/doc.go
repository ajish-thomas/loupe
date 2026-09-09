// Package embedded provides the CPython runtime tree that
// scripts/generate-runtime.sh produces, for internal/extract to unpack.
//
// RuntimeTarZst holds a zstd-compressed tar of a complete, pruned CPython
// installation (stdlib plus numpy/polars/matplotlib/fastexcel and
// loupe_kernel itself in site-packages). It comes from exactly one of
// two mutually exclusive files, selected by the loupe_embed build tag:
//
//   - embedded_stub.go (default, no tag): RuntimeTarZst is nil. This is
//     what every plain `go build`/`go test` in this repo uses --
//     internal/kernel falls back to the project's python/.venv (dev
//     mode).
//   - embedded_real.go (-tags loupe_embed): //go:embeds the real
//     runtime.tar.zst that scripts/generate-runtime.sh writes here. This
//     is what a real release build uses to be an actual single,
//     air-gapped binary -- the point of PLAN.md's milestones 1-2.
//
// Building with -tags loupe_embed without having run the generator
// first fails at compile time (go:embed requires the file to exist) --
// intentionally, rather than silently shipping an empty runtime.
package embedded

//go:generate ../../scripts/generate-runtime.sh
