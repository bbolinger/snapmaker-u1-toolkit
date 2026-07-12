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
    # Candidates in priority order:
    #  1. cwd-walk: find a .env at cwd or any ancestor (developer convenience —
    #     `cd repo; python3 script` Just Works)
    #  2. Hermes runtime canonical location: /opt/data/.env. This is the
    #     deployed runtime's env file; works regardless of where the
    #     subprocess was spawned from (live harness regression 2026-06-28:
    #     U1_OPERATOR was missing because Hermes invoked the workflow with
    #     cwd=/tmp, where the cwd-walk found no .env).
    candidates: list[Path] = []
    try:
        cur = Path.cwd().resolve()
        for parent in [cur, *cur.parents]:
            candidates.append(parent / ".env")
    except OSError:
        pass
    candidates.append(Path("/opt/data/.env"))
    for candidate in candidates:
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
    # Honor HOME env first so Git Bash/MSYS users and pytest's monkeypatch
    # get the documented XDG-style behavior. On Windows, `Path.home()`
    # asks Windows profile APIs and silently ignores HOME, which makes
    # the test suite fail and surprises Git Bash users. (Hermes Windows
    # install smoke, 2026-06-22.)
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".local" / "share"
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
    # Test/fresh-install overrides should win even when this suite is run on
    # Hermes host paths where /opt/data/snapmaker_u1 exists. In normal runtime
    # with no explicit XDG/HOME monkeypatch, keep the Hermes default.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return _xdg_data_home() / "snapmaker-u1"
    if os.environ.get("XDG_DATA_HOME"):
        return _xdg_data_home() / "snapmaker-u1"
    home = os.environ.get("HOME", "")
    if "pytest-" in home or "pytest-of-" in home:
        return _xdg_data_home() / "snapmaker-u1"
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


def set_printer(host: str, port: int | None = None) -> Path:
    """Persist the printer endpoint into the config file so every future
    process (including emitted child commands) resolves it. MERGES with
    existing keys - orca_bin and friends survive; never a wholesale
    replace."""
    import uuid as _uuid
    path = get_config_path()
    cfg = _load_file()
    cfg["host"] = str(host)
    if port is not None:
        cfg["port"] = int(port)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{_uuid.uuid4().hex}")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.replace(tmp, path)
    return path


def get_orca_bin(
        default: str = "/opt/data/tools/orcaslicer/squashfs-root/bin/orca-slicer",
) -> str:
    """OrcaSlicer executable: ORCA_SLICER_BIN env > config 'orca_bin' > the
    Linux deploy default. The config-file source is what makes the path
    survive into EMITTED child commands - a one-shot shell export dies with
    the shell that ran the first command, which is exactly how the first
    real Windows slice failed (2026-07-12, WinError 2 on the default Linux
    path)."""
    _load_dotenv_if_present()
    file_cfg = _load_file()
    return str(os.environ.get("ORCA_SLICER_BIN")
               or file_cfg.get("orca_bin") or default)


def get_u1_base_url(host: str | None = None, port: int | None = None) -> str:
    return f"http://{host or get_u1_host()}:{port or get_u1_port()}"


def get_operator_binding() -> tuple[str, str] | None:
    """Resolve the (platform, user_id) pair the confirm-start hook binds
    the operator's YES to — e.g. ("telegram", "123456789"). Returns None
    when unconfigured: the hook then refuses every YES (fail closed) and
    the workflow warns at arm time.

    Priority:
      1. U1_OPERATOR_BINDING env — "platform:user_id". A non-empty value
         that doesn't parse returns None rather than falling through: an
         explicit override that's malformed should surface as missing, not
         silently bind to whatever the fallbacks say.
      2. u1_config.json key "operator_binding" — same format, same rule.
      3. TELEGRAM_ALLOWED_USERS env (the gateway allowlist of sender ids) —
         used only when it holds exactly ONE id; several ids can't name
         THE operator.
      4. TELEGRAM_HOME_CHANNEL env — the chat the bed-clear DM is delivered
         to; in a single-operator DM setup the chat id IS the operator's
         user id.

    U1_OPERATOR is a display identity ("telegram:brent"), not the gateway's
    numeric user id, so it is deliberately NOT a source here — the hook
    compares against the message context's user_id, which is numeric.
    """
    _load_dotenv_if_present()
    raw = os.environ.get("U1_OPERATOR_BINDING", "").strip()
    if not raw:
        file_val = _load_file().get("operator_binding")
        raw = str(file_val).strip() if file_val is not None else ""
    if raw:
        platform, sep, user_id = raw.partition(":")
        platform = platform.strip().lower()
        user_id = user_id.strip()
        if sep and platform and user_id:
            return platform, user_id
        return None
    allowed = [u.strip() for u in
               os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
               if u.strip()]
    if len(allowed) == 1:
        return "telegram", allowed[0]
    home = os.environ.get("TELEGRAM_HOME_CHANNEL", "").strip()
    if home:
        return "telegram", home
    return None
