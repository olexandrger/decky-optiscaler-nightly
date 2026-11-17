#!/usr/bin/env bash
# Cross-compile the live-control ASI plugin for Windows x64.
#
# The result runs inside the game's Proton prefix alongside OptiScaler, so it is
# a Windows DLL regardless of what the plugin itself is built on. Uses zig cc
# when it is installed (it bundles the mingw-w64 headers and libraries), else
# the mingw-w64 cross compiler (apt: g++-mingw-w64-x86-64), which CI uses.
#
# The Config layout comes from asi/generated/config_mirror.h, regenerated here
# from asi/optiscaler_ref/Config.h -- which must be the Config.h of the bundled
# OptiScaler (scripts/sync_optiscaler_headers.py; bundling a nightly runs it).
# The output is not committed (bin/.gitignore): CI builds it for every release.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${1:-$here/../bin/decky_optiscaler_live.asi}"

python3 "$here/../scripts/generate_asi_layout.py" >/dev/null

flags=(-shared -O2 -fno-exceptions -std=c++17
       -Wall -Wextra -Werror -Wno-unused-parameter -Wno-cast-function-type
       -I"$here" -s)

if command -v zig >/dev/null; then
  zig cc -target x86_64-windows-gnu "${flags[@]}" -o "$out" "$here/live.cpp" -lpsapi -lkernel32
  # The linker also drops an import library and debug symbols next to the
  # output; neither belongs in the shipped plugin.
  outdir="$(cd "$(dirname "$out")" && pwd)"
  rm -f "$outdir/live.lib" "$outdir/$(basename "${out%.*}").pdb" "$outdir/${out##*/}.lib"
elif command -v x86_64-w64-mingw32-g++ >/dev/null; then
  # -static: no libstdc++/libgcc DLLs to ship next to it.
  x86_64-w64-mingw32-g++ "${flags[@]}" -static -o "$out" "$here/live.cpp" -lpsapi
else
  echo "need zig or x86_64-w64-mingw32-g++ (apt install g++-mingw-w64-x86-64)" >&2
  exit 1
fi

echo "built $out ($(wc -c < "$out" | tr -d ' ') bytes)"
