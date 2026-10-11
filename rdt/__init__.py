"""RDT host core: read and write firmware variables and memory over a COM port or LAN.

The CLI and the GUI both sit on ``Target``; see ``rdt.target``.
"""

from .errors import (
    AccessRefused, BadReply, CommandRejected, CommandTimeout, ConfigError, DeviceError, LinkDown, LinkError,
    McuSilent, RdtError, WriteNotConfirmed,
)
from .profile import Profile, Range
from .session import LinkState, Session
from .target import ConnectionConfig, McuConfig, RawRef, Target, TargetConfig, parse_ref

__version__ = "0.1.0"

__all__ = [
    "AccessRefused", "BadReply", "CommandRejected", "CommandTimeout", "ConfigError", "ConnectionConfig",
    "DeviceError", "LinkDown", "LinkError", "LinkState", "McuConfig", "McuSilent", "Profile", "Range", "RawRef",
    "RdtError", "Session", "Target", "TargetConfig", "WriteNotConfirmed", "parse_ref",
]
