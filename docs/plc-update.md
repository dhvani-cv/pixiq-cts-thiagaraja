# PLC Communication — Review & Pending Updates

## Redundancy: ack vs cycle_start

**Current behavior:** Vision writes both signals back-to-back every cycle:
1. `ack=1` (reg 40021) — tells PLC "results are written, read them"
2. `cycle_start=1` (reg 40010) — tells PLC "ready for next cone"

No wait between them — PLC may not have finished processing results before cycle_start arrives.

**Action needed:** Confirm with PLC programmer:
- Does PLC release next cone on `ack=1` or `cycle_start=1`?
- If PLC only uses ack → remove cycle_start (dead register)
- If PLC uses both → add poll for `ack=0` before writing `cycle_start=1` to ensure PLC finished processing
- If PLC only uses cycle_start → remove ack

## Stale Trigger Handling — Potential Deadlock

In `poll_trigger_and_read()`: if material_no=0 after 5 settle retries, vision clears trigger + writes ack=1, but **never re-sends cycle_start=1**. If PLC waits for cycle_start before sending the next trigger, this creates a deadlock.

**Action needed:** After flushing a stale trigger, vision should restart the handshake from cycle_start=1.

## write_output — Multiple Round-Trips

`write_output()` makes 6 individual `write_single_register` calls (result, camera_error, basket_echo, material_echo, loader_echo, defect_type). Could be a single `write_multiple_registers` (Modbus FC16) to reduce from 6 round-trips to 1 (~30-50ms savings).

## Dead Code: pipeline.py

`src/pipeline.py` is an old v1 pipeline completely superseded by `inspection_service.py`:
- No cycle_start signal
- Calls `self.plc.clear_ack()` which doesn't exist
- Passes config incorrectly to PLCClient
- Missing all production features

**Action:** Delete after confirming no other scripts import it.

## Dead Methods: read_trigger() and read_input()

Only used by `pipeline.py`. Once pipeline.py is deleted, these can be removed from `PLCClient`.

## Register Map (current production — ghcl)

### Input (PLC → Vision)
| Register | Address (0-based) | Field |
|----------|-------------------|-------|
| 40001 | 0 | sample_counter |
| 40002 | 1 | trigger |
| 40008 | 7 | c2c_start (0=disabled, 1=normal, 2=trial) |
| 40009 | 8 | material_no |
| 40012 | 11 | basket_no |
| 40013 | 12 | loader_id |

### Output (Vision → PLC)
| Register | Address (0-based) | Field |
|----------|-------------------|-------|
| 40003 | 2 | result (1=Good, 2=Defect, 3=Error) |
| 40005 | 4 | uv_light |
| 40006 | 5 | vl_light |
| 40007 | 6 | yarntail_light |
| 40010 | 9 | cycle_start |
| 40015 | 14 | camera_error |
| 40016 | 15 | ips_status |
| 40017 | 16 | basket_no echo |
| 40018 | 17 | material_no echo |
| 40019 | 18 | loader_no echo |
| 40020 | 19 | defect_type (0-7) |
| 40021 | 20 | ack |

### Defect Type Codes (reg 40020)
| Code | Meaning |
|------|---------|
| 0 | Good |
| 1 | Stain |
| 2 | Wrong Pattern |
| 3 | Wrong Cone Diameter |
| 4 | Wrong Tube Diameter |
| 5 | Missing Tail |
| 6 | Thread Mixup |
| 7 | No Material ID |
