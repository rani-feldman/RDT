"""Simulated RDT device: answers the SCPI memory commands of one or more MCUs.

It stands in for the real firmware until the memory commands exist there. Run it with
``python -m sim --config sim/examples/two_mcus.toml``.
"""

from .config import SimConfig, TransportConfig, load_config
from .device import Device, ScpiError
from .mcu import Mcu, Signal
from .memory import Memory, MemoryFault, Region
from .profile import CommandPattern, Profile
from .transports import Simulator

__all__ = [
    "CommandPattern", "Device", "Mcu", "Memory", "MemoryFault", "Profile", "Region", "ScpiError",
    "Signal", "SimConfig", "Simulator", "TransportConfig", "load_config",
]
