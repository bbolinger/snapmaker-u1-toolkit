"""Cross-platform exclusive inter-process file lock for safety state.

One primitive, used by every safety-critical read-modify-write in the
toolkit (request document, Stage-2 nonce consumption, audit seq+append):

    with exclusive_lock(lock_path):
        ... read, decide, write ...

Backends:
  POSIX:   fcntl.flock(LOCK_EX) — blocking, advisory, released on close/exit.
  Windows: msvcrt.locking on a 1-byte region, polled non-blocking with
           backoff so it blocks like flock instead of msvcrt's own
           10-tries-then-raise LK_LOCK behavior. Released on close/exit.

Design rules (v2.4.1 Windows port):
  * NO silent fallback. If neither backend imports, this module fails to
    import and every caller fails loud — a safety-state mutation must never
    run unlocked. Callers already treat lock errors as fail-closed.
  * Lock a SIDECAR file, never the data file. Windows msvcrt locks are
    mandatory and per-HANDLE: a process holding a lock on byte 0 of a data
    file cannot read that byte back through a second handle, which breaks
    read-under-lock patterns (audit's seq count re-open did exactly that).
    Sidecar files are never read, so the mandatory-lock semantics reduce
    to flock-style mutual exclusion.
  * Blocking, no timeout — flock parity. Locks here are held for
    milliseconds around small-file writes; a wedged holder is a process
    bug, and both OSes release the lock when the holder dies.

Same-process semantics match flock: each exclusive_lock() opens its own
file handle, so two threads (or a re-entrant caller) contend exactly like
two processes do. Do not nest the same lock.
"""
from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path

if os.name == 'nt':
    import msvcrt

    # Contention errnos from the CRT: EACCES from LK_NBLCK on a held lock
    # (the documented case), EDEADLOCK from LK_LOCK's retry exhaustion
    # (defensive — we never use LK_LOCK, but CRT builds have varied).
    _CONTENTION = (errno.EACCES, errno.EDEADLOCK)

    def _lock_fd(fd: int) -> None:
        delay = 0.05
        while True:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in _CONTENTION:
                    raise  # real error, not contention — fail loud
            time.sleep(delay)
            delay = min(delay * 2, 0.5)

    def _unlock_fd(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def exclusive_lock(lock_path: Path | str):
    """Blocking exclusive inter-process lock on ``lock_path``.

    Creates the lock file if missing (the parent dir must exist — callers
    ensure their request dir first, and a vanished dir should fail loud,
    not be silently recreated by the lock layer). Hold it across the FULL
    read-modify-write. Any failure to acquire raises; callers treat that
    as fail-closed for safety decisions.
    """
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        _lock_fd(fd)
        try:
            yield
        finally:
            try:
                _unlock_fd(fd)
            except OSError:
                pass  # close() releases the lock regardless, on both OSes
    finally:
        os.close(fd)
