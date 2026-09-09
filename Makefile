.PHONY: test test-go test-python test-web vet test-embedded benchmark-ingest release test-release
.DEFAULT_GOAL := test

internal/embedded/runtime.tar.zst: $(wildcard python/src/loupe_kernel/*.py) python/uv.lock python/pyproject.toml scripts/generate-runtime.sh
	scripts/generate-runtime.sh

release: internal/embedded/runtime.tar.zst
	CGO_ENABLED=0 go build -tags loupe_embed -o loupe .

test-release: release test-embedded
	LOUPE_TEST_BINARY=./loupe node --test web/browser.test.mjs

test: test-go test-python test-web

test-go:
	go test -race ./...

test-python:
	python/.venv/bin/python -m pytest python/tests

test-web:
	node --test web/browser.test.mjs

vet:
	go vet ./...

test-embedded: internal/embedded/runtime.tar.zst
	go test -tags loupe_embed ./...

benchmark-ingest:
	python/.venv/bin/python scripts/benchmark-ingest.py
