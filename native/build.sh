#!/usr/bin/env bash
# Build the SolarSight C++ core into a shared library next to this script (bible.md 9.0).
# Usage: bash native/build.sh   (needs g++ with C++17)
set -euo pipefail
cd "$(dirname "$0")"
case "$(uname -s)" in
  Darwin) EXT=dylib ;;
  MINGW*|MSYS*|CYGWIN*) EXT=dll ;;
  *) EXT=so ;;
esac
g++ -O2 -std=c++17 -Wall -Wextra -Werror -shared -fPIC -fvisibility=hidden \
  -o "libsolarsight.${EXT}" solarsight.cpp
echo "built native/libsolarsight.${EXT}"
