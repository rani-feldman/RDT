"""COM port transport (pyserial). A COM port can be opened by only one program at a time."""

from __future__ import annotations

from ..errors import LinkError
from .base import Transport


class SerialTransport(Transport):
    def __init__(self, port: str, baud: int = 115200):
        super().__init__()
        self.port = port
        self.baud = baud
        self._serial = None

    @property
    def description(self) -> str:
        return f"serial {self.port} {self.baud}"

    @property
    def is_open(self) -> bool:
        return self._serial is not None

    def open(self) -> None:
        import serial  # pyserial

        self.close()
        try:
            # serial_for_url also accepts URLs such as loop:// and socket://host:port.
            self._serial = serial.serial_for_url(self.port, self.baud, timeout=0, write_timeout=2)
        except (serial.SerialException, OSError, ValueError) as e:
            raise LinkError(f"{self.description}: {e}") from None
        self._buf.clear()

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    def write(self, data: bytes) -> None:
        import serial

        if self._serial is None:
            raise LinkError(f"{self.description}: not open")
        try:
            self._serial.write(data)
        except (serial.SerialException, OSError) as e:
            self.close()
            raise LinkError(f"{self.description}: {e}") from None

    def send_break(self, duration: float) -> None:
        import serial

        if self._serial is None:
            raise LinkError(f"{self.description}: not open")
        try:
            self._serial.send_break(duration)
        except (serial.SerialException, OSError) as e:
            raise LinkError(f"{self.description}: break failed: {e}") from None

    def _recv(self, timeout: float) -> bytes:
        import serial

        if self._serial is None:
            raise LinkError(f"{self.description}: not open")
        try:
            self._serial.timeout = max(timeout, 0)
            return self._serial.read(max(1, self._serial.in_waiting))
        except (serial.SerialException, OSError) as e:
            self.close()
            raise LinkError(f"{self.description}: {e}") from None
