"""JSON-RPC 2.0 over stdio with LSP framing (Content-Length headers).

One background thread reads the server's stdout and routes each message:
responses wake the caller waiting on that id, notifications and server-to-client
requests go to the handlers supplied by the owner.
"""

from __future__ import annotations

import json
import threading
from typing import Any, BinaryIO, Callable


class LspError(RuntimeError):
    pass


def encode(message: dict) -> bytes:
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


def read_message(stream: BinaryIO) -> dict | None:
    """Read one framed message. Returns None at end of stream."""
    length = None
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            if length is None:
                continue  # stray blank line between messages
            break
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    body = stream.read(length)
    if len(body) < length:
        return None
    return json.loads(body)


class Connection:
    def __init__(
        self,
        reader: BinaryIO,
        writer: BinaryIO,
        on_notification: Callable[[str, Any], None],
        on_request: Callable[[str, Any], Any] = lambda method, params: None,
    ):
        self._reader, self._writer = reader, writer
        self._on_notification, self._on_request = on_notification, on_request
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, tuple[threading.Event, dict]] = {}
        self._closed = False
        self._thread = threading.Thread(target=self._read_loop, name="lsp-reader", daemon=True)
        self._thread.start()

    def _send(self, message: dict) -> None:
        with self._write_lock:
            try:
                self._writer.write(encode(message))
                self._writer.flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                raise LspError(f"language server is not running: {exc}") from exc

    def notify(self, method: str, params: Any = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: Any = None, timeout: float = 60.0) -> Any:
        with self._state_lock:
            if self._closed:
                raise LspError("language server connection is closed")
            request_id = self._next_id
            self._next_id += 1
            slot = (threading.Event(), {})
            self._pending[request_id] = slot
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        done, box = slot
        if not done.wait(timeout):
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise LspError(f"{method} timed out after {timeout:.0f}s")
        if "error" in box:
            err = box["error"]
            raise LspError(f"{method} failed: {err.get('message', err)}")
        return box.get("result")

    def _read_loop(self) -> None:
        try:
            while True:
                message = read_message(self._reader)
                if message is None:
                    break
                self._dispatch(message)
        except (ValueError, OSError):
            pass
        finally:
            with self._state_lock:
                self._closed = True
                pending, self._pending = self._pending, {}
            for done, box in pending.values():
                box["error"] = {"message": "language server exited"}
                done.set()

    def _dispatch(self, message: dict) -> None:
        if "method" in message and "id" in message:  # server asks the client something
            try:
                result = self._on_request(message["method"], message.get("params"))
                self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})
            except LspError:
                pass
        elif "method" in message:
            self._on_notification(message["method"], message.get("params"))
        elif "id" in message:
            with self._state_lock:
                slot = self._pending.pop(message["id"], None)
            if slot is not None:
                done, box = slot
                box.update({k: message[k] for k in ("result", "error") if k in message})
                done.set()
