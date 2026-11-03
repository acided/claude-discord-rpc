"""Single-instance lock for the daemon (OS-level, released automatically if the process dies)."""

import os
from pathlib import Path

if os.name == "nt":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def acquire(path: Path) -> int | None:
    """Return an fd holding the lock, or None if another process holds it. Keep the fd open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    if _try_lock(fd):
        return fd
    os.close(fd)
    return None


def is_held(path: Path) -> bool:
    """True if some process (the daemon) currently holds the lock."""
    fd = acquire(path)
    if fd is None:
        return True
    _unlock(fd)
    os.close(fd)
    return False
