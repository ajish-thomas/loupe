// Package web contains the UI assets shipped in every Plotter executable.
package web

import "embed"

// Assets includes all browser dependencies so serving the UI never depends on
// the working directory or a runtime network download.
//
//go:embed index.html app.js vendor
var Assets embed.FS
