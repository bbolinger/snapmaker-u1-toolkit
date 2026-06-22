#!/usr/bin/env python3
"""Shared Snapmaker U1 connection + data-dir config.

CONNECTION resolution (host/port):
1. Environment variables: SNAPMAKER_U1_HOST / SNAPMAKER_U1_PORT
2. JSON config: <data-dir>/u1_config.json
3. Last-resort defaults (port only; host is required)

DATA-DIR resolution (where runtime state lives — configs, photos, ledgers):
1. SNAPMAKER_U1_DATA_DIR env var (explicit override)
2. /opt/data/snapmaker_u1 if it already exists (auto-detects Hermes-style install)
3. ~/.local/share/snapmaker-u1 (fresh-install default, follows XDG Base Dir)

All path/host lookups happen on first call — no module-import-time disk I/O.
This means importing this module never fails for missing config; failure
happens on the first call to get_u1_host() with no override available.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

FALLBACK_PORT = 7125

# One-shot dotenv loader so standalone Python users (esp. on Windows where
# `source .env` doesn't exist) get the same convenience as Linux shell users
# without pulling python-dotenv as a hard dependency. Walks up from cwd
# looking for a `.env` file; parses `KEY=VALUE` lines; only sets vars not
# already in os.environ (explicit env vars always win).
_DOTENV_LOADED = False


def _load_dotenv_if_present() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    try:
        cur = Path.cwd().resolve()
    except OSError:
        return
    for parent in [cur, *cur.parents]:
        candidate = parent / ".env"
        if not candidate.exists():
            continue
        try:
            for raw in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip()
                # Strip a single matching quote pair: KEY="val" or KEY='val'
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                if k and k not in os.environ:
                    os.environ[k] = v
        except OSError:
            pass
        return  # first .env wins; don't keep walking


def _xdg_data_home() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg)
    return Path.home() / ".local" / "share"


def get_data_dir() -> Path:
    """Resolve the runtime data dir using the 3-tier fallback documented above.

    Called fresh each time so env changes after import are honored. Cheap —
    one stat() call worst case. Lazily loads .env on first call.
    """
    _load_dotenv_if_present()
    env = os.environ.get("SNAPMAKER_U1_DATA_DIR")
    if env:
        return Path(env)
    hermes_default = Path("/opt/data/snapmaker_u1")
    if hermes_default.exists():
        return hermes_default
    return _xdg_data_home() / "snapmaker-u1"


def get_config_path() -> Path:
    """Path to the u1_config.json file (per-data-dir, env-overridable)."""
    _load_dotenv_if_present()
    env = os.environ.get("SNAPMAKER_U1_CONFIG")
    if env:
        return Path(env)
    return get_data_dir() / "u1_config.json"


# Back-compat alias — older scripts and tests reference CONFIG_PATH as a
# module attribute. Resolves on access so it tracks env changes.
class _ConfigPathProxy:
    """Module-attribute shim so `u1_config.CONFIG_PATH` works as before
    but defers resolution to first use."""
    def __fspath__(self) -> str:
        return str(get_config_path())
    def __str__(self) -> str:
        return str(get_config_path())
    def __repr__(self) -> str:
        return repr(get_config_path())
    def __eq__(self, other: object) -> bool:
        return get_config_path() == other
    @property
    def exists_path(self) -> Path:
        return get_config_path()
    def exists(self) -> bool:
        return get_config_path().exists()
    def read_text(self, *args, **kwargs) -> str:
        return get_config_path().read_text(*args, **kwargs)


CONFIG_PATH = _ConfigPathProxy()  # type: ignore[assignment]


def _load_file() -> dict[str, Any]:
    try:
        data = json.loads(get_config_path().read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_u1_host(default: str | None = None) -> str:
    _load_dotenv_if_present()
    file_cfg = _load_file()
    host = os.environ.get("SNAPMAKER_U1_HOST") or file_cfg.get("host") or default
    if not host:
        raise RuntimeError(
            f"Snapmaker U1 host not configured; set SNAPMAKER_U1_HOST or "
            f"{get_config_path()}"
        )
    return str(host)


def get_u1_port(default: int = FALLBACK_PORT) -> int:
    _load_dotenv_if_present()
    file_cfg = _load_file()
    raw = os.environ.get("SNAPMAKER_U1_PORT") or file_cfg.get("port") or default
    return int(raw)


def get_u1_base_url(host: str | None = None, port: int | None = None) -> str:
    return f"http://{host or get_u1_host()}:{port or get_u1_port()}"
