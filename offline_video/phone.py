"""Phone access: serve the app on the computer's Wi-Fi address so phones on the same network can use it."""

from __future__ import annotations

import io
import secrets
import socket
import threading

import segno
from werkzeug.serving import make_server

KEY_COOKIE = "ov_key"


def new_key() -> str:
    return secrets.token_urlsafe(16)


def lan_ip() -> str | None:
    """The computer's address on the local network (e.g. 192.168.1.20), or None when offline."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent; this only picks the outgoing interface
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def qr_svg(text: str) -> str:
    return segno.make(text, error="m").svg_inline(scale=5, dark="#000", light="#fff", border=2)


def print_qr(text: str) -> None:
    """Print a QR code to the terminal (skipped where the console can't show the block characters)."""
    buf = io.StringIO()
    segno.make(text, error="m").terminal(out=buf, compact=True)
    try:
        print(buf.getvalue(), end="")
    except UnicodeEncodeError:
        pass


class PhoneServer:
    """A second web server for the same app, listening on the Wi-Fi address instead of 127.0.0.1."""

    def __init__(self, app, port: int):
        self.app = app
        self.port = port
        self._server = None
        self.ip: str | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> str:
        """Start listening; returns the address phones should open. Raises OSError on failure."""
        if self._server:
            return f"http://{self.ip}:{self._server.server_port}"
        ip = lan_ip()
        if ip is None:
            raise OSError("This computer doesn't seem to be connected to a Wi-Fi or local network.")
        try:
            server = make_server(ip, self.port, self.app, threaded=True)
        except OSError:
            server = make_server(ip, 0, self.app, threaded=True)  # port taken on that address: any free one
        self._server, self.ip = server, ip
        threading.Thread(target=server.serve_forever, name="phone-server", daemon=True).start()
        return f"http://{ip}:{server.server_port}"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
