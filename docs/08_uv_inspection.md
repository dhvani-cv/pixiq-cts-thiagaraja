# Chapter 8: UV Inspection

## 8.1 Overview

UV inspection detects polymer fiber mixup (wrong material blended in) by analyzing UV fluorescence patterns. Different polymers fluoresce differently under UV light, creating concentric bands visible as a local dip in the radial blue-channel brightness profile.

**Source:** `src/inspection/uv_inspection.py` — `UVInspection` class

**No training required** — the algorithm is physics-based. Two thresholds need calibration (see §8.9).

## 8.2 Physics Rationale

- Pure yarn has a smooth, monotonic radial fluorescence profile under UV light
- Polymer mixup creates concentric fluorescence bands (different polymer fluoresces at different intensity)
- **Blue channel is used directly** (not a green/blue ratio) — measured directly against labeled good/bad Indorama cones: blue mean ~40-80, green mean ~10-20, red mean ~5-9. Green sits close to the sensor noise floor on this camera (no yellow-green emission filter), so a ratio like log(G/B) ends up dividing by the *strong* channel using the *weak* one as its noisier partner. This is specific to this camera/installation — re-verify channel means directly against any new camera before assuming blue is still the right one.
- A reflect-padded moving-average baseline captures the natural radial gradient (not the defect bands) without losing radius range at the crop edges
- The most prominent dip's depth (as % of local baseline) = `depth_pct`
- Separately, whether that dip holds up around the full 360° circumference (vs. only part of it, like a one-sided lighting artifact) = `coverage_pct`
- A cone is flagged if **either** `depth_pct` or `coverage_pct` crosses its threshold — validated directly against labeled cones, they catch different defect shapes: broad circumferential bands show up mainly in coverage, sharp localized spots show up mainly in depth

## 8.3 Algorithm

```
UV Frame
        │
        ▼
YOLO (UV model) → yarn_cone bbox + yarn_tube bbox
        │
        ▼
Crop cone from bbox (clamp to frame bounds)
        │
        ▼
Derive geometry:
  - tube center (crop coordinates)
  - inner_r = tube radius
  - outer_r = cone radius × (1 - outer_margin)
        │
        ▼
Reject if outer_r - inner_r < MIN_ANNULUS_WIDTH (60px) — too narrow for a
reliable radial profile
        │
        ▼
Polar-unwarp the blue channel around the tube center into a
(720 angle bins, radius) image
        │
        ▼
Average across all 720 angles → 1D radial brightness profile
(cancels sensor/thread noise, keeps any feature at a consistent radius)
        │
        ▼
Subtract reflect-padded moving-average baseline (high-pass filter)
        │
        ▼
Find most prominent narrow dip → depth_pct (% of local baseline)
        │
        ▼
At that same candidate radius, check per-angle (not pooled) whether the
dip is present — "dark" cutoff calibrated PER FRAME against several
control radii on the same image, not a fixed constant → coverage_pct
        │
        ▼
depth_pct > radial_dip_threshold OR coverage_pct > coverage_threshold
    → DEFECT (has_mixup=True)
otherwise
    → PASS (has_mixup=False)
```

## 8.4 Why per-frame coverage calibration (not a fixed threshold)

Checking "is this dip present at every angle" sounds like it should use a
simple fixed threshold (e.g. "darker than 0.2 standard deviations below
local average"). It doesn't work: allowing each angle to search a small
window for its own local minimum (needed because real yarn winding isn't
perfectly circular/centered, so a genuine ring drifts a few px in radius
between angles) introduces an order-statistic bias — the minimum of any
noisy window is *always* going to look darker than that window's own mean,
even with no real defect present. A fixed threshold can't tell "real dip"
from "just the smallest sample in a noisy window" once that per-angle
search is involved.

The fix: calibrate the "how dark counts as dark" cutoff **from the same
frame**, using the identical local-minimum statistic measured at several
control radii elsewhere in the same annulus (away from the candidate ring).
That captures what pure noise looks like on *this* image, including the
same search bias, and only counts an angle as "dark" if it's darker than
that image's own noise floor — not an arbitrary global constant.

## 8.5 Validation Results

Validated against real labeled Indorama production images across several
batches (`Annular_uv_indo_80000`, `Annular_outerscale-0.02_uv_indo_80000`,
`indorama_7500_good_bad`; ~35-60 images per batch):

| Class | depth_pct | coverage_pct |
|-------|-----------|---------------|
| Good | up to ~1.33% | up to ~18.2% |
| Defect | from ~1.0% (weakest) up to 5.7% | from ~4% (sharp-spot defects, caught by depth instead) up to 93% |

- Jointly-optimized OR thresholds: `depth_pct=1.33%`, `coverage_pct=18.2%`
- 94-100% accuracy across batches tested, 0 false positives
- Depth and coverage were shown to catch different defect shapes — neither
  alone caught every defect in the validation batches, the OR combination
  did

**This is a smaller validation set than the original log(G/B) version**
(1959 images vs. ~35-60 here). Treat current thresholds as a strong pilot
calibration, not a final locked-in number — recalibrate as more labeled
good/bad cones come in from this floor (see §8.9).

### Why the previous log(G/B) version was replaced

The earlier algorithm (log(G/B) ratio + degree-2 polynomial baseline,
validated 0.024 threshold on a 1950-good/9-defect dataset) drifted badly
once deployed on Indorama's actual camera. Its threshold had already been
manually raised from 0.024 to 0.77-0.8 in the field — a ~30x jump, well
outside its documented validation range — with good and bad cones barely
separated at that point (radial_dip values for real "OK" cones were
regularly landing at 0.4-0.7, right up against the 0.77 cutoff). Root
cause: log(G/B) assumed green carried real fluorescence signal, which
isn't true on this specific camera (no yellow-green emission filter) — see
§8.2.

## 8.6 Consecutive Detection Failure

If YOLO fails to detect cone or tube in the UV frame, or the annular
region is too small/dark for a reliable radial profile:

- `detection_failed=True` returned — UV check is **skipped** for this cone (not counted as Good or Defect)
- VL and Tail results still determine the final verdict
- Consecutive failure counter increments
- At 5 consecutive failures → `logger.error()` fires (likely camera/hardware issue, not a real defect)
- Counter resets on any successful detection

Note: if a cone/tube ARE detected and the annulus is valid, but no dip is
found at all in the radial profile, that's treated as a normal good cone
(`depth_pct=0.0, coverage_pct=0.0`), not a detection failure.

## 8.7 Result Fields

```python
@dataclass
class UVResult:
    has_mixup: bool              # depth_pct > threshold OR coverage_pct > threshold
    radial_dip: float            # depth_pct (%) — field name kept from the previous
                                  # version for API/UI compatibility
    gb_ratio: float               # repurposed to carry coverage_pct (%) — field
                                  # name kept from the previous version for
                                  # API/UI compatibility, meaning has changed
    detection_failed: bool       # YOLO/compute failed
    cone_bbox: Optional[tuple]   # cone bbox in UV frame
```

**Note on field names**: `radial_dip` and `gb_ratio` are historical names
from the log(G/B) version, kept unchanged so the API/UI/logging pipeline
downstream of `UVResult` didn't need to change. `radial_dip` now holds
`depth_pct`, and `gb_ratio` now holds `coverage_pct` — neither is
literally what the field name says anymore. If this becomes confusing,
consider a follow-up rename across the whole pipeline (UI, API, DB
schema) rather than patching just this file.

## 8.8 Configuration

```json
{
    "uv_inspection": {
        "yolo_weights": "weights/Indorama_UV.pt",
        "yolo_conf": 0.3,
        "radial_dip_threshold": 1.33,
        "coverage_threshold": 18.2,
        "outer_margin": 0.1
    }
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `radial_dip_threshold` | 1.33 | Max dip depth (% of local baseline) before flagging mixup |
| `coverage_threshold` | 18.2 | Max angular coverage (%, 0-100) before flagging mixup |
| `outer_margin` | 0.10 | Fraction of radius to exclude at cone edge (noisy pixels) |
| `yolo_conf` | 0.3 | Lower than VL (UV images are noisier) |

### Constants (hardcoded)

| Constant | Value | Description |
|----------|-------|-------------|
| `N_ANGLE_BINS` | 720 | Polar-unwrap angular resolution |
| `BASELINE_WINDOW` | 31 | px, moving-average width for the high-pass detrend |
| `EDGE_TRIM` | 5 | px, guard against reflect-padding artifacts at profile tips |
| `MIN_PROMINENCE` | 0.3 | Minimum dip prominence (blue intensity units) to count |
| `MIN_DIP_SPACING` | 10 | px, min distance between candidate dips |
| `COVERAGE_BAND` | 25 | px, ± window each side of a radius used for its local baseline |
| `COVERAGE_SEARCH` | 8 | px, ± radius jitter tolerance per angle |
| `COVERAGE_NULL_STEP` | 40 | px, spacing between control radii for the null distribution |
| `COVERAGE_NULL_PERCENTILE` | 10 | "dark" = darker than this percentile of the null |
| `MIN_ANNULUS_WIDTH` | 60 | px, minimum outer_r - inner_r for a reliable profile |
| `_UV_DETECTION_FAIL_THRESHOLD` | 5 | Consecutive failures before operator alert |

## 8.9 Calibration

UV calibration is installation-only (no model training), but now needs
**two** thresholds instead of one:

1. Run 10+ known-good cones, check `radial_dip` (depth_pct) and `gb_ratio`
   (coverage_pct) values via `GET /results` or the service logs
2. Note the maximum depth_pct and maximum coverage_pct seen across those
   good cones
3. Set `radial_dip_threshold` and `coverage_threshold` at (or just above)
   those maxima — a cone is flagged if *either* value crosses its
   threshold, so both need to sit above the full observed good-cone range
4. If real defect cones are available too, prefer a *jointly* optimized
   pair of thresholds over picking each independently — this session found
   picking each threshold in isolation (maximizing separation for that
   metric alone) could drag one threshold low enough to introduce a false
   positive that a joint search avoids. Grid-search both thresholds
   together, maximizing overall (good passes + bad caught) accuracy,
   rather than tuning depth and coverage as two unrelated problems
5. `POST /teaching/uv` with the new thresholds (confirm both keys are
   supported by the teaching endpoint — this may need a small update if it
   was only ever built for a single scalar threshold)

Recalibrate if the UV camera or lighting setup changes — and treat the
current defaults (1.33% / 18.2%) as a pilot calibration from a modest
sample (~35-60 labeled images), not a final number. Re-run this procedure
as more labeled good/bad cones accumulate from production.
