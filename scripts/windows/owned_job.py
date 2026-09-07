"""Kill-on-close Windows job for a suspended child and all its descendants."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import subprocess

import psutil


class BasicLimits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                ('flags', wintypes.DWORD), ('min_working', ctypes.c_size_t),
                ('max_working', ctypes.c_size_t), ('active', wintypes.DWORD),
                ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                ('scheduling', wintypes.DWORD)]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', BasicLimits), ('io', ctypes.c_ulonglong * 6),
                ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]


class OwnedJob:
    def __init__(self):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        for name, args in (
            ('CreateJobObjectW', [ctypes.c_void_p, wintypes.LPCWSTR]),
            ('OpenProcess', [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD])):
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def start(self, command, **kwargs) -> subprocess.Popen:
        # Suspension prevents a child escaping before job assignment.
        process = subprocess.Popen(command, creationflags=0x4, **kwargs)
        handle = self.kernel.OpenProcess(0x0100 | 0x0001, False, process.pid)
        try:
            if not handle or not self.kernel.AssignProcessToJobObject(self.handle, handle):
                raise ctypes.WinError(ctypes.get_last_error())
            psutil.Process(process.pid).resume()
        except BaseException:
            process.terminate()
            process.wait(timeout=5)
            raise
        finally:
            if handle:
                self.kernel.CloseHandle(handle)
        return process

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
