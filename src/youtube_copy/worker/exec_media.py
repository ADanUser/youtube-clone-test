"""Linux exec boundary: rlimits, parent-death signal, no sockets, no inherited secrets."""

import ctypes
import errno
import os
import resource
import signal
import sys


def restrict_network() -> None:
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    context = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("sandbox_initialization_failed")
    try:
        for name in (b"socket", b"socketpair", b"connect"):
            syscall = lib.seccomp_syscall_resolve_name(name)
            if syscall < 0 or lib.seccomp_rule_add(context, 0x00050000 | errno.EPERM, syscall, 0):
                raise RuntimeError("sandbox_rule_failed")
        if lib.seccomp_load(context):
            raise RuntimeError("sandbox_load_failed")
    finally:
        lib.seccomp_release(context)


def run() -> None:
    parent, memory, cpu, file_limit = map(int, sys.argv[1:5])
    command = sys.argv[5:]
    if not command or command[0] not in ("/usr/bin/ffprobe", "/usr/bin/ffmpeg"):
        raise SystemExit(125)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise SystemExit(125)
    if os.getppid() != parent:
        raise SystemExit(125)
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        restrict_network()
    except Exception:
        raise SystemExit(125) from None
    os.execve(command[0], command, {"PATH": "/usr/bin:/bin", "LANG": "C"})


if __name__ == "__main__":
    run()
