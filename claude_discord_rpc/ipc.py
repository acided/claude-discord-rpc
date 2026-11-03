"""Minimal Discord RPC client over the local IPC named pipe (Windows) / unix socket."""

import json
import os
import socket
import struct
import sys
import tempfile
import uuid
from typing import Any

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2


class DiscordIPCError(OSError):
    pass


def _candidate_paths() -> list[str]:
    if sys.platform == "win32":
        return [rf"\\.\pipe\discord-ipc-{i}" for i in range(10)]
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or tempfile.gettempdir()
    dirs = [base, os.path.join(base, "app", "com.discordapp.Discord"), os.path.join(base, "snap.discord")]
    return [os.path.join(d, f"discord-ipc-{i}") for d in dirs for i in range(10)]


class DiscordIPC:
    def __init__(self, client_id: str):
        self.client_id = client_id
        self._pipe: Any = None
        self._sock: socket.socket | None = None

    # transport -----------------------------------------------------------
    def _open(self) -> None:
        last_error: Exception | None = None
        for path in _candidate_paths():
            try:
                if sys.platform == "win32":
                    self._pipe = open(path, "r+b", buffering=0)
                else:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(path)
                    self._sock = sock
                return
            except OSError as exc:
                last_error = exc
        raise DiscordIPCError(f"Discord IPC not available: {last_error}")

    def _write(self, data: bytes) -> None:
        if self._pipe is not None:
            self._pipe.write(data)
            self._pipe.flush()
        elif self._sock is not None:
            self._sock.sendall(data)
        else:
            raise DiscordIPCError("not connected")

    def _read_exact(self, size: int) -> bytes:
        buf = b""
        while len(buf) < size:
            if self._pipe is not None:
                chunk = self._pipe.read(size - len(buf))
            elif self._sock is not None:
                chunk = self._sock.recv(size - len(buf))
            else:
                raise DiscordIPCError("not connected")
            if not chunk:
                raise DiscordIPCError("connection closed by Discord")
            buf += chunk
        return buf

    def _send(self, op: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._write(struct.pack("<II", op, len(body)) + body)

    def _recv(self) -> tuple[int, dict[str, Any]]:
        op, length = struct.unpack("<II", self._read_exact(8))
        return op, json.loads(self._read_exact(length).decode("utf-8"))

    # public API ----------------------------------------------------------
    def connect(self) -> dict[str, Any]:
        self._open()
        self._send(OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
        op, data = self._recv()
        if op == OP_CLOSE or data.get("evt") != "READY":
            self.close()
            raise DiscordIPCError(f"handshake rejected: {data}")
        return data

    def set_activity(self, activity: dict[str, Any] | None) -> dict[str, Any]:
        """Send SET_ACTIVITY (None clears). Returns Discord's response frame."""
        self._send(
            OP_FRAME,
            {
                "cmd": "SET_ACTIVITY",
                "args": {"pid": os.getpid(), "activity": activity},
                "nonce": str(uuid.uuid4()),
            },
        )
        op, data = self._recv()
        if op == OP_CLOSE:
            self.close()
            raise DiscordIPCError(f"closed by Discord: {data}")
        return data

    def close(self) -> None:
        for handle in (self._pipe, self._sock):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        self._pipe = None
        self._sock = None
