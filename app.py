from js import document, FileReader, Blob, URL, enableDnD
import js, asyncio
from xml.etree import ElementTree as ET
from datetime import datetime, timedelta, timezone
from xml.dom import minidom

TCX_NS = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
NS3_NS = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"
ET.register_namespace("", TCX_NS); ET.register_namespace("xsi", XSI_NS); ET.register_namespace("ns3", NS3_NS)

# ---------- Utility ----------
def iso_to_dt(s:str)->datetime:
    s = s.replace("Z","+00:00")
    try: return datetime.fromisoformat(s)
    except Exception: return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")

def dt_to_iso(d:datetime)->str:
    return d.astimezone(timezone.utc).isoformat().replace("+00:00","Z")

def fmt_dur(seconds: float) -> str:
    seconds = int(round(seconds))
    h = seconds // 3600; m = (seconds % 3600) // 60; s = seconds % 60
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"

def tcx_root():
    root = ET.Element(f"{{{TCX_NS}}}TrainingCenterDatabase", {
        f"{{{XSI_NS}}}schemaLocation": f"{TCX_NS} {TCX_NS.replace('/v2','v2')}"
    })
    ET.SubElement(root, "Folders"); ET.SubElement(root, "Activities")
    return root

def mk(parent, tag, text=None):
    e = ET.SubElement(parent, tag)
    if text is not None: e.text = text
    return e

# ---------- Parsers ----------

def parse_gpx(text:str):
    root = ET.fromstring(text)
    gns = {"gpx": root.tag.split('}')[0].strip('{')}
    pts=[]
    for tp in root.findall(".//gpx:trkpt", gns):
        t = tp.findtext("gpx:time", namespaces=gns)
        la = tp.get("lat"); lo = tp.get("lon")
        ele = tp.findtext("gpx:ele", namespaces=gns)
        pts.append({"time": t, "lat": float(la) if la else None, "lon": float(lo) if lo else None,
                    "ele": float(ele) if ele else None, "hr":None, "cad":None, "pwr":None})
    return pts

def parse_tcx(text:str):
    ns = {"tcx": TCX_NS, "ns3": NS3_NS}
    root = ET.fromstring(text)
    pts=[]
    for tp in root.findall(".//tcx:Trackpoint", ns):
        time = tp.findtext("tcx:Time", namespaces=ns)
        pos = tp.find("tcx:Position", ns)
        lat = lon = None
        if pos is not None:
            la = pos.findtext("tcx:LatitudeDegrees", namespaces=ns)
            lo = pos.findtext("tcx:LongitudeDegrees", namespaces=ns)
            lat = float(la) if la else None; lon = float(lo) if lo else None
        ele = tp.findtext("tcx:AltitudeMeters", namespaces=ns)
        hr  = tp.findtext(".//tcx:HeartRateBpm/tcx:Value", namespaces=ns)
        cad = tp.findtext("tcx:Cadence", namespaces=ns)
        pwr = tp.findtext(".//ns3:TPX/ns3:Watts", namespaces=ns)
        pts.append({"time": time, "lat":lat, "lon":lon, "ele": float(ele) if ele else None,
                    "hr": int(hr) if hr else None, "cad": int(cad) if cad else None,
                    "pwr": int(pwr) if pwr else None})
    return pts

def parse_any(name:str, text:str):
    if name.lower().endswith(".gpx"): return parse_gpx(text)
    if name.lower().endswith(".tcx"): return parse_tcx(text)
    raise ValueError("Unsupported file type")

# ---------- Rebase & Emit ----------

def rebase(points, new_start:datetime):
    if not points: return [], new_start, new_start
    src0 = iso_to_dt(points[0]["time"])
    shift = new_start - src0
    out=[]
    for p in points:
        q = dict(p)
        t = iso_to_dt(p["time"]) + shift
        q["time"] = dt_to_iso(t)
        out.append(q)
    return out, iso_to_dt(out[0]["time"]), iso_to_dt(out[-1]["time"])

SPORT_MAP = {"swim":"Swimming","bike":"Biking","run":"Running","transition":"Other"}


def make_activity(start_iso, sport, label, points, stop_iso, lap_distance_m=None):
    act = ET.Element("Activity", {"Sport": sport})
    mk(act, "Id", start_iso)
    lap = ET.SubElement(act, "Lap", {"StartTime": start_iso})
    total = max(0.0, (iso_to_dt(stop_iso) - iso_to_dt(start_iso)).total_seconds())
    mk(lap, "TotalTimeSeconds", f"{total:.1f}")
    mk(lap, "DistanceMeters", f"{float(lap_distance_m):.1f}" if lap_distance_m is not None else "0.0")
    mk(lap, "MaximumSpeed", "0.0"); mk(lap, "Calories", "0")
    mk(lap, "Intensity", "Active"); mk(lap, "TriggerMethod", "Manual")
    trk = ET.SubElement(lap, "Track")
    for p in points:
        tp = ET.SubElement(trk, "Trackpoint")
        mk(tp, "Time", p["time"])
        if p.get("lat") is not None and p.get("lon") is not None:
            pos = ET.SubElement(tp, "Position")
            mk(pos, "LatitudeDegrees", f"{p['lat']:.8f}")
            mk(pos, "LongitudeDegrees", f"{p['lon']:.8f}")
        if p.get("ele") is not None: mk(tp, "AltitudeMeters", f"{p['ele']:.2f}")
        if p.get("hr") is not None:
            hr = ET.SubElement(tp, "HeartRateBpm"); mk(hr, "Value", str(p["hr"]))
        if p.get("cad") is not None: mk(tp, "Cadence", str(p["cad"]))
        if p.get("pwr") is not None:
            ext = ET.SubElement(tp, "Extensions")
            tpx = ET.SubElement(ext, f"{{{NS3_NS}}}TPX")
            mk(tpx, f"{{{NS3_NS}}}Watts", str(p["pwr"]))
    mk(lap, "Notes", label)
    return act

# ---------- File Reading & UI ----------

async def read_file(file_obj):
    fr = FileReader()
    done = asyncio.get_event_loop().create_future()
    def onload(ev): done.set_result(fr.result)
    fr.onload = onload
    fr.readAsText(file_obj)
    txt = await done
    return txt

files_cache = []  # list of dicts: {name, role, points, start, stop, count}

async def scan_click(evt):
    global files_cache
    tableWrap = document.getElementById("tableWrap")
    tbody = document.getElementById("fileTbody")
    tbody.innerHTML = ""
    files_cache = []
    fsel = document.getElementById("files")
    if not fsel.files.length:
        document.getElementById("scanStatus").textContent = "Choose GPX/TCX files first."; return
    document.getElementById("scanStatus").textContent = "Parsing…"

    for i in range(fsel.files.length):
        f = fsel.files.item(i)
        text = await read_file(f)
        try:
            pts = parse_any(f.name, text)
            start = iso_to_dt(pts[0]["time"]) if pts else None
            stop  = iso_to_dt(pts[-1]["time"]) if pts else None
            files_cache.append({
                "name": f.name,
                "role": guess_role_from_name(f.name),
                "points": pts,
                "start": start,
                "stop": stop,
                "count": len(pts),
            })
        except Exception:
            files_cache.append({"name": f.name, "role": "ignore", "points": [], "start": None, "stop": None, "count": 0})

    # Sort by start time initially
    files_cache.sort(key=lambda x: x["start"] or datetime.max)

    for item in files_cache:
        tr = document.createElement("tr"); tr.classList.add("file-row")
        # drag handle
        drag = document.createElement("td"); drag.innerHTML = '<span class="drag-handle">☰</span>'; tr.appendChild(drag)
        td_name = document.createElement("td"); td_name.textContent = item["name"]; tr.appendChild(td_name)
        td_role = document.createElement("td")
        sel = document.createElement("select"); sel.className = "form-select form-select-sm"
        for r in ["swim","bike","run","transition","ignore"]:
            opt = document.createElement("option"); opt.value = r; opt.textContent = r.capitalize()
            if r == item["role"]: opt.selected = True
            sel.appendChild(opt)
        td_role.appendChild(sel); tr.appendChild(td_role)
        td_start = document.createElement("td"); td_start.textContent = item["start"].isoformat() if item["start"] else "—"; tr.appendChild(td_start)
        td_end   = document.createElement("td"); td_end.textContent   = item["stop"].isoformat() if item["stop"] else "—"; tr.appendChild(td_end)
        dur = (item["stop"] - item["start"]).total_seconds() if item["start"] and item["stop"] else 0
        td_dur = document.createElement("td"); td_dur.textContent = fmt_dur(dur) if dur else "—"; tr.appendChild(td_dur)
        td_pts = document.createElement("td"); td_pts.textContent = str(item["count"]); tr.appendChild(td_pts)
        tr._roleSelect = sel
        tbody.appendChild(tr)

    # enable drag-and-drop on current rows
    enableDnD("fileTbody")

    tableWrap.style.display = "block"
    document.getElementById("scanStatus").textContent = f"Loaded {len(files_cache)} file(s). Drag to set priority and assign roles."


def guess_role_from_name(name:str) -> str:
    low = name.lower()
    if "swim" in low: return "swim"
    if "bike" in low or "ride" in low or "cycle" in low: return "bike"
    if "run" in low: return "run"
    if "t1" in low or "t2" in low or "trans" in low: return "transition"
    return "ignore"


def collect_roles_from_table():
    tbody = document.getElementById("fileTbody")
    rows = list(tbody.children)  # in current visual order
    roles = []
    for row in rows:
        sel = row._roleSelect
        fname = row.children[1].textContent
        item = next((x for x in files_cache if x["name"] == fname), None)
        if not item: continue
        role = sel.value
        roles.append({**item, "role": role})
    roles = [r for r in roles if r["role"] != "ignore" and r["start"] is not None]
    return roles

# ---------- Preview & Build Plan ----------

def build_plan(roles, infer_missing: bool):
    # The first occurrence (topmost) of each role is chosen when multiple exist
    swim = next((r for r in roles if r["role"]=="swim"), None)
    bike = next((r for r in roles if r["role"]=="bike"), None)
    run  = next((r for r in roles if r["role"]=="run"),  None)
    if not (swim and bike and run):
        return None, "Need at least one Swim, one Bike, and one Run (topmost per role is used)."

    # Prefer transition files that fall between the adjacent legs; else infer
    transitions = [r for r in roles if r["role"]=="transition"]
    t1_item = next((t for t in transitions if swim["stop"] <= t["start"] <= bike["start"]), None)
    t2_item = next((t for t in transitions if bike["stop"] <= t["start"] <= run["start"]), None)

    t1_inf = t2_inf = None
    if infer_missing:
        if t1_item is None:
            t1_inf = max(0.0, (bike["start"] - swim["stop"]).total_seconds())
        if t2_item is None:
            t2_inf = max(0.0, (run["start"] - bike["stop"]).total_seconds())

    plan = {"swim": swim, "bike": bike, "run": run,
            "t1_file": t1_item, "t2_file": t2_item,
            "t1_inferred": t1_inf, "t2_inferred": t2_inf}
    return plan, None


def render_preview(plan):
    timeline = document.getElementById("timeline")
    lines = []
    def line(label, start, stop, extra=""):
        dur = (stop - start).total_seconds()
        lines.append(f"{label:<12} {start.isoformat()}  →  {stop.isoformat()}   [{fmt_dur(dur)}] {extra}")
    s, b, r = plan["swim"], plan["bike"], plan["run"]
    line("Swim", s["start"], s["stop"], f"{s['count']} pts")
    if plan["t1_file"]: t=plan["t1_file"]; line("T1 (file)", t["start"], t["stop"], f"{t['count']} pts")
    elif plan["t1_inferred"] is not None:
        t1s, t1e = s["stop"], s["stop"] + timedelta(seconds=plan["t1_inferred"]) ; line("T1 (inferred)", t1s, t1e)
    line("Bike", b["start"], b["stop"], f"{b['count']} pts")
    if plan["t2_file"]: t=plan["t2_file"]; line("T2 (file)", t["start"], t["stop"], f"{t['count']} pts")
    elif plan["t2_inferred"] is not None:
        t2s, t2e = b["stop"], b["stop"] + timedelta(seconds=plan["t2_inferred"]) ; line("T2 (inferred)", t2s, t2e)
    line("Run", r["start"], r["stop"], f"{r['count']} pts")
    timeline.innerText = "
".join(lines)
    document.getElementById("previewBox").style.display = "block"

# ---------- Build TCX from Plan ----------

def build_tcx_from_plan(plan, compact: bool, swim_dist_m):
    root = tcx_root(); activities = root.find("Activities"); ids = []

    def add_item(item, sport_key, label, dist=None):
        act = make_activity(dt_to_iso(item["start"]), SPORT_MAP[sport_key], label, item["points"], dt_to_iso(item["stop"]), lap_distance_m=dist)
        activities.append(act); ids.append(dt_to_iso(item["start"]))

    if not compact:
        add_item(plan["swim"], "swim", "Swim", dist=swim_dist_m)
        if plan["t1_file"]:
            add_item(plan["t1_file"], "transition", "Transition T1")
        elif plan["t1_inferred"] is not None:
            t1s = plan["swim"]["stop"]; t1e = t1s + timedelta(seconds=plan["t1_inferred"]) 
            pts = [{"time": dt_to_iso(t1s)}, {"time": dt_to_iso(t1e)}]
            activities.append(make_activity(dt_to_iso(t1s), SPORT_MAP["transition"], "Transition T1", pts, dt_to_iso(t1e))); ids.append(dt_to_iso(t1s))
        add_item(plan["bike"], "bike", "Bike")
        if plan["t2_file"]:
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
        if plan["t1_file"]:
            t1_pts, t1s, t1e = rebase(plan["t1_file"]["points"], cursor)
            activities.append(make_activity(dt_to_iso(t1s), SPORT_MAP["transition"], "Transition T1", t1_pts, dt_to_iso(t1e))); ids.append(dt_to_iso(t1s)); cursor = t1e
        elif plan["t1_inferred"] is not None:
            t1s = cursor; t1e = t1s + timedelta(seconds=plan["t1_inferred"]) 
            pts = [{"time": dt_to_iso(t1s)}, {"time": dt_to_iso(t1e)}]
            activities.append(make_activity(dt_to_iso(t1s), SPORT_MAP["transition"], "Transition T1", pts, dt_to_iso(t1e))); ids.append(dt_to_iso(t1s)); cursor = t1e
        bike_pts, b0, b1 = rebase(plan["bike"]["points"], cursor)
        activities.append(make_activity(dt_to_iso(b0), SPORT_MAP["bike"], "Bike", bike_pts, dt_to_iso(b1))); ids.append(dt_to_iso(b0)); cursor = b1
        if plan["t2_file"]:
            t2_pts, t2s, t2e = rebase(plan["t2_file"]["points"], cursor)
            activities.append(make_activity(dt_to_iso(t2s), SPORT_MAP["transition"], "Transition T2", t2_pts, dt_to_iso(t2e))); ids.append(dt_to_iso(t2s)); cursor = t2e
        elif plan["t2_inferred"] is not None:
            t2s = cursor; t2e = t2s + timedelta(seconds=plan["t2_inferred"]) 
            pts = [{"time": dt_to_iso(t2s)}, {"time": dt_to_iso(t2e)}]
            activities.append(make_activity(dt_to_iso(t2s), SPORT_MAP["transition"], "Transition T2", pts, dt_to_iso(t2e))); ids.append(dt_to_iso(t2s)); cursor = t2e
        run_pts, r0, r1 = rebase(plan["run"]["points"], cursor)
        activities.append(make_activity(dt_to_iso(r0), SPORT_MAP["run"], "Run", run_pts, dt_to_iso(r1))); ids.append(dt_to_iso(r0))

    mss = ET.SubElement(activities, "MultiSportSession")
    first = ET.SubElement(mss, "FirstSport"); ar = ET.SubElement(first, "ActivityRef"); mk(ar, "Id", ids[0])
    for aid in ids[1:]:
        ns = ET.SubElement(mss, "NextSport"); ar = ET.SubElement(ns, "ActivityRef"); mk(ar, "Id", aid)

    xml = ET.tostring(root, encoding="utf-8")
    pretty = minidom.parseString(xml).toprettyxml(indent="  ", encoding="utf-8")
    return pretty

# ---------- Event handlers ----------

async def preview_click(evt):
    roles = collect_roles_from_table()
    infer_missing = bool(document.getElementById("infer").checked)
    plan, err = build_plan(roles, infer_missing)
    if err:
        document.getElementById("previewStatus").textContent = err; return
    render_preview(plan)
    document.getElementById("previewStatus").textContent = ""

async def merge_click(evt):
    roles = collect_roles_from_table()
    infer_missing = bool(document.getElementById("infer").checked)
    compact = bool(document.getElementById("compact").checked)
    swim_distance_input = document.getElementById("swimDist").value
    swim_dist_m = float(swim_distance_input) if swim_distance_input else None

    plan, err = build_plan(roles, infer_missing)
    if err:
        document.getElementById("mergeStatus").textContent = err; return

    xml_bytes = build_tcx_from_plan(plan, compact, swim_dist_m)
    blob = Blob.new([xml_bytes], { "type": "application/vnd.garmin.tcx+xml" })
    url = URL.createObjectURL(blob)
    a = document.createElement("a"); a.href = url; a.download = "triathlon.tcx"; a.click()
    URL.revokeObjectURL(url)
    document.getElementById("mergeStatus").textContent = "Done ✔︎  Upload triathlon.tcx to Strava."

# wire buttons

document.getElementById("scan").addEventListener("click", lambda e: asyncio.ensure_future(scan_click(e)))
document.getElementById("preview").addEventListener("click", lambda e: asyncio.ensure_future(preview_click(e)))
document.getElementById("merge").addEventListener("click", lambda e: asyncio.ensure_future(merge_click(e)))
