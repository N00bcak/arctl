"""Supported-host process identity inspection."""

from __future__ import annotations

import ctypes
import errno
import functools
import platform
from dataclasses import dataclass
from pathlib import Path

from .errors import ProcessError, StateError

_PROC_PIDTBSDINFO = 3
_DARWIN_ZOMBIE = 5


@dataclass(frozen=True)
class ProcessIdentity:
    platform: str
    pid: int
    pgid: int
    start_time: int
    state: str

    @property
    def alive(self) -> bool:
        return self.state != "zombie"


class _ProcBSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def process_backend(system: str | None = None) -> str:
    detected = platform.system() if system is None else system
    if detected == "Linux":
        return "procfs"
    if detected == "Darwin":
        return "libproc"
    raise StateError(
        f"unsupported operating system {detected or 'unknown'}; arctl supports Linux and macOS"
    )


def inspect_process(pid: int) -> ProcessIdentity | None:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("pid must be a positive integer")
    backend = process_backend()
    if backend == "procfs":
        return _inspect_linux_process(pid)
    return _inspect_darwin_process(pid)


def _parse_linux_stat(stat: str, *, expected_pid: int) -> ProcessIdentity:
    try:
        close = stat.rfind(")")
        pid = int(stat[: stat.index(" ")])
        fields = stat[close + 2 :].split()
        if close < 0 or pid != expected_pid or len(fields) < 20:
            raise ValueError
        state = fields[0]
        pgid = int(fields[2])
        start_time = int(fields[19])
        if pgid <= 0 or start_time <= 0:
            raise ValueError
    except (ValueError, IndexError) as error:
        raise ProcessError("could not identify managed process") from error
    return ProcessIdentity(
        platform="Linux",
        pid=pid,
        pgid=pgid,
        start_time=start_time,
        state="zombie" if state == "Z" else "running",
    )


def _inspect_linux_process(pid: int) -> ProcessIdentity | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ProcessError("could not identify managed process") from error
    return _parse_linux_stat(stat, expected_pid=pid)


def _decode_darwin_process(
    info: _ProcBSDInfo, *, expected_pid: int
) -> ProcessIdentity:
    start_time = int(info.pbi_start_tvsec) * 1_000_000 + int(
        info.pbi_start_tvusec
    )
    if info.pbi_pid != expected_pid or info.pbi_pgid <= 0 or start_time <= 0:
        raise ProcessError("could not identify managed process")
    return ProcessIdentity(
        platform="Darwin",
        pid=int(info.pbi_pid),
        pgid=int(info.pbi_pgid),
        start_time=start_time,
        state="zombie" if info.pbi_status == _DARWIN_ZOMBIE else "running",
    )


def _inspect_darwin_process(pid: int) -> ProcessIdentity | None:
    try:
        proc_pidinfo = _darwin_proc_pidinfo()
        info = _ProcBSDInfo()
        size = ctypes.sizeof(info)
        ctypes.set_errno(0)
        written = proc_pidinfo(
            pid,
            _PROC_PIDTBSDINFO,
            0,
            ctypes.byref(info),
            size,
        )
    except (AttributeError, OSError) as error:
        raise ProcessError("could not identify managed process") from error
    if written == 0 and ctypes.get_errno() in {errno.ESRCH, errno.ENOENT}:
        return None
    if written != size:
        raise ProcessError("could not identify managed process")
    return _decode_darwin_process(info, expected_pid=pid)


@functools.lru_cache(maxsize=1)
def _darwin_proc_pidinfo():
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    proc_pidinfo = libproc.proc_pidinfo
    proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    proc_pidinfo.restype = ctypes.c_int
    return proc_pidinfo
