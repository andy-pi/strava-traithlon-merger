# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# app.py — browser entry point using PyScript; imports shared logic from core.py

import asyncio
from datetime import datetime
from pyodide.ffi import create_proxy

from core import (
    parse_any,
    iso_to_dt,
    fmt_dur,
    build_plan_from_files,
    build_tcx_from_plan,
    summary_lines_from_plan, fmt_dt_human
)
from js import URL, Blob, FileReader, document, console, window

# enableDnD may be provided by a separate JS helper; guard absence
try:
    from js import enableDnD  # type: ignore
except Exception:
    enableDnD = None  # type: ignore

files_cache = []  # list of dicts: {name, role, points, start, stop, count}
_proxies = []     # keep JS proxies alive to avoid "borrowed proxy destroyed"

# ---------- tiny logger helpers ----------
def log(*args):
    try: console.log(*args)
    except Exception: pass

def warn(*args):
    try: console.warn(*args)
    except Exception: pass

def err(*args):
    try: console.error(*args)
    except Exception: pass

log("app.py: loaded")

# ---------- File Reading ----------
async def read_file(file_obj):
    fr = FileReader.new()  # <-- use .new() in Pyodide
    done = asyncio.get_event_loop().create_future()

    # proxy the JS callback so it doesn't get GC'd
    cb = create_proxy(lambda ev: done.set_result(fr.result))
    fr.onload = cb
    fr.readAsText(file_obj)

    txt = await done
    try:
        cb.destroy()  # optional cleanup
    except Exception:
        pass
    return txt


async def scan_click(evt):
    log("scan_click: start")
    try:
        global files_cache
        tableWrap = document.getElementById("tableWrap")
        tbody = document.getElementById("fileTbody")
        tbody.innerHTML = ""
        files_cache = []
        fsel = document.getElementById("files")
        if fsel is None:
            err("scan_click: #files input not found")
            if document.getElementById("scanStatus"):
                document.getElementById("scanStatus").textContent = "Error: file input not found"
            return
        log("scan_click: file input present; count =", fsel.files.length)
        if not fsel.files.length:
            if document.getElementById("scanStatus"):
                document.getElementById("scanStatus").textContent = "Choose GPX/TCX files first."
            warn("scan_click: no files selected")
            return
        if document.getElementById("scanStatus"):
            document.getElementById("scanStatus").textContent = "Parsing…"

        for i in range(fsel.files.length):
            f = fsel.files.item(i)
            log(f"scan_click: reading file[{i}] name={f.name} size={getattr(f, 'size', 'n/a')}")
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
                log(f"scan_click: parsed ok -> {f.name}; points={len(pts)} start={start} stop={stop}")
            except Exception as e:
                files_cache.append({
                    "name": f.name, "role": "ignore",
                    "points": [], "start": None, "stop": None, "count": 0,
                })
                err(f"scan_click: parse failed for {f.name} -> {e}")

        # Sort by start time initially (use datetime.max to avoid mixing types)
        files_cache.sort(key=lambda x: x["start"] or datetime.max)
        log("scan_click: sorted files by start")

        for item in files_cache:
            tr = document.createElement("tr"); tr.classList.add("file-row")
            drag = document.createElement("td")
            drag.innerHTML = '<span class="drag-handle">☰</span>'
            tr.appendChild(drag)

            td_name = document.createElement("td"); td_name.textContent = item["name"]; tr.appendChild(td_name)

            td_role = document.createElement("td")
            sel = document.createElement("select"); sel.className = "form-select form-select-sm"
            for r in ["swim","bike","run","transition","ignore"]:
                opt = document.createElement("option"); opt.value = r; opt.textContent = r.capitalize()
                if r == item["role"]: opt.selected = True
                sel.appendChild(opt)
            td_role.appendChild(sel); tr.appendChild(td_role)

            td_start = document.createElement("td")
            td_start.textContent = fmt_dt_human(item["start"]) if item["start"] else "—"
            tr.appendChild(td_start)

            td_end = document.createElement("td")
            td_end.textContent = fmt_dt_human(item["stop"]) if item["stop"] else "—"
            tr.appendChild(td_end)
            dur = (item["stop"] - item["start"]).total_seconds() if item["start"] and item["stop"] else 0
            td_dur = document.createElement("td"); td_dur.textContent = fmt_dur(dur) if dur else "—"; tr.appendChild(td_dur)

            tr._roleSelect = sel
            tbody.appendChild(tr)

        # enable drag-and-drop if helper is present
        try:
            if enableDnD:
                enableDnD("fileTbody")
                log("scan_click: enableDnD applied")
            else:
                warn("scan_click: enableDnD not available; skipping")
        except Exception as e:
            warn("scan_click: enableDnD failed:", e)

        tableWrap.style.display = "block"
        if document.getElementById("scanStatus"):
            document.getElementById("scanStatus").textContent = f"Loaded {len(files_cache)} file(s). Drag to reorder and assign roles."
        log("scan_click: done; files_cache size =", len(files_cache))
    except Exception as e:
        err("scan_click: top-level exception:", e)
        if document.getElementById("scanStatus"):
            document.getElementById("scanStatus").textContent = f"Error: {e}"

def guess_role_from_name(name:str) -> str:
    low = name.lower()
    if "swim" in low: return "swim"
    if "bike" in low or "ride" in low or "cycle" in low: return "bike"
    if "run" in low: return "run"
    if "t1" in low or "t2" in low or "trans" in low: return "transition"
    return "ignore"

def collect_roles_from_table():
    tbody = document.getElementById("fileTbody")
    rows = list(tbody.children)
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

# ---------- Event handlers ----------
async def preview_click(evt):
    log("preview_click")
    roles = collect_roles_from_table()
    infer_missing = bool(document.getElementById("infer").checked)
    swim = next((r for r in roles if r["role"]=="swim"), None)
    bike = next((r for r in roles if r["role"]=="bike"), None)
    run  = next((r for r in roles if r["role"]=="run"), None)
    t1   = next((r for r in roles if r["role"]=="transition" and swim and swim["stop"] <= r["start"] <= bike["start"]), None)
    t2   = next((r for r in roles if r["role"]=="transition" and bike and bike["stop"] <= r["start"] <= run["start"]), None)

    plan, err = build_plan_from_files(swim, bike, run, t1, t2, infer_missing=infer_missing)
    if err:
        document.getElementById("previewStatus").textContent = err
        warn("preview_click:", err)
        return

    lines = summary_lines_from_plan(plan, compact=bool(document.getElementById("compact").checked))
    document.getElementById("timeline").innerText = "\n".join(lines)
    document.getElementById("previewBox").style.display = "block"
    document.getElementById("previewStatus").textContent = ""
    log("preview_click: summary rendered")

async def merge_click(evt):
    log("merge_click")
    roles = collect_roles_from_table()
    infer_missing = bool(document.getElementById("infer").checked)
    compact = bool(document.getElementById("compact").checked)
    swim_distance_input = document.getElementById("swimDist").value
    swim_dist_m = float(swim_distance_input) if swim_distance_input else None

    swim = next((r for r in roles if r["role"]=="swim"), None)
    bike = next((r for r in roles if r["role"]=="bike"), None)
    run  = next((r for r in roles if r["role"]=="run"), None)
    t1   = next((r for r in roles if r["role"]=="transition" and swim and swim["stop"] <= r["start"] <= bike["start"]), None)
    t2   = next((r for r in roles if r["role"]=="transition" and bike and bike["stop"] <= r["start"] <= run["start"]), None)

    plan, err = build_plan_from_files(swim, bike, run, t1, t2, infer_missing=infer_missing)
    if err:
        document.getElementById("mergeStatus").textContent = err
        warn("merge_click:", err)
        return

    # Show verification summary BEFORE download
    document.getElementById("timeline").innerText = "\n".join(summary_lines_from_plan(plan, compact=compact))
    document.getElementById("previewBox").style.display = "block"

    try:
        xml_bytes = build_tcx_from_plan(plan, compact=compact, swim_dist_m=swim_dist_m)
        blob = Blob.new([xml_bytes], { "type": "application/vnd.garmin.tcx+xml" })
        url = URL.createObjectURL(blob)
        a = document.createElement("a"); a.href = url; a.download = "triathlon.tcx"; a.click()
        URL.revokeObjectURL(url)
        document.getElementById("mergeStatus").textContent = "Done ✔︎  Upload triathlon.tcx to Strava."
        log("merge_click: file built and download triggered")
    except Exception as e:
        err("merge_click: build/download failed:", e)
        document.getElementById("mergeStatus").textContent = f"Error: {e}"

# ---------- Bind handlers with proxies (prevents GC) ----------
def _bind_handlers(evt=None):
    log("_bind_handlers: wiring buttons (with proxies)")
    scan_btn = document.getElementById("scan")
    prev_btn = document.getElementById("preview")
    merge_btn = document.getElementById("merge")
    if not (scan_btn and prev_btn and merge_btn):
        err("_bind_handlers: one or more buttons not found; check index.html IDs")
        return

    # Wrap callbacks in pyodide proxies and keep references
    scan_cb = create_proxy(lambda e: asyncio.ensure_future(scan_click(e)))
    prev_cb = create_proxy(lambda e: asyncio.ensure_future(preview_click(e)))
    merge_cb = create_proxy(lambda e: asyncio.ensure_future(merge_click(e)))
    _proxies.extend([scan_cb, prev_cb, merge_cb])

    scan_btn.addEventListener("click", scan_cb)
    prev_btn.addEventListener("click", prev_cb)
    merge_btn.addEventListener("click", merge_cb)
    log("_bind_handlers: wired")

# If DOM is already ready, bind now; else bind on DOMContentLoaded
try:
    rs = document.readyState
    log("document.readyState =", rs)
    if rs in ("interactive", "complete"):
        _bind_handlers()
    else:
        window.addEventListener("DOMContentLoaded", create_proxy(_bind_handlers))
except Exception as e:
    err("ready/bind failed:", e)
