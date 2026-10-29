"""Minimal Windows pseudo console (ConPTY) driver for tests: start a program attached to a real console, write keys to
it, read the VT bytes it paints, resize it. ctypes only, no extra package. Windows 10 1809+."""
import ctypes
import os
import subprocess
import threading
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

HPCON = wintypes.HANDLE
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STILL_ACTIVE = 259


class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD)]


k32.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p, wintypes.DWORD]
k32.CreatePseudoConsole.argtypes = [COORD, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(HPCON)]
k32.CreatePseudoConsole.restype = ctypes.HRESULT
k32.ResizePseudoConsole.argtypes = [HPCON, COORD]
k32.ResizePseudoConsole.restype = ctypes.HRESULT
k32.ClosePseudoConsole.argtypes = [HPCON]
k32.ClosePseudoConsole.restype = None
k32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
k32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
                                          ctypes.c_void_p, ctypes.c_void_p]
k32.CreateProcessW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOEXW), ctypes.POINTER(PROCESS_INFORMATION)]
k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
k32.DeleteProcThreadAttributeList.restype = None
k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
k32.CloseHandle.argtypes = [wintypes.HANDLE]


class ConPty:
    def __init__(self, args, env, cwd, cols=100, rows=30):
        self.buf = bytearray()
        in_r, in_w = wintypes.HANDLE(), wintypes.HANDLE()
        out_r, out_w = wintypes.HANDLE(), wintypes.HANDLE()
        for r, w in ((in_r, in_w), (out_r, out_w)):
            if not k32.CreatePipe(ctypes.byref(r), ctypes.byref(w), None, 0):
                raise ctypes.WinError(ctypes.get_last_error())
        self.hpc = HPCON()
        k32.CreatePseudoConsole(COORD(cols, rows), in_r, out_w, 0, ctypes.byref(self.hpc))
        k32.CloseHandle(in_r)
        k32.CloseHandle(out_w)
        self._in_w, self._out_r = in_w, out_r
        size = ctypes.c_size_t()
        k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))      # sizing call: fails by design
        self._attrs = ctypes.create_string_buffer(size.value)
        if not k32.InitializeProcThreadAttributeList(self._attrs, 1, 0, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not k32.UpdateProcThreadAttribute(self._attrs, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, self.hpc.value, ctypes.sizeof(HPCON), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si.lpAttributeList = ctypes.cast(self._attrs, ctypes.c_void_p)
        # No STARTF_USESTDHANDLES: the pseudo console supplies the child's stdin/stdout/stderr. With the flag set and null
        # handles the child would get no standard handles at all. Our own stdio is not inherited (bInheritHandles=FALSE).
        pi = PROCESS_INFORMATION()
        envblock = "".join(f"{k}={v}\0" for k, v in env.items()) + "\0"
        cmd = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))      # CreateProcessW may write into it
        envbuf = ctypes.create_unicode_buffer(envblock, len(envblock))
        ok = k32.CreateProcessW(None, cmd, None, None, False, EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
                                ctypes.cast(envbuf, ctypes.c_void_p), str(cwd), ctypes.byref(si), ctypes.byref(pi))
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        self._proc = pi.hProcess
        k32.CloseHandle(pi.hThread)
        self._t = threading.Thread(target=self._pump, daemon=True)
        self._t.start()

    def _pump(self):
        chunk = ctypes.create_string_buffer(65536)
        n = wintypes.DWORD()
        while True:              # drain until the pipe breaks: ClosePseudoConsole waits for its last output to be read
            if not k32.ReadFile(self._out_r, chunk, 65536, ctypes.byref(n), None) or n.value == 0:
                return
            self.buf.extend(chunk.raw[:n.value])

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        n = wintypes.DWORD()
        k32.WriteFile(self._in_w, data, len(data), ctypes.byref(n), None)

    def resize(self, cols, rows):
        k32.ResizePseudoConsole(self.hpc, COORD(cols, rows))

    def poll(self):
        code = wintypes.DWORD()
        k32.GetExitCodeProcess(self._proc, ctypes.byref(code))
        return None if code.value == STILL_ACTIVE else code.value

    def wait(self, timeout):
        k32.WaitForSingleObject(self._proc, int(timeout * 1000))
        code = self.poll()
        if code is None:
            raise subprocess.TimeoutExpired("conpty", timeout)
        return code

    def kill(self):
        if self.poll() is None:
            k32.TerminateProcess(self._proc, 1)

    def close(self):
        self.kill()
        k32.ClosePseudoConsole(self.hpc)       # also ends the output pipe, which stops the reader
        self._t.join(3)
        k32.DeleteProcThreadAttributeList(self._attrs)
        for h in (self._in_w, self._out_r, self._proc):
            k32.CloseHandle(h)
