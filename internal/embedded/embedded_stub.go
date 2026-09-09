//go:build !loupe_embed

package embedded

// RuntimeTarZst is nil in the default (dev-mode) build -- see doc.go.
var RuntimeTarZst []byte
