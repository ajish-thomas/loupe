package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"testing"
)

func TestFileListing(t *testing.T) {
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "folder"), 0700); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"a.csv", "B.PARQUET", "ignored.txt", "x.jsonl", "quote\".ipc"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte("abc"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink(filepath.Join(root, "missing"), filepath.Join(root, "broken.csv")); err != nil {
		t.Fatal(err)
	}
	server := New("", nil)
	recorder := httptest.NewRecorder()
	server.ServeHTTP(recorder, httptest.NewRequest("GET", "/fs?path="+url.QueryEscape(root), nil))
	if recorder.Code != http.StatusOK {
		t.Fatalf("%d: %s", recorder.Code, recorder.Body.String())
	}
	var listing directoryListing
	if err := json.Unmarshal(recorder.Body.Bytes(), &listing); err != nil {
		t.Fatal(err)
	}
	if listing.Path != root || listing.Parent != filepath.Dir(root) {
		t.Fatalf("unexpected paths: %+v", listing)
	}
	expected := []string{"folder", "B.PARQUET", "a.csv", "quote\".ipc", "x.jsonl"}
	if len(listing.Entries) != len(expected) {
		t.Fatalf("unexpected entries: %+v", listing.Entries)
	}
	for i, name := range expected {
		entry := listing.Entries[i]
		if entry.Name != name || entry.IsDir != (i == 0) || (i != 0 && entry.Size != 3) {
			t.Fatalf("unexpected entry: %+v", entry)
		}
	}
}

func TestFileListingErrorsAndDefaults(t *testing.T) {
	root := t.TempDir()
	file := filepath.Join(root, "file.csv")
	if err := os.WriteFile(file, nil, 0600); err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name, path string
		status     int
	}{
		{"relative", "relative", 400}, {"missing", filepath.Join(root, "absent"), 404},
		{"file", file, 400}, {"empty directory", filepath.Join(root, "."), 200}, {"default cwd", "", 200},
	} {
		t.Run(test.name, func(t *testing.T) {
			recorder := httptest.NewRecorder()
			New("", nil).ServeHTTP(recorder, httptest.NewRequest("GET", "/fs?path="+url.QueryEscape(test.path), nil))
			if recorder.Code != test.status {
				t.Fatalf("%d: %s", recorder.Code, recorder.Body.String())
			}
			var result map[string]any
			if err := json.Unmarshal(recorder.Body.Bytes(), &result); err != nil {
				t.Fatal(err)
			}
			if test.status != 200 && result["error"] == nil {
				t.Fatalf("missing error: %v", result)
			}
		})
	}
	recorder := httptest.NewRecorder()
	New("", nil).ServeHTTP(recorder, httptest.NewRequest("POST", "/fs", nil))
	if recorder.Code != 405 {
		t.Fatalf("POST status: %d", recorder.Code)
	}
}
