# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# cli.py — simple CLI entry point with filenames set here

from pathlib import Path
from core import (
    build_plan_from_files,
    build_tcx_from_plan,
    iso_to_dt,
    parse_any,
    summary_lines_from_plan,
)

# ----------- CONFIG: edit these paths/flags -----------
SWIM_FILE = "swim.tcx"
BIKE_FILE = "bike.gpx"
RUN_FILE  = "run.gpx"

# Optional transition files (set to None to ignore and infer)
T1_FILE = None        # e.g., "T1.gpx"
T2_FILE = None        # e.g., "T2.gpx"

# Flags
INFER_MISSING   = True    # infer T1/T2 from gaps if files missing
COMPACT         = False   # rebase to back-to-back timeline but keep T1/T2 durations
SWIM_DISTANCE_M = None    # e.g., 1500.0 for pool swim without GPS

OUT_FILE = "triathlon.tcx"
# ------------------------------------------------------


def _read_points(path: str):
    if path is None:
        return None
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="ignore")
    pts = parse_any(p.name, text)
    return {
        "name": p.name,
        "points": pts,
        "start": iso_to_dt(pts[0]["time"]) if pts else None,
        "stop": iso_to_dt(pts[-1]["time"]) if pts else None,
        "count": len(pts),
    }


def main():
    swim = _read_points(SWIM_FILE)
    bike = _read_points(BIKE_FILE)
    run  = _read_points(RUN_FILE)
    t1   = _read_points(T1_FILE) if T1_FILE else None
    t2   = _read_points(T2_FILE) if T2_FILE else None

    plan, err = build_plan_from_files(
        swim, bike, run,
        t1=t1, t2=t2,
        infer_missing=INFER_MISSING
    )
    if err:
        raise SystemExit(err)

    # --- Verification summary BEFORE writing ---
    print("=== Merge Preview ===")
    for ln in summary_lines_from_plan(plan, compact=COMPACT):
        print(ln)


    xml_bytes = build_tcx_from_plan(
        plan,
        compact=COMPACT,
        swim_dist_m=SWIM_DISTANCE_M
    )

    Path(OUT_FILE).write_bytes(
        xml_bytes if isinstance(xml_bytes, (bytes, bytearray)) else xml_bytes.encode("utf-8")
    )
    print(f"\nWrote {OUT_FILE}  (T1={'file' if t1 else plan['t1_inferred']}  T2={'file' if t2 else plan['t2_inferred']})")


if __name__ == "__main__":
    main()
