"""Pick the interpreter that runs the daemon.

On Windows the daemon runs from a renamed copy of pythonw.exe (``claude-rpc.exe``) whose
version resource says "Claude Code Discord RPC", so it is recognizable in Task Manager
instead of showing up as yet another "Python". Any failure falls back to plain pythonw.exe.
"""

import ctypes
import os
import shutil
import struct
import sys
from pathlib import Path

from claude_discord_rpc import paths

EXE_NAME = "claude-rpc.exe"
DESCRIPTION = "Claude Code Discord RPC"

_RT_VERSION = 16
_LANG_EN_US = 0x0409
_CODEPAGE_UNICODE = 1200


def _pad(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % 4)


def _node(key: str, value: bytes = b"", value_len: int = 0, text: bool = False, children: tuple[bytes, ...] = ()) -> bytes:
    """One VERSIONINFO block: wLength, wValueLength, wType, szKey, Value, Children."""
    data = struct.pack("<HHH", 0, value_len, int(text)) + (key + "\0").encode("utf-16-le")
    if value:
        data = _pad(data) + value
    for child in children:
        data = _pad(data) + child
    return struct.pack("<H", len(data)) + data[2:]


def _string(key: str, text: str) -> bytes:
    return _node(key, (text + "\0").encode("utf-16-le"), len(text) + 1, text=True)


def build_version_info(description: str, version: tuple[int, int, int, int]) -> bytes:
    """Serialize a minimal VS_VERSIONINFO resource."""
    ms, ls = (version[0] << 16) | version[1], (version[2] << 16) | version[3]
    fixed = struct.pack(
        "<13I",
        0xFEEF04BD, 0x00010000,  # signature, struct version
        ms, ls, ms, ls,  # file / product version
        0x3F, 0,  # flags mask, flags
        0x00040004, 1, 0,  # VOS_NT_WINDOWS32, VFT_APP, no subtype
        0, 0,  # date
    )
    dotted = ".".join(map(str, version))
    strings = _node(
        f"{_LANG_EN_US:04x}{_CODEPAGE_UNICODE:04x}",
        children=(
            _string("FileDescription", description),
            _string("ProductName", description),
            _string("FileVersion", dotted),
            _string("ProductVersion", dotted),
            _string("OriginalFilename", EXE_NAME),
        ),
    )
    translation = _node("Translation", struct.pack("<HH", _LANG_EN_US, _CODEPAGE_UNICODE), 4)
    return _node(
        "VS_VERSION_INFO",
        fixed,
        len(fixed),
        children=(_node("StringFileInfo", children=(strings,)), _node("VarFileInfo", children=(translation,))),
    )


def _version_languages(exe: Path) -> list[int]:
    """Languages of the existing RT_VERSION #1 resource, so it can be replaced instead of duplicated."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LoadLibraryExW.restype = ctypes.c_void_p
    kernel32.LoadLibraryExW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.FreeLibrary.argtypes = [ctypes.c_void_p]
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint16, ctypes.c_void_p)
    kernel32.EnumResourceLanguagesW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, callback_type, ctypes.c_void_p]

    module = kernel32.LoadLibraryExW(str(exe), None, 0x2)  # LOAD_LIBRARY_AS_DATAFILE
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    langs: list[int] = []
    try:
        callback = callback_type(lambda _m, _t, _n, lang, _p: langs.append(lang) or 1)
        kernel32.EnumResourceLanguagesW(module, _RT_VERSION, 1, callback, None)
    finally:
        kernel32.FreeLibrary(module)
    return langs


def _set_version_info(exe: Path, resource: bytes) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.BeginUpdateResourceW.restype = ctypes.c_void_p
    kernel32.BeginUpdateResourceW.argtypes = [ctypes.c_wchar_p, ctypes.c_int]
    kernel32.UpdateResourceW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint16, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.EndUpdateResourceW.argtypes = [ctypes.c_void_p, ctypes.c_int]

    old_langs = _version_languages(exe)
    handle = kernel32.BeginUpdateResourceW(str(exe), False)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    ok = True
    for lang in old_langs:
        ok = ok and kernel32.UpdateResourceW(handle, _RT_VERSION, 1, lang, None, 0)
    buffer = ctypes.create_string_buffer(resource, len(resource))
    ok = ok and kernel32.UpdateResourceW(handle, _RT_VERSION, 1, _LANG_EN_US, buffer, len(resource))
    error = ctypes.get_last_error()
    if not kernel32.EndUpdateResourceW(handle, not ok) or not ok:
        raise ctypes.WinError(error or ctypes.get_last_error())


def _renamed_pythonw(base: Path) -> Path:
    """Return DATA_DIR/claude-rpc.exe, (re)creating it when the source pythonw.exe changed."""
    source = base / "pythonw.exe"
    target = paths.DATA_DIR / EXE_NAME
    stamp_file = target.with_suffix(".source")
    st = source.stat()
    stamp = f"{source}|{st.st_size}|{st.st_mtime_ns}"
    try:
        if target.exists() and stamp_file.read_text(encoding="utf-8") == stamp:
            return target
    except OSError:
        pass

    tmp = target.with_suffix(".tmp")
    shutil.copyfile(source, tmp)
    try:
        version = tuple(sys.version_info[:3]) + (0,)
        _set_version_info(tmp, build_version_info(DESCRIPTION, version))  # type: ignore[arg-type]
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    stamp_file.write_text(stamp, encoding="utf-8")
    return target


def daemon_command() -> tuple[str, dict[str, str] | None]:
    """Interpreter path and environment (None = inherit) for spawning the daemon."""
    exe = Path(sys.executable)
    if sys.platform != "win32":
        return str(exe), None

    # base_prefix, not exe's folder: a venv's Scripts\ has no python DLL next to it.
    base = Path(sys.base_prefix)
    try:
        renamed = _renamed_pythonw(base)
        env = dict(os.environ)
        # The copy lives outside the Python install: point it at the stdlib and the python DLL.
        env["PYTHONHOME"] = str(base)
        env["PATH"] = os.pathsep.join([str(base), env.get("PATH", "")])
        env.pop("PYTHONPATH", None)
        return str(renamed), env
    except Exception:  # noqa: BLE001 - a nicer process name is never worth a missing presence
        pythonw = exe.with_name("pythonw.exe")
        return str(pythonw if pythonw.exists() else exe), None
