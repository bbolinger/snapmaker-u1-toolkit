"""The cross-platform lock must actually exclude PROCESSES, not just threads.

u1_lockfile.exclusive_lock guards every safety-critical read-modify-write
(request doc, Stage-2 nonce, audit seq). The load-bearing test is real
multi-process contention on a lost-update workload: N subprocesses each do
read-increment-write on a shared counter under the lock; any lost update
means the mutual exclusion is fake. This same file runs unmodified on
Windows, where it validates the msvcrt backend the dev boxes can't.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from u1_lockfile import exclusive_lock

_WORKER = r"""
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from u1_lockfile import exclusive_lock

counter = Path(sys.argv[2])
lock = Path(sys.argv[3])
n = int(sys.argv[4])
for _ in range(n):
    with exclusive_lock(lock):
        value = int(counter.read_text() or "0")
        counter.write_text(str(value + 1))
"""

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")


def test_interprocess_lost_update_prevented(tmp_path):
    counter = tmp_path / "counter"
    counter.write_text("0")
    lock = tmp_path / "counter.lock"
    procs = 4
    increments = 50
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER, _SCRIPTS_DIR,
             str(counter), str(lock), str(increments)],
        )
        for _ in range(procs)
    ]
    for w in workers:
        assert w.wait(timeout=120) == 0
    assert int(counter.read_text()) == procs * increments, (
        "lost update: the lock did not exclude concurrent processes"
    )


def test_threads_contend_like_processes(tmp_path):
    # Each exclusive_lock() opens its own handle, so threads in one process
    # must serialize exactly like separate processes (flock/msvcrt parity).
    counter = tmp_path / "counter"
    counter.write_text("0")
    lock = tmp_path / "counter.lock"
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        for _ in range(25):
            with exclusive_lock(lock):
                counter.write_text(str(int(counter.read_text()) + 1))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert int(counter.read_text()) == 8 * 25


def test_lock_released_on_exit(tmp_path):
    lock = tmp_path / "x.lock"
    with exclusive_lock(lock):
        pass
    # Re-acquire immediately; a leaked hold would block forever (the
    # subprocess-based tests above would also hang, but this pins the
    # same-process release path explicitly).
    acquired = []

    def second():
        with exclusive_lock(lock):
            acquired.append(True)

    t = threading.Thread(target=second)
    t.start()
    t.join(timeout=10)
    assert acquired == [True]


def test_missing_parent_dir_fails_loud(tmp_path):
    # The lock layer must not silently mkdir its way past a vanished
    # request dir — that hides state corruption. It raises; safety
    # callers treat any lock failure as fail-closed.
    with pytest.raises(OSError):
        with exclusive_lock(tmp_path / "gone" / "x.lock"):
            pass


def test_exception_inside_lock_still_releases(tmp_path):
    lock = tmp_path / "x.lock"
    with pytest.raises(RuntimeError):
        with exclusive_lock(lock):
            raise RuntimeError("boom")
    with exclusive_lock(lock):  # would deadlock if the first hold leaked
        pass
