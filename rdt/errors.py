"""Exceptions raised by the RDT host core. Every one of them is an ``RdtError``."""

from __future__ import annotations


class RdtError(Exception):
    """Base class for every error the host core raises on purpose."""


class ConfigError(RdtError):
    """A profile, target file or command-line option is invalid."""


class SymbolError(RdtError):
    """A name, type or expression can't be resolved from the MCU's ELF."""


class WriteRefused(RdtError):
    """The workspace's write safety refused a write: read-only, out of limits, or not confirmed."""


class ImageMismatch(WriteRefused):
    """The MCU's ELF does not match the firmware it runs (SYM-8): its reads and writes are refused
    until the right ELF is loaded or the user allows access anyway."""


class LinkError(RdtError):
    """The transport could not be opened, or it broke while in use."""


class LinkDown(RdtError):
    """The link is down. Values are stale and writes are refused until RDT reconnects."""


class AccessRefused(RdtError):
    """The address range is outside the MCU's ELF memory and the listed ranges, or is read-only."""


class McuSilent(RdtError):
    """A secondary MCU stopped answering. Only that MCU's accesses fail; the others keep working."""


class CommandTimeout(RdtError):
    """The device sent no reply. The firmware does this when a command fails."""


class CommandRejected(RdtError):
    """The device answered a read with an error instead of data."""

    def __init__(self, error: "DeviceError"):
        super().__init__(f"device error {error}")
        self.error = error


class BadReply(RdtError):
    """A reply arrived but could not be decoded, or had the wrong size."""


class WriteNotConfirmed(RdtError):
    """The value read back after a write is not the value written."""

    def __init__(self, message: str, written: bytes, read_back: bytes | None):
        super().__init__(message)
        self.written = written
        self.read_back = read_back


class DeviceError:
    """One entry of the SCPI error queue, such as ``-240,"MCU not responding"``."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f'{self.code},"{self.message}"'

    def __repr__(self) -> str:
        return f"DeviceError({self.code}, {self.message!r})"
