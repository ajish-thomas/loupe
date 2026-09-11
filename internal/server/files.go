package server

import (
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

type fileEntry struct {
	Name  string `json:"name"`
	IsDir bool   `json:"is_dir"`
	Size  int64  `json:"size"`
}

type directoryListing struct {
	Path    string      `json:"path"`
	Parent  string      `json:"parent"`
	Entries []fileEntry `json:"entries"`
}

func (s *Server) handleFS(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	fail := func(status int, message string) {
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": message})
	}
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		fail(http.StatusMethodNotAllowed, "Use GET to list a directory")
		return
	}
	path := r.URL.Query().Get("path")
	if path == "" {
		var err error
		path, err = os.Getwd()
		if err != nil {
			fail(http.StatusInternalServerError, "Cannot locate the working directory")
			return
		}
	}
	path = filepath.Clean(path)
	if !filepath.IsAbs(path) {
		fail(http.StatusBadRequest, "Path must be absolute")
		return
	}
	info, err := os.Stat(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			fail(http.StatusNotFound, "Directory not found")
		} else {
			fail(http.StatusForbidden, "Cannot access directory")
		}
		return
	}
	if !info.IsDir() {
		fail(http.StatusBadRequest, "Path must be a directory")
		return
	}
	entries, err := os.ReadDir(path)
	if err != nil {
		fail(http.StatusForbidden, "Cannot read directory")
		return
	}
	listing := directoryListing{Path: path, Parent: filepath.Dir(path), Entries: []fileEntry{}}
	for _, entry := range entries {
		if r.Context().Err() != nil {
			return
		}
		info, err := os.Stat(filepath.Join(path, entry.Name()))
		if err != nil {
			continue
		} // vanished entries and broken symlinks
		if !info.IsDir() {
			if !info.Mode().IsRegular() {
				continue
			}
			switch strings.ToLower(filepath.Ext(entry.Name())) {
			case ".csv", ".parquet", ".ndjson", ".jsonl", ".arrow", ".ipc":
			default:
				continue
			}
		}
		listing.Entries = append(listing.Entries, fileEntry{entry.Name(), info.IsDir(), info.Size()})
	}
	sort.Slice(listing.Entries, func(i, j int) bool {
		a, b := listing.Entries[i], listing.Entries[j]
		if a.IsDir != b.IsDir {
			return a.IsDir
		}
		return a.Name < b.Name
	})
	_ = json.NewEncoder(w).Encode(listing)
}
