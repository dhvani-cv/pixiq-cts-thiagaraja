"""
PLC Client — Modbus TCP communication with Siemens PLC.

All register addresses are loaded from config (no hardcoded addresses).
Uses pyModbusTCP for Modbus TCP protocol.

IMPORTANT: All socket operations are serialized with a threading.Lock
because eventlet monkey-patches sockets and does not allow two green
threads to read/write the same socket concurrently.

Register map (40001-based → 0-based in pyModbusTCP):
    Input (PLC → Vision):
        40001 (0): sample_counter
        40002 (1): trigger (inspection_start) — vision clears to 0
        40008 (7): c2c_start (1=enabled from PLC display)
        40009 (8): material_no
        40012 (11): basket_no
        40013 (12): loader_id

    Output (Vision → PLC):
        40003 (2): result (1=Good, 2=Defect, 3=Error)
        40015 (14): camera_error
        40017 (16): basket_no (echo)
        40018 (17): material_no (echo)
        40019 (18): loader_no (echo)
        40020 (19): defect_type (0=Good, 1=Stain, 2=Wrong Pattern, etc.)
        40021 (20): ack — vision sets 1, PLC clears to 0

    Lights (Vision → PLC):
        40005 (4): uv_light
        40006 (5): vl_light (LED)
        40007 (6): yarntail_light

Handshake protocol:
    1. Vision sets cycle_start=1 (40010) — "ready for next cone"
    2. PLC sees cycle_start=1, releases cone, clears cycle_start to 0
    3. PLC writes sample_counter, material_no, basket_no, loader_id
    4. PLC sets trigger=1
    5. Vision polls trigger, reads 1 → clears trigger (writes 0)
    6. Vision reads sample_counter, material_no, basket_no, loader_id
    7. Cameras capture (hardware Line0 triggers — only 1 cone in-flight)
    8. Vision inspects → writes result + echo fields → sets ack=1
    9. PLC reads results → PLC clears ack → repeat from step 1
"""

import logging
import threading
import time
from typing import Optional

from pyModbusTCP.client import ModbusClient

from .data_types import PLCInput, PLCOutput

logger = logging.getLogger(__name__)

# Light names indexed by light_id (0-based)
_LIGHT_NAMES = ["uv", "vl", "yarntail"]


class PLCClient:
    """Modbus TCP client for PLC communication.

    All register addresses come from the config dict under the "registers" key.
    If a register address is not configured, the corresponding feature is skipped.

    All socket operations are serialized via self._lock to prevent
    eventlet "simultaneous read on fileno" errors.
    """

    def __init__(self, config: dict):
        self.host = config.get("host", "192.168.2.1")
        self.port = config.get("port", 502)
        self.unit_id = config.get("unit_id", 1)
        self.timeout = config.get("timeout", 3.0)

        # Load register addresses from config
        regs = config.get("registers", {})
        inp = regs.get("input", {})
        out = regs.get("output", {})
        light = regs.get("light", {})

        # Input registers (PLC → Vision)
        self._reg_trigger = inp.get("trigger")
        self._reg_sample_counter = inp.get("sample_counter")
        self._reg_c2c_start = inp.get("c2c_start")
        self._reg_material_no = inp.get("material_no")
        self._reg_basket_no = inp.get("basket_no")
        self._reg_loader_id = inp.get("loader_id")

        # Output registers (Vision → PLC)
        self._reg_result = out.get("result")
        self._reg_camera_error = out.get("camera_error")
        self._reg_basket_no_echo = out.get("basket_no_echo")
        self._reg_material_no_echo = out.get("material_no_echo")
        self._reg_loader_no_echo = out.get("loader_no_echo")
        self._reg_ips_status = out.get("ips_status")
        self._reg_cycle_start = out.get("cycle_start")
        self._reg_defect_type = out.get("defect_type")
        self._reg_ack = out.get("ack")

        # Light registers
        self._light_regs = {}
        for i, name in enumerate(_LIGHT_NAMES):
            addr = light.get(name)
            if addr is not None:
                self._light_regs[i] = (name, addr)

        self._client: Optional[ModbusClient] = None
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_open

    def connect(self) -> bool:
        """Connect to PLC.

        Returns:
            True if connection successful.
        """
        with self._lock:
            if self.connected:
                return True

            self._client = ModbusClient(
                host=self.host,
                port=self.port,
                unit_id=self.unit_id,
                timeout=self.timeout,
                auto_open=False,
                auto_close=False,
            )

            if self._client.open():
                logger.info("PLC connected: %s:%d", self.host, self.port)
                return True
            else:
                logger.error("PLC connection failed: %s:%d", self.host, self.port)
                self._client = None
                return False

    def disconnect(self):
        """Disconnect from PLC."""
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
                logger.info("PLC disconnected")

    # ── Trigger ────────────────────────────────────────────────────────

    def read_trigger(self) -> bool:
        """Read the trigger flag from PLC (40002).

        PLC sets trigger=1 when material data is ready to read.
        Call clear_trigger() after reading.

        Returns:
            True if trigger is set, False otherwise or on error.
        """
        if not self.connected or self._reg_trigger is None:
            return False

        with self._lock:
            try:
                result = self._client.read_holding_registers(self._reg_trigger, 1)
                if result is None:
                    return False
                triggered = result[0] == 1
                if triggered:
                    logger.debug("PLC trigger=1 detected (reg %d)", self._reg_trigger)
                return triggered
            except Exception:
                return False

    def clear_trigger(self) -> bool:
        """Clear trigger flag (write 0) to tell PLC we've seen the signal.

        Returns:
            True if write successful.
        """
        if not self.connected or self._reg_trigger is None:
            return False

        with self._lock:
            try:
                result = self._client.write_single_register(self._reg_trigger, 0)
                if not result:
                    logger.error("PLC clear trigger failed")
                    return False
                logger.debug("PLC trigger cleared (wrote 0 to reg %d)", self._reg_trigger)
                return True
            except Exception as e:
                logger.error("PLC clear trigger error: %s", e)
                return False

    def read_c2c_start(self) -> int:
        """Read c2c_start mode from PLC (40008).

        Values:
            0 = Disabled (inspection/teaching/capture/trial all stopped)
            1 = Normal operation (inspection/teaching/capture enabled)
            2 = Trial run (inspect but don't write results to PLC)

        Returns:
            Integer mode value (0, 1, or 2). Defaults to 1 if not configured.
        """
        if not self.connected or self._reg_c2c_start is None:
            return 1  # Default: enabled if register not configured

        with self._lock:
            try:
                result = self._client.read_holding_registers(self._reg_c2c_start, 1)
                if result is None:
                    return 1
                logger.debug("PLC c2c_start=%d (reg %d)", result[0], self._reg_c2c_start)
                return result[0]
            except Exception:
                return 1

    # ── Input ──────────────────────────────────────────────────────────

    def poll_trigger_and_read(self, max_settle: int = 5, settle_ms: int = 50) -> Optional[PLCInput]:
        """Single atomic read: check trigger + read all data in one Modbus call.

        Reads registers 0-12 (40001-40013) in one bulk call.
        If trigger (reg 1) is not 1, returns None.
        If trigger is 1 but material_no is 0, re-reads up to max_settle times.
        If still 0 after settling, this is a stale trigger — clears trigger
        + writes ack to flush it, then returns None so caller retries.

        Returns:
            PLCInput if trigger=1 and data read OK. None if no trigger or error.
        """
        if not self.connected:
            return None

        with self._lock:
            try:
                regs = self._client.read_holding_registers(0, 13)
                if regs is None:
                    return None

                # Check trigger in the same read
                if regs[1] != 1:
                    return None

                # Trigger=1 — but PLC may not have written material data yet
                # Re-read if material_no is 0 (give PLC time to finish writing)
                retries = 0
                while regs[8] == 0 and retries < max_settle:
                    retries += 1
                    time.sleep(settle_ms / 1000.0)
                    regs = self._client.read_holding_registers(0, 13)
                    if regs is None:
                        return None

                material_no = regs[8] if self._reg_material_no is not None else 0

                # If material is still 0 after settling, this is a stale trigger
                # Clear trigger + write ack to flush the stale cycle
                if material_no == 0:
                    logger.warning(
                        "PLC stale trigger: counter=%d material=0 after %dms — flushing (clear trigger + ack)",
                        regs[0], retries * settle_ms,
                    )
                    self._client.write_single_register(self._reg_trigger, 0)
                    if self._reg_ack is not None:
                        self._client.write_single_register(self._reg_ack, 1)
                    return None

                sample_counter = regs[0] if self._reg_sample_counter is not None else 0
                basket_no = regs[11] if self._reg_basket_no is not None else 0
                loader_id = regs[12] if self._reg_loader_id is not None else 0
                c2c_start = regs[7] if self._reg_c2c_start is not None else 0

                if retries > 0:
                    logger.info(
                        "PLC read: trigger=1 counter=%d material=%d basket=%d loader=%d c2c=%d (settled after %dms)",
                        sample_counter, material_no, basket_no, loader_id, c2c_start, retries * settle_ms,
                    )
                else:
                    logger.info(
                        "PLC read: trigger=1 counter=%d material=%d basket=%d loader=%d c2c=%d",
                        sample_counter, material_no, basket_no, loader_id, c2c_start,
                    )

                return PLCInput(
                    trigger=True,
                    sample_counter=sample_counter,
                    material_no=material_no,
                    basket_no=basket_no,
                    loader_id=loader_id,
                    c2c_start=c2c_start,
                )

            except Exception as e:
                logger.error("PLC poll_trigger_and_read error: %s", e)
                return None

    def read_input(self) -> Optional[PLCInput]:
        """Read material data from PLC (after trigger detected and cleared).

        Reads all input registers in a single bulk Modbus call (fewer
        round-trips). Registers 40001-40013 (addresses 0-12) are read at once.
        PLC guarantees data is stable between trigger=1 and ack=1.

        Returns:
            PLCInput with current values, or None on error.
        """
        if not self.connected:
            logger.error("PLC not connected — cannot read")
            return None

        with self._lock:
            try:
                regs = self._client.read_holding_registers(0, 13)
                if regs is None:
                    logger.error("PLC bulk read registers 0-12 failed")
                    return None

                trigger = bool(regs[1]) if self._reg_trigger is not None else False
                sample_counter = regs[0] if self._reg_sample_counter is not None else 0
                material_no = regs[8] if self._reg_material_no is not None else 0
                basket_no = regs[11] if self._reg_basket_no is not None else 0
                loader_id = regs[12] if self._reg_loader_id is not None else 0
                c2c_start = regs[7] if self._reg_c2c_start is not None else 0

                logger.info(
                    "PLC read_input: regs[0-12]=%s → trigger=%d counter=%d material=%d basket=%d loader=%d c2c=%d",
                    regs, regs[1], regs[0], regs[8], regs[11], regs[12], regs[7],
                )

                return PLCInput(
                    trigger=trigger,
                    sample_counter=sample_counter,
                    material_no=material_no,
                    basket_no=basket_no,
                    loader_id=loader_id,
                    c2c_start=c2c_start,
                )

            except Exception as e:
                logger.error("PLC read error: %s", e)
                return None

    # ── Output ─────────────────────────────────────────────────────────

    def write_output(self, output: PLCOutput) -> bool:
        """Write inspection results to PLC.

        Writes result (40003) and echo fields (40017-40019).

        Args:
            output: PLCOutput with result code and echo fields.

        Returns:
            True if all configured writes succeeded.
        """
        if not self.connected:
            logger.error("PLC not connected — cannot write")
            return False

        with self._lock:
            try:
                ok = True

                for addr, value, name in [
                    (self._reg_result, output.result_code, "result"),
                    (self._reg_camera_error, output.camera_error, "camera_error"),
                    (self._reg_basket_no_echo, output.basket_no, "basket_no_echo"),
                    (self._reg_material_no_echo, output.material_no, "material_no_echo"),
                    (self._reg_loader_no_echo, output.loader_no, "loader_no_echo"),
                    (self._reg_defect_type, output.defect_type_code, "defect_type"),
                ]:
                    if addr is not None:
                        logger.debug("PLC write: %s=%d → reg %d", name, value, addr)
                        result = self._client.write_single_register(addr, value)
                        if not result:
                            logger.error("PLC write %s to reg %d failed", name, addr)
                            ok = False

                logger.info(
                    "PLC write: result=%d, material=%d, basket=%d, loader=%d",
                    output.result_code, output.material_no,
                    output.basket_no, output.loader_no,
                )
                return ok

            except Exception as e:
                logger.error("PLC write error: %s", e)
                return False

    def write_camera_error(self, error_code: int) -> bool:
        """Write camera error code to PLC (40015). 0=OK.

        Returns:
            True if write successful.
        """
        if self._reg_camera_error is None:
            return True
        return self.write_register(self._reg_camera_error, error_code)

    def write_ips_status(self, status: int) -> bool:
        """Write IPS status to PLC (40016).

        Args:
            status: 1=Active (inspect/capture/teaching), 2=Trial run, 3=Disabled.

        Returns:
            True if write successful.
        """
        if self._reg_ips_status is None:
            return True
        return self.write_register(self._reg_ips_status, status)

    def ack_complete(self) -> bool:
        """Set ack flag (40021) to signal PLC that results are ready.

        PLC reads results, then PLC clears the ack. Vision does NOT
        clear ack — PLC owns the ack lifecycle.

        Returns:
            True if write successful.
        """
        if not self.connected or self._reg_ack is None:
            return False

        with self._lock:
            try:
                result = self._client.write_single_register(self._reg_ack, 1)
                if not result:
                    logger.error("PLC ack write failed")
                    return False
                logger.debug("PLC ack=1 written (reg %d)", self._reg_ack)
                return True
            except Exception as e:
                logger.error("PLC ack error: %s", e)
                return False

    def write_cycle_start(self) -> bool:
        """Set cycle_start=1 (40010) to tell PLC we're ready for the next cone.

        PLC reads this, releases the next cone onto the conveyor, then
        clears cycle_start to 0. This ensures only 1 cone is in-flight
        between the PLC trigger point and the cameras at any time.

        Returns:
            True if write successful.
        """
        if not self.connected or self._reg_cycle_start is None:
            return False

        with self._lock:
            try:
                result = self._client.write_single_register(self._reg_cycle_start, 1)
                if not result:
                    logger.error("PLC cycle_start write failed")
                    return False
                logger.debug("PLC cycle_start=1 written (reg %d)", self._reg_cycle_start)
                return True
            except Exception as e:
                logger.error("PLC cycle_start error: %s", e)
                return False

    # ── Generic register access ────────────────────────────────────────

    def write_register(self, address: int, value: int) -> bool:
        """Write a single holding register, reconnecting once on failure.

        The Modbus TCP session can die silently during long idle (PLC-side
        timeout) — is_open still reports True but the next write fails
        (observed as 'write register 15 failed' on operator stop after
        idle, 2026-07-17 18:00). Operator-facing writes (ips_status,
        lights) go through here, so on failure reconnect and retry exactly
        once instead of giving up. Hot-cycle writes (ack, cycle_start,
        write_output) intentionally do NOT auto-reconnect — the cycle loop
        owns reconnect there and a blocking connect inside the ack path
        could stall the conveyor handshake.

        Args:
            address: 0-based register address.
            value: Integer value to write.

        Returns:
            True if write successful.
        """
        if self.connected and self._try_write_single(address, value):
            return True

        logger.warning(
            "PLC write register %d failed (stale/dropped session) — reconnecting and retrying once",
            address,
        )
        self.disconnect()
        if not self.connect():
            logger.error("PLC write register %d failed — reconnect unsuccessful", address)
            return False
        if self._try_write_single(address, value):
            logger.info("PLC write register %d succeeded after reconnect", address)
            return True
        logger.error("PLC write register %d failed after reconnect", address)
        return False

    def _try_write_single(self, address: int, value: int) -> bool:
        """One locked write attempt — no logging, no retry.

        Holds the lock only around the single Modbus call, never across
        a reconnect.
        """
        with self._lock:
            try:
                return self._client.write_single_register(address, value) is True
            except Exception:
                return False

    def keepalive(self) -> bool:
        """Cheap 1-register read to keep the Modbus session alive during idle.

        Called periodically by the service while inspection is stopped so
        the PLC-side idle timeout never kills the session — and if it
        already died, detect and repair it here instead of on the
        operator's next Stop/Start click.

        Returns:
            True if the session is alive (possibly after a reconnect).
        """
        probe_reg = self._reg_ips_status if self._reg_ips_status is not None else 0
        if self.connected:
            with self._lock:
                try:
                    if self._client.read_holding_registers(probe_reg, 1) is not None:
                        return True
                except Exception:
                    pass
            logger.warning("PLC keepalive probe failed — session dropped, reconnecting")
        self.disconnect()
        return self.connect()

    def read_registers(self, address: int, count: int) -> Optional[list]:
        """Read multiple holding registers.

        Args:
            address: 0-based start address.
            count: Number of registers to read.

        Returns:
            List of integer values, or None on error.
        """
        if not self.connected:
            logger.error(
                "PLC not connected — cannot read registers %d-%d",
                address, address + count - 1,
            )
            return None

        with self._lock:
            try:
                result = self._client.read_holding_registers(address, count)
                if result is None:
                    logger.error("PLC read registers %d-%d failed", address, address + count - 1)
                return result
            except Exception as e:
                logger.error("PLC read registers error: %s", e)
                return None

    # ── Light control ──────────────────────────────────────────────────

    def control_light(self, light_id: int, turn_on: bool) -> bool:
        """Turn a light on or off via PLC register.

        Args:
            light_id: 0=UV, 1=VL, 2=Yarntail.
            turn_on: True to turn on, False to turn off.

        Returns:
            True if write successful.
        """
        entry = self._light_regs.get(light_id)
        if entry is None:
            logger.error("Light %d not configured", light_id)
            return False

        name, address = entry
        value = 1 if turn_on else 0
        ok = self.write_register(address, value)

        if ok:
            logger.info("Light '%s' turned %s", name, "ON" if turn_on else "OFF")
        return ok

    def read_light_status(self) -> dict:
        """Read status of all configured lights from PLC registers.

        Returns:
            Dict mapping light name to bool (True=on), or empty dict on error.
        """
        status = {}
        for _light_id, (name, address) in self._light_regs.items():
            regs = self.read_registers(address, 1)
            if regs is not None:
                status[name] = bool(regs[0])
        return status
