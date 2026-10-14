"""RDT host core: read and write firmware variables and memory over a COM port or LAN.

The CLI and the GUI both sit on ``Target``; see ``rdt.target``.
"""

from .errors import (
    AccessRefused, BadReply, CommandRejected, CommandTimeout, ConfigError, DeviceError, LinkDown, LinkError,
    ImageMismatch, McuSilent, RdtError, SymbolError, WriteNotConfirmed, WriteRefused,
)
from .profile import Profile, Range
from .session import LinkState, Session
from .symbols import EnumValue, Location
from .target import ConnectionConfig, McuConfig, Target, TargetConfig
from .workspace import Tab, Watch, Workspace

__version__ = "0.1.0"

__all__ = [
    "AccessRefused", "BadReply", "CommandRejected", "CommandTimeout", "ConfigError", "ConnectionConfig",
    "DeviceError", "ImageMismatch", "LinkDown", "LinkError", "LinkState", "McuConfig", "McuSilent", "Profile", "Range",
    "RdtError", "Session", "SymbolError", "Target", "TargetConfig", "WriteNotConfirmed", "EnumValue", "Location",
    "WriteRefused", "Tab", "Watch", "Workspace",
]
