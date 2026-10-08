#!/usr/bin/env bash
# docker-entrypoint.sh — S3-sync shim wrapping bake.py/serve.py so the image
# itself never has to be rebuilt when the content changes (see README.md
# "Deploying to Fargate"). All channel-specific data (franken-ts input,
# baked loop package) lives in S3; only code + the GPAC/ffmpeg toolchain are
# baked into the image.
#
# Contract (when the relevant *_S3_URI env var is set, the corresponding
# positional path argument must be OMITTED from the container CMD — this
# script supplies it after syncing):
#
#   serve.py:
#     LOOP_PACKAGE_S3_URI   s3://bucket/prefix/<channel>  (required)
#     CMD:  serve.py --epoch-utc ... --port 8080 [other flags]
#     (no package-dir positional argument)
#     When LOOP_PACKAGE_S3_URI is set, this is the production path: after
#     syncing, the flags are parsed here and handed to gunicorn (wsgi:app)
#     instead of Flask's single-threaded dev server (see PERFS.md) --
#     serve.py's own argparse remains the source of truth for flag names,
#     this translates the serving options into environment variables for
#     gunicorn/wsgi.py. GUNICORN_WORKERS overrides the default worker count.
#     Access logs (client, request line, status, bytes, user agent) go to
#     stdout, i.e. CloudWatch on ECS.
#
#   bake.py:
#     FRANKEN_TS_S3_URI     s3://bucket/prefix/<channel>-input  (required)
#     LOOP_PACKAGE_S3_URI   s3://bucket/prefix/<channel>        (required;
#                           bake output is synced back up here)
#     CMD:  bake.py --segment-duration 4.0 [other flags]
#     (no input-path positional argument, no --output flag)
#
# If the relevant *_S3_URI env var is NOT set, both scripts fall back to
# running exactly as given (e.g. for local `docker run` testing with a
# bind-mounted volume) — the shim is then a pure passthrough.
#
# Any other CMD (e.g. `--help`, or a shell) is exec'd through unchanged.

set -euo pipefail

LOOP_PACKAGE_LOCAL_DIR="${LOOP_PACKAGE_LOCAL_DIR:-/var/loop-package}"
FRANKEN_TS_LOCAL_DIR="${FRANKEN_TS_LOCAL_DIR:-/var/franken-ts-input}"

log() { echo "[entrypoint] $*" >&2; }

sync_down() {
    local s3_uri="$1" local_dir="$2"
    log "syncing ${s3_uri} -> ${local_dir}"
    mkdir -p "$local_dir"
    aws s3 sync "$s3_uri" "$local_dir" --no-progress --delete
}

sync_up() {
    local local_dir="$1" s3_uri="$2"
    log "syncing ${local_dir} -> ${s3_uri}"
    aws s3 sync "$local_dir" "$s3_uri" --no-progress --delete
}

SUBCOMMAND="${1:-}"

case "$SUBCOMMAND" in
    serve.py)
        shift
        if [[ -n "${LOOP_PACKAGE_S3_URI:-}" ]]; then
            sync_down "$LOOP_PACKAGE_S3_URI" "$LOOP_PACKAGE_LOCAL_DIR"

            HOST="0.0.0.0"
            PORT="8080"
            EPOCH_UTC=""
            DVR_WINDOW_SECONDS=""
            WINDOW_SEGMENTS=""
            CONTINUOUS_TIMELINE="false"
            PERIOD_ON_SEGMENTATION=""
            PERIOD_ON_SEGMENTATION_APPLY=""
            CHANNEL_NAME=""
            TIMESHIFT="false"
            TIMESHIFT_START_PARAM=""
            TIMESHIFT_END_PARAM=""
            TIMESHIFT_MAX_SPAN_SECONDS=""
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --host) HOST="$2"; shift 2 ;;
                    --port) PORT="$2"; shift 2 ;;
                    --epoch-utc) EPOCH_UTC="$2"; shift 2 ;;
                    --channel-name) CHANNEL_NAME="$2"; shift 2 ;;
                    --dvr-window-seconds) DVR_WINDOW_SECONDS="$2"; shift 2 ;;
                    --window-segments) WINDOW_SEGMENTS="$2"; shift 2 ;;
                    --continuous-timeline) CONTINUOUS_TIMELINE="true"; shift ;;
                    --period-on-segmentation) PERIOD_ON_SEGMENTATION="$2"; shift 2 ;;
                    --period-on-segmentation-apply) PERIOD_ON_SEGMENTATION_APPLY="$2"; shift 2 ;;
                    --timeshift) TIMESHIFT="true"; shift ;;
                    --timeshift-start-param) TIMESHIFT_START_PARAM="$2"; shift 2 ;;
                    --timeshift-end-param) TIMESHIFT_END_PARAM="$2"; shift 2 ;;
                    --timeshift-max-span-seconds) TIMESHIFT_MAX_SPAN_SECONDS="$2"; shift 2 ;;
                    *)
                        log "ERROR: unrecognized serve.py flag in production mode: $1"
                        exit 2
                        ;;
                esac
            done
            if [[ -z "$EPOCH_UTC" ]]; then
                log "ERROR: --epoch-utc is required"
                exit 2
            fi

            export LOOP_PACKAGE_DIR="$LOOP_PACKAGE_LOCAL_DIR"
            export EPOCH_UTC DVR_WINDOW_SECONDS WINDOW_SEGMENTS CONTINUOUS_TIMELINE
            export PERIOD_ON_SEGMENTATION PERIOD_ON_SEGMENTATION_APPLY CHANNEL_NAME
            export TIMESHIFT TIMESHIFT_START_PARAM TIMESHIFT_END_PARAM
            export TIMESHIFT_MAX_SPAN_SECONDS
            WORKERS="${GUNICORN_WORKERS:-4}"
            log "starting gunicorn (${WORKERS} workers) on ${HOST}:${PORT}"
            exec gunicorn --bind "${HOST}:${PORT}" --workers "$WORKERS" \
                --access-logfile - \
                --access-logformat '%(h)s "%(r)s" %(s)s %(b)s "%(a)s"' \
                wsgi:app
        else
            log "LOOP_PACKAGE_S3_URI not set — running serve.py as given (local/dev mode)"
            exec python3 serve.py "$@"
        fi
        ;;
    bake.py)
        shift
        if [[ -n "${FRANKEN_TS_S3_URI:-}" ]]; then
            if [[ -z "${LOOP_PACKAGE_S3_URI:-}" ]]; then
                log "ERROR: FRANKEN_TS_S3_URI is set but LOOP_PACKAGE_S3_URI is not" \
                    "— nowhere to publish the baked package to."
                exit 2
            fi
            sync_down "$FRANKEN_TS_S3_URI" "$FRANKEN_TS_LOCAL_DIR"
            python3 bake.py "$FRANKEN_TS_LOCAL_DIR" --output "$LOOP_PACKAGE_LOCAL_DIR" "$@"
            sync_up "$LOOP_PACKAGE_LOCAL_DIR" "$LOOP_PACKAGE_S3_URI"
        else
            log "FRANKEN_TS_S3_URI not set — running bake.py as given (local/dev mode)"
            exec python3 bake.py "$@"
        fi
        ;;
    *)
        exec python3 "$@"
        ;;
esac
