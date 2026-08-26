"""Data types for PLC communication."""

from dataclasses import dataclass
from typing import Optional


@dataclass(slots=True)
class PLCInput:
    """Data read from PLC when trigger fires.

    The PLC sets trigger=1 (40002) when material data is ready. After vision
    reads all input registers, it clears trigger (writes 0). Camera capture
    then proceeds via independent hardware Line0 triggers.

    Attributes:
        trigger: True when PLC signals material data is ready.
        sample_counter: Part counter from PLC (40001).
        material_no: Numeric material identifier (40009).
        basket_no: Basket/sorting identifier (40012).
        loader_id: Loader identifier (40013).
        c2c_start: PLC display mode (40008): 0=Disabled, 1=Normal, 2=Trial.
    """
    trigger: bool = False
    sample_counter: int = 0
    material_no: int = 0
    basket_no: int = 0
    loader_id: int = 0
    c2c_start: int = 0


@dataclass(slots=True)
class PLCOutput:
    """Data written to PLC after all inspections complete.

    After writing results, vision sets ack=1 (40021). PLC reads results,
    then PLC clears ack. Vision does NOT clear ack.

    Register map (Vision → PLC):
        40003: result (1=Good, 2=Defect, 3=Error)
        40015: camera_error (error code, 0=OK)
        40017: basket_no (echo back to PLC)
        40018: material_no (echo back to PLC)
        40019: loader_no (echo back to PLC)
        40020: defect_type (0=Good, 1=Stain, 2=Wrong Pattern, 3=Wrong Cone Dia, 4=Wrong Tube Dia, 5=Missing Tail, 6=Thread Mixup, 7=No Material ID)
        40021: ack (vision sets 1, PLC clears to 0)

    Attributes:
        result_code: 1=Good, 2=Defect, 3=Error
        camera_error: Error code (0=OK)
        basket_no: Echo basket_no back to PLC.
        material_no: Echo material_no back to PLC.
        loader_no: Echo loader_id back to PLC.
        defect_type_code: Defect type number (0=Good).
    """
    result_code: int  # Combined final result
    camera_error: int = 0
    basket_no: int = 0
    material_no: int = 0
    loader_no: int = 0
    defect_type_code: int = 0

    @classmethod
    def from_results(
        cls,
        vl_code: Optional[int] = None,
        uv_code: Optional[int] = None,
        tail_code: Optional[int] = None,
        material_no: int = 0,
        basket_no: int = 0,
        loader_id: int = 0,
    ) -> "PLCOutput":
        """Create PLCOutput from individual inspection results.

        Combined result_code logic:
            - None codes are skipped (camera timed out / disabled)
            - If ANY result is 3 (Error) → 3
            - If ANY result is 2 (Defect) → 2
            - If all available results are 1 → 1 (Good)
            - If NO results at all → 3 (Error — nothing inspected)

        Args:
            vl_code: Visible light result (1/2/3) or None if skipped
            uv_code: UV result (1/2/3) or None if disabled/skipped
            tail_code: Tail result (1/2/3) or None if disabled/skipped
            material_no: Material number to echo back to PLC.
            basket_no: Basket number to echo back to PLC.
            loader_id: Loader ID to echo back to PLC.
        """
        codes = []
        if vl_code is not None:
            codes.append(vl_code)
        if uv_code is not None:
            codes.append(uv_code)
        if tail_code is not None:
            codes.append(tail_code)

        # Priority: Error > Defect > Good. No codes at all → Error.
        if not codes:
            combined = 3
        elif 3 in codes:
            combined = 3
        elif 2 in codes:
            combined = 2
        else:
            combined = 1

        return cls(
            result_code=combined,
            camera_error=0,
            basket_no=basket_no,
            material_no=material_no,
            loader_no=loader_id,
            defect_type_code=0,  # Set by caller after inspection
        )
