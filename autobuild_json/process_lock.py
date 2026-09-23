"""Single writer across application processes (released automatically on crash)."""
import os

from .errors import FlowError
from .results import private_directory


class ProcessLock:
    def __init__(self, root):
        self.root = root
        self.fd = None

    def acquire(self):
        private_directory(self.root)
        path = self.root / ".application.lock"
        if path.is_symlink():
            raise FlowError("CONFIGURATION_ERROR", "process_lock")
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                if os.name == "nt":
                    import msvcrt
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b"0")
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except Exception:
                os.close(fd)
                raise
            self.fd = fd
        except OSError:
            raise FlowError("CONFIGURATION_ERROR", "process_lock") from None

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
