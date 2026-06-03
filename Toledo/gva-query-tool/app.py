"""
Toledo GVA Query Tool
Flask backend — serves the chat UI, calls Claude API, logs to Google Sheets.

Spatial analysis is pre-computed at startup:
  - Incidents assigned to neighborhoods (point-in-polygon, shapely)
  - Incidents assigned to school attendance zones (point-in-polygon, shapely)
  - Incidents within 250 m / 500 m / 1000 m of each park (Haversine)
  - Incidents within 250 m / 500 m / 1000 m of each high school (Haversine)

Map API endpoints:
  GET /api/incidents           → all geocoded incidents as compact JSON
  GET /api/incidents?year=2023 → filtered by one or more years
  GET /api/layers/neighborhoods → neighborhood boundary GeoJSON
  GET /api/layers/parks         → park point GeoJSON (centroids)
  GET /api/layers/schools       → high school point GeoJSON
  GET /api/layers/school_areas  → school attendance zone GeoJSON
"""

import os
import json
import uuid
import csv as csv_module
import traceback
from math import radians, sin, cos, sqrt, atan2
from datetime import datetime, timezone
from collections import defaultdict
from flask import Flask, request, jsonify
import anthropic

app = Flask(__name__)

# ── Path helpers ───────────────────────────────────────────────────────────────

APP_DIR          = os.path.dirname(os.path.abspath(__file__))
DATA_DIR         = os.environ.get("DATA_DIR", os.path.join(APP_DIR, ".."))
INSTRUCTIONS_DIR = os.path.join(APP_DIR, "data")

DIST_THRESHOLDS = [250, 500, 1000]   # metres

# ── In-memory stores for the map API (populated at startup) ───────────────────

_INCIDENTS_FOR_MAP: list = []    # [{lat, lon, k, i, year, month}]
_LAYER_GEOJSON: dict     = {}    # keyed by layer name


# ── Spatial helpers ────────────────────────────────────────────────────────────

def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    R = 6_371_000
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi    = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def load_neighborhoods(path: str):
    """Return (name, shapely_geom) list and raw GeoJSON dict."""
    from shapely.geometry import shape
    with open(path, encoding="utf-8") as f:
        fc = json.load(f)
    pairs = [
        (feat["properties"]["name"], shape(feat["geometry"]))
        for feat in fc["features"] if feat.get("geometry")
    ]
    # Serve a stripped-down GeoJSON (name + geometry only)
    simple_fc = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature",
             "properties": {"name": feat["properties"]["name"]},
             "geometry": feat["geometry"]}
            for feat in fc["features"] if feat.get("geometry")
        ]
    }
    return pairs, simple_fc


def load_park_centroids(path: str):
    """Return (name, lon, lat) list and Point GeoJSON dict."""
    from shapely.geometry import shape
    with open(path, encoding="utf-8") as f:
        fc = json.load(f)
    pts, features = [], []
    for feat in fc["features"]:
        if not feat.get("geometry"):
            continue
        name = feat["properties"].get("Parks", "Unknown")
        c = shape(feat["geometry"]).centroid
        pts.append((name, c.x, c.y))
        features.append({
            "type": "Feature",
            "properties": {"name": name},
            "geometry": {"type": "Point", "coordinates": [c.x, c.y]}
        })
    return pts, {"type": "FeatureCollection", "features": features}


def load_school_areas(shp_base: str):
    """Return (school_name, shapely_geom) list and Polygon GeoJSON dict."""
    import shapefile
    from shapely.geometry import shape
    sf = shapefile.Reader(shp_base)
    fields = [f[0] for f in sf.fields[1:]]
    pairs, features = [], []
    for sr in sf.shapeRecords():
        if sr.shape.shapeType == 0:
            continue
        props = dict(zip(fields, sr.record))
        school = props.get("highschool", "Unknown")
        geom   = shape(sr.shape.__geo_interface__)
        pairs.append((school, geom))
        features.append({
            "type": "Feature",
            "properties": {"school": school},
            "geometry": sr.shape.__geo_interface__
        })
    return pairs, {"type": "FeatureCollection", "features": features}


def load_school_points(shp_base: str):
    """Return (name, lon, lat) list and Point GeoJSON dict.
    NOTE: field names in this shapefile are swapped — 'long' holds latitude
    (~41.6) and 'lat' holds longitude (~-83.5).
    """
    import shapefile
    sf = shapefile.Reader(shp_base)
    fields = [f[0] for f in sf.fields[1:]]
    pts, features = [], []
    for sr in sf.shapeRecords():
        props = dict(zip(fields, sr.record))
        name = props["high_schoo"]
        lat  = props["long"]   # mislabelled — actually latitude
        lon  = props["lat"]    # mislabelled — actually longitude
        pts.append((name, lon, lat))
        features.append({
            "type": "Feature",
            "properties": {"name": name},
            "geometry": {"type": "Point", "coordinates": [lon, lat]}
        })
    return pts, {"type": "FeatureCollection", "features": features}


def assign_point(lon: float, lat: float, named_geoms):
    from shapely.geometry import Point
    pt = Point(lon, lat)
    for name, geom in named_geoms:
        try:
            if geom.contains(pt):
                return name
        except Exception:
            pass
    return None


# ── Date parsing ──────────────────────────────────────────────────────────────

def parse_gva_date(s: str) -> datetime:
    s = s.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",   # ISO 8601 with Z  — e.g. 2026-05-29T04:00:00Z
        "%Y-%m-%dT%H:%M:%S",    # ISO 8601 no Z
        "%m/%d/%Y %I:%M:%S %p", # M/D/YYYY with time — e.g. 5/29/2026 4:00:00 AM
        "%m/%d/%Y",             # M/D/YYYY date only
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {s!r}")


# ── Bucket helpers ────────────────────────────────────────────────────────────

def _new_bucket():
    return {"k": 0, "i": 0, "inj": 0, "all": 0}

def _add(bucket, k, i):
    bucket["k"] += k; bucket["i"] += i; bucket["all"] += 1
    if k + i > 0: bucket["inj"] += 1

def _row(prefix, s):
    vc = s["k"] + s["i"]
    return f"{prefix},{s['k']},{s['i']},{vc},{s['inj']},{s['all']}"


# ── Main startup function ──────────────────────────────────────────────────────

def build_system_context(data_dir: str, instructions_dir: str) -> str:
    global _INCIDENTS_FOR_MAP, _LAYER_GEOJSON

    parts = []

    # 1. CLAUDE.md
    claude_md = os.path.join(instructions_dir, "CLAUDE.md")
    if os.path.exists(claude_md):
        with open(claude_md, encoding="utf-8") as f:
            parts.append(f.read())
    else:
        print(f"WARNING: {claude_md} not found.")

    # 2. Load GVA incidents
    gva_path = os.path.join(data_dir, "GVA_Toledo_260603_geocoded.csv")
    if not os.path.exists(gva_path):
        print(f"WARNING: {gva_path} not found.")
        return "\n".join(parts)

    print("  Loading incidents…")
    incidents = []
    with open(gva_path, encoding="utf-8-sig") as f:
        for row in csv_module.DictReader(f):
            try:
                dt  = parse_gva_date(row["Incident Date"])
                lon = float(row["x"]) if row.get("x", "").strip() else None
                lat = float(row["y"]) if row.get("y", "").strip() else None
                k   = int(row["Victims Killed"])
                i   = int(row["Victims Injured"])
                incidents.append({
                    "year": dt.year, "month": dt.month,
                    "lon": lon, "lat": lat, "k": k, "i": i,
                })
            except Exception:
                pass
    print(f"  {len(incidents):,} incidents loaded.")

    # 3. Load spatial layers
    nbhd_path = os.path.join(data_dir, "Neighborhoods.geojson")
    parks_path = os.path.join(data_dir, "Parks.geojson")
    hs_shp    = os.path.join(data_dir, "High schools", "High_Schools")
    sa_shp    = os.path.join(data_dir, "school_assign_poly", "school_assign_poly")

    neighborhoods, school_areas, school_pts, parks = [], [], [], []

    try:
        if os.path.exists(nbhd_path):
            neighborhoods, _LAYER_GEOJSON["neighborhoods"] = load_neighborhoods(nbhd_path)
            print(f"  {len(neighborhoods)} neighborhoods.")
    except Exception as e:
        print(f"  WARNING: neighborhoods: {e}")

    try:
        if os.path.exists(parks_path):
            parks, _LAYER_GEOJSON["parks"] = load_park_centroids(parks_path)
            print(f"  {len(parks)} parks.")
    except Exception as e:
        print(f"  WARNING: parks: {e}")

    try:
        if os.path.exists(hs_shp + ".shp"):
            school_pts, _LAYER_GEOJSON["schools"] = load_school_points(hs_shp)
            print(f"  {len(school_pts)} schools.")
    except Exception as e:
        print(f"  WARNING: schools: {e}")

    try:
        if os.path.exists(sa_shp + ".shp"):
            school_areas, _LAYER_GEOJSON["school_areas"] = load_school_areas(sa_shp)
            print(f"  {len(school_areas)} school areas.")
    except Exception as e:
        print(f"  WARNING: school areas: {e}")

    # 4. Spatial assignment + distances
    print("  Running spatial assignments…")
    for inc in incidents:
        lon, lat = inc["lon"], inc["lat"]
        if lon is None or lat is None:
            inc["nbhd"] = "No coordinates"
            inc["school_area"] = "No coordinates"
            inc["park_dists"] = {}
            inc["sch_dists"]  = {}
            continue
        inc["nbhd"]        = assign_point(lon, lat, neighborhoods) or "Unassigned"
        inc["school_area"] = assign_point(lon, lat, school_areas)  or "Unassigned"
        inc["park_dists"]  = {n: haversine_m(lon, lat, pl, pa) for n, pl, pa in parks}
        inc["sch_dists"]   = {n: haversine_m(lon, lat, sl, sa) for n, sl, sa in school_pts}
    print("  Done.")

    # 5. Cache incidents for the map API
    _INCIDENTS_FOR_MAP = [
        {"lat": inc["lat"], "lon": inc["lon"],
         "k": inc["k"], "i": inc["i"],
         "year": inc["year"]}
        for inc in incidents if inc["lat"] is not None
    ]

    # 6. Build summary tables
    hdr = "victims_killed,victims_injured,victim_count,injurious_incidents,all_incidents"

    ann = defaultdict(_new_bucket)
    mon = defaultdict(_new_bucket)
    for inc in incidents:
        _add(ann[inc["year"]], inc["k"], inc["i"])
        _add(mon[(inc["year"], inc["month"])], inc["k"], inc["i"])

    ann_lines = [f"year,{hdr}"]
    for yr in sorted(ann): ann_lines.append(_row(yr, ann[yr]))

    mon_lines = [f"year,month,{hdr}"]
    for (yr, mo) in sorted(mon):
        if mon[(yr, mo)]["all"]: mon_lines.append(_row(f"{yr},{mo}", mon[(yr, mo)]))

    nbhd_ann = defaultdict(lambda: defaultdict(_new_bucket))
    for inc in incidents: _add(nbhd_ann[inc["nbhd"]][inc["year"]], inc["k"], inc["i"])
    nbhd_lines = [f'neighborhood,year,{hdr}']
    for nbhd in sorted(nbhd_ann):
        for yr in sorted(nbhd_ann[nbhd]):
            s = nbhd_ann[nbhd][yr]
            if s["all"]: nbhd_lines.append(_row(f'"{nbhd}",{yr}', s))

    sa_ann = defaultdict(lambda: defaultdict(_new_bucket))
    for inc in incidents: _add(sa_ann[inc["school_area"]][inc["year"]], inc["k"], inc["i"])
    sa_lines = [f'school_area,year,{hdr}']
    for area in sorted(sa_ann):
        for yr in sorted(sa_ann[area]):
            s = sa_ann[area][yr]
            if s["all"]: sa_lines.append(_row(f'"{area}",{yr}', s))

    park_ann = {n: {t: defaultdict(_new_bucket) for t in DIST_THRESHOLDS} for n, _, _ in parks}
    for inc in incidents:
        for pn, dm in inc.get("park_dists", {}).items():
            for t in DIST_THRESHOLDS:
                if dm <= t: _add(park_ann[pn][t][inc["year"]], inc["k"], inc["i"])
    park_lines = [f'park,within_meters,year,{hdr}']
    for pn in sorted(park_ann):
        for t in DIST_THRESHOLDS:
            for yr in sorted(park_ann[pn][t]):
                s = park_ann[pn][t][yr]
                if s["all"]: park_lines.append(_row(f'"{pn}",{t},{yr}', s))

    sch_ann = {n: {t: defaultdict(_new_bucket) for t in DIST_THRESHOLDS} for n, _, _ in school_pts}
    for inc in incidents:
        for sn, dm in inc.get("sch_dists", {}).items():
            for t in DIST_THRESHOLDS:
                if dm <= t: _add(sch_ann[sn][t][inc["year"]], inc["k"], inc["i"])
    sch_lines = [f'school,within_meters,year,{hdr}']
    for sn in sorted(sch_ann):
        for t in DIST_THRESHOLDS:
            for yr in sorted(sch_ann[sn][t]):
                s = sch_ann[sn][t][yr]
                if s["all"]: sch_lines.append(_row(f'"{sn}",{t},{yr}', s))

    # 7. Assemble
    parts += [
        "\n\n---\n\n## Pre-computed data: annual totals for Toledo\n\n"
        "Note: 2026 is a partial year (through early June) — exclude from trend analyses.\n\n"
        "```\n" + "\n".join(ann_lines) + "\n```",

        "\n\n---\n\n## Pre-computed data: monthly totals for Toledo\n\n"
        "Zero-count months are omitted.\n\n"
        "```\n" + "\n".join(mon_lines) + "\n```",

        "\n\n---\n\n## Pre-computed data: by neighborhood and year\n\n"
        "Assigned via point-in-polygon. Incidents outside all polygons → 'Unassigned'; "
        "missing coordinates → 'No coordinates'.\n\n"
        "```\n" + "\n".join(nbhd_lines) + "\n```",

        "\n\n---\n\n## Pre-computed data: by school assignment area and year\n\n"
        "Zones: Bowsher, Rogers, Scott, Start, Waite, Woodward.\n\n"
        "```\n" + "\n".join(sa_lines) + "\n```",

        "\n\n---\n\n## Pre-computed data: incidents within distance of each park (by year)\n\n"
        "Thresholds: 250 m, 500 m, 1000 m (cumulative). Only non-zero rows shown.\n\n"
        "```\n" + "\n".join(park_lines) + "\n```",

        "\n\n---\n\n## Pre-computed data: incidents within distance of each high school (by year)\n\n"
        "Thresholds: 250 m, 500 m, 1000 m (cumulative). Only non-zero rows shown.\n\n"
        "```\n" + "\n".join(sch_lines) + "\n```",
    ]

    return "\n".join(parts)


# ── Build system prompt at startup ─────────────────────────────────────────────

print("Building system prompt (spatial analysis may take a few seconds)…")
SYSTEM_PROMPT = build_system_context(DATA_DIR, INSTRUCTIONS_DIR)
print(f"System prompt ready: {len(SYSTEM_PROMPT):,} chars (~{len(SYSTEM_PROMPT)//4:,} tokens)")

# ── Anthropic client ───────────────────────────────────────────────────────────

_api_key = os.environ.get("ANTHROPIC_API_KEY")
if not _api_key:
    raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set.")
claude_client = anthropic.Anthropic(api_key=_api_key)
MODEL      = "claude-sonnet-4-6"
MAX_TOKENS = 2048

# ── Google Sheets logging ──────────────────────────────────────────────────────

SPREADSHEET_ID      = os.environ.get("GOOGLE_SPREADSHEET_ID",
                                     "1g5EBEUyAL9H3Zfo3ShX6-HVr2eQ83HYnU9BZllXb39c")
SHEET_NAME          = os.environ.get("GOOGLE_SHEET_NAME", "Chat Logs")
FEEDBACK_SHEET_NAME = os.environ.get("GOOGLE_FEEDBACK_SHEET_NAME", "Feedback")
FALLBACK_LOG        = "chat_log.csv"
FALLBACK_FEEDBACK   = "feedback_log.csv"
SHEET_HEADERS       = ["timestamp", "session_id", "name", "org", "email",
                       "turn", "user_message", "assistant_response"]
FEEDBACK_HEADERS    = ["timestamp", "session_id", "name", "org", "email",
                       "got_info_needed_1to5", "accurate_reliable_1to5",
                       "sus1", "sus2", "sus3", "sus4", "sus5",
                       "sus6", "sus7", "sus8", "sus9", "sus10",
                       "features_requested"]

_sheets_service    = None
_sheets_init_error = None


def get_sheets_service():
    global _sheets_service, _sheets_init_error
    if _sheets_service:
        return _sheets_service
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not creds_json:
        _sheets_init_error = "GOOGLE_SERVICE_ACCOUNT_JSON is not set."
        return None
    if not SPREADSHEET_ID:
        _sheets_init_error = "GOOGLE_SPREADSHEET_ID is not set."
        return None
    try:
        import json as _json
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_service_account_info(
            _json.loads(creds_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        _sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        _sheets_init_error = None
        return _sheets_service
    except Exception as e:
        _sheets_init_error = str(e)
        print(f"Google Sheets init error: {traceback.format_exc()}")
        return None


def _append_row(sheet_name, row, fallback_file, headers):
    service = get_sheets_service()
    if service:
        try:
            service.spreadsheets().values().append(
                spreadsheetId=SPREADSHEET_ID,
                range=f"'{sheet_name}'!A:H",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ).execute()
            return True
        except Exception:
            print(f"Sheets append error: {traceback.format_exc()}")
    write_header = not os.path.exists(fallback_file)
    with open(fallback_file, "a", newline="", encoding="utf-8") as f:
        w = csv_module.writer(f)
        if write_header: w.writerow(headers)
        w.writerow(row)
    return False


def log_exchange(session_id, name, org, email, turn, user_msg, assistant_msg):
    _append_row(SHEET_NAME, [
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        session_id, name, org, email, turn, user_msg, assistant_msg,
    ], FALLBACK_LOG, SHEET_HEADERS)


def log_feedback(session_id, name, org, email, got_info, accurate,
                 sus1, sus2, sus3, sus4, sus5,
                 sus6, sus7, sus8, sus9, sus10, features):
    _append_row(FEEDBACK_SHEET_NAME, [
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        session_id, name, org, email,
        got_info, accurate,
        sus1, sus2, sus3, sus4, sus5, sus6, sus7, sus8, sus9, sus10,
        features,
    ], FALLBACK_FEEDBACK, FEEDBACK_HEADERS)


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return HTML_PAGE, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/incidents")
def api_incidents():
    """Return geocoded incidents as compact JSON. Optional ?year=2023&year=2024."""
    year_filter = request.args.getlist("year", type=int)
    data = _INCIDENTS_FOR_MAP
    if year_filter:
        data = [d for d in data if d["year"] in year_filter]
    return jsonify(data)


@app.route("/api/layers/<layer_type>")
def api_layer(layer_type):
    """Return GeoJSON for a named layer: neighborhoods, parks, schools, school_areas."""
    if layer_type not in _LAYER_GEOJSON:
        return jsonify({"error": f"Unknown layer '{layer_type}'. "
                        "Valid: neighborhoods, parks, schools, school_areas"}), 404
    return jsonify(_LAYER_GEOJSON[layer_type])


@app.route("/chat", methods=["POST"])
def chat():
    body       = request.get_json(force=True)
    messages   = body.get("messages", [])
    user_info  = body.get("userInfo", {})
    session_id = body.get("sessionId", str(uuid.uuid4()))
    turn       = body.get("turn", 1)
    last_user  = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")

    try:
        resp = claude_client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT, messages=messages,
        )
        reply = resp.content[0].text
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    log_exchange(session_id,
                 user_info.get("name", ""), user_info.get("org", ""),
                 user_info.get("email", ""), turn, last_user, reply)
    return jsonify({"response": reply})


@app.route("/feedback", methods=["POST"])
def feedback():
    body      = request.get_json(force=True)
    user_info = body.get("userInfo", {})
    log_feedback(
        body.get("sessionId", ""),
        user_info.get("name", ""), user_info.get("org", ""), user_info.get("email", ""),
        body.get("got_info", ""), body.get("accurate", ""),
        body.get("sus1",""), body.get("sus2",""), body.get("sus3",""),
        body.get("sus4",""), body.get("sus5",""), body.get("sus6",""),
        body.get("sus7",""), body.get("sus8",""), body.get("sus9",""),
        body.get("sus10",""), body.get("features","").strip(),
    )
    return jsonify({"ok": True})


@app.route("/debug/sheets")
def debug_sheets():
    import json as _json
    report = {
        "env_GOOGLE_SERVICE_ACCOUNT_JSON": "SET" if os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") else "MISSING",
        "env_GOOGLE_SPREADSHEET_ID": SPREADSHEET_ID or "MISSING",
        "env_GOOGLE_SHEET_NAME": SHEET_NAME,
        "env_GOOGLE_FEEDBACK_SHEET_NAME": FEEDBACK_SHEET_NAME,
    }
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if creds_json:
        try:
            parsed = _json.loads(creds_json)
            report["service_account_email"] = parsed.get("client_email", "not found")
            report["json_parse"] = "OK"
        except Exception as e:
            report["json_parse"] = f"FAILED: {e}"
            return jsonify(report)
    service = get_sheets_service()
    if _sheets_init_error:
        report["service_init"] = f"FAILED: {_sheets_init_error}"
        return jsonify(report)
    report["service_init"] = "OK"
    try:
        meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
        tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
        report["spreadsheet_title"]   = meta.get("properties", {}).get("title", "")
        report["tabs_found"]          = tabs
        report["chat_tab_exists"]     = SHEET_NAME in tabs
        report["feedback_tab_exists"] = FEEDBACK_SHEET_NAME in tabs
    except Exception as e:
        report["spreadsheet_fetch"] = f"FAILED: {e}"
        return jsonify(report)
    try:
        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID, range=f"'{SHEET_NAME}'!A:H",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [[
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "DEBUG-TEST","Debug","Debug","debug@test.com",
                0,"[debug test — safe to delete]","[debug test]",
            ]]},
        ).execute()
        report["test_write"] = "OK — delete the test row from the sheet"
    except Exception as e:
        report["test_write"] = f"FAILED: {e}"
    return jsonify(report)


# ── HTML ───────────────────────────────────────────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Toledo GVA Query Tool (Testing Phase)</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet-src.min.js"></script>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #f1f5f9;
  color: #1e293b;
  height: 100dvh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ── Registration overlay ── */
#reg-overlay {
  position: fixed; inset: 0;
  background: rgba(15, 23, 42, 0.72);
  display: flex; align-items: center; justify-content: center;
  z-index: 200;
}
#reg-card {
  background: #fff; border-radius: 12px;
  padding: 2rem 2.25rem; max-width: 440px; width: 92%;
  box-shadow: 0 24px 64px rgba(0,0,0,.25);
}
#reg-card h2 { font-size: 1.1875rem; font-weight: 700; color: #1e3a5f; margin-bottom: .375rem; }
#reg-card .sub { font-size: .875rem; color: #64748b; margin-bottom: 1.5rem; line-height: 1.5; }
.field { margin-bottom: 1rem; }
.field label { display: block; font-size: .8125rem; font-weight: 600; color: #374151; margin-bottom: .3rem; }
.field input {
  width: 100%; padding: .5rem .75rem;
  border: 1px solid #d1d5db; border-radius: 6px;
  font-size: .9375rem; outline: none; font-family: inherit;
  transition: border-color .15s, box-shadow .15s;
}
.field input:focus { border-color: #1e3a5f; box-shadow: 0 0 0 3px rgba(30,58,95,.12); }
#reg-btn {
  width: 100%; padding: .625rem; background: #1e3a5f; color: #fff;
  border: none; border-radius: 6px; font-size: .9375rem; font-weight: 600;
  cursor: pointer; margin-top: .25rem; transition: background .15s;
}
#reg-btn:hover { background: #265080; }
.reg-err { font-size: .8125rem; color: #dc2626; margin-top: .5rem; display: none; }

/* ── Header ── */
header {
  background: #1e3a5f; color: #fff;
  padding: .875rem 1.25rem;
  display: flex; align-items: center; gap: .75rem;
  flex-shrink: 0;
}
header h1 { font-size: 1rem; font-weight: 700; letter-spacing: .01em; flex: 1; }
.badge {
  font-size: .625rem; font-weight: 700; background: #f59e0b; color: #1c1917;
  padding: .2em .55em; border-radius: 4px; text-transform: uppercase; letter-spacing: .05em;
}
#done-btn {
  padding: .4rem .9rem; background: transparent;
  border: 1.5px solid rgba(255,255,255,.55); border-radius: 6px;
  color: #fff; font-size: .875rem; font-weight: 600;
  cursor: pointer; white-space: nowrap; flex-shrink: 0;
  transition: background .15s, border-color .15s;
}
#done-btn:hover { background: rgba(255,255,255,.12); border-color: #fff; }

/* ── Feedback modal ── */
#fb-overlay {
  position: fixed; inset: 0; background: rgba(15,23,42,.72);
  display: none; align-items: center; justify-content: center;
  z-index: 300; padding: 1rem;
}
#fb-overlay.open { display: flex; }
#fb-card {
  background: #fff; border-radius: 12px;
  padding: 1.75rem 2rem; max-width: 780px; width: 100%;
  max-height: 90vh; overflow-y: auto;
  box-shadow: 0 24px 64px rgba(0,0,0,.25);
}
#fb-card h2 { font-size: 1.125rem; font-weight: 700; color: #1e3a5f; margin-bottom: .375rem; }
#fb-card .sub { font-size: .8125rem; color: #64748b; margin-bottom: 1.25rem; line-height: 1.5; }
.sus-grid { width: 100%; border-collapse: collapse; margin-bottom: 1.25rem; table-layout: fixed; }
.sus-grid th, .sus-grid td { text-align: center; padding: .35rem .25rem; vertical-align: middle; }
.sus-grid th:first-child, .sus-grid td:first-child { text-align: left; width: 52%; padding-right: .75rem; }
.sus-grid th { font-size: .7rem; font-weight: 600; color: #64748b; border-bottom: 2px solid #e2e8f0; padding-bottom: .5rem; line-height: 1.3; }
.sus-grid tbody tr:nth-child(even) { background: #f8fafc; }
.sus-grid tbody tr:hover { background: #eff6ff; }
.sus-grid td { font-size: .8125rem; color: #374151; border-bottom: 1px solid #f1f5f9; }
.sus-grid input[type="radio"] { accent-color: #1e3a5f; cursor: pointer; width: 1.05rem; height: 1.05rem; }
.fb-field { margin-bottom: 1rem; }
.fb-field > label { display: block; font-size: .8125rem; font-weight: 600; color: #374151; margin-bottom: .4rem; }
#fb-features {
  width: 100%; padding: .5rem .75rem;
  border: 1px solid #d1d5db; border-radius: 6px;
  font-size: .9375rem; font-family: inherit;
  resize: vertical; min-height: 80px; outline: none;
  transition: border-color .15s, box-shadow .15s;
}
#fb-features:focus { border-color: #1e3a5f; box-shadow: 0 0 0 3px rgba(30,58,95,.1); }
#fb-submit {
  width: 100%; padding: .625rem; background: #1e3a5f; color: #fff;
  border: none; border-radius: 6px; font-size: .9375rem; font-weight: 600;
  cursor: pointer; margin-top: .25rem; transition: background .15s;
}
#fb-submit:hover:not(:disabled) { background: #265080; }
#fb-submit:disabled { background: #94a3b8; cursor: not-allowed; }
#fb-thankyou { display: none; text-align: center; padding: 1rem 0; }
#fb-thankyou p { font-size: 1rem; color: #1e3a5f; font-weight: 600; margin-bottom: .5rem; }
#fb-thankyou .sub2 { font-size: .875rem; color: #64748b; }
.fb-err { font-size: .8125rem; color: #dc2626; margin-top: .5rem; display: none; }

/* ── Chat area ── */
#chat-wrap {
  flex: 1; overflow-y: auto; padding: 1.25rem;
  display: flex; flex-direction: column; gap: 1rem;
}
.notice-card, .info-card {
  border-radius: 8px; padding: 1rem 1.125rem;
  font-size: .875rem; line-height: 1.6;
}
.notice-card { background: #fffbeb; border: 1px solid #fcd34d; }
.notice-card .card-label { color: #92400e; }
.info-card { background: #eff6ff; border: 1px solid #bfdbfe; }
.info-card .card-label { color: #1e40af; }
.card-label {
  display: block; font-size: .75rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .06em; margin-bottom: .375rem;
}

/* Messages */
.msg { display: flex; flex-direction: column; max-width: min(78%, 680px); }
.msg.user { align-self: flex-end;  align-items: flex-end; }
.msg.asst { align-self: flex-start; align-items: flex-start; }
.msg-label {
  font-size: .6875rem; font-weight: 600; color: #94a3b8;
  text-transform: uppercase; letter-spacing: .05em; margin-bottom: .2rem;
}
.bubble { padding: .75rem 1rem; border-radius: 10px; font-size: .9375rem; line-height: 1.65; }
.msg.user .bubble { background: #1e3a5f; color: #fff; border-bottom-right-radius: 3px; }
.msg.asst .bubble { background: #fff; border: 1px solid #e2e8f0; border-bottom-left-radius: 3px; }
.msg.asst .bubble p { margin-bottom: .5em; }
.msg.asst .bubble p:last-child { margin-bottom: 0; }
.msg.asst .bubble h1,
.msg.asst .bubble h2,
.msg.asst .bubble h3 { margin: .75em 0 .3em; font-size: 1em; }
.msg.asst .bubble ul,
.msg.asst .bubble ol { margin: .35em 0 .35em 1.4em; }
.msg.asst .bubble li { margin-bottom: .2em; }
.msg.asst .bubble table { border-collapse: collapse; font-size: .875em; margin: .5em 0; width: 100%; }
.msg.asst .bubble th,
.msg.asst .bubble td { border: 1px solid #e2e8f0; padding: .3em .65em; text-align: left; }
.msg.asst .bubble th { background: #f8fafc; font-weight: 600; }
.msg.asst .bubble pre {
  background: #f8fafc; padding: .75em; border-radius: 6px;
  overflow-x: auto; font-size: .85em; margin: .5em 0;
}
.msg.asst .bubble code { font-size: .875em; background: #f1f5f9; padding: .1em .3em; border-radius: 3px; }
.msg.asst .bubble pre code { background: none; padding: 0; }
.thinking .bubble { color: #94a3b8; font-style: italic; }

/* Examples card */
.examples-card {
  background: #f8fafc; border: 1px solid #e2e8f0;
  border-radius: 8px; padding: 1rem 1.125rem; font-size: .875rem;
}
.examples-grid { display: grid; grid-template-columns: 1fr 1fr; gap: .5rem; margin-top: .25rem; }
.example-btn {
  background: #fff; border: 1px solid #cbd5e1; border-radius: 6px;
  padding: .5rem .75rem; font-size: .8125rem; color: #1e3a5f;
  cursor: pointer; text-align: left; line-height: 1.45;
  transition: background .15s, border-color .15s; font-family: inherit;
}
.example-btn:hover { background: #eff6ff; border-color: #93c5fd; }
@media (max-width: 560px) { .examples-grid { grid-template-columns: 1fr; } }

/* ── Chart wrapper ── */
.chart-wrap {
  background: #fff; border: 1px solid #e2e8f0;
  border-radius: 10px; padding: 1.25rem 1.25rem .75rem;
  margin-top: .25rem; max-width: min(78%, 680px); align-self: flex-start;
}
.chart-wrap canvas { display: block; max-height: 320px; }

/* ── Map wrapper ── */
.map-wrap {
  background: #fff; border: 1px solid #e2e8f0;
  border-radius: 10px; overflow: hidden;
  margin-top: .25rem; max-width: min(92%, 720px); align-self: flex-start;
  width: 100%;
}
.map-title {
  font-size: .8125rem; font-weight: 600; color: #374151;
  padding: .625rem 1rem .375rem;
}
.map-container { height: 420px; width: 100%; }
.map-legend {
  padding: .5rem 1rem .625rem;
  font-size: .75rem; color: #64748b;
  display: flex; flex-wrap: wrap; gap: .5rem .875rem; align-items: center;
  border-top: 1px solid #f1f5f9;
}
.legend-dot {
  display: inline-block; width: 10px; height: 10px;
  border-radius: 50%; margin-right: .2rem; vertical-align: middle;
}

/* ── Input bar ── */
#input-bar {
  background: #fff; border-top: 1px solid #e2e8f0;
  padding: .875rem 1.25rem;
  display: flex; gap: .625rem; align-items: flex-end; flex-shrink: 0;
}
#user-input {
  flex: 1; padding: .625rem .875rem;
  border: 1px solid #d1d5db; border-radius: 8px;
  font-size: .9375rem; font-family: inherit;
  resize: none; line-height: 1.5;
  min-height: 44px; max-height: 140px; outline: none;
  transition: border-color .15s, box-shadow .15s;
}
#user-input:focus { border-color: #1e3a5f; box-shadow: 0 0 0 3px rgba(30,58,95,.1); }
#send-btn {
  padding: .625rem 1.125rem; background: #1e3a5f; color: #fff;
  border: none; border-radius: 8px; font-size: .9375rem; font-weight: 600;
  cursor: pointer; flex-shrink: 0; transition: background .15s;
}
#send-btn:hover:not(:disabled) { background: #265080; }
#send-btn:disabled { background: #94a3b8; cursor: not-allowed; }
</style>
</head>
<body>

<!-- Registration overlay -->
<div id="reg-overlay">
  <div id="reg-card">
    <h2>Toledo GVA Query Tool</h2>
    <p class="sub">Please enter your information to begin. Your session will be logged for evaluation purposes.</p>
    <div class="field"><label for="r-name">Full name</label>
      <input id="r-name" type="text" placeholder="Jane Smith" autocomplete="name"></div>
    <div class="field"><label for="r-org">Organization</label>
      <input id="r-org" type="text" placeholder="Your organization" autocomplete="organization"></div>
    <div class="field"><label for="r-email">Email address</label>
      <input id="r-email" type="email" placeholder="you@example.com" autocomplete="email"></div>
    <button id="reg-btn" onclick="register()">Enter Tool</button>
    <div class="reg-err" id="reg-err"></div>
  </div>
</div>

<!-- Feedback modal -->
<div id="fb-overlay">
  <div id="fb-card">
    <div id="fb-form-wrap">
      <h2>Session feedback</h2>
      <p class="sub">Rate your agreement with each statement (1 = Strongly disagree, 5 = Strongly agree). All 12 are required; the last is optional.</p>
      <table class="sus-grid">
        <thead>
          <tr>
            <th></th><th>1<br>Strongly<br>disagree</th><th>2<br>Disagree</th>
            <th>3<br>Neutral</th><th>4<br>Agree</th><th>5<br>Strongly<br>agree</th>
          </tr>
        </thead>
        <tbody>
          <tr><td>1. I was able to get the information I was looking for.</td><td><input type="radio" name="got_info" value="1"></td><td><input type="radio" name="got_info" value="2"></td><td><input type="radio" name="got_info" value="3"></td><td><input type="radio" name="got_info" value="4"></td><td><input type="radio" name="got_info" value="5"></td></tr>
          <tr><td>2. The information seemed accurate and reliable.</td><td><input type="radio" name="accurate" value="1"></td><td><input type="radio" name="accurate" value="2"></td><td><input type="radio" name="accurate" value="3"></td><td><input type="radio" name="accurate" value="4"></td><td><input type="radio" name="accurate" value="5"></td></tr>
          <tr><td>3. I think that I would like to use this tool frequently.</td><td><input type="radio" name="sus1" value="1"></td><td><input type="radio" name="sus1" value="2"></td><td><input type="radio" name="sus1" value="3"></td><td><input type="radio" name="sus1" value="4"></td><td><input type="radio" name="sus1" value="5"></td></tr>
          <tr><td>4. I found the tool unnecessarily complex.</td><td><input type="radio" name="sus2" value="1"></td><td><input type="radio" name="sus2" value="2"></td><td><input type="radio" name="sus2" value="3"></td><td><input type="radio" name="sus2" value="4"></td><td><input type="radio" name="sus2" value="5"></td></tr>
          <tr><td>5. I thought the tool was easy to use.</td><td><input type="radio" name="sus3" value="1"></td><td><input type="radio" name="sus3" value="2"></td><td><input type="radio" name="sus3" value="3"></td><td><input type="radio" name="sus3" value="4"></td><td><input type="radio" name="sus3" value="5"></td></tr>
          <tr><td>6. I think that I would need the support of a technical person to be able to use this tool.</td><td><input type="radio" name="sus4" value="1"></td><td><input type="radio" name="sus4" value="2"></td><td><input type="radio" name="sus4" value="3"></td><td><input type="radio" name="sus4" value="4"></td><td><input type="radio" name="sus4" value="5"></td></tr>
          <tr><td>7. I found the various functions in this tool were well integrated.</td><td><input type="radio" name="sus5" value="1"></td><td><input type="radio" name="sus5" value="2"></td><td><input type="radio" name="sus5" value="3"></td><td><input type="radio" name="sus5" value="4"></td><td><input type="radio" name="sus5" value="5"></td></tr>
          <tr><td>8. I thought there was too much inconsistency in this tool.</td><td><input type="radio" name="sus6" value="1"></td><td><input type="radio" name="sus6" value="2"></td><td><input type="radio" name="sus6" value="3"></td><td><input type="radio" name="sus6" value="4"></td><td><input type="radio" name="sus6" value="5"></td></tr>
          <tr><td>9. I would imagine that most people would learn to use this tool very quickly.</td><td><input type="radio" name="sus7" value="1"></td><td><input type="radio" name="sus7" value="2"></td><td><input type="radio" name="sus7" value="3"></td><td><input type="radio" name="sus7" value="4"></td><td><input type="radio" name="sus7" value="5"></td></tr>
          <tr><td>10. I found the tool very cumbersome to use.</td><td><input type="radio" name="sus8" value="1"></td><td><input type="radio" name="sus8" value="2"></td><td><input type="radio" name="sus8" value="3"></td><td><input type="radio" name="sus8" value="4"></td><td><input type="radio" name="sus8" value="5"></td></tr>
          <tr><td>11. I felt very confident using this tool.</td><td><input type="radio" name="sus9" value="1"></td><td><input type="radio" name="sus9" value="2"></td><td><input type="radio" name="sus9" value="3"></td><td><input type="radio" name="sus9" value="4"></td><td><input type="radio" name="sus9" value="5"></td></tr>
          <tr><td>12. I needed to learn a lot of things before I could get going with this tool.</td><td><input type="radio" name="sus10" value="1"></td><td><input type="radio" name="sus10" value="2"></td><td><input type="radio" name="sus10" value="3"></td><td><input type="radio" name="sus10" value="4"></td><td><input type="radio" name="sus10" value="5"></td></tr>
        </tbody>
      </table>
      <div class="fb-field">
        <label for="fb-features">What features or data would you like to see added? <span style="font-weight:400;color:#94a3b8">(optional)</span></label>
        <textarea id="fb-features" placeholder="e.g. other distance thresholds, different spatial layers, filtering by time period…"></textarea>
      </div>
      <button id="fb-submit" onclick="submitFeedback()">Submit feedback</button>
      <div class="fb-err" id="fb-err"></div>
    </div>
    <div id="fb-thankyou">
      <p>Thank you for your feedback!</p>
      <p class="sub2">Your responses have been recorded. You can close this window.</p>
    </div>
  </div>
</div>

<!-- Header -->
<header>
  <h1>Toledo GVA Query Tool <span class="badge">Testing Phase</span></h1>
  <button id="done-btn" onclick="openFeedback()">I'm done</button>
</header>

<!-- Chat -->
<div id="chat-wrap">
  <div class="notice-card">
    <span class="card-label">Notice</span>
    Thank you for agreeing to test this tool, which is powered by Claude AI. Responses are being evaluated. AI-generated responses may be mistaken or misleading.
  </div>
  <div class="info-card">
    <span class="card-label">About this tool</span>
    This tool is an interface for exploring <a href="https://www.gunviolencearchive.org/" target="_blank" rel="noopener" style="color:#1e3a5f;">Gun Violence Archive (GVA)</a> data from Toledo, Ohio, 2016–present. The GVA data have been geocoded to support spatial analyses, including breakdowns by neighborhood, school assignment area, and proximity to parks and high schools. It knows the number of victims (fatal and nonfatal) per incident, but does not have information about demographics or circumstances. Type your question below to get started. When you're finished, please click "I'm done" to provide some feedback.
  </div>
  <div class="examples-card">
    <span class="card-label">Example questions</span>
    <div class="examples-grid">
      <button class="example-btn" onclick="useExample(this)">Show me a map of all shootings in Toledo in 2023.</button>
      <button class="example-btn" onclick="useExample(this)">Which school assignment area had the most gun violence in 2022?</button>
      <button class="example-btn" onclick="useExample(this)">How many people were shot within 500 meters of a park in 2024?</button>
      <button class="example-btn" onclick="useExample(this)">Show a map of shootings in 2024 with neighborhood boundaries.</button>
    </div>
  </div>
</div>

<!-- Input -->
<div id="input-bar">
  <textarea id="user-input"
    placeholder="Ask a question about gun violence in Toledo…"
    rows="1"
    onkeydown="handleKey(event)"
    oninput="autoResize(this)"></textarea>
  <button id="send-btn" onclick="sendMessage()">Send</button>
</div>

<script>
  // ── State ──────────────────────────────────────────────────────────────────
  let userInfo  = {};
  let sessionId = (typeof crypto.randomUUID === "function")
                    ? crypto.randomUUID()
                    : Math.random().toString(36).slice(2) + Date.now().toString(36);
  let turn = 0, messages = [], busy = false;

  // ── Registration ───────────────────────────────────────────────────────────
  function register() {
    const name  = document.getElementById("r-name").value.trim();
    const org   = document.getElementById("r-org").value.trim();
    const email = document.getElementById("r-email").value.trim();
    const err   = document.getElementById("reg-err");
    err.style.display = "none";
    if (!name || !org || !email) {
      err.textContent = "Please fill in all three fields.";
      err.style.display = "block"; return;
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      err.textContent = "Please enter a valid email address.";
      err.style.display = "block"; return;
    }
    userInfo = { name, org, email };
    document.getElementById("reg-overlay").style.display = "none";
    document.getElementById("user-input").focus();
  }
  document.addEventListener("keydown", e => {
    if (e.key === "Enter" && document.getElementById("reg-overlay").style.display !== "none") register();
  });

  function handleKey(e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  }
  function autoResize(el) {
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 140) + "px";
  }

  // ── Chart rendering ────────────────────────────────────────────────────────
  const CHART_COLORS = ["#1e3a5f","#3b82f6","#10b981","#f59e0b","#ef4444","#8b5cf6","#06b6d4","#84cc16"];

  function renderChart(container, spec) {
    try {
      const canvas = document.createElement("canvas");
      container.appendChild(canvas);
      const labels = spec.series[0].values.map(v => v.label);
      const isLine = spec.type === "line";
      const datasets = spec.series.map((s, idx) => ({
        label: s.name,
        data: s.values.map(v => v.value),
        backgroundColor: isLine ? CHART_COLORS[idx % CHART_COLORS.length] + "33" : CHART_COLORS[idx % CHART_COLORS.length],
        borderColor: CHART_COLORS[idx % CHART_COLORS.length],
        borderWidth: isLine ? 2 : 1,
        fill: isLine && spec.series.length === 1,
        tension: 0.3, pointRadius: 4,
      }));
      new Chart(canvas, {
        type: isLine ? "line" : "bar",
        data: { labels, datasets },
        options: {
          responsive: true,
          plugins: {
            title: { display: !!spec.title, text: spec.title || "", font: { size: 14, weight: "600" }, color: "#1e293b", padding: { bottom: 12 } },
            legend: { display: datasets.length > 1 },
          },
          scales: {
            x: { title: { display: !!spec.x_axis, text: spec.x_axis || "", color: "#64748b" }, grid: { display: false } },
            y: { title: { display: !!spec.y_axis, text: spec.y_axis || "", color: "#64748b" }, beginAtZero: true, grid: { color: "#f1f5f9" } },
          },
        },
      });
    } catch (e) {
      container.innerHTML = "<em style='color:#94a3b8'>Chart could not be rendered.</em>";
    }
  }

  // ── Map rendering ──────────────────────────────────────────────────────────
  async function renderMap(container, spec) {
    // Outer wrapper
    const wrap = document.createElement("div");
    wrap.className = "map-wrap";

    if (spec.title) {
      const titleEl = document.createElement("div");
      titleEl.className = "map-title";
      titleEl.textContent = spec.title;
      wrap.appendChild(titleEl);
    }

    const mapDiv = document.createElement("div");
    mapDiv.className = "map-container";
    // Unique ID required by Leaflet
    mapDiv.id = "map_" + Date.now() + "_" + Math.random().toString(36).slice(2);
    wrap.appendChild(mapDiv);
    container.appendChild(wrap);

    // Initialise Leaflet after the div is in the DOM
    await new Promise(r => requestAnimationFrame(r));

    const lmap = L.map(mapDiv, { preferCanvas: true })
                  .setView([41.6641, -83.5552], 12);

    L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
      subdomains: "abcd", maxZoom: 19,
    }).addTo(lmap);

    const legendItems = [];

    // ── Optional polygon layers (drawn first, under incidents) ────────────
    if (spec.show_neighborhoods) {
      try {
        const gj = await fetch("/api/layers/neighborhoods").then(r => r.json());
        L.geoJSON(gj, {
          style: feat => ({
            color: "#3b82f6", weight: 1.5, opacity: 0.7,
            fillColor: "#3b82f6", fillOpacity: 0.06,
          }),
          onEachFeature: (feat, layer) => layer.bindTooltip(feat.properties.name, { sticky: true }),
        }).addTo(lmap);
        legendItems.push({ color: "#3b82f6", label: "Neighborhoods", shape: "square" });
      } catch(e) { console.error("neighborhoods layer:", e); }
    }

    if (spec.show_school_areas) {
      try {
        const gj = await fetch("/api/layers/school_areas").then(r => r.json());
        L.geoJSON(gj, {
          style: feat => ({
            color: "#8b5cf6", weight: 1.5, opacity: 0.7,
            fillColor: "#8b5cf6", fillOpacity: 0.08,
          }),
          onEachFeature: (feat, layer) => layer.bindTooltip(feat.properties.school + " attendance zone", { sticky: true }),
        }).addTo(lmap);
        legendItems.push({ color: "#8b5cf6", label: "School zones", shape: "square" });
      } catch(e) { console.error("school_areas layer:", e); }
    }

    // ── Park markers ──────────────────────────────────────────────────────
    if (spec.show_parks) {
      try {
        const gj = await fetch("/api/layers/parks").then(r => r.json());
        for (const feat of gj.features) {
          const [lon, lat] = feat.geometry.coordinates;
          L.circleMarker([lat, lon], {
            radius: 5, fillColor: "#10b981", color: "#065f46",
            weight: 1, opacity: 1, fillOpacity: 0.8,
          }).bindTooltip(feat.properties.name).addTo(lmap);
        }
        legendItems.push({ color: "#10b981", label: "Parks", shape: "circle" });
      } catch(e) { console.error("parks layer:", e); }
    }

    // ── School markers ────────────────────────────────────────────────────
    if (spec.show_schools) {
      try {
        const gj = await fetch("/api/layers/schools").then(r => r.json());
        for (const feat of gj.features) {
          const [lon, lat] = feat.geometry.coordinates;
          // School = blue diamond marker (rotated square)
          const icon = L.divIcon({
            html: `<div style="width:12px;height:12px;background:#1d4ed8;border:2px solid #1e3a5f;border-radius:2px;transform:rotate(45deg);"></div>`,
            className: "",
            iconSize: [12, 12],
            iconAnchor: [6, 6],
          });
          L.marker([lat, lon], { icon })
           .bindTooltip(feat.properties.name + " High School")
           .addTo(lmap);
        }
        legendItems.push({ color: "#1d4ed8", label: "High schools", shape: "diamond" });
      } catch(e) { console.error("schools layer:", e); }
    }

    // ── Incident dots ─────────────────────────────────────────────────────
    const yearParams = (spec.years || []).map(y => `year=${y}`).join("&");
    try {
      const incidents = await fetch("/api/incidents" + (yearParams ? "?" + yearParams : "")).then(r => r.json());
      const hasFatal = incidents.some(d => d.k > 0);

      for (const inc of incidents) {
        const isFatal  = inc.k > 0;
        const victims  = inc.k + inc.i;
        const radius   = Math.max(4, Math.min(14, 4 + victims * 2));
        const fillColor = isFatal ? "#dc2626" : "#f97316";
        const color     = isFatal ? "#991b1b" : "#9a3412";

        L.circleMarker([inc.lat, inc.lon], {
          radius, fillColor, color, weight: 0.8, opacity: 0.9, fillOpacity: 0.65,
        })
        .bindPopup(
          `<b>${victims} victim${victims !== 1 ? "s" : ""}</b> (${inc.k} killed, ${inc.i} injured)<br>Year: ${inc.year}`
        )
        .addTo(lmap);
      }

      if (incidents.length > 0) {
        legendItems.push({ color: "#dc2626", label: "Fatal incident", shape: "circle" });
        legendItems.push({ color: "#f97316", label: "Non-fatal incident", shape: "circle" });
      }
    } catch(e) {
      console.error("incidents:", e);
    }

    // ── Legend ─────────────────────────────────────────────────────────────
    if (legendItems.length > 0) {
      const legendEl = document.createElement("div");
      legendEl.className = "map-legend";
      for (const item of legendItems) {
        const span = document.createElement("span");
        let dotHtml;
        if (item.shape === "diamond") {
          dotHtml = `<span style="display:inline-block;width:10px;height:10px;background:${item.color};transform:rotate(45deg);border-radius:1px;margin-right:.3rem;vertical-align:middle;"></span>`;
        } else if (item.shape === "square") {
          dotHtml = `<span style="display:inline-block;width:12px;height:8px;background:${item.color};opacity:0.35;border:1.5px solid ${item.color};margin-right:.3rem;vertical-align:middle;"></span>`;
        } else {
          dotHtml = `<span class="legend-dot" style="background:${item.color};"></span>`;
        }
        span.innerHTML = dotHtml + item.label;
        legendEl.appendChild(span);
      }
      wrap.appendChild(legendEl);
    }

    // Trigger Leaflet resize after layout settles
    setTimeout(() => lmap.invalidateSize(), 100);
  }

  // ── appendMessage ──────────────────────────────────────────────────────────
  function appendMessage(role, content, isMarkdown) {
    const wrap = document.getElementById("chat-wrap");

    if (role === "assistant" && isMarkdown) {
      // Split on ```chart and ```geomap code fences
      const tagRegex = /```(chart|geomap)\n([\s\S]*?)```/g;
      const parts = [];
      let last = 0, match;
      while ((match = tagRegex.exec(content)) !== null) {
        if (match.index > last) parts.push({ kind: "text", content: content.slice(last, match.index) });
        parts.push({ kind: match[1], content: match[2].trim() });
        last = match.index + match[0].length;
      }
      if (last < content.length) parts.push({ kind: "text", content: content.slice(last) });

      let firstEl = null;
      const labelDiv = document.createElement("div");
      labelDiv.className = "msg-label";
      labelDiv.style.cssText = "align-self:flex-start";
      labelDiv.textContent = "Assistant";
      wrap.appendChild(labelDiv);

      for (const part of parts) {
        if (part.kind === "text" && part.content.trim()) {
          const div = document.createElement("div");
          div.className = "msg asst";
          const bubble = document.createElement("div");
          bubble.className = "bubble";
          bubble.innerHTML = marked.parse(part.content.trim());
          div.appendChild(bubble);
          wrap.appendChild(div);
          if (!firstEl) firstEl = div;
        } else if (part.kind === "chart") {
          try {
            const spec = JSON.parse(part.content);
            const chartWrap = document.createElement("div");
            chartWrap.className = "chart-wrap";
            renderChart(chartWrap, spec);
            wrap.appendChild(chartWrap);
            if (!firstEl) firstEl = chartWrap;
          } catch (e) { console.error("Chart JSON parse error:", e); }
        } else if (part.kind === "geomap") {
          try {
            const spec = JSON.parse(part.content);
            const placeholder = document.createElement("div");
            wrap.appendChild(placeholder);
            renderMap(placeholder, spec).catch(e => console.error("Map render error:", e));
            if (!firstEl) firstEl = placeholder;
          } catch (e) { console.error("Map JSON parse error:", e); }
        }
      }
      wrap.scrollTop = wrap.scrollHeight;
      return firstEl || labelDiv;
    }

    const div = document.createElement("div");
    div.className = "msg " + (role === "user" ? "user" : "asst");
    const label = document.createElement("div");
    label.className = "msg-label";
    label.textContent = role === "user" ? (userInfo.name || "You") : "Assistant";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    if (isMarkdown) bubble.innerHTML = marked.parse(content);
    else bubble.textContent = content;
    div.appendChild(label);
    div.appendChild(bubble);
    wrap.appendChild(div);
    wrap.scrollTop = wrap.scrollHeight;
    return div;
  }

  function useExample(btn) {
    const input = document.getElementById("user-input");
    input.value = btn.textContent.trim();
    input.focus();
    autoResize(input);
  }

  // ── Feedback ───────────────────────────────────────────────────────────────
  function openFeedback() { document.getElementById("fb-overlay").classList.add("open"); }

  async function submitFeedback() {
    const allFields = ["got_info","accurate","sus1","sus2","sus3","sus4","sus5","sus6","sus7","sus8","sus9","sus10"];
    const fieldValues = {};
    for (const name of allFields) fieldValues[name] = document.querySelector(`input[name="${name}"]:checked`);
    const err = document.getElementById("fb-err");
    err.style.display = "none";
    if (allFields.some(n => !fieldValues[n])) {
      err.textContent = "Please respond to all 12 statements before submitting.";
      err.style.display = "block"; return;
    }
    const btn = document.getElementById("fb-submit");
    btn.disabled = true; btn.textContent = "Submitting…";
    const payload = { userInfo, sessionId, features: document.getElementById("fb-features").value };
    for (const name of allFields) payload[name] = fieldValues[name].value;
    try { await fetch("/feedback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); } catch(_) {}
    document.getElementById("fb-form-wrap").style.display = "none";
    document.getElementById("fb-thankyou").style.display  = "block";
  }

  // ── Send message ───────────────────────────────────────────────────────────
  async function sendMessage() {
    if (busy) return;
    const input = document.getElementById("user-input");
    const text  = input.value.trim();
    if (!text) return;
    busy = true;
    document.getElementById("send-btn").disabled = true;
    input.value = ""; input.style.height = "auto";
    appendMessage("user", text, false);
    messages.push({ role: "user", content: text });
    turn++;
    const thinkEl = appendMessage("asst", "Thinking…", false);
    thinkEl.classList.add("thinking");
    try {
      const res  = await fetch("/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ messages, userInfo, sessionId, turn }) });
      const data = await res.json();
      thinkEl.remove();
      if (data.error) appendMessage("asst", "Error: " + data.error, false);
      else {
        appendMessage("asst", data.response, true);
        messages.push({ role: "assistant", content: data.response });
      }
    } catch (err) {
      thinkEl.remove();
      appendMessage("asst", "A network error occurred. Please try again.", false);
    }
    busy = false;
    document.getElementById("send-btn").disabled = false;
    input.focus();
  }
</script>
</body>
</html>"""


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
