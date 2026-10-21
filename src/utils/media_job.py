"""Cross-process ownership and source identity primitives for private media jobs."""
import contextlib
import os
from pathlib import Path

def process_identity(pid):
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                times = [wintypes.FILETIME() for _ in range(4)]
                if not kernel.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                    return None
                return f'{times[0].dwHighDateTime}:{times[0].dwLowDateTime}'
            finally:
                kernel.CloseHandle(handle)
        stat = Path(f'/proc/{pid}/stat').read_text()
        return stat[stat.rfind(')') + 2:].split()[19]  # starttime, unaffected by spaces in comm
    except (OSError, IndexError):
        return None

@contextlib.contextmanager
def file_lock(path, blocking=True):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b'0')
            os.lseek(descriptor, 0, os.SEEK_SET)
            # LK_LOCK has a bounded built-in retry; acquisition failure is busy.
            msvcrt.locking(descriptor, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        # Closing releases the lock even on cancellation/exception.
        os.close(descriptor)

def source_signature(path):
    stat = os.stat(path)
    return [stat.st_size, stat.st_mtime_ns, stat.st_ino]
