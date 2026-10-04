"""Pure response parsers for projector protocol replies.

Each parser takes a raw reply string and returns a normalized value string. This
module touches NO TouchDesigner objects and uses only the standard library, so it
is safe to call from a background worker thread and to import headless.

Parsers are registered by NAME. A capability's 'parse' field names one; the TD
extension reads names() to inject them into DriverManager (extra_parsers) and
dispatches with get(name). pjlink_text is the safe default for an unrecognized
name. Adding a vendor's reply format is a new function + one REGISTRY entry here.
"""

from __future__ import annotations
from typing import Callable, Dict, Optional

# PJLink error replies that mean "no value" (ERR1..ERR4, ERRA).
_PJLINK_ERRS = ('ERR1', 'ERR2', 'ERR3', 'ERR4', 'ERRA')


def pjlink_power(resp: str) -> str:
    """PJLink %1POWR reply -> status digit ('0'..'3'), 'OK', or 'ERR'."""
    r = (resp or '').strip().upper()
    if '=' in r:
        v = r.split('=', 1)[1].strip()
        if v in ('0', '1', '2', '3'):
            return v
        if v == 'OK':
            return 'OK'
    return 'ERR'


def pjlink_ack(resp: str) -> str:
    """A PJLink command ack: '=OK' -> 'OK', anything else -> 'ERR'."""
    r = (resp or '').strip().upper()
    if '=' in r and r.split('=', 1)[1].strip() == 'OK':
        return 'OK'
    return 'ERR'


def pjlink_text(resp: str) -> str:
    """A PJLink info reply (e.g. %1INF2=EB-2250U) -> its text value; '' on error."""
    r = (resp or '').strip()
    if '=' in r:
        v = r.split('=', 1)[1].strip()
        if v.upper() in _PJLINK_ERRS:
            return ''
        return v
    return ''


def pjlink_lamp(resp: str) -> str:
    """PJLink %1LAMP reply ('hours on/off [hours on/off ...]') -> cumulative lamp
    hour count(s): '8262', or '8262/13' for a two-lamp unit; '' on error."""
    r = (resp or '').strip()
    if '=' not in r:
        return ''
    v = r.split('=', 1)[1].strip()
    if v.upper() in _PJLINK_ERRS:
        return ''
    toks = v.split()
    hours = [toks[i] for i in range(0, len(toks), 2)]   # skip the on/off flags
    return '/'.join(hours) if hours else v


def escvp_val(resp: str) -> str:
    """ESC/VP21 query reply 'KEY=VALUE' -> VALUE; 'ERR' -> ''."""
    r = (resp or '').strip()
    if r.upper() == 'ERR':
        return ''
    return r.split('=', 1)[1].strip() if '=' in r else r


def escvp_ack(resp: str) -> str:
    """ESC/VP21 command ack: bare prompt -> 'OK', 'ERR' -> 'ERR'."""
    return 'ERR' if (resp or '').strip().upper() == 'ERR' else 'OK'


REGISTRY: Dict[str, Callable[[str], str]] = {
    'pjlink_power': pjlink_power,
    'pjlink_ack': pjlink_ack,
    'pjlink_text': pjlink_text,
    'pjlink_lamp': pjlink_lamp,
    'escvp_val': escvp_val,
    'escvp_ack': escvp_ack,
}

# The safe fallback when a capability names an unknown parser (matches the
# extension's prior behavior of defaulting unrecognized parses to info text).
DEFAULT = 'pjlink_text'


def names() -> set:
    """The registered parser names (for DriverManager.extra_parsers)."""
    return set(REGISTRY)


def get(name: str) -> Optional[Callable[[str], str]]:
    """The parser callable for a name, falling back to pjlink_text."""
    return REGISTRY.get(name) or REGISTRY[DEFAULT]
