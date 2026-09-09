//go:build loupe_embed

package embedded

import _ "embed"

// RuntimeTarZst is produced by scripts/generate-runtime.sh -- see doc.go.
//
//go:embed runtime.tar.zst
var RuntimeTarZst []byte
