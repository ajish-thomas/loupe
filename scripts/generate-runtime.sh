#!/usr/bin/env bash
# Assembles the embedded Python runtime: a pruned, stripped CPython plus
# the runtime dependencies from python/pyproject.toml, plus a bundled
# glibc (see "Bundled glibc" below), precompiled and compressed into
# internal/embedded/runtime.tar.zst for internal/embedded's loupe_embed
# build to //go:embed.
#
# Only this script needs network access, and only at build time -- see
# PLAN.md's "Build-time generator". It shells out to system tools (curl,
# tar, strip, zstd, uv, bsdtar) freely, since it never runs on an end
# user's machine; the runtime code that later decompresses this file
# (internal/extract) uses a pure-Go zstd decoder precisely so the shipped
# binary has no such external-tool dependency.
#
# Usage: scripts/generate-runtime.sh
set -euo pipefail

# Pinned python-build-standalone release (PLAN.md, "Build-time
# generator", step 1). Bump deliberately, not silently: re-run this
# script and re-check the resulting size/import-time numbers against
# PLAN.md's milestone-1 budget whenever this changes.
PBS_RELEASE="20260901"
PBS_PYTHON_VERSION="3.13.15"
PBS_ASSET="cpython-${PBS_PYTHON_VERSION}+${PBS_RELEASE}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_ASSET}"
PBS_SHA256="8a689a077337bea6d1c4bc0b7df1d52fcaa28f5f67e50df8bf417c1e3f9d8874"
PY_MINOR="3.13" # must match the release above; used for the site-packages path

# Bundled glibc (+ libgcc_s/libstdc++/libz, which numpy/matplotlib's
# native extensions also need -- see the comment above BUNDLE_LIBS below).
# Pinned to AlmaLinux 8 BaseOS packages -- the same glibc 2.28 floor as
# the manylinux_2_28 wheels polars/numpy/matplotlib/fastexcel already
# publish against, so this doesn't raise the compatibility floor those
# wheels already impose. AlmaLinux 8's glibc itself targets Linux kernel
# >= 3.2.0 (confirmed via `file` on the extracted libc.so.6) -- a very
# old, broad floor. Verified end to end (LD_DEBUG showing zero fallback
# to host libraries, full numpy/polars/matplotlib/fastexcel import, and a
# real loupe_kernel socket round trip) before pinning these.
ALMA_BASE_URL="https://repo.almalinux.org/almalinux/8/BaseOS/x86_64/os/Packages"
declare -A ALMA_PACKAGES=(
	[glibc-2.28-251.el8_10.40.x86_64.rpm]=d7a83dee4d25476eaf4a38be8894e916c79620e20cb7bb955a75ec71069f7e54
	[libgcc-8.5.0-28.el8_10.alma.1.x86_64.rpm]=629a08266f7c1397c00d7c32c0ac6d110fe56e993dcf94024559aa28a570f18d
	[libstdc++-8.5.0-28.el8_10.alma.1.x86_64.rpm]=0302b9006d719a31dd51aeaf31fec03d70aad5c318674537bd80c49f9174b066
	[zlib-1.2.11-25.el8.x86_64.rpm]=ab40e85f180a6d38ce0291408b7ef5c422250f53cb26c44399ef1a4e8622778e
)
# Real filenames inside those RPMs (they use the unversioned SONAME as a
# symlink to a versioned real file; both must be bundled). Found by
# `ldd`-ing every .so in a built tree and noting what resolved outside
# /lib*/lib{c,m,pthread,dl,rt,util}* -- i.e. this list is not guessed, it
# is exactly what numpy/matplotlib's native extensions were observed to
# need beyond core glibc. libgfortran/libquadmath are NOT here: numpy
# bundles those itself (in numpy.libs/), so they never touch this bundle.
BUNDLE_LIBS=(
	usr/lib64/ld-linux-x86-64.so.2 usr/lib64/ld-2.28.so
	usr/lib64/libc.so.6 usr/lib64/libc-2.28.so
	usr/lib64/libm.so.6 usr/lib64/libm-2.28.so
	usr/lib64/libpthread.so.0 usr/lib64/libpthread-2.28.so
	usr/lib64/libdl.so.2 usr/lib64/libdl-2.28.so
	usr/lib64/librt.so.1 usr/lib64/librt-2.28.so
	usr/lib64/libutil.so.1 usr/lib64/libutil-2.28.so
)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT="$REPO_ROOT/internal/embedded/runtime.tar.zst"

for tool in curl tar strip zstd sha256sum uv bsdtar; do
	command -v "$tool" >/dev/null || { echo "generate-runtime: missing required tool: $tool" >&2; exit 1; }
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
TREE="$WORK/python"
GLIBC="$WORK/glibc"

echo "==> downloading $PBS_ASSET"
curl -fSL -o "$WORK/cpython.tar.gz" "$PBS_URL"
echo "$PBS_SHA256  $WORK/cpython.tar.gz" | sha256sum -c -

echo "==> extracting standalone CPython"
mkdir -p "$TREE"
tar xzf "$WORK/cpython.tar.gz" -C "$WORK"
PY="$TREE/bin/python3"
STDLIB="$TREE/lib/python${PY_MINOR}"
SITE_PACKAGES="$STDLIB/site-packages"

echo "==> resolving pinned runtime dependencies from python/pyproject.toml"
( cd "$REPO_ROOT/python" && uv export --no-hashes --no-dev --format requirements-txt ) \
	| grep -v '^-e ' >"$WORK/requirements.txt"
cat "$WORK/requirements.txt"

echo "==> installing runtime dependencies into the standalone tree"
# --target installs alongside the bundled pip, not instead of it; pip
# itself is pruned below, after it's done its job.
"$PY" -m pip install --no-cache-dir --disable-pip-version-check \
	--target "$SITE_PACKAGES" -r "$WORK/requirements.txt"

echo "==> installing loupe_kernel itself"
cp -r "$REPO_ROOT/python/src/loupe_kernel" "$SITE_PACKAGES/"

echo "==> pruning: stdlib GUI/bootstrap modules this project never uses"
rm -rf "$STDLIB"/{idlelib,tkinter,turtledemo,ensurepip,lib2to3}
rm -f "$STDLIB/turtle.py"

echo "==> pruning: bundled pip (no runtime installs happen; packages are baked in)"
rm -rf "$SITE_PACKAGES"/pip "$SITE_PACKAGES"/pip-*.dist-info "$SITE_PACKAGES"/README.txt

echo "==> pruning: third-party test suites and sample data"
find "$SITE_PACKAGES" -type d \( -name tests -o -name testing -o -name test \) -prune -exec rm -rf {} +
rm -rf "$SITE_PACKAGES/matplotlib/mpl-data/sample_data"

echo "==> stripping native extensions (--strip-unneeded: safe, debug info only)"
find "$TREE" -name "*.so" -print0 | xargs -0 --no-run-if-empty strip --strip-unneeded

echo "==> removing stale __pycache__ and recompiling deterministically"
find "$TREE/lib" -type d -name "__pycache__" -prune -exec rm -rf {} +
"$PY" -m compileall -q -j0 "$STDLIB"

echo "==> bundling glibc (see 'Bundled glibc' comment above): so the"
echo "    extracted CPython and its native extensions -- Polars' Rust"
echo "    runtime among them -- never depend on whatever glibc happens"
echo "    to be installed on the host"
mkdir -p "$GLIBC" "$GLIBC/LICENSES"
RPM_DIR="$WORK/rpms"
mkdir -p "$RPM_DIR"
for pkg in "${!ALMA_PACKAGES[@]}"; do
	curl -fsSL -o "$RPM_DIR/$pkg" "$ALMA_BASE_URL/$pkg"
	echo "${ALMA_PACKAGES[$pkg]}  $RPM_DIR/$pkg" | sha256sum -c -
done
EXTRACT_DIR="$WORK/rpm-extract"
mkdir -p "$EXTRACT_DIR"
for pkg in "${!ALMA_PACKAGES[@]}"; do
	bsdtar xf "$RPM_DIR/$pkg" -C "$EXTRACT_DIR"
done
for f in "${BUNDLE_LIBS[@]}"; do
	cp -P "$EXTRACT_DIR/$f" "$GLIBC/"
done
# libgcc_s.so.1 and libstdc++.so.6 have build-specific versioned real
# names (e.g. libgcc_s-8-20210514.so.1) rather than a fixed one; glob for
# them instead of hardcoding.
cp -P "$EXTRACT_DIR"/lib64/libgcc_s.so.1 "$EXTRACT_DIR"/lib64/libgcc_s-*.so.1 "$GLIBC/"
cp -P "$EXTRACT_DIR"/usr/lib64/libstdc++.so.6 "$EXTRACT_DIR"/usr/lib64/libstdc++.so.6.*.* "$GLIBC/"
cp -P "$EXTRACT_DIR"/usr/lib64/libz.so.1 "$EXTRACT_DIR"/usr/lib64/libz.so.1.*.* "$GLIBC/"
strip --strip-unneeded "$GLIBC"/*.so* 2>/dev/null || true

# LGPL (glibc) and zlib-license (zlib) notices, plus the GCC Runtime
# Library Exception under which libgcc_s/libstdc++ (GPLv3+exception) are
# redistributed here. This is a good-faith attribution pass, not a
# substitute for legal review before any broader/commercial distribution.
cp "$EXTRACT_DIR/usr/share/licenses/glibc/COPYING.LIB" "$GLIBC/LICENSES/glibc-COPYING.LIB"
cp "$EXTRACT_DIR/usr/share/licenses/glibc/COPYING" "$GLIBC/LICENSES/glibc-COPYING"
cp "$EXTRACT_DIR/usr/share/licenses/zlib/README" "$GLIBC/LICENSES/zlib-README"
cat >"$GLIBC/LICENSES/NOTICE.md" <<'EOF'
# Third-party binaries bundled here

glibc, libgcc_s, libstdc++, and libz are bundled unmodified from
AlmaLinux 8 BaseOS (see scripts/generate-runtime.sh for exact package
versions and checksums), so the embedded CPython runtime does not depend
on whatever glibc happens to be installed on the host.

- glibc: LGPL 2.1 (glibc-COPYING.LIB) with some GPL-licensed utilities
  (glibc-COPYING). Source: https://repo.almalinux.org/almalinux/8/BaseOS/
  and https://vault.almalinux.org/ (source RPMs).
- libgcc_s, libstdc++: GPLv3 with the GCC Runtime Library Exception
  (https://www.gnu.org/licenses/gcc-exception-3.1.html), which permits
  redistributing these runtime libraries with a program not itself under
  the GPL. Source: as above.
- libz: zlib License (zlib-README), a permissive, non-copyleft license.

These binaries are unmodified. Rebuilding this project against a
different compatible glibc/libstdc++/libz build (e.g. the host system's
own) is possible by adjusting scripts/generate-runtime.sh.
EOF

echo "==> verifying the assembled runtime works standalone (dev-style direct exec)"
"$PY" -c "
import numpy, polars, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import fastexcel
import loupe_kernel
print('  numpy', numpy.__version__, '| polars', polars.__version__,
      '| matplotlib', matplotlib.__version__, '| loupe_kernel', loupe_kernel.__version__)
"

echo "==> verifying the assembled runtime works via the bundled glibc loader"
echo "    (this is how internal/kernel actually invokes it in a real build)"
LOADER_TRACE=$(LD_DEBUG=libs "$GLIBC/ld-linux-x86-64.so.2" --library-path "$GLIBC" "$PY" -c "
import numpy, polars, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import fastexcel
print('bundled-glibc import OK')
" 2>&1)
echo "$LOADER_TRACE" | grep "bundled-glibc import OK" >/dev/null || {
	echo "generate-runtime: import via the bundled glibc loader failed:" >&2
	echo "$LOADER_TRACE" >&2
	exit 1
}
# The whole point: every "calling init" line must be either one of our
# own package's extension modules (anywhere under $TREE -- e.g. numpy's,
# polars', matplotlib's .so, or a wheel's own bundled .libs dependency
# such as numpy.libs/libgfortran) or one of our bundled glibc libraries
# ($GLIBC). Anything else means resolution fell back to a host system
# path (/usr/lib, /lib64, ...), which defeats the point of bundling.
if echo "$LOADER_TRACE" | grep "calling init" | grep -qv -- "$WORK"; then
	echo "generate-runtime: a library resolved from outside $WORK -- fell back to a host path:" >&2
	echo "$LOADER_TRACE" | grep "calling init" | grep -v -- "$WORK" >&2
	exit 1
fi

echo "==> compressing (zstd -19; this is the slow, one-time build-time step)"
mkdir -p "$(dirname "$OUT")"
tar cf - -C "$WORK" python glibc | zstd -19 -T0 -q -f -o "$OUT"

echo "==> wrote $OUT ($(du -h "$OUT" | cut -f1) compressed; uncompressed: $(du -sh "$TREE" | cut -f1) python + $(du -sh "$GLIBC" | cut -f1) glibc)"
