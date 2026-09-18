#!/usr/bin/env bash
# run.sh — bake a franken-ts output into a loop package, then serve it, in
# one command. Convenience wrapper around bake.py + serve.py (SCOPE.md §4).
#
# Usage:
#   ./run.sh <ts_file> [options]
#
# Options:
#   --markers PATH         Path to .markers.json sidecar
#                          (default: <ts_file> with .ts -> .markers.json)
#   --output DIR           Loop package output directory
#                          (default: ./loop-package next to this script)
#   --segment-duration N   Nominal segment duration in seconds passed to
#                          bake.py (default: 4.0). Real segments are
#                          cue-driven and may be longer -- see SCOPE.md §4.1.
#   --epoch-utc TIMESTAMP  Channel epoch, ISO8601 UTC, e.g.
#                          2026-01-01T00:00:00Z (default: now, i.e. the
#                          channel's loop 0 starts the moment serve starts).
#   --host HOST             serve.py bind host (default: 0.0.0.0)
#   --port PORT              serve.py bind port (default: 8080)
#   --dvr-window-seconds N  Approximate DVR window / manifest size in
#                          seconds (default: 30). Converted to a segment
#                          count using the package's nominal segment
#                          duration. Ignored if --window-segments is given.
#   --window-segments N     Exact number of segments to advertise per
#                          manifest response. Overrides --dvr-window-seconds.
#   --skip-bake            Skip the bake step and serve an existing
#                          --output package directory as-is.
#   --python PATH           Python interpreter to use (default: python3, or
#                          .venv/bin/python if a .venv exists next to this
#                          script).
#   -v, --verbose           Passed through to bake.py.
#   -h, --help              Show this message.
#
# Examples:
#   ./run.sh ../outputs/break-and-popos.ts
#   ./run.sh ../outputs/break-and-popos.ts --output /var/loop-packages/ch1 \
#            --epoch-utc 2026-01-01T00:00:00Z --port 9000
#   ./run.sh --skip-bake --output /var/loop-packages/ch1
#
# Ctrl+C stops the serve process. The bake step only runs once per
# invocation (SCOPE.md §4.1: "run once per schedule change, never in the
# hot serving path") -- rerun this script (without --skip-bake) whenever
# the source .ts/.markers.json changes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TS_FILE=""
MARKERS=""
OUTPUT="${SCRIPT_DIR}/loop-package"
SEGMENT_DURATION="4.0"
EPOCH_UTC=""
HOST="0.0.0.0"
PORT="8080"
DVR_WINDOW_SECONDS="30"
WINDOW_SEGMENTS=""
SKIP_BAKE="0"
VERBOSE="0"
PYTHON=""

usage() {
    sed -n '2,44p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --markers) MARKERS="$2"; shift 2 ;;
        --output) OUTPUT="$2"; shift 2 ;;
        --segment-duration) SEGMENT_DURATION="$2"; shift 2 ;;
        --epoch-utc) EPOCH_UTC="$2"; shift 2 ;;
        --host) HOST="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --dvr-window-seconds) DVR_WINDOW_SECONDS="$2"; shift 2 ;;
        --window-segments) WINDOW_SEGMENTS="$2"; shift 2 ;;
        --skip-bake) SKIP_BAKE="1"; shift ;;
        --python) PYTHON="$2"; shift 2 ;;
        -v|--verbose) VERBOSE="1"; shift ;;
        -h|--help) usage; exit 0 ;;
        *)
            if [[ -z "$TS_FILE" ]]; then
                TS_FILE="$1"
                shift
            else
                echo "Unknown argument: $1" >&2
                usage
                exit 1
            fi
            ;;
    esac
done

if [[ "$SKIP_BAKE" != "1" && -z "$TS_FILE" ]]; then
    echo "Error: <ts_file> is required unless --skip-bake is given." >&2
    usage
    exit 1
fi

if [[ -z "$PYTHON" ]]; then
    if [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
        PYTHON="${SCRIPT_DIR}/.venv/bin/python"
    else
        PYTHON="python3"
    fi
fi

if [[ -z "$EPOCH_UTC" ]]; then
    EPOCH_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "No --epoch-utc given, defaulting to now: ${EPOCH_UTC}"
fi

# Make sure a locally-built gpac/MP4Box (with scte35dec support -- see
# SCOPE.md §6/§7) is on PATH if it's been installed next to this repo's
# conventional location; harmless no-op otherwise.
if [[ -d "${HOME}/gpac-local/bin" ]]; then
    export PATH="${HOME}/gpac-local/bin:${PATH}"
fi
if [[ -d "${HOME}/gpac-local/lib" ]]; then
    export DYLD_LIBRARY_PATH="${HOME}/gpac-local/lib:${DYLD_LIBRARY_PATH:-}"
    export LD_LIBRARY_PATH="${HOME}/gpac-local/lib:${LD_LIBRARY_PATH:-}"
fi

if [[ "$SKIP_BAKE" != "1" ]]; then
    BAKE_ARGS=("$TS_FILE" --output "$OUTPUT" --segment-duration "$SEGMENT_DURATION")
    [[ -n "$MARKERS" ]] && BAKE_ARGS+=(--markers "$MARKERS")
    [[ "$VERBOSE" == "1" ]] && BAKE_ARGS+=(-v)

    echo "==> Baking loop package: ${TS_FILE} -> ${OUTPUT}"
    "$PYTHON" "${SCRIPT_DIR}/bake.py" "${BAKE_ARGS[@]}"
else
    echo "==> --skip-bake given, reusing existing package at ${OUTPUT}"
    if [[ ! -f "${OUTPUT}/loop_descriptor.json" ]]; then
        echo "Error: ${OUTPUT}/loop_descriptor.json not found -- nothing to serve." >&2
        exit 1
    fi
fi

echo "==> Serving loop package: ${OUTPUT} (epoch=${EPOCH_UTC})"

# 0.0.0.0/:: are bind addresses, not something you can put in a browser/
# player URL bar -- print localhost instead for display purposes only.
DISPLAY_HOST="$HOST"
if [[ "$DISPLAY_HOST" == "0.0.0.0" || "$DISPLAY_HOST" == "::" ]]; then
    DISPLAY_HOST="localhost"
fi
echo ""
echo "    HLS:  http://${DISPLAY_HOST}:${PORT}/master.m3u8"
echo "    DASH: http://${DISPLAY_HOST}:${PORT}/manifest.mpd"
echo ""

exec "$PYTHON" "${SCRIPT_DIR}/serve.py" "$OUTPUT" --epoch-utc "$EPOCH_UTC" --host "$HOST" --port "$PORT" \
    --dvr-window-seconds "$DVR_WINDOW_SECONDS" \
    ${WINDOW_SEGMENTS:+--window-segments "$WINDOW_SEGMENTS"}
