package version

import "testing"

func TestString(t *testing.T) {
	got := String()
	if got == "" {
		t.Fatal("String() returned an empty string")
	}
	if got != Version {
		t.Fatalf("String() = %q, want %q", got, Version)
	}
}
