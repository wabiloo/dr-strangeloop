"""Read the `[infrastructure.*]` tables of a channel config."""

INFRA_TABLES = ("aws", "s3", "express", "docker")


def infra_table(cfg: dict, name: str) -> dict:
    return cfg.get("infrastructure", {}).get(name, {})


def reject_legacy_tables(cfg: dict, config_path: str = "config") -> None:
    """[aws]/[s3]/[express]/[docker] moved under [infrastructure]; a config
    still using them would silently fall back to defaults (wrong region or
    bucket), so fail loudly instead."""
    old = [t for t in INFRA_TABLES if t in cfg]
    if old:
        names = ", ".join(f"[{t}]" for t in old)
        new = ", ".join(f"[infrastructure.{t}]" for t in old)
        raise SystemExit(f"{config_path}: {names} moved under [infrastructure] -- rename to {new}")
