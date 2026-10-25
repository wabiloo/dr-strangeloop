#!/usr/bin/env python3
"""Rewrite channel config TOML files to the current key layout.

    uv run --project its-a-live python its-a-live/migrate_config.py data/channels
    uv run --project its-a-live python its-a-live/migrate_config.py --check some.toml

Each path is a file or a directory (searched for *.toml). A file is migrated when its
`config_version` is older than the current one (a missing marker counts as 1);
current files, and TOML files that are not channel configs (no `[deploy]`), are
left untouched, so running it twice is harmless. Comments are
kept (a comment block directly above a key moves with it). `--check` only
reports and exits 1 if anything needs migrating; `--dry-run` also prints the
diff. See _legacy_cfg.py for the list of renames.
"""

from __future__ import annotations

import argparse
import difflib
import sys
import tomllib
from pathlib import Path

from _legacy_cfg import CONFIG_VERSION, config_version, find_legacy_keys, migrate_toml_text


def _files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            out.extend(sorted(p.rglob("*.toml")))
        elif p.is_file():
            out.append(p)
        else:
            raise SystemExit(f"no such file or directory: {raw}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="channel config files or directories of them")
    parser.add_argument("--check", action="store_true", help="only report; exit 1 if a file needs migrating")
    parser.add_argument("--dry-run", action="store_true", help="show the diff, write nothing")
    args = parser.parse_args(argv)

    pending = 0
    for path in _files(args.paths):
        text = path.read_text(encoding="utf-8")
        try:
            cfg = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            print(f"skipped (not valid TOML): {path}: {exc}", file=sys.stderr)
            continue
        if "deploy" not in cfg:
            continue  # not a channel config
        legacy = find_legacy_keys(cfg)
        version = config_version(cfg)
        if version >= CONFIG_VERSION and not legacy:
            continue
        pending += 1
        new_text, warnings = migrate_toml_text(text)
        print(f"{'would migrate' if args.check or args.dry_run else 'migrated'}: {path}")
        print(f"    config_version {version} -> {CONFIG_VERSION}")
        for line in legacy:
            print(f"    {line}")
        for warning in warnings:
            print(f"    WARNING: {warning}", file=sys.stderr)
        if args.dry_run:
            sys.stdout.writelines(difflib.unified_diff(text.splitlines(True), new_text.splitlines(True), str(path), str(path), n=1))
        if not args.check and not args.dry_run:
            path.write_text(new_text, encoding="utf-8")
    if not pending:
        print("nothing to migrate")
    return 1 if (args.check and pending) else 0


if __name__ == "__main__":
    raise SystemExit(main())
