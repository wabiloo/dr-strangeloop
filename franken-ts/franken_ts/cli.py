from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.rule import Rule
from rich.text import Text

from .config import load_config
from .diagnostics import build_diag_rows, render_terminal_table, render_html_table
from .extract import extract_clip
from .ffmpeg import assemble_ts, write_concat_playlist
from .pts import find_idr_pts
from .report import generate_report
from .scte35 import generate_xml
from .timeline import build_timeline, all_forced_keyframe_times
from .tsduck import inject_markers, verify_markers
from .utils import check_tool
from .validate import validate_inputs
from .cache import entry_cache_key

_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "franken_ts"

console = Console()


# ── Output helpers ────────────────────────────────────────────────────────────

def _ok(msg: str) -> None:
    console.print(f"  [bold green]✓[/bold green]  {msg}")

def _warn(msg: str) -> None:
    console.print(f"  [bold yellow]⚠[/bold yellow]  {msg}")

def _err(msg: str) -> None:
    console.print(f"  [bold red]✗[/bold red]  {msg}")

def _info(msg: str) -> None:
    console.print(f"     [dim]{msg}[/dim]")

def _show_subprocess_error(exc: subprocess.CalledProcessError) -> None:
    """Print captured stderr from a failed subprocess in a readable block."""
    if exc.stderr:
        console.print()
        console.rule("[red]subprocess output[/red]", style="red dim")
        for line in exc.stderr.splitlines()[-40:]:   # last 40 lines to avoid flooding
            console.print(f"  [dim red]{line}[/dim red]")
        console.rule(style="red dim")
        console.print()


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


# ── Logging setup ─────────────────────────────────────────────────────────────

class _CliLogHandler(logging.Handler):
    """Route log records through the shared Rich console using the same visual style as the CLI helpers."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            self.handleError(record)
            return
        if record.levelno >= logging.ERROR:
            console.print(f"  [bold red]✗[/bold red]  {msg}")
        elif record.levelno >= logging.WARNING:
            console.print(f"  [bold yellow]⚠[/bold yellow]  {msg}")
        else:
            console.print(f"     [dim]{msg}[/dim]")


def _setup_logging(verbosity: int, debug: bool) -> None:
    level = logging.WARNING
    if debug or verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO
    handler = _CliLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]


# ── CLI entry point ────────────────────────────────────────────────────────────

@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("config", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output", "-o", type=click.Path(dir_okay=False, path_type=Path),
              default=None, help="Override output file path from config.")
@click.option("--temp-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="Directory for temporary files (default: system temp).")
@click.option("--normalize", is_flag=True, default=False,
              help="Pre-transcode non-conforming inputs to match output spec.")
@click.option("--cache-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None,
              help=f"Cache directory for normalized files (default: {_DEFAULT_CACHE_DIR}).")
@click.option("--no-cache", is_flag=True, default=False,
              help="Disable the normalization cache.")
@click.option("--debug", is_flag=True, default=False,
              help="Keep all temporary files and enable verbose logging.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Print commands without executing them.")
@click.option("--skip-transcode", is_flag=True, default=False,
              help="Skip ffmpeg step (use existing TS at output path).")
@click.option("--skip-inject", is_flag=True, default=False,
              help="Stop after ffmpeg transcode, before tsduck injection.")
@click.option("--verify", is_flag=True, default=False,
              help="Run tsduck extraction after injection and generate an HTML report.")
@click.option("-v", "--verbose", count=True,
              help="Increase log verbosity (-v = INFO, -vv = DEBUG).")
def main(
    config: Path,
    output: Optional[Path],
    temp_dir: Optional[Path],
    normalize: bool,
    cache_dir: Optional[Path],
    no_cache: bool,
    debug: bool,
    dry_run: bool,
    skip_transcode: bool,
    skip_inject: bool,
    verify: bool,
    verbose: int,
) -> None:
    """Build an MPEG-TS file with SCTE-35 markers from a YAML asset list."""
    _setup_logging(verbose, debug)

    console.print()
    console.print(Rule("[bold cyan]🧟 franken-ts[/bold cyan]", style="cyan dim"))
    console.print(f"  [dim]config:[/dim] {config}")
    if dry_run:
        console.print("  [dim yellow]dry-run mode — no files will be written[/dim yellow]")
    console.print()

    # Step 0 — dependency check
    for tool in ("ffmpeg", "ffprobe", "tsp"):
        try:
            check_tool(tool)
        except RuntimeError as exc:
            _err(str(exc))
            sys.exit(1)
    _ok("Dependencies found")

    try:
        cfg = load_config(config)
    except Exception as exc:
        _err(f"Config error: {exc}")
        sys.exit(1)

    if output:
        cfg.output.file = output

    # Every clip is always extracted + normalized (see extract.py).  The cache
    # is enabled by default and only disabled with --no-cache.  The historical
    # --normalize flag / cfg.normalize are accepted for backwards compatibility
    # but no longer gate anything.
    _ = normalize or cfg.normalize
    effective_cache_dir: Optional[Path] = None
    if not no_cache:
        effective_cache_dir = cache_dir or _DEFAULT_CACHE_DIR

    own_temp = temp_dir is None
    if temp_dir is None:
        temp_dir = Path(tempfile.mkdtemp(prefix="franken_ts_"))
    else:
        temp_dir.mkdir(parents=True, exist_ok=True)

    if debug:
        _info(f"temp dir:  {temp_dir}")
    if effective_cache_dir and debug:
        _info(f"cache dir: {effective_cache_dir}")

    try:
        _run_pipeline(
            cfg=cfg,
            temp_dir=temp_dir,
            cache_dir=effective_cache_dir,
            dry_run=dry_run,
            skip_transcode=skip_transcode,
            skip_inject=skip_inject,
            verify=verify,
        )
    except subprocess.CalledProcessError as exc:
        _err(f"Subprocess failed (exit {exc.returncode}): {exc.cmd[0]}")
        _show_subprocess_error(exc)
        if debug:
            raise
        sys.exit(1)
    except Exception as exc:
        _err(str(exc))
        if debug:
            raise
        sys.exit(1)
    finally:
        if own_temp and not debug and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

    console.print()


# ── Clip duration table ───────────────────────────────────────────────────────

def _build_clip_table(assets, infos, entries, framerate):
    from rich import box as _box
    from rich.table import Table as _Table
    from rich.text import Text as _Text
    from .validate import VideoInfo

    frame_dur = 1.0 / framerate

    t = _Table(
        title="Clip Durations",
        box=_box.SIMPLE_HEAD,
        show_lines=True,
        title_style="bold",
    )
    t.add_column("#",             justify="right", style="dim", width=3)
    t.add_column("File",          no_wrap=False, max_width=28, overflow="ellipsis")
    t.add_column("YAML start",    justify="right")
    t.add_column("YAML duration", justify="right")
    t.add_column("Source dur.",   justify="right")
    t.add_column("Clip frames",   justify="right")
    t.add_column("Inpoint",       justify="right")
    t.add_column("Outpoint",      justify="right")
    t.add_column("Frame-accurate", justify="center")

    def _fmt(s: float) -> str:
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{int(h):02d}:{int(m):02d}:{sec:06.3f}"

    for idx, (asset, entry) in enumerate(zip(assets, entries), 1):
        # Use the normalized file's info (entry.source_file) for accurate duration,
        # but show the original filename (asset.file.name) for readability.
        info: VideoInfo = infos[entry.source_file]
        yaml_start = asset.start_seconds() or 0.0
        yaml_dur = asset.duration_seconds()

        src_dur_s = _fmt(info.duration)
        yaml_start_s = _fmt(yaml_start)
        yaml_dur_s = _fmt(yaml_dur) if yaml_dur is not None else "[dim]full[/dim]"

        frames = round(entry.clip_duration * framerate)
        inpoint_s = _fmt(entry.inpoint)
        outpoint_s = _fmt(entry.outpoint)

        # Check frame accuracy
        inp_exact = abs(entry.inpoint - entry.inpoint_raw) < 1e-9
        out_exact = abs(entry.outpoint - entry.outpoint_raw) < 1e-9
        src_ok = (entry.outpoint_raw <= info.duration + frame_dur)

        if not src_ok:
            acc = _Text("✗ exceeds src", style="bold red")
        elif inp_exact and out_exact:
            acc = _Text("✓", style="green")
        else:
            snapped = []
            if not inp_exact:
                snapped.append(f"in {entry.inpoint_raw:.4f}→{entry.inpoint:.4f}")
            if not out_exact:
                snapped.append(f"out {entry.outpoint_raw:.4f}→{entry.outpoint:.4f}")
            acc = _Text("⚠ snapped\n" + "  ".join(snapped), style="yellow")

        t.add_row(
            str(idx),
            asset.file.name,
            yaml_start_s,
            yaml_dur_s,
            src_dur_s,
            str(frames),
            inpoint_s,
            outpoint_s,
            acc,
        )

    return t


# ── Pipeline ──────────────────────────────────────────────────────────────────

def _run_pipeline(
    cfg,
    temp_dir: Path,
    cache_dir: Optional[Path],
    dry_run: bool,
    skip_transcode: bool,
    skip_inject: bool,
    verify: bool,
) -> None:

    # Timeline entries keep pointing at the ORIGINAL source files so the clip
    # table, report and diagnostics show real filenames.  The extracted segment
    # paths are tracked separately and only used for assembly.
    original_assets = list(cfg.assets)

    # Ensure the output directory exists BEFORE anything writes there.  The
    # config's output path may be relative (e.g. "../outputs/foo.ts") and resolve
    # to a missing directory depending on the working directory.  If it is
    # missing, the final `tsp` injection step fails to create the file — and a
    # TSDuck spliceinject FileListener-thread shutdown bug turns that clean error
    # into an indefinite hang ("Injecting SCTE-35 markers..." never returns).
    if not dry_run:
        cfg.output.file.parent.mkdir(parents=True, exist_ok=True)

    # ── Step 1: validate ─────────────────────────────────────────────────────
    # normalize=True: every clip is extracted+normalized regardless, so format
    # mismatches (fps/resolution) are informational warnings, never errors.
    with console.status("  Validating inputs...", spinner="dots"):
        report, infos = validate_inputs(cfg.assets, cfg.output, normalize=True)

    for w in report.warnings:
        _warn(w)
    if report.has_errors:
        for e in report.errors:
            _err(e)
        raise RuntimeError(f"Validation failed ({len(report.errors)} error(s)).")
    _ok(f"Validated [bold]{len(cfg.assets)}[/bold] asset(s)")

    # ── Step 2: build timeline ────────────────────────────────────────────────
    entries, boundaries = build_timeline(cfg.assets, infos, cfg.output.framerate,
                                          global_slate_image=cfg.slate_image)
    total_duration = entries[-1].output_end if entries else 0.0
    ad_count = sum(1 for b in boundaries if b.is_start)

    _ok(
        f"Timeline: [bold]{len(entries)}[/bold] clip(s), "
        f"[bold]{ad_count}[/bold] ad break(s), "
        f"total [bold]{_fmt_elapsed(total_duration)}[/bold]"
    )
    console.print()
    console.print(_build_clip_table(original_assets, infos, entries, cfg.output.framerate))
    console.print()

    forced_kf_times = all_forced_keyframe_times(entries)
    video_playlist = temp_dir / "video.txt"
    audio_playlist = temp_dir / "audio.txt"
    intermediate_ts = temp_dir / "intermediate.ts"
    xml_path = temp_dir / "scte35.xml"

    # ── Step 3: extract + assemble ────────────────────────────────────────────
    if not skip_transcode:
        t0 = time.monotonic()

        # 3a — extract every clip into keyframe-clean video/audio segments.
        video_segs: list[Path] = []
        audio_segs: list[Path] = []
        # Within-run dedup: entries with an identical cache key only need
        # extracting once even when they appear multiple times in a schedule.
        run_cache: dict[str, tuple[Path, Path]] = {}
        n_total = len(entries)
        n_disk_hit = 0
        n_run_hit = 0
        for idx, entry in enumerate(entries):
            # Use the same key as the persistent cache so both caches are
            # always consistent — two entries are identical iff they produce
            # the same extracted segments (same source, cut, overlays, etc).
            key = entry_cache_key(entry, cfg.output)
            prefix = f"  [[bold]{idx + 1}/{n_total}[/bold]] [cyan]{entry.source_file.name}[/cyan]"

            if key in run_cache:
                v, a = run_cache[key]
                console.print(f"{prefix}  [dim green]run-cache hit[/dim green]")
                n_run_hit += 1
            else:
                # Check persistent disk cache first.
                disk_hit = False
                if cache_dir is not None and not dry_run:
                    from . import cache as _cache
                    cached = _cache.lookup(entry, cfg.output, cache_dir)
                    if cached is not None:
                        v, a = cached.video, cached.audio
                        disk_hit = True
                        n_disk_hit += 1
                        console.print(f"{prefix}  [dim green]disk-cache hit[/dim green]")

                if not disk_hit:
                    with console.status(f"{prefix}  [dim]extracting...[/dim]", spinner="dots"):
                        result = extract_clip(
                            entry, cfg.output, temp_dir, idx,
                            cache_dir=cache_dir, dry_run=dry_run,
                        )
                    v, a = result.segments.video, result.segments.audio
                    console.print(f"{prefix}  [dim]done[/dim]")

                run_cache[key] = (v, a)
            video_segs.append(v)
            audio_segs.append(a)

        summary_parts = [f"[bold]{n_total}[/bold] clip(s)"]
        if n_disk_hit:
            summary_parts.append(f"[green]{n_disk_hit} disk-cache hit(s)[/green]")
        if n_run_hit:
            summary_parts.append(f"[green]{n_run_hit} run-cache hit(s)[/green]")
        _ok(f"Extracted {', '.join(summary_parts)}")

        # 3b — assemble final TS (video re-encode + forced IDRs, audio muxed in).
        with console.status("  Assembling final TS...", spinner="dots"):
            write_concat_playlist(video_segs, video_playlist)
            write_concat_playlist(audio_segs, audio_playlist)
            assemble_ts(
                video_playlist, audio_playlist, intermediate_ts, cfg.output,
                forced_kf_times, dry_run=dry_run,
            )
        _ok(f"Assembled → [dim]{intermediate_ts.name}[/dim]  [dim]({_fmt_elapsed(time.monotonic() - t0)})[/dim]")
    else:
        _warn("Skipping transcode (--skip-transcode)")
        intermediate_ts = cfg.output.file

    if skip_inject:
        if not skip_transcode:
            shutil.copy2(intermediate_ts, cfg.output.file)
            _ok(f"Saved TS (no markers) → {cfg.output.file}")
        _warn("Stopping before injection (--skip-inject)")
        return

    # ── Step 4: PTS detection ─────────────────────────────────────────────────
    with console.status("  Detecting IDR frame PTS values...", spinner="dots"):
        pts_map, muxer_offset = find_idr_pts(
            intermediate_ts, boundaries, cfg.output.framerate, cfg.output.gop
        )

    _ok(
        f"PTS detected for [bold]{len(pts_map)}[/bold] splice point(s)  "
        f"[dim](muxer offset: {muxer_offset:.6f}s)[/dim]"
    )
    for boundary in boundaries:
        pts = pts_map[(boundary.event_id, boundary.is_start)]
        label = "start" if boundary.is_start else "stop "
        _info(
            f"Event [bold]{boundary.event_id}[/bold]  {label}  "
            f"{pts / 90_000:.3f}s  [dim](PTS {pts:,})[/dim]"
        )

    # ── Step 5: generate SCTE-35 XML ──────────────────────────────────────────
    with console.status("  Generating SCTE-35 XML...", spinner="dots"):
        generate_xml(boundaries, pts_map, xml_path)
    _ok(f"SCTE-35 XML → [dim]{xml_path.name}[/dim]")

    # ── Step 6: inject ────────────────────────────────────────────────────────
    t0 = time.monotonic()
    with console.status("  Injecting SCTE-35 markers...", spinner="dots"):
        inject_markers(intermediate_ts, xml_path, cfg.output.file, dry_run=dry_run)
    _ok(
        f"Output → [bold]{cfg.output.file}[/bold]  "
        f"[dim]({_fmt_elapsed(time.monotonic() - t0)})[/dim]"
    )

    # ── Step 7: verify + diagnostics + report ────────────────────────────────
    if verify:
        with console.status("  Verifying injected markers...", spinner="dots"):
            ok = verify_markers(cfg.output.file, boundaries, temp_dir, dry_run=dry_run)
        if not ok:
            raise RuntimeError("Marker verification failed — check splice-info-tables.xml")
        _ok(f"Verification passed — [bold]{ad_count}[/bold] event(s) confirmed")

        # ── Diagnostics table ─────────────────────────────────────────────────
        verify_xml = temp_dir / "splice-info-tables.xml"
        with console.status("  Building boundary diagnostics...", spinner="dots"):
            diag_rows = build_diag_rows(
                entries=entries,
                boundaries=boundaries,
                pts_map=pts_map,
                final_ts=cfg.output.file,
                verify_xml=verify_xml if verify_xml.exists() else None,
                framerate=cfg.output.framerate,
                muxer_offset=muxer_offset,
            )
        console.print()
        console.print(render_terminal_table(diag_rows, cfg.output.framerate, muxer_offset))
        diag_html = render_html_table(diag_rows, cfg.output.framerate, muxer_offset)

        # ── HTML report ───────────────────────────────────────────────────────
        report_path = cfg.output.file.with_name(cfg.output.file.stem + "_report.html")
        with console.status("  Generating HTML report...", spinner="dots"):
            generate_report(
                ts_file=cfg.output.file,
                entries=entries,
                boundaries=boundaries,
                pts_map=pts_map,
                framerate=cfg.output.framerate,
                output_path=report_path,
                diag_html=diag_html,
                dry_run=dry_run,
            )
        _ok(f"Report → [bold]{report_path}[/bold]")


if __name__ == "__main__":
    main()
