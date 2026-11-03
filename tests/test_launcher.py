import shutil
import struct
import sys
from pathlib import Path

import pytest

from claude_discord_rpc import launcher


def _blocks(data: bytes, offset: int, end: int):
    """Yield (key, start, length) for sibling VERSIONINFO blocks in data[offset:end]."""
    while offset < end:
        length, _value_len, _type = struct.unpack_from("<HHH", data, offset)
        key_end = data.index(b"\0\0", offset + 6)
        key_end += (key_end - offset) % 2  # stay on a UTF-16 boundary
        yield data[offset + 6 : key_end].decode("utf-16-le"), offset, length
        offset += length + (-length % 4)


def test_version_info_layout():
    data = launcher.build_version_info("Claude Code Discord RPC", (3, 14, 7, 0))
    length, value_len, _ = struct.unpack_from("<HHH", data)
    assert length == len(data)
    assert value_len == 52
    signature = struct.unpack_from("<I", data, data.index(struct.pack("<I", 0xFEEF04BD)))[0]
    assert signature == 0xFEEF04BD
    assert "Claude Code Discord RPC\0".encode("utf-16-le") in data
    assert "040904b0".encode("utf-16-le") in data

    children_start = 40 + 52  # header+key padded to 40 bytes, then VS_FIXEDFILEINFO
    keys = [key for key, _, _ in _blocks(data, children_start, len(data))]
    assert keys == ["StringFileInfo", "VarFileInfo"]


@pytest.mark.skipif(sys.platform != "win32", reason="PE resources are Windows-only")
def test_set_version_info_on_real_pythonw(tmp_path):
    source = Path(sys.base_prefix) / "pythonw.exe"
    if not source.exists():
        pytest.skip("no pythonw.exe in this Python install")
    exe = tmp_path / "copy.exe"
    shutil.copyfile(source, exe)

    launcher._set_version_info(exe, launcher.build_version_info("Test Description", (1, 2, 3, 4)))

    assert launcher._version_languages(exe) == [0x0409]
    assert "Test Description\0".encode("utf-16-le") in exe.read_bytes()
