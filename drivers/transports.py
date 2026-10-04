"""Pure wire transports for projector protocols.

One request/response per call, on a short-lived socket. This module touches NO
TouchDesigner objects and only uses the standard library, so it is safe to call
from Envoy's background worker threads (the ironclad "a worker touches no TD
object" rule) and to import from a headless test.

Every transport has the SAME signature so the caller can dispatch by name:

    fn(ip: str, port: int, timeout_s: float, command: str, ctx: dict) -> str

`ctx` carries protocol extras (e.g. {'password': ...} for PJLink); a transport
ignores keys it does not need. The return is the raw reply, lightly cleaned per
protocol ('' for a bare ack, 'ERR' on a protocol-level failure).

Transports are registered by NAME. The TD extension reads names() and injects
them into DriverManager (extra_transports) and dispatches with get(name); adding
a new vendor's wire protocol is a new function + one REGISTRY entry here, with no
edit to the device-agnostic DriverManager.
"""

from __future__ import annotations
import socket
import hashlib
from typing import Callable, Dict, Optional


def pjlink(ip: str, port: int, timeout_s: float, command: str,
           ctx: Optional[dict] = None) -> str:
    """One PJLink request/response on its own short-lived connection. Handles the
    'PJLINK 1 <seed>' MD5-auth handshake when the projector requires it; a
    no-auth 'PJLINK 0' banner needs no prefix. ctx: {'password': str}."""
    password = str((ctx or {}).get('password', ''))
    s = socket.create_connection((ip, port), timeout=timeout_s)
    try:
        s.settimeout(timeout_s)
        banner = s.recv(64).decode('ascii', 'replace').strip()
        prefix = ''
        parts = banner.split()
        if banner.upper().startswith('PJLINK') and len(parts) >= 2 and parts[1] == '1':
            seed = parts[2] if len(parts) >= 3 else ''
            prefix = hashlib.md5((seed + password).encode('ascii', 'ignore')).hexdigest()
        s.sendall((prefix + command + '\r').encode('ascii', 'ignore'))
        return s.recv(64).decode('ascii', 'replace').strip()
    finally:
        try:
            s.close()
        except Exception:
            pass


def esc_vp21(ip: str, port: int, timeout_s: float, command: str,
             ctx: Optional[dict] = None) -> str:
    """One Epson ESC/VP.net request/response: the 16-byte handshake, then a text
    command terminated with CR. Returns the reply with its trailing ':' prompt and
    CR stripped ('' for a bare ack, 'ERR' on handshake/transport failure)."""
    s = socket.create_connection((ip, port), timeout=timeout_s)
    try:
        s.settimeout(timeout_s)
        s.sendall(b'ESC/VP.net' + bytes([0x10, 0x03, 0x00, 0x00, 0x00, 0x00]))
        if not s.recv(16).startswith(b'ESC/VP.net'):
            return 'ERR'
        s.sendall((command + '\r').encode('ascii', 'ignore'))
        raw = s.recv(64).decode('ascii', 'replace')
        return raw.replace('\r', '').replace(':', '').strip()
    finally:
        try:
            s.close()
        except Exception:
            pass


REGISTRY: Dict[str, Callable[..., str]] = {
    'pjlink': pjlink,
    'esc_vp21': esc_vp21,
}


def names() -> set:
    """The registered transport type names (for DriverManager.extra_transports)."""
    return set(REGISTRY)


def get(name: str) -> Optional[Callable[..., str]]:
    """The transport callable for a type name, or None if unregistered."""
    return REGISTRY.get(name)
