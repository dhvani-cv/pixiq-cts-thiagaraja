"""
Mock PLC Client for testing without a physical Siemens PLC.

Simulates PLC behavior for local development and testing.
"""

import logging
from typing import Optional

from .data_types import PLCInput, PLCOutput

logger = logging.getLogger(__name__)

# Light names indexed by light_id (0-based)
_LIGHT_NAMES = ["uv", "vl", "yarntail"]


class MockPLCClient:
    """Mock PLC client that simulates Modbus TCP communication.

    This class provides the same interface as the real PLCClient but does
    not require a network connection or a physical PLC. It maintains an
    in-memory state for registers.
    """

    def __init__(self, config: dict):
        self.host = config.get("host", "localhost")
        self.port = config.get("port", 502)
        self._connected = False

        # In-memory simulation of PLC registers
        self._registers = {
            "trigger": 0,
            "ack": 0,
            "material_no": 123,
            "sample_counter": 0,
            "basket_no": 1,
            "loader_id": 99,
            "c2c_start": 1,
            "lights": {name: 0 for name in _LIGHT_NAMES},
        }

        logger.info("MockPLCClient initialized")

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        """Simulates connecting to the PLC."""
        if self._connected:
            return True
        self._connected = True
        logger.info("MockPLC connected (simulated)")
        return True

    def disconnect(self):
        """Simulates disconnecting from the PLC."""
        if self._connected:
            self._connected = False
            logger.info("MockPLC disconnected (simulated)")

    def read_trigger(self) -> bool:
        if self._registers["trigger"] == 1:
            logger.info("MockPLC: Trigger is active (1)")
            return True
        # Automatically set trigger for next cycle for simulation
        self._registers["trigger"] = 1
        return False

    def clear_trigger(self) -> bool:
        logger.info("MockPLC: Clearing trigger (writing 0)")
        self._registers["trigger"] = 0
        return True

    def read_c2c_start(self) -> bool:
        return self._registers["c2c_start"] == 1

    def read_input(self) -> Optional[PLCInput]:
        if not self.connected:
            return None

        self._registers["sample_counter"] += 1

        return PLCInput(
            trigger=self._registers["trigger"] == 1,
            sample_counter=self._registers["sample_counter"],
            material_no=self._registers["material_no"],
            basket_no=self._registers["basket_no"],
            loader_id=self._registers["loader_id"],
            c2c_start=self._registers["c2c_start"] == 1,
        )

    def write_output(self, output: PLCOutput) -> bool:
        if not self.connected:
            return False

        logger.info(
            "MockPLC: Writing output - result=%d, material=%d, basket=%d, loader=%d",
            output.result_code, output.material_no, output.basket_no, output.loader_no,
        )
        return True

    def write_camera_error(self, error_code: int) -> bool:
        logger.info("MockPLC: Camera error = %d", error_code)
        return True

    def ack_complete(self) -> bool:
        logger.info("MockPLC: Acknowledging complete (setting ack=1)")
        self._registers["ack"] = 1
        return True

    def write_register(self, address: int, value: int) -> bool:
        logger.info("MockPLC: Writing value %d to register %d (simulated)", value, address)
        return True

    def read_registers(self, address: int, count: int) -> Optional[list]:
        logger.info("MockPLC: Reading %d registers from address %d (simulated)", count, address)
        return [0] * count

    def control_light(self, light_id: int, turn_on: bool) -> bool:
        if light_id >= len(_LIGHT_NAMES):
            return False
        light_name = _LIGHT_NAMES[light_id]
        status = "ON" if turn_on else "OFF"
        logger.info("MockPLC: Turning %s light %s", light_name, status)
        self._registers["lights"][light_name] = 1 if turn_on else 0
        return True

    def read_light_status(self) -> dict:
        return {name: bool(val) for name, val in self._registers["lights"].items()}
