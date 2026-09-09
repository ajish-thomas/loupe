// Package version holds the loupe build version.
package version

// Version is the loupe release version. It is overwritten at build time
// via -ldflags "-X .../internal/version.Version=...".
var Version = "0.0.0-dev"

// String returns the version string.
func String() string {
	return Version
}
