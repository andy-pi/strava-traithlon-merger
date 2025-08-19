# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# core.py — shared logic for GPX/TCX parsing, transition inference, TCX writing

from xml.etree import ElementTree as ET
from xml.dom import minidom
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

TCX_NS  = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
XSI_NS  = "http://www.w3.org/2001/XMLSchema-instance"
NS3_NS  = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"

# IMPORTANT: avoid ns\d prefixes (reserved); use a safe one:
ET.register_namespace("", TCX_NS)
ET.register_namespace("xsi", XSI_NS)
ET.register_namespace("tpx", NS3_NS)

SPORT_MAP = {"swim": "Swimming", "bike": "Biking", "run": "Running", "transition": "Other"}

# ----------------- time helpers -----------------
def iso_to_dt(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        # Fallback: 2024-01-01T12:34:56Z
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")

def dt_to_iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def fmt_dur(seconds: float) -> str:
    s = int(round(seconds))
    h, m = divmod(s, 3600)
    m, s = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"

def fmt_dt_human(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")

# ----------------- XML helpers -----------------
def tcx_root():
    root = ET.Element(f"{{{TCX_NS}}}TrainingCenterDatabase", {
        f"{{{XSI_NS}}}schemaLocation": f"{TCX_NS} {TCX_NS.replace('/v2','v2')}"
    })
    ET.SubElement(root, "Folders")
    ET.SubElement(root, "Activities")
    return root

def mk(parent, tag, text=None):
    e = ET.SubElement(parent, tag)
    if text is not None:
        e.text = text
    return e

# ----------------- Parsers -----------------
def parse_gpx(text: str) -> List[Dict]:
    root = ET.fromstring(text)
    # Handle both namespaced and non-namespaced GPX
    if "}" in root.tag:
        gns = {"gpx": root.tag.split("}")[0].strip("{")}
        path_trkpt = ".//gpx:trkpt"
        get_time = lambda tp: tp.findtext("gpx:time", namespaces=gns)
        get_ele  = lambda tp: tp.findtext("gpx:ele",  namespaces=gns)
    else:
        gns = {}
        path_trkpt = ".//trkpt"
        get_time = lambda tp: tp.findtext("time")
        get_ele  = lambda tp: tp.findtext("ele")

    pts = []
    for tp in root.findall(path_trkpt, gns):
        t = get_time(tp)
        la = tp.get("lat"); lo = tp.get("lon")
        ele = get_ele(tp)
        pts.append({
            "time": t,
            "lat": float(la) if la else None,
            "lon": float(lo) if lo else None,
            "ele": float(ele) if ele else None,
            "hr": None, "cad": None, "pwr": None
        })
    return pts


def parse_tcx(text: str) -> List[Dict]:
    ns = {"tcx": TCX_NS, "ns3": NS3_NS}
    root = ET.fromstring(text)
    pts = []
    for tp in root.findall(".//tcx:Trackpoint", ns):
        time = tp.findtext("tcx:Time", namespaces=ns)
        pos = tp.find("tcx:Position", ns)
        lat = lon = None
        if pos is not None:
            la = pos.findtext("tcx:LatitudeDegrees", namespaces=ns)
            lo = pos.findtext("tcx:LongitudeDegrees", namespaces=ns)
            lat = float(la) if la else None
            lon = float(lo) if lo else None
        ele = tp.findtext("tcx:AltitudeMeters", namespaces=ns)
        hr  = tp.findtext(".//tcx:HeartRateBpm/tcx:Value", namespaces=ns)
        cad = tp.findtext("tcx:Cadence", namespaces=ns)
        pwr = tp.findtext(".//ns3:TPX/ns3:Watts", namespaces=ns)
        pts.append({
            "time": time,
            "lat": lat, "lon": lon,
            "ele": float(ele) if ele else None,
            "hr": int(hr) if hr else None,
            "cad": int(cad) if cad else None,
            "pwr": int(pwr) if pwr else None
        })
    return pts

def parse_any(name: str, text: str) -> List[Dict]:
    lname = name.lower()
    if lname.endswith(".gpx"):
        return parse_gpx(text)
    if lname.endswith(".tcx"):
        return parse_tcx(text)
    raise ValueError(f"Unsupported file type for {name!r}")

# ----------------- timeline helpers -----------------
def rebase(points: List[Dict], new_start: datetime):
    if not points:
        return [], new_start, new_start
    src0 = iso_to_dt(points[0]["time"])
    shift = new_start - src0
    out = []
    for p in points:
        q = dict(p)
        t = iso_to_dt(p["time"]) + shift
        q["time"] = dt_to_iso(t)
        out.append(q)
    return out, iso_to_dt(out[0]["time"]), iso_to_dt(out[-1]["time"])

def infer_gaps(swim, bike, run):
    """Return (t1_secs, t2_secs) from leg boundaries, never negative."""
    t1 = max(0.0, (bike["start"] - swim["stop"]).total_seconds())
    t2 = max(0.0, (run["start"]  - bike["stop"]).total_seconds())
    return t1, t2

# ----------------- TCX build -----------------
def make_activity(start_iso: str, sport: str, label: str, points: List[Dict], stop_iso: str, lap_distance_m: Optional[float] = None):
    act = ET.Element("Activity", {"Sport": sport})
    mk(act, "Id", start_iso)
    lap = ET.SubElement(act, "Lap", {"StartTime": start_iso})
    total = max(0.0, (iso_to_dt(stop_iso) - iso_to_dt(start_iso)).total_seconds())
    mk(lap, "TotalTimeSeconds", f"{total:.1f}")
    mk(lap, "DistanceMeters", f"{float(lap_distance_m):.1f}" if lap_distance_m is not None else "0.0")
    mk(lap, "MaximumSpeed", "0.0")
    mk(lap, "Calories", "0")
    mk(lap, "Intensity", "Active")
    mk(lap, "TriggerMethod", "Manual")
    trk = ET.SubElement(lap, "Track")
    for p in points:
        tp = ET.SubElement(trk, "Trackpoint")
        mk(tp, "Time", p["time"])
        if p.get("lat") is not None and p.get("lon") is not None:
            pos = ET.SubElement(tp, "Position")
            mk(pos, "LatitudeDegrees", f"{p['lat']:.8f}")
            mk(pos, "LongitudeDegrees", f"{p['lon']:.8f}")
        if p.get("ele") is not None:
            mk(tp, "AltitudeMeters", f"{p['ele']:.2f}")
        if p.get("hr") is not None:
            hr = ET.SubElement(tp, "HeartRateBpm")
            mk(hr, "Value", str(p["hr"]))
        if p.get("cad") is not None:
            mk(tp, "Cadence", str(p["cad"]))
        if p.get("pwr") is not None:
            ext = ET.SubElement(tp, "Extensions")
            tpx = ET.SubElement(ext, f"{{{NS3_NS}}}TPX")
            mk(tpx, f"{{{NS3_NS}}}Watts", str(p["pwr"]))
    mk(lap, "Notes", label)
    return act

def build_plan_from_files(swim, bike, run, t1=None, t2=None, infer_missing=True):
    """
    swim/bike/run/t1/t2 are dicts with: name, points, start, stop, count (or None)
    """
    if not (swim and bike and run):
        return None, "Need Swim, Bike, and Run."
    plan = {"swim": swim, "bike": bike, "run": run, "t1_file": t1, "t2_file": t2,
            "t1_inferred": None, "t2_inferred": None}

    if infer_missing:
        t1_sec, t2_sec = infer_gaps(swim, bike, run)
        if t1 is None:
            plan["t1_inferred"] = t1_sec
        if t2 is None:
            plan["t2_inferred"] = t2_sec
    return plan, None

def build_tcx_from_plan(plan, compact: bool, swim_dist_m: Optional[float]) -> bytes:
    root = tcx_root()
    activities = root.find("Activities")
    ids = []

    def add_item(item, sport_key, label, dist=None):
        act = make_activity(dt_to_iso(item["start"]), SPORT_MAP[sport_key], label, item["points"], dt_to_iso(item["stop"]), lap_distance_m=dist)
        activities.append(act)
        ids.append(dt_to_iso(item["start"]))

    if not compact:
        add_item(plan["swim"], "swim", "Swim", dist=swim_dist_m)
        if plan["t1_file"] is not None:
            add_item(plan["t1_file"], "transition", "Transition T1")
        elif plan["t1_inferred"] is not None:
            t1s = plan["swim"]["stop"]; t1e = t1s + timedelta(seconds=plan["t1_inferred"])
            pts = [{"time": dt_to_iso(t1s)}, {"time": dt_to_iso(t1e)}]
            activities.append(make_activity(dt_to_iso(t1s), SPORT_MAP["transition"], "Transition T1", pts, dt_to_iso(t1e))); ids.append(dt_to_iso(t1s))
        add_item(plan["bike"], "bike", "Bike")
        if plan["t2_file"] is not None:
            add_item(plan["t2_file"], "transition", "Transition T2")
        elif plan["t2_inferred"] is not None:
            t2s = plan["bike"]["stop"]; t2e = t2s + timedelta(seconds=plan["t2_inferred"])
            pts = [{"time": dt_to_iso(t2s)}, {"time": dt_to_iso(t2e)}]
            activities.append(make_activity(dt_to_iso(t2s), SPORT_MAP["transition"], "Transition T2", pts, dt_to_iso(t2e))); ids.append(dt_to_iso(t2s))
        add_item(plan["run"], "run", "Run")
    else:
        cursor = plan["swim"]["start"]
        swim_pts, s0, s1 = rebase(plan["swim"]["points"], cursor)
        activities.append(make_activity(dt_to_iso(s0), SPORT_MAP["swim"], "Swim", swim_pts, dt_to_iso(s1), lap_distance_m=swim_dist_m)); ids.append(dt_to_iso(s0)); cursor = s1

        if plan["t1_file"] is not None:
            t1_pts, t1s, t1e = rebase(plan["t1_file"]["points"], cursor)
            activities.append(make_activity(dt_to_iso(t1s), SPORT_MAP["transition"], "Transition T1", t1_pts, dt_to_iso(t1e))); ids.append(dt_to_iso(t1s)); cursor = t1e
        elif plan["t1_inferred"] is not None:
            t1s = cursor; t1e = t1s + timedelta(seconds=plan["t1_inferred"])
            pts = [{"time": dt_to_iso(t1s)}, {"time": dt_to_iso(t1e)}]
            activities.append(make_activity(dt_to_iso(t1s), SPORT_MAP["transition"], "Transition T1", pts, dt_to_iso(t1e))); ids.append(dt_to_iso(t1s)); cursor = t1e

        bike_pts, b0, b1 = rebase(plan["bike"]["points"], cursor)
        activities.append(make_activity(dt_to_iso(b0), SPORT_MAP["bike"], "Bike", bike_pts, dt_to_iso(b1))); ids.append(dt_to_iso(b0)); cursor = b1

        if plan["t2_file"] is not None:
            t2_pts, t2s, t2e = rebase(plan["t2_file"]["points"], cursor)
            activities.append(make_activity(dt_to_iso(t2s), SPORT_MAP["transition"], "Transition T2", t2_pts, dt_to_iso(t2e))); ids.append(dt_to_iso(t2s)); cursor = t2e
        elif plan["t2_inferred"] is not None:
            t2s = cursor; t2e = t2s + timedelta(seconds=plan["t2_inferred"])
            pts = [{"time": dt_to_iso(t2s)}, {"time": dt_to_iso(t2e)}]
            activities.append(make_activity(dt_to_iso(t2s), SPORT_MAP["transition"], "Transition T2", pts, dt_to_iso(t2e))); ids.append(dt_to_iso(t2s)); cursor = t2e

        run_pts, r0, r1 = rebase(plan["run"]["points"], cursor)
        activities.append(make_activity(dt_to_iso(r0), SPORT_MAP["run"], "Run", run_pts, dt_to_iso(r1))); ids.append(dt_to_iso(r0))

    # Link MultiSport
    mss = ET.SubElement(activities, "MultiSportSession")
    first = ET.SubElement(mss, "FirstSport"); ar = ET.SubElement(first, "ActivityRef"); mk(ar, "Id", ids[0])
    for aid in ids[1:]:
        ns = ET.SubElement(mss, "NextSport"); ar = ET.SubElement(ns, "ActivityRef"); mk(ar, "Id", aid)

    xml = ET.tostring(root, encoding="utf-8")
    pretty = minidom.parseString(xml).toprettyxml(indent="  ", encoding="utf-8")
    return pretty  # bytes or str both fine for writing


def summary_lines_from_plan(plan, compact: bool = False):
    """
    Produce human-readable summary lines.
    If compact=True, times are rebased back-to-back (like in TCX export).
    """
    lines = []

    def line(label, start, stop, count=None):
        dur = (stop - start).total_seconds()
        m, s = divmod(int(dur), 60)
        suffix = f"  {count} trackpoints" if count is not None else ""
        lines.append(f"{label:<12} {fmt_dt_human(start)} → {fmt_dt_human(stop)}  [{m}:{s:02d}]{suffix}")


    if not compact:
        # --- Normal mode: show real-world times ---
        s = plan["swim"]; b = plan["bike"]; r = plan["run"]
        line("Swim", s["start"], s["stop"], s.get("count"))
        if plan.get("t1_file"):
            t = plan["t1_file"]; line("T1 (file)", t["start"], t["stop"], t.get("count"))
        elif plan.get("t1_inferred") is not None:
            t1s = s["stop"]; t1e = t1s + timedelta(seconds=plan["t1_inferred"]); line("T1 (inferred)", t1s, t1e)
        line("Bike", b["start"], b["stop"], b.get("count"))
        if plan.get("t2_file"):
            t = plan["t2_file"]; line("T2 (file)", t["start"], t["stop"], t.get("count"))
        elif plan.get("t2_inferred") is not None:
            t2s = b["stop"]; t2e = t2s + timedelta(seconds=plan["t2_inferred"]); line("T2 (inferred)", t2s, t2e)
        line("Run", r["start"], r["stop"], r.get("count"))

    else:
        # --- Compact mode: simulate rebased timeline ---
        cursor = plan["swim"]["start"]

        # Swim
        _, s0, s1 = rebase(plan["swim"]["points"], cursor)
        line("Swim", s0, s1, plan["swim"].get("count"))
        cursor = s1

        # T1
        if plan["t1_file"]:
            _, t1s, t1e = rebase(plan["t1_file"]["points"], cursor)
            line("T1 (file)", t1s, t1e, plan["t1_file"].get("count"))
            cursor = t1e
        elif plan.get("t1_inferred") is not None:
            t1s = cursor; t1e = t1s + timedelta(seconds=plan["t1_inferred"])
            line("T1 (inferred)", t1s, t1e)
            cursor = t1e

        # Bike
        _, b0, b1 = rebase(plan["bike"]["points"], cursor)
        line("Bike", b0, b1, plan["bike"].get("count"))
        cursor = b1

        # T2
        if plan["t2_file"]:
            _, t2s, t2e = rebase(plan["t2_file"]["points"], cursor)
            line("T2 (file)", t2s, t2e, plan["t2_file"].get("count"))
            cursor = t2e
        elif plan.get("t2_inferred") is not None:
            t2s = cursor; t2e = t2s + timedelta(seconds=plan["t2_inferred"])
            line("T2 (inferred)", t2s, t2e)
            cursor = t2e

        # Run
        _, r0, r1 = rebase(plan["run"]["points"], cursor)
        line("Run", r0, r1, plan["run"].get("count"))

    return lines

