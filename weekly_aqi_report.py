#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Weekly Air Quality Index Report of Punjab - generator
Directorate of Environmental Monitoring Center (EMC), EPA Punjab, Lahore

WHAT IT DOES
    Reads station-level hourly AQMS exports or wide district-average dashboard
    CSVs placed in this folder, the 41-district
    boundary shapefile and the AQMS location shapefile, computes district
    hourly / time-period / daily / weekly AQI, and writes:
        output/Weekly_AQI_Report_<dates>.pdf      (same layout as the EMC report)
        output/Weekly_AQI_Stats_<dates>.xlsx      (all computed numbers, for checking)

HOW TO RUN (VS Code)
    1. pip install -r requirements.txt
    2. Put the raw hourly file(s) (.xlsx / .xls / .csv) in this folder
       (or in a sub-folder called  raw_data).
    3. Put the two shapefiles anywhere inside this folder (e.g. shapefiles/).
       The script finds them by itself: the POLYGON layer = districts,
       the POINT layer = AQMS. Or set DISTRICT_SHP / AQMS_SHP below.
    4. Press Run (F5) or:  python weekly_aqi_report.py
       Optional:          python weekly_aqi_report.py --start 2026-09-19

Everything you may need to adjust is in the CONFIG block below.
"""

import argparse
import difflib
import math
import re
import sys
import warnings
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.path import Path as MPath
from matplotlib.markers import MarkerStyle
import matplotlib.transforms as mtransforms
import matplotlib.patheffects as pe

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER, TA_LEFT
from reportlab.platypus import Paragraph
from reportlab.pdfbase.pdfmetrics import stringWidth

warnings.filterwarnings("ignore")

# =============================================================================
# CONFIG  - change only this block
# =============================================================================
BASE_DIR = Path(__file__).resolve().parent

RAW_DATA_DIR = None        # None -> BASE_DIR/raw_data if it exists, else BASE_DIR
DISTRICT_SHP = None        # None -> auto-detect the polygon .shp inside BASE_DIR
AQMS_SHP = None            # None -> auto-detect the point .shp inside BASE_DIR
ASSETS_DIR = BASE_DIR / "assets"
OUTPUT_DIR = BASE_DIR / "output"

WEEK_START = None          # "2026-09-19" or None -> the last 7 days found in the data
DAYFIRST = True            # dates in raw file written as dd/mm/yyyy
TIMESTAMP_IS_HOUR_END = False  # True if the dashboard stamps 01:00 for the 00:00-01:00 hour

# Data-completeness rules (same as the Method note in the report)
MIN_HOURS_PERIOD = 4       # of 8 hours for a Morning / Evening / Night value
MIN_HOURS_DAY = 12         # of 24 hours for a daily value
MIN_DAYS_FOR_RANKING = 4   # districts with fewer valid days are shown with * and not ranked
EXCLUDE_HOURS_WITHOUT_PM = True   # drop hours whose AQI has no valid PM10 / PM2.5 reading
AQI_CEILING = 500

# Column names in the raw file. Leave None to auto-detect; set if detection fails,
# e.g. COLUMN_MAP = {"station": "Station Name", "datetime": "Date Time", "aqi": "AQI",
#                    "pm25": "PM2.5", "pm10": "PM10", "district": "District"}
COLUMN_MAP = {}

# Attribute (field) names in the shapefiles. None -> auto-detect.
DISTRICT_NAME_FIELD = "District_N"
AQMS_NAME_FIELD = None       # e.g. "Station"
AQMS_DISTRICT_FIELD = None   # e.g. "District" (only used if STATION_DISTRICT_SOURCE = "aqms_attribute")

# How each station is assigned to a district:
#   "auto"           : AQMS shapefile location (point-in-polygon) -> else raw 'District' column
#   "spatial"        : only AQMS shapefile location (point-in-polygon)
#   "aqms_attribute" : the district field in the AQMS shapefile
#   "raw"            : the District column of the raw data
STATION_DISTRICT_SOURCE = "auto"

# Manual fixes, applied first:  {"raw station name": "District name as in the shapefile"}
STATION_DISTRICT_OVERRIDES = {}
# Manual fixes for station names that do not match the AQMS shapefile:
#   {"raw station name": "station name in AQMS shapefile"}
STATION_NAME_OVERRIDES = {}

# Name shown in the report for a shapefile district name, e.g. {"Dera Ghazi Khan": "DG Khan"}
DISPLAY_NAMES = {"Dera Ghazi Khan": "DG Khan", "D.G. Khan": "DG Khan", "D.G.Khan": "DG Khan",
                 "Toba Tek Singh": "Toba Tek Singh", "R.Y. Khan": "Rahim Yar Khan"}

# Map options
AQMS_MAP_MODE = "stations"   # "stations": one pin at every AQMS location
                             # "district": one pin per district (as in the old report)
LABEL_ALL_DISTRICTS_ON_COVER = False   # False: label only districts with data (as in report)
LABEL_OFFSETS = {}           # nudge labels in points, e.g. {"Lahore": (6, -4), "Kasur": (0, -6)}
MAP_DPI = 300

ORG_LINE = "Directorate of Environmental Monitoring Center (EMC), EPA Punjab, Lahore"
DEPARTMENT = "EP&CCD"
# =============================================================================


# ----------------------------------------------------------------------------- AQI scale
CATEGORIES = [  # lo, hi, name, fill, text colour, legend label
    (0, 50, "Good", "#00B050", "#000000", "0-50"),
    (51, 100, "Satisfactory", "#92D050", "#000000", "51-100"),
    (101, 150, "Moderate", "#FFFF00", "#000000", "101-150"),
    (151, 200, "Unhealthy for Sensitive Groups", "#ED7D31", "#000000", "151-200"),
    (201, 300, "Unhealthy", "#FF0000", "#000000", "201-300"),
    (301, 400, "Very Unhealthy", "#7030A0", "#FFFFFF", "301-400"),
    (401, 10 ** 9, "Hazardous", "#C00000", "#FFFFFF", "400+"),
]
NO_DATA_FILL = "#C9C9C9"      # districts with no data on the cover map
AQMS_MAP_FILL = "#CFCFCF"

PERIODS = [  # key, label, hours, time text
    ("morning", "Morning", range(8, 16), "08 AM – 03 PM"),
    ("evening", "Evening", range(16, 24), "04 PM – 11 PM"),
    ("night", "Night", range(0, 8), "12 AM – 07 AM"),
]


def rnd(x):
    """Round half up (152.5 -> 153)."""
    return int(math.floor(float(x) + 0.5))


def cat_of(v):
    v = rnd(v)
    for c in CATEGORIES:
        if v <= c[1]:
            return c
    return CATEGORIES[-1]


# ----------------------------------------------------------------------------- helpers
def nkey(s):
    """normalised key for matching names."""
    s = str(s).lower().replace("&", "and")
    return re.sub(r"[^a-z0-9]", "", s)


DISTRICT_SYNONYMS = {
    "dgkhan": "deraghazikhan", "dgk": "deraghazikhan", "deraghazi": "deraghazikhan",
    "ryk": "rahimyarkhan", "rykhan": "rahimyarkhan", "rahimyar": "rahimyarkhan",
    "ttsingh": "tobateksingh", "tts": "tobateksingh", "tobatek": "tobateksingh",
    "mbdin": "mandibahauddin", "mandibahudin": "mandibahauddin", "mandibahaudin": "mandibahauddin",
    "mandibahauddinn": "mandibahauddin", "mbahauddin": "mandibahauddin",
    "sheikupura": "sheikhupura", "shekhupura": "sheikhupura", "sheikhupur": "sheikhupura",
    "nankana": "nankanasahib", "pakpatan": "pakpattan", "muzafargarh": "muzaffargarh",
    "muzaffergarh": "muzaffargarh", "kotadu": "kotaddu", "bahawalnager": "bahawalnagar",
    "rawalpindicity": "rawalpindi", "lahorecity": "lahore", "taunsasharif": "taunsa",
    "taunsasharief": "taunsa", "hafizabadcity": "hafizabad",
}


def dkey(s):
    k = nkey(s)
    for suffix in ("district", "distt", "dist"):
        if k.endswith(suffix) and len(k) > len(suffix) + 2:
            k = k[: -len(suffix)]
    return DISTRICT_SYNONYMS.get(k, k)


def pretty_name(name):
    name = str(name).strip()
    if name in DISPLAY_NAMES:
        return DISPLAY_NAMES[name]
    if name.isupper() or name.islower():
        name = name.title()
    return DISPLAY_NAMES.get(name, name)


def log(msg=""):
    print(msg, flush=True)


# ----------------------------------------------------------------------------- shapefiles
def find_shapefiles():
    dshp, ashp = DISTRICT_SHP, AQMS_SHP
    if dshp and ashp:
        return Path(dshp), Path(ashp)
    found = [p for p in BASE_DIR.rglob("*.shp") if OUTPUT_DIR not in p.parents]
    polys, points = [], []
    for p in found:
        try:
            g = gpd.read_file(p, rows=5)
            gt = set(g.geom_type.dropna().unique())
        except Exception:
            continue
        if gt & {"Polygon", "MultiPolygon"}:
            polys.append(p)
        elif gt & {"Point", "MultiPoint"}:
            points.append(p)
    if not dshp:
        if not polys:
            sys.exit("ERROR: no district (polygon) shapefile found. Set DISTRICT_SHP in CONFIG.")
        # prefer the one whose name mentions district
        polys.sort(key=lambda p: (0 if "dist" in p.name.lower() else 1, p.name))
        dshp = polys[0]
    if not ashp:
        if not points:
            sys.exit("ERROR: no AQMS (point) shapefile found. Set AQMS_SHP in CONFIG.")
        points.sort(key=lambda p: (0 if re.search(r"aqms|station|monitor", p.name.lower()) else 1, p.name))
        ashp = points[0]
    return Path(dshp), Path(ashp)


def pick_field(gdf, preferred, candidates, what):
    if preferred:
        if preferred not in gdf.columns:
            sys.exit(f"ERROR: field '{preferred}' not in {what} shapefile. Fields: {list(gdf.columns)}")
        return preferred
    cols = [c for c in gdf.columns if c != "geometry"]
    for cand in candidates:
        for c in cols:
            if nkey(c) == cand:
                return c
    for cand in candidates:
        for c in cols:
            if cand in nkey(c) and gdf[c].dtype == object:
                return c
    txt = [c for c in cols if gdf[c].dtype == object]
    if txt:
        return txt[0]
    sys.exit(f"ERROR: could not find a name field in the {what} shapefile. Set it in CONFIG.")


def load_districts(path):
    g = gpd.read_file(path)
    g = g[g.geometry.notna() & ~g.geometry.is_empty].copy()
    f = pick_field(g, DISTRICT_NAME_FIELD,
                   ["district", "districtname", "distname", "dist", "adm2en", "adm2name", "name2", "name"],
                   "district")
    g["dname"] = g[f].astype(str).map(pretty_name)
    g = g.dissolve(by="dname", as_index=False)[["dname", "geometry"]]
    g["dkey"] = g["dname"].map(dkey)
    log(f"  District layer : {path.name}  field '{f}'  -> {len(g)} districts")
    return g


def load_aqms(path, districts):
    a = gpd.read_file(path)
    a = a[a.geometry.notna() & ~a.geometry.is_empty].copy()
    if a.crs is None and districts.crs is not None:
        a = a.set_crs(districts.crs)
    elif a.crs is not None and districts.crs is not None and a.crs != districts.crs:
        a = a.to_crs(districts.crs)
    if (a.geom_type == "MultiPoint").any():
        a["geometry"] = a.geometry.centroid
    nf = pick_field(a, AQMS_NAME_FIELD,
                    ["stationname", "station", "sitename", "site", "aqms", "aqmsname", "location", "name"],
                    "AQMS")
    a["sname"] = a[nf].astype(str).str.strip()
    a["skey"] = a["sname"].map(nkey)
    j = gpd.sjoin(a[["sname", "skey", "geometry"]], districts[["dname", "geometry"]],
                  how="left", predicate="within")
    j = j[~j.index.duplicated(keep="first")]
    a["sp_district"] = j["dname"]
    miss = a["sp_district"].isna()
    if miss.any():  # points just outside a boundary -> nearest district
        near = gpd.sjoin_nearest(a.loc[miss, ["geometry"]], districts[["dname", "geometry"]], how="left")
        near = near[~near.index.duplicated(keep="first")]
        a.loc[miss, "sp_district"] = near["dname"]
    if AQMS_DISTRICT_FIELD and AQMS_DISTRICT_FIELD in a.columns:
        a["attr_district"] = a[AQMS_DISTRICT_FIELD].astype(str).map(lambda s: match_district(s, districts))
    else:
        a["attr_district"] = None
    log(f"  AQMS layer     : {path.name}  field '{nf}'  -> {len(a)} stations in "
        f"{a['sp_district'].nunique()} districts")
    return a


def match_district(name, districts):
    if name is None or (isinstance(name, float) and np.isnan(name)):
        return None
    k = dkey(name)
    lut = dict(zip(districts["dkey"], districts["dname"]))
    if k in lut:
        return lut[k]
    close = difflib.get_close_matches(k, list(lut), n=1, cutoff=0.8)
    return lut[close[0]] if close else None


# ----------------------------------------------------------------------------- raw data
ALIASES = {
    "station": ["stationname", "station", "sitename", "site", "aqms", "aqmsname", "aqmsstation",
                "monitoringstation", "location", "locationname", "stationid", "name"],
    "district": ["district", "districtname", "distname", "dist"],
    "datetime": ["datetime", "dateandtime", "datetimepkt", "timestamp", "recordedat", "readingtime",
                 "dateandtimepkt", "localtime", "datetimelocal", "fromdate", "startdate", "date_time"],
    "date": ["date", "day", "readingdate"],
    "hour": ["hour", "hr", "time", "hours", "hourofday"],
    "aqi": ["aqi", "overallaqi", "aqivalue", "aqiindex", "usaqi", "airqualityindex", "aqiusepa"],
    "pm25": ["pm25", "pm25ugm3", "pm25gm3", "pm25gm", "pm25conc", "pm25ugm", "pm2", "pm25value"],
    "pm10": ["pm10", "pm10ugm3", "pm10gm3", "pm10gm", "pm10conc", "pm10ugm", "pm10value"],
}
POLLUTANT_PREFIX = ("pm", "no", "so", "co", "o3", "nh", "h2s")


def detect_columns(cols):
    norm = {c: nkey(c) for c in cols}
    found = {}
    for key, user_col in COLUMN_MAP.items():
        if user_col in cols:
            found[key] = user_col
    for key, alist in ALIASES.items():
        if key in found:
            continue
        for a in alist:
            hit = [c for c in cols if norm[c] == a and c not in found.values()]
            if hit:
                found[key] = hit[0]
                break
    # looser rules
    if "aqi" not in found:
        hit = [c for c in cols if "aqi" in norm[c] and not norm[c].startswith(POLLUTANT_PREFIX)]
        if hit:
            found["aqi"] = hit[0]
    for key, pref in (("pm25", "pm25"), ("pm10", "pm10")):
        if key not in found:
            hit = [c for c in cols if norm[c].startswith(pref) and "aqi" not in norm[c]]
            if hit:
                found[key] = hit[0]
    if "datetime" not in found:
        hit = [c for c in cols if ("date" in norm[c] and "time" in norm[c])]
        if hit:
            found["datetime"] = hit[0]
    return found


def read_table(path):
    """Return list of (sheet/label, DataFrame) with the header row auto-detected."""
    out = []
    if path.suffix.lower() == ".csv":
        raw = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                raw = {path.stem: pd.read_csv(path, header=None, dtype=str, encoding=enc,
                                              sep=None, engine="python")}
                break
            except Exception:
                continue
        if raw is None:
            log(f"  ! could not read {path.name}")
            return out
    else:
        try:
            raw = pd.read_excel(path, header=None, sheet_name=None)
        except Exception as e:
            log(f"  ! could not read {path.name}: {e}")
            return out
    for sheet, df in raw.items():
        if df is None or df.empty:
            continue
        hdr = None
        for i in range(min(30, len(df))):
            vals = [nkey(v) for v in df.iloc[i].tolist() if pd.notna(v)]
            if len(vals) >= 2 and any(v in ALIASES["aqi"] or "aqi" == v[:3] for v in vals) and \
                    any(("date" in v or "time" in v or v in ALIASES["hour"]) for v in vals):
                hdr = i
                break
        if hdr is None:
            continue
        body = df.iloc[hdr + 1:].copy()
        cols, seen = [], {}
        for c in df.iloc[hdr].tolist():
            c = str(c).strip() if pd.notna(c) else "unnamed"
            seen[c] = seen.get(c, 0) + 1
            cols.append(c if seen[c] == 1 else f"{c}_{seen[c]}")
        body.columns = cols
        body = body.dropna(how="all")
        out.append((sheet, body))
    return out


def parse_datetime(series):
    s = series.copy()
    if pd.api.types.is_datetime64_any_dtype(s):
        return pd.to_datetime(s)
    num = pd.to_numeric(s, errors="coerce")
    if num.notna().mean() > 0.9 and num.dropna().between(20000, 80000).all():
        return pd.to_datetime("1899-12-30") + pd.to_timedelta(num, unit="D")
    s = s.astype(str).str.strip()
    is24 = s.str.contains(r"(?:^|\s)24:00")
    s = s.str.replace(r"(?:(?<=\s)|^)24:00(:00)?", "00:00", regex=True)
    dt = pd.to_datetime(s, dayfirst=DAYFIRST, errors="coerce", format="mixed")
    iso_dates = s.str.match(r"^\d{4}-\d{2}-\d{2}(?:$|[ T])")
    if iso_dates.any():
        dt.loc[iso_dates] = pd.to_datetime(
            s.loc[iso_dates], dayfirst=False, errors="coerce", format="mixed"
        )
    dt = dt.where(~is24, dt + pd.Timedelta(days=1))
    return dt


def parse_hour(series):
    def one(v):
        if pd.isna(v):
            return np.nan
        if hasattr(v, "hour"):
            return v.hour
        if isinstance(v, (int, float, np.integer, np.floating)):
            f = float(v)
            return f * 24 if 0 < f < 1 else f
        m = re.match(r"\s*(\d{1,2})", str(v))
        if not m:
            return np.nan
        h = int(m.group(1))
        if re.search(r"pm", str(v), re.I) and h < 12:
            h += 12
        if re.search(r"am", str(v), re.I) and h == 12:
            h = 0
        return h
    return series.map(one)


def read_district_aggregate(path):
    """Read dashboard CSVs with one hourly row and district-qualified columns."""
    if path.suffix.lower() != ".csv":
        return None
    df = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            df = pd.read_csv(path, dtype=str, encoding=enc)
            break
        except Exception:
            continue
    if df is None or df.empty:
        return None

    requested_time = COLUMN_MAP.get("datetime")
    time_col = requested_time if requested_time in df.columns else next(
        (c for c in df.columns if nkey(c) in
         ("time", "datetime", "timestamp", "dateandtime", "recordedat")), None)
    if time_col is None:
        return None

    fields = {}
    for col in df.columns:
        parts = re.split(r"\s*[•·]\s*", str(col), maxsplit=1)
        if len(parts) != 2:
            continue
        district, pollutant = (part.strip() for part in parts)
        key = nkey(pollutant)
        if key == "aqi":
            field = "aqi"
        elif key.startswith("pm25"):
            field = "pm25"
        elif key.startswith("pm10"):
            field = "pm10"
        else:
            continue
        fields.setdefault(district, {})[field] = col

    fields = {district: cols for district, cols in fields.items() if "aqi" in cols}
    if not fields:
        return None

    dt = parse_datetime(df[time_col])
    if TIMESTAMP_IS_HOUR_END:
        dt = dt - pd.Timedelta(hours=1)
    frames = []
    for district, cols in fields.items():
        part = pd.DataFrame({
            "station": district,
            "district_raw": district,
            "dt": dt.values,
            "aqi": pd.to_numeric(df[cols["aqi"]], errors="coerce").values,
            "pm25": pd.to_numeric(df[cols["pm25"]], errors="coerce").values
                     if "pm25" in cols else np.nan,
            "pm10": pd.to_numeric(df[cols["pm10"]], errors="coerce").values
                    if "pm10" in cols else np.nan,
            "has_pm_cols": "pm25" in cols or "pm10" in cols,
        })
        frames.append(part)
    return pd.concat(frames, ignore_index=True), len(fields)


def load_raw(input_files=None):
    def data_files(folder):
        return [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".csv", ".xlsx", ".xls", ".xlsm")
                and not p.name.startswith(("~$", "Weekly_AQI_Stats"))] if folder.is_dir() else []

    if input_files is not None:
        files = [Path(path) for path in input_files]
        rdir = files[0].parent if files else BASE_DIR
    elif RAW_DATA_DIR:
        rdir = Path(RAW_DATA_DIR)
        files = data_files(rdir)
    else:  # raw_data sub-folder first, then the script folder itself
        rdir = BASE_DIR / "raw_data"
        files = data_files(rdir)
        if not files:
            rdir, files = BASE_DIR, data_files(BASE_DIR)
    if not files:
        sys.exit(f"ERROR: no raw .xlsx/.xls/.csv file found in {rdir}")
    frames = []
    source_mode = None
    for f in sorted(files):
        tables = read_table(f)
        if not tables:
            aggregate = read_district_aggregate(f)
            if aggregate is not None:
                if source_mode == "station":
                    sys.exit("ERROR: station-level and district-aggregate files cannot be combined.")
                source_mode = "district_aggregate"
                part, district_count = aggregate
                frames.append(part)
                log(f"  Raw data       : {f.name}  {len(part):,} rows  "
                    f"{district_count} district aggregates ({part['dt'].notna().sum():,} dated rows)")
                continue
            log(f"  ! {f.name}: no table with an AQI and a date/time column - skipped")
            continue
        for sheet, df in tables:
            cm = detect_columns(list(df.columns))
            if "aqi" not in cm:
                log(f"  ! {f.name}[{sheet}]: no AQI column found - skipped. Columns: {list(df.columns)}")
                continue
            if "datetime" in cm:
                dt = parse_datetime(df[cm["datetime"]])
                if "hour" in cm and dt.dt.hour.fillna(0).eq(0).all():
                    dt = dt.dt.normalize() + pd.to_timedelta(parse_hour(df[cm["hour"]]), unit="h")
            elif "date" in cm and "hour" in cm:
                dt = parse_datetime(df[cm["date"]]).dt.normalize() + \
                     pd.to_timedelta(parse_hour(df[cm["hour"]]), unit="h")
            elif "date" in cm:
                dt = parse_datetime(df[cm["date"]])
            else:
                log(f"  ! {f.name}[{sheet}]: no date/time column found - skipped")
                continue
            if TIMESTAMP_IS_HOUR_END:
                dt = dt - pd.Timedelta(hours=1)
            if "station" in cm:
                st = df[cm["station"]].astype(str).str.strip()
            else:
                st = pd.Series(sheet if len(tables) > 1 else f.stem, index=df.index)
            part = pd.DataFrame({
                "station": st.values,
                "district_raw": df[cm["district"]].astype(str).str.strip().values if "district" in cm else None,
                "dt": dt.values,
                "aqi": pd.to_numeric(df[cm["aqi"]], errors="coerce").values,
                "pm25": pd.to_numeric(df[cm["pm25"]], errors="coerce").values if "pm25" in cm else np.nan,
                "pm10": pd.to_numeric(df[cm["pm10"]], errors="coerce").values if "pm10" in cm else np.nan,
            })
            part["has_pm_cols"] = ("pm25" in cm) or ("pm10" in cm)
            if source_mode == "district_aggregate":
                sys.exit("ERROR: station-level and district-aggregate files cannot be combined.")
            source_mode = "station"
            frames.append(part)
            log(f"  Raw data       : {f.name}[{sheet}]  {len(part):,} rows  columns -> "
                + ", ".join(f"{k}='{v}'" for k, v in cm.items()))
    if not frames:
        sys.exit("ERROR: no supported raw data found. Use station-level rows or a district-average "
                 "CSV with a Time column and district • AQI columns.")
    raw = pd.concat(frames, ignore_index=True)
    raw = raw[raw["dt"].notna() & raw["station"].notna() & (raw["station"] != "nan")]
    return raw, source_mode


# ----------------------------------------------------------------------------- station -> district
def assign_districts(raw, districts, aqms, source_mode="station"):
    if source_mode == "district_aggregate":
        raw = raw.copy()
        mapping = {}
        for name in raw["district_raw"].dropna().unique():
            mapping[name] = match_district(name, districts)
        raw["district"] = raw["district_raw"].map(mapping)
        match_df = pd.DataFrame([
            {"Raw district aggregate": name, "District used": dist,
             "Assigned by": "dashboard district name" if dist else "NOT ASSIGNED"}
            for name, dist in mapping.items()
        ])
        bad = [name for name, dist in mapping.items() if dist is None]
        if bad:
            log(f"  ! {len(bad)} district aggregate(s) did not match the district layer: "
                + ", ".join(map(str, bad)))
        return raw[raw["district"].notna()], match_df

    stations = raw.groupby("station")["district_raw"].agg(
        lambda s: s.dropna().mode().iloc[0] if s.dropna().size else None).reset_index()
    akeys = dict(zip(aqms["skey"], aqms.index))
    rows = []
    for _, r in stations.iterrows():
        st, draw = r["station"], r["district_raw"]
        method, score, aqms_name, dist = "", None, None, None
        # 1. manual override
        if st in STATION_DISTRICT_OVERRIDES:
            dist = match_district(STATION_DISTRICT_OVERRIDES[st], districts) or STATION_DISTRICT_OVERRIDES[st]
            method = "manual override"
        # 2. match to AQMS shapefile
        idx = None
        target = STATION_NAME_OVERRIDES.get(st, st)
        k = nkey(target)
        if k in akeys:
            idx, score = akeys[k], 1.0
        else:
            cands = difflib.get_close_matches(k, list(akeys), n=1, cutoff=0.72)
            if cands:
                idx = akeys[cands[0]]
                score = round(difflib.SequenceMatcher(None, k, cands[0]).ratio(), 2)
            else:  # containment e.g. "Lahore Town Hall AQMS" vs "Town Hall"
                cont = [kk for kk in akeys if len(kk) > 4 and (kk in k or k in kk)]
                if len(cont) == 1:
                    idx, score = akeys[cont[0]], 0.7
        if idx is not None:
            aqms_name = aqms.at[idx, "sname"]
        if dist is None:
            src = STATION_DISTRICT_SOURCE
            if src in ("auto", "spatial") and idx is not None:
                dist, method = aqms.at[idx, "sp_district"], "AQMS location (point in polygon)"
            elif src == "aqms_attribute" and idx is not None and aqms.at[idx, "attr_district"]:
                dist, method = aqms.at[idx, "attr_district"], "AQMS shapefile district field"
            if dist is None and src in ("auto", "raw") and draw:
                dist = match_district(draw, districts)
                method = "raw District column" if dist else ""
            if dist is None and src == "auto":  # district name written inside the station name
                sk = nkey(st)
                hits = [(len(k), n) for k, n in zip(districts["dkey"], districts["dname"]) if len(k) > 3 and k in sk]
                if hits:
                    dist, method = max(hits)[1], "district name found in station name"
        rows.append({"Raw station name": st, "Raw district": draw, "Matched AQMS (shapefile)": aqms_name,
                     "Name match score": score, "District used": dist, "Assigned by": method or "NOT ASSIGNED"})
    m = pd.DataFrame(rows)
    bad = m[m["District used"].isna()]
    if len(bad):
        log(f"  ! {len(bad)} station(s) could not be placed in a district and are excluded: "
            + ", ".join(bad["Raw station name"].astype(str)))
        log("    -> fix with STATION_NAME_OVERRIDES or STATION_DISTRICT_OVERRIDES in CONFIG")
    raw = raw.merge(m[["Raw station name", "District used"]], left_on="station",
                    right_on="Raw station name", how="left")
    raw = raw.rename(columns={"District used": "district"}).drop(columns=["Raw station name"])
    return raw[raw["district"].notna()], m


# ----------------------------------------------------------------------------- statistics
def compute(raw, week_start):
    days = [week_start + timedelta(days=i) for i in range(7)]
    t0, t1 = pd.Timestamp(days[0]), pd.Timestamp(days[-1]) + pd.Timedelta(days=1)
    d = raw[(raw["dt"] >= t0) & (raw["dt"] < t1)].copy()
    n_in = len(d)
    d = d[d["aqi"].notna() & (d["aqi"] >= 0)]
    d["aqi"] = d["aqi"].clip(upper=AQI_CEILING)
    n_nopm = 0
    if EXCLUDE_HOURS_WITHOUT_PM:
        pm_ok = (d["pm25"].fillna(-1) > 0) | (d["pm10"].fillna(-1) > 0)
        applies = d["has_pm_cols"].astype(bool)
        drop = applies & ~pm_ok
        n_nopm = int(drop.sum())
        d = d[~drop]
    d["hour_ts"] = d["dt"].dt.floor("h")
    st_hr = d.groupby(["district", "station", "hour_ts"], as_index=False)["aqi"].mean()
    dh = st_hr.groupby(["district", "hour_ts"], as_index=False).agg(aqi=("aqi", "mean"),
                                                                     n_st=("station", "nunique"))
    dh["date"] = dh["hour_ts"].dt.normalize()
    dh["hour"] = dh["hour_ts"].dt.hour
    log(f"  Week rows: {n_in:,}  |  excluded without valid PM: {n_nopm:,}  |  "
        f"district-hours: {len(dh):,}")

    res = {}
    for dist, g in dh.groupby("district"):
        daily, periods = {}, {}
        for day in days:
            dd = g[g["date"] == pd.Timestamp(day)]
            daily[day] = dd["aqi"].mean() if len(dd) >= MIN_HOURS_DAY else None
            for pk, _, hours, _ in PERIODS:
                pp = dd[dd["hour"].isin(list(hours))]
                periods[(day, pk)] = pp["aqi"].mean() if len(pp) >= MIN_HOURS_PERIOD else None
        valid = [v for v in daily.values() if v is not None]
        weekly = float(np.mean(valid)) if valid else None
        pk_row = g.loc[g["aqi"].idxmax()] if len(g) else None
        res[dist] = {
            "district": dist, "daily": daily, "periods": periods, "weekly": weekly,
            "valid_days": len(valid),
            "stations": int(st_hr[st_hr["district"] == dist]["station"].nunique()),
            "peak": (pk_row["aqi"], pk_row["hour_ts"]) if pk_row is not None else None,
        }
    return res, days, dh, st_hr


def rank_districts(res):
    have = [r for r in res.values() if r["weekly"] is not None]
    ranked = sorted([r for r in have if r["valid_days"] >= MIN_DAYS_FOR_RANKING],
                    key=lambda r: -r["weekly"])
    unranked = sorted([r for r in have if r["valid_days"] < MIN_DAYS_FOR_RANKING],
                      key=lambda r: -r["weekly"])
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    for r in unranked:
        r["rank"] = None
    return ranked, unranked


# ----------------------------------------------------------------------------- date text
def ordinal(n):
    return "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def range_texts(days):
    a, b = days[0], days[-1]
    if a.month == b.month and a.year == b.year:
        rich = f"{a.day}<super>{ordinal(a.day)}</super> to {b.day}<super>{ordinal(b.day)}</super> {b:%B %Y}"
        plain = f"{a.day}–{b.day} {b:%B %Y}"
    elif a.year == b.year:
        rich = (f"{a.day}<super>{ordinal(a.day)}</super> {a:%B} to "
                f"{b.day}<super>{ordinal(b.day)}</super> {b:%B %Y}")
        plain = f"{a.day} {a:%B} – {b.day} {b:%B %Y}"
    else:
        rich = (f"{a.day}<super>{ordinal(a.day)}</super> {a:%B %Y} to "
                f"{b.day}<super>{ordinal(b.day)}</super> {b:%B %Y}")
        plain = f"{a.day} {a:%B %Y} – {b.day} {b:%B %Y}"
    return rich, plain


# ----------------------------------------------------------------------------- maps
def _set_map_font():
    from matplotlib import font_manager
    have = {f.name for f in font_manager.fontManager.ttflist}
    for f in ("Arial", "Liberation Sans", "Helvetica", "DejaVu Sans"):
        if f in have:
            plt.rcParams["font.family"] = f
            return


_set_map_font()


def _fig_for(gdf, box_w_pt, box_h_pt):
    fig = plt.figure(figsize=(box_w_pt / 72, box_h_pt / 72), dpi=MAP_DPI)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    fig.patch.set_alpha(0)
    ax.patch.set_alpha(0)
    return fig, ax


def _label_points(districts):
    pts = districts.geometry.representative_point()
    return dict(zip(districts["dname"], zip(pts.x, pts.y)))


def render_cover_map(districts, res, out_png, box_w, box_h):
    g = districts.copy()
    g["val"] = g["dname"].map(lambda n: res[n]["weekly"] if n in res else None)
    g["fill"] = g["val"].map(lambda v: cat_of(v)[3] if v is not None and not pd.isna(v) else NO_DATA_FILL)
    fig, ax = _fig_for(g, box_w, box_h)
    g.plot(ax=ax, color=g["fill"], edgecolor="white", linewidth=0.6)
    lp = _label_points(g)
    for _, r in g.iterrows():
        has = r["val"] is not None and not pd.isna(r["val"])
        if not has and not LABEL_ALL_DISTRICTS_ON_COVER:
            continue
        x, y = lp[r["dname"]]
        dx, dy = LABEL_OFFSETS.get(r["dname"], (0, 0))
        tcol = "white" if has and cat_of(r["val"])[4] == "#FFFFFF" else "#333333"
        ax.annotate(r["dname"], (x, y), xytext=(dx, dy), textcoords="offset points",
                    ha="center", va="center", fontsize=5.2, color=tcol)
    ax.margins(0.01)
    fig.savefig(out_png, dpi=MAP_DPI, transparent=True)
    plt.close(fig)


PIN_R, PIN_CY = 1.0, 1.75      # pin head radius and head-centre height (tip at 0,0)


def _pin_marker():
    # tear-drop with the tip at (0,0)
    t = np.linspace(np.radians(-35), np.radians(215), 48)
    head = np.column_stack([PIN_R * np.cos(t), PIN_CY + PIN_R * np.sin(t)])
    verts = np.vstack([[0, 0], head[::-1], [0, 0]])
    return MarkerStyle(MPath(verts, closed=True))


def render_aqms_map(districts, aqms, out_png, box_w, box_h):
    fig, ax = _fig_for(districts, box_w, box_h)
    districts.plot(ax=ax, color=AQMS_MAP_FILL, edgecolor="white", linewidth=0.7)
    counts = aqms.groupby("sp_district").size()
    lp = _label_points(districts)
    pin = _pin_marker()
    green, badge = "#1E7B3A", "#5BA3D9"
    pin_size = 11
    k = pin_size * 0.5 / (PIN_CY + PIN_R)          # marker units -> points
    dot_d = 0.9 * PIN_R * k                        # white dot diameter (points)
    dot = mtransforms.offset_copy(ax.transData, fig=fig, x=0, y=PIN_CY * k, units="points")

    if AQMS_MAP_MODE == "stations":
        ax.scatter(aqms.geometry.x, aqms.geometry.y, marker=pin, s=pin_size ** 2, color=green,
                   linewidths=0, zorder=5)
        ax.scatter(aqms.geometry.x, aqms.geometry.y, marker="o", s=dot_d ** 2,
                   color="white", linewidths=0, zorder=6, transform=dot)
        for dname, n in counts.items():
            if dname not in lp:
                continue
            x, y = lp[dname]
            dx, dy = LABEL_OFFSETS.get(dname, (0, 0))
            ax.annotate(dname, (x, y), xytext=(dx, dy - 6), textcoords="offset points", ha="center",
                        va="center", fontsize=5.2, color="#1E6B35", fontweight="bold", zorder=7,
                        path_effects=[pe.withStroke(linewidth=1.6, foreground=AQMS_MAP_FILL)])
            half = len(dname) * 1.45
            ax.annotate(str(n), (x, y), xytext=(dx + half + 4.5, dy - 6), textcoords="offset points",
                        ha="center", va="center", fontsize=4.2, color="white", fontweight="bold",
                        zorder=8, bbox=dict(boxstyle="circle,pad=0.25", fc=badge, ec="none"))
    else:  # one pin per district
        for dname, grp in aqms.groupby("sp_district"):
            x, y = grp.geometry.x.mean(), grp.geometry.y.mean()
            if len(grp) > 3 and dname in lp:
                x, y = lp[dname]
            ax.scatter([x], [y], marker=pin, s=pin_size ** 2, color=green, linewidths=0, zorder=5)
            ax.scatter([x], [y], marker="o", s=dot_d ** 2, color="white", linewidths=0,
                       zorder=6, transform=dot)
            dx, dy = LABEL_OFFSETS.get(dname, (0, 0))
            ax.annotate(dname, (x, y), xytext=(dx, dy - 5), textcoords="offset points", ha="center",
                        va="top", fontsize=5.2, color="#1E6B35", fontweight="bold", zorder=7,
                        path_effects=[pe.withStroke(linewidth=1.6, foreground=AQMS_MAP_FILL)])
            ax.annotate(str(len(grp)), (x, y), xytext=(4.5, pin_size * 0.95), textcoords="offset points",
                        ha="center", va="center", fontsize=4.2, color="white", fontweight="bold",
                        zorder=8, bbox=dict(boxstyle="circle,pad=0.25", fc=badge, ec="none"))
    ax.margins(0.02)
    fig.savefig(out_png, dpi=MAP_DPI, transparent=True)
    plt.close(fig)


# ----------------------------------------------------------------------------- PDF drawing
PW, PH = A4
C_PANEL = HexColor("#C5E0B4")
C_BAND = HexColor("#E2EFD9")
C_HEAD = HexColor("#538135")
C_LINE = HexColor("#70AD47")
C_TITLEGREEN = HexColor("#548235")
C_DARKGREEN = HexColor("#385723")
C_GREY_TXT = HexColor("#595959")
C_PAGE_NO = HexColor("#2E75B6")


def Y(top):
    """convert a distance from the top of the page to reportlab's y."""
    return PH - top


def asset(name):
    p = ASSETS_DIR / name
    return str(p) if p.exists() else None


def draw_image_fit(c, path, x, top, w, h):
    if not path:
        return
    from reportlab.lib.utils import ImageReader
    img = ImageReader(path)
    iw, ih = img.getSize()
    s = min(w / iw, h / ih)
    dw, dh = iw * s, ih * s
    c.drawImage(img, x + (w - dw) / 2, Y(top) - h + (h - dh) / 2, dw, dh, mask="auto")


def para(c, text, x, top, width, font="Times-Roman", size=10, leading=None, align=TA_LEFT,
         color=black, **kw):
    st = ParagraphStyle("p", fontName=font, fontSize=size, leading=leading or size * 1.25,
                        alignment=align, textColor=color, **kw)
    p = Paragraph(text, st)
    _, h = p.wrap(width, 2000)
    p.drawOn(c, x, Y(top) - h)
    return h


def footer(c, page_no, plain_dates, cover=False):
    c.setStrokeColor(C_LINE)
    c.setLineWidth(0.6)
    x0 = 214 if cover else 40
    c.line(x0, Y(811), 533, Y(811))
    c.setFillColor(C_GREY_TXT)
    if cover:
        c.setFont("Helvetica", 5.6)
        c.drawString(x0, Y(821), f"{ORG_LINE.replace(', Lahore', ', Lahore|')} Weekly AQI Report | {plain_dates}")
    else:
        c.setFont("Helvetica", 6.6)
        c.drawString(x0, Y(821), ORG_LINE)
        c.drawRightString(527, Y(821), f"Weekly AQI Report | {plain_dates}")
    c.setFillColor(C_PAGE_NO)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(552, Y(822), str(page_no))


def draw_legend(c, top, x0=35, x1=560):
    n = len(CATEGORIES)
    gap = 2.0
    w = (x1 - x0 - gap * (n - 1)) / n
    for i, cat in enumerate(CATEGORIES):
        x = x0 + i * (w + gap)
        cx = x + w / 2
        c.setFillColor(black)
        c.setFont("Times-Bold", 5.8)
        c.drawCentredString(cx, Y(top), cat[5])
        c.setFont("Times-Roman", 5.4)
        if cat[2].startswith("Unhealthy for"):
            c.drawCentredString(cx, Y(top + 9), "(Unhealthy for Sensitive")
            c.drawCentredString(cx, Y(top + 16), "Groups)")
        else:
            c.drawCentredString(cx, Y(top + 9), f"({cat[2]})")
        c.setFillColor(HexColor(cat[3]))
        c.roundRect(x, Y(top + 32), w, 13, 2, stroke=0, fill=1)


def value_box(c, cx, top, w, h, v, size=15, font="Times-Bold"):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        c.setFillColor(HexColor("#F2F2F2"))
        c.setStrokeColor(HexColor("#A6A6A6"))
        c.setDash(2, 1.5)
        c.setLineWidth(0.6)
        c.rect(cx - w / 2, Y(top + h), w, h, stroke=1, fill=1)
        c.setDash()
        c.setFillColor(HexColor("#8C8C8C"))
        c.setFont("Helvetica-Oblique", 6.3)
        c.drawCentredString(cx, Y(top + h / 2 + 2.2), "No data")
        return
    cat = cat_of(v)
    c.setFillColor(HexColor(cat[3]))
    c.setStrokeColor(HexColor("#262626"))
    c.setLineWidth(0.6)
    c.rect(cx - w / 2, Y(top + h), w, h, stroke=1, fill=1)
    c.setFillColor(HexColor(cat[4]))
    c.setFont(font, size)
    c.drawCentredString(cx, Y(top + h / 2 + size * 0.34), str(rnd(v)))


# --- small icons (standard PDF fonts have no sun/moon glyphs)
def icon_sun(c, cx, cy, filled=True):
    col = HexColor("#FFC000")
    c.setStrokeColor(col)
    c.setFillColor(col)
    c.setLineWidth(0.8)
    r = 2.6
    c.circle(cx, cy, r, stroke=1, fill=1 if filled else 0)
    for k in range(8):
        a = math.radians(k * 45)
        c.line(cx + math.cos(a) * (r + 1.2), cy + math.sin(a) * (r + 1.2),
               cx + math.cos(a) * (r + 3.0), cy + math.sin(a) * (r + 3.0))


def icon_moon(c, cx, cy):
    c.setFillColor(HexColor("#404040"))
    c.circle(cx, cy, 4.2, stroke=0, fill=1)
    c.setFillColor(white)
    c.circle(cx + 2.2, cy + 0.9, 3.9, stroke=0, fill=1)


def tri(c, x, y, size, direction, color):
    c.setFillColor(color)
    p = c.beginPath()
    if direction == "left":
        p.moveTo(x, y); p.lineTo(x + size, y + size * 0.6); p.lineTo(x + size, y - size * 0.6)
    elif direction == "up":
        p.moveTo(x, y); p.lineTo(x + size * 1.2, y); p.lineTo(x + size * 0.6, y + size)
    else:  # down
        p.moveTo(x, y + size); p.lineTo(x + size * 1.2, y + size); p.lineTo(x + size * 0.6, y)
    p.close()
    c.drawPath(p, stroke=0, fill=1)


def gauge(c, cx, cy, r_out, r_in, value):
    n = len(CATEGORIES)
    seg = 180.0 / n
    for i, cat in enumerate(CATEGORIES):
        c.setFillColor(HexColor(cat[3]))
        c.setStrokeColor(white)
        c.setLineWidth(2.2)
        start = 180 - (i + 1) * seg
        c.wedge(cx - r_out, cy - r_out, cx + r_out, cy + r_out, start, seg, stroke=1, fill=1)
    c.setFillColor(white)
    c.circle(cx, cy, r_in, stroke=0, fill=1)
    # needle
    bounds = [(0, 50), (50, 100), (100, 150), (150, 200), (200, 300), (300, 400), (400, 500)]
    v = max(0, min(500, value))
    idx = next(i for i, (lo, hi) in enumerate(bounds) if v <= hi)
    lo, hi = bounds[idx]
    frac = (v - lo) / (hi - lo)
    ang = math.radians(180 - (idx + frac) * seg)
    L = r_out * 0.93
    tipx, tipy = cx + L * math.cos(ang), cy + L * math.sin(ang)
    px, py = -math.sin(ang), math.cos(ang)
    bw = 4.2
    c.setFillColor(HexColor("#404040"))
    p = c.beginPath()
    p.moveTo(tipx, tipy)
    p.lineTo(cx + px * bw, cy + py * bw)
    p.lineTo(cx - px * bw, cy - py * bw)
    p.close()
    c.drawPath(p, stroke=0, fill=1)
    c.circle(cx, cy, 6.5, stroke=0, fill=1)


# ----------------------------------------------------------------------------- pages
def page_cover(c, ctx):
    rich, plain = ctx["rich_dates"], ctx["plain_dates"]
    c.setFillColor(C_PANEL)
    c.rect(0, 0, 193, PH, stroke=0, fill=1)
    draw_image_fit(c, asset("punjab_logo.png"), 32, 23, 128, 101)
    c.setFillColor(C_TITLEGREEN)
    c.setFont("Times-Bold", 13.5)
    for i, t in enumerate(["PUNJAB WEEKLY", "AIR QUALITY", "REPORT"]):
        c.drawString(20, Y(181 + i * 27.5), t)
    para(c, f"A guide for Deputy Commissioners to manage air quality {rich}", 20, 272, 152,
         font="Helvetica-Oblique", size=10.5, leading=16)
    para(c, f"Currently for {ctx['n_aqms_districts']} districts with the live air quality monitoring system",
         20, 366, 155, font="Helvetica-Oblique", size=10.5, leading=16)
    draw_image_fit(c, asset("epa_logo_large.png"), 28, 629, 125, 87)
    para(c, ORG_LINE.replace(", Lahore", " Lahore").replace("Punjab Lahore", "Punjab Lahore") + ".",
         20, 722, 160, font="Helvetica-Bold", size=10, leading=13.5)

    # title, average box, gauge
    c.setFillColor(black)
    c.setFont("Times-Bold", 13.5)
    c.drawCentredString(395, Y(45), "Weekly Air Quality Index Report of Punjab")
    avg = ctx["punjab_avg"]
    c.setFillColor(HexColor("#FFFFF2"))
    c.setStrokeColor(black)
    c.setLineWidth(0.6)
    c.rect(221, Y(130), 135, 53, stroke=1, fill=1)
    c.setFillColor(black)
    c.setFont("Times-Bold", 12)
    c.drawCentredString(288.5, Y(97), "Average AQI Value")
    t1, t2 = "in Punjab ", str(avg)
    w1, w2 = stringWidth(t1, "Times-Bold", 12), stringWidth(t2, "Times-Bold", 15)
    xs = 288.5 - (w1 + w2) / 2
    c.drawString(xs, Y(117), t1)
    c.setFont("Times-Bold", 15)
    c.drawString(xs + w1, Y(117), t2)
    gauge(c, 483, Y(142), 75, 35, avg)
    c.setStrokeColor(HexColor("#404040"))
    c.setLineWidth(1.3)
    c.line(214, Y(156), 574, Y(156))

    # map
    draw_image_fit(c, ctx["cover_map"], 212, 212, 350, 390)

    # bottom strip
    c.line(214, Y(662), 574, Y(662))
    draw_image_fit(c, asset("qr_chatbot.png"), 215, 672, 86, 86)
    tri(c, 305, Y(678.5), 5, "left", black)
    para(c, "Chatbot for instant AQI readings and safety guidelines for citizens", 304, 672, 95,
         font="Helvetica-Bold", size=6.8, leading=9.5, firstLineIndent=7)
    draw_image_fit(c, asset("helpline_1373.png"), 447, 672, 128, 78)
    tri(c, 448, Y(760), 4.5, "up", black)
    para(c, "Citizen helpline for complaints and feedback", 447, 752, 128,
         font="Helvetica-Bold", size=6.6, leading=9, firstLineIndent=8)
    footer(c, 1, plain, cover=True)


def page_ranking(c, ctx):
    ranked, unranked, pages = ctx["ranked"], ctx["unranked"], ctx["page_of"]
    c.setFillColor(C_HEAD)
    c.rect(0, Y(85), PW, 85, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 17)
    c.drawCentredString(PW / 2, Y(35), "WEEKLY DISTRICTS RANKING")
    c.setFont("Helvetica-Bold", 11.5)
    c.drawCentredString(PW / 2, Y(50), "AIR QUALITY INDEX")
    para(c, ctx["rich_dates"], 0, 55, PW, font="Helvetica-Oblique", size=12.5, align=TA_CENTER, color=white)

    c.setFillColor(black)
    c.setFont("Times-Bold", 13)
    c.drawCentredString(PW / 2, Y(110), "Districts Ranking (Air Quality Index)")
    draw_legend(c, 124)

    rows = ranked + unranked
    x_sr, x_d, x_b, x_p, x_e = 50, 86, 182, 500, 546
    top, hh = 164, 30
    rh = min(11.66, (760 - top - hh - 34) / max(len(rows), 1))
    body_top = top + hh
    body_bot = body_top + rh * len(rows) + 3
    # header
    c.setFillColor(C_BAND)
    c.rect(x_sr, Y(body_top), x_e - x_sr, hh, stroke=0, fill=1)
    c.setFillColor(black)
    c.setFont("Times-Bold", 11)
    c.drawCentredString((x_sr + x_d) / 2, Y(top + 12), "Sr.")
    c.drawCentredString((x_sr + x_d) / 2, Y(top + 24), "No")
    c.setFont("Times-Bold", 12)
    c.drawCentredString((x_d + x_b) / 2, Y(top + 19), "District")
    c.drawRightString(x_p - 6, Y(top + 19), "Average AQI Value")
    c.setFont("Times-Bold", 11)
    c.drawCentredString((x_p + x_e) / 2, Y(top + 12), "Page")
    c.drawCentredString((x_p + x_e) / 2, Y(top + 24), "No")

    maxv = max([r["weekly"] for r in rows] + [1])
    bar_max_w = 272
    for i, r in enumerate(rows):
        rt = body_top + i * rh + 1.5
        base = rt + rh * 0.72
        c.setFillColor(black)
        c.setFont("Times-Roman", 8.6)
        c.drawCentredString((x_sr + x_d) / 2, Y(base), str(i + 1))
        c.setFont("Times-Roman", 7.8)
        name = r["district"] + ("*" if r["rank"] is None else "")
        c.drawCentredString((x_d + x_b) / 2, Y(base), name)
        c.setFont("Times-Roman", 8.6)
        c.drawCentredString((x_p + x_e) / 2, Y(base), str(pages[r["district"]]))
        v = rnd(r["weekly"])
        if r["rank"] is not None:
            w = r["weekly"] / maxv * bar_max_w
            c.setFillColor(HexColor(cat_of(v)[3]))
            c.roundRect(x_b + 4, Y(rt + rh * 0.82), w, rh * 0.62, 2, stroke=0, fill=1)
            c.setFillColor(black)
            c.setFont("Times-Bold", 7.3)
            c.drawRightString(x_p - 6, Y(base), str(v))
        else:
            c.setFillColor(HexColor("#7F7F7F"))
            c.setFont("Helvetica-Oblique", 6.3)
            nd = r["valid_days"]
            c.drawString(x_b + 4, Y(base - 0.5),
                         f"* Not ranked – valid data for {nd} of 7 days only")
            c.setFont("Times-Roman", 7.3)
            c.drawRightString(x_p - 6, Y(base), f"{v}*")
    # extra rows
    extra = [(len(rows) + 1, "Desired Response from Deputy Commissioner", ctx["dc_pages_txt"]),
             (len(rows) + 2, "Health Guidelines for Citizens", str(ctx["health_page"]))]
    y = body_bot
    c.setStrokeColor(black)
    c.setLineWidth(0.6)
    for n, txt, pg in extra:
        c.line(x_sr, Y(y), x_e, Y(y))
        c.setFillColor(black)
        c.setFont("Times-Roman", 10.5)
        c.drawCentredString((x_sr + x_d) / 2, Y(y + 12.5), str(n))
        c.drawString(x_d + 4, Y(y + 12.5), txt)
        c.drawCentredString((x_p + x_e) / 2, Y(y + 12.5), pg)
        y += 17
    # grid
    c.line(x_sr, Y(body_top), x_e, Y(body_top))
    c.rect(x_sr, Y(y), x_e - x_sr, y - top, stroke=1, fill=0)
    for x in (x_d, x_p):
        c.line(x, Y(top), x, Y(y))
    c.line(x_b, Y(top), x_b, Y(body_bot))
    footer(c, 2, ctx["plain_dates"])


def page_method(c, ctx):
    c.setFillColor(C_BAND)
    c.rect(0, Y(122), PW, 122, stroke=0, fill=1)
    c.setFillColor(C_DARKGREEN)
    c.setFont("Times-Bold", 13)
    c.drawString(45, Y(27), "Data Collection and AQI Calculation:")
    dep = DEPARTMENT.replace("&", "&amp;")
    if ctx["source_mode"] == "district_aggregate":
        source_text = ("The dashboard export supplies hourly district-average AQI and pollutant values. "
                       "These district aggregates are used directly; individual station readings are not "
                       "reconstructed.")
    else:
        source_text = (f"{dep} has deployed {ctx['n_aqms']} Air Quality Monitoring Stations (AQMS) across Punjab "
                       "to monitor ambient air quality. Hourly pollutant concentrations and AQI values are fetched "
                       "from the AQMS dashboard, and the AQI is calculated on the basis of the breakpoints "
                       "notified by EPA Punjab.")
    h = para(c, source_text,
             45, 37, 505, size=10.5, leading=15, align=TA_JUSTIFY)
    para(c, f"The AQMS installed in {ctx['n_aqms_districts']} districts of Punjab are shown in the following map.",
         45, 37 + h + 5, 505, size=10.5, leading=15)

    # map panel
    c.setFillColor(HexColor("#EDEDED"))
    c.rect(40, Y(558), 516, 424, stroke=0, fill=1)
    c.setStrokeColor(C_LINE)
    c.setLineWidth(3)
    c.line(40, Y(135), 556, Y(135))
    c.line(40, Y(557), 556, Y(557))
    draw_image_fit(c, ctx["aqms_map"], 60, 140, 380, 414)
    # legend box
    c.setFillColor(white)
    c.setStrokeColor(black)
    c.setLineWidth(0.7)
    c.rect(392, Y(534), 145, 55, stroke=1, fill=1)
    tri(c, 408, Y(500), 5, "down", HexColor("#1E7B3A"))
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(419, Y(500), "AQMS installed")
    c.setFillColor(HexColor("#5BA3D9"))
    c.circle(413, Y(518), 5.2, stroke=0, fill=1)
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 6)
    c.drawCentredString(413, Y(520), "#")
    c.setFont("Helvetica-Bold", 9)
    c.drawString(422, Y(521), "No. of AQMS installed")

    # lower band
    c.setFillColor(C_BAND)
    c.rect(0, Y(792), PW, 220, stroke=0, fill=1)
    y = 584
    txts = [
        "The first page of the report gives a <b>snapshot</b> of AQI values in the districts.",
        "The <b>weekly</b> and <b>daily average air quality index</b> of each district is given in the report "
        "for the guidance of <b>Deputy Commissioners</b>.",
        "The <b>daily breakdown</b> shows the <b>time periods</b> that must be prioritised for <b>strict action</b> "
        "to protect citizens.",
        "The AQI guide will assist the district administration to plan <b>localised action</b> in "
        "<b>coordination</b> with the <b>Incharge District Officer (Environment)</b>.",
    ]
    for t in txts:
        y += para(c, t, 45, y, 505, size=10.5, leading=15, align=TA_JUSTIFY) + 6
    c.setStrokeColor(HexColor("#A9D18E"))
    c.setLineWidth(0.6)
    c.line(45, Y(y + 1), 550, Y(y + 1))
    source_method = ("The supplied hourly district-average AQI is used directly. "
                     if ctx["source_mode"] == "district_aggregate" else
                     "For districts with more than one station, the hourly AQI of all reporting stations is averaged. ")
    para(c, "<b>Method:</b> " + source_method
            + f"Time-period values are the average of the hourly values in that period (at least "
            f"{MIN_HOURS_PERIOD} of 8 hours required); daily values are the 24-hour average (at least "
            f"{MIN_HOURS_DAY} hours required); the weekly value is the average of the daily values. "
            + ("Hours with an AQI but without valid PM<sub>10</sub>/PM<sub>2.5</sub> readings "
               "were excluded, since such values reflect gaseous pollutants only and understate air pollution. "
               if EXCLUDE_HOURS_WITHOUT_PM else "")
            + f"Readings at the AQI ceiling ({AQI_CEILING}) were retained. \"No data\" means the minimum number "
              "of hours was not available.",
         45, y + 7, 505, size=8, leading=11.6, align=TA_JUSTIFY)
    footer(c, 3, ctx["plain_dates"])


def recommendations(r, days, ctx):
    name = r["district"]
    cat = cat_of(r["weekly"])
    out = [f"The AQI level in {name} was <b>{cat[2].lower()}</b> during the reporting week."]
    if r["rank"] is None:
        nd = r["valid_days"]
        out[0] += (f" <b>Note:</b> valid data was available for only {nd} of 7 days, "
                   "so this value is indicative and the district is not ranked.")
    pmeans = {}
    for pk, lab, _, rng in PERIODS:
        vals = [r["periods"][(d, pk)] for d in days if r["periods"][(d, pk)] is not None]
        if vals:
            pmeans[pk] = (np.mean(vals), lab, rng)
    s = ""
    if pmeans:
        pk = max(pmeans, key=lambda k: pmeans[k][0])
        m, lab, rng = pmeans[pk]
        s = (f"The highest levels were recorded during <b>{lab.lower()} ({rng.replace(chr(8211), chr(8211))})</b> "
             f"hours (average AQI {rnd(m)})")
    dv = {d: v for d, v in r["daily"].items() if v is not None}
    if dv:
        wd = max(dv, key=dv.get)
        s += f"; the worst day was <b>{wd:%A} {wd:%d.%m.%Y}</b> (AQI {rnd(dv[wd])})."
    elif s:
        s += "."
    n200 = sum(1 for v in r["periods"].values() if v is not None and rnd(v) > 200)
    if n200:
        s += f" AQI exceeded 200 in {n200} of {7 * len(PERIODS)} time periods."
    if s:
        out.append(s)
    out.append(f"The mitigation and precautionary measures are to be ensured as mentioned on pages "
               f"{ctx['dc_pages'][0]} to {ctx['dc_pages'][1]}.")
    out.append(f"Health Guidelines for citizens (page {ctx['health_page']}) are to be communicated to "
               "ensure public health.")
    return out


def page_district(c, ctx, r, page_no):
    days = ctx["days"]
    # header band
    c.setFillColor(C_BAND)
    c.rect(0, Y(83), PW, 83, stroke=0, fill=1)
    draw_image_fit(c, asset("punjab_logo.png"), 27, 14, 64, 54)
    draw_image_fit(c, asset("epa_logo_small.png"), 502, 26, 56, 36)
    c.setFillColor(black)
    c.setFont("Times-Bold", 15)
    cx = 290
    c.drawCentredString(cx, Y(33), r["district"])
    cat = cat_of(r["weekly"])
    lab = "Weekly Average AQI Value:"
    cname = cat[2]
    wl, wc = stringWidth(lab, "Times-Roman", 11), stringWidth(cname, "Times-Roman", 11)
    bw, gap = 52, 9
    total = wl + gap + bw + gap + wc
    x = cx - total / 2
    c.setFont("Times-Roman", 11)
    c.drawString(x, Y(56), lab)
    value_box(c, x + wl + gap + bw / 2, 40.5, bw, 22.5, r["weekly"], size=14)
    c.setFillColor(black)
    c.setFont("Times-Roman", 11)
    c.drawString(x + wl + gap + bw + gap, Y(56), cname)

    # recommendations
    c.setFont("Times-Bold", 10.5)
    c.drawString(63, Y(100), "Recommendations to Deputy Commissioner")
    items = recommendations(r, days, ctx)
    st = ParagraphStyle("b", fontName="Times-Roman", fontSize=9, leading=12.2, leftIndent=26,
                        bulletIndent=12, bulletFontName="Times-Roman", bulletFontSize=9)
    y = 110
    ps = []
    for t in items:
        p = Paragraph(t, st, bulletText="•")
        _, h = p.wrap(560 - 34 - 8, 1000)
        ps.append((p, h))
    box_h = sum(h for _, h in ps) + 7
    c.setStrokeColor(black)
    c.setLineWidth(0.7)
    c.rect(34, Y(106 + box_h), 526, box_h, stroke=1, fill=0)
    for p, h in ps:
        p.drawOn(c, 34, Y(y) - h)
        y += h
    b = 106 + box_h  # bottom of box

    # daily averages
    c.setFillColor(black)
    c.setFont("Times-Bold", 10.5)
    t = "Daily Average AQI in the Past 7 Days"
    c.drawString(45, Y(b + 18), t)
    c.setLineWidth(0.6)
    c.line(45, Y(b + 19.5), 45 + stringWidth(t, "Times-Bold", 10.5), Y(b + 19.5))
    c.setStrokeColor(HexColor("#404040"))
    c.line(45, Y(b + 25), 550, Y(b + 25))
    for i, d in enumerate(days):
        cxi = 77.5 + i * 73.2
        value_box(c, cxi, b + 32, 55, 24, r["daily"][d], size=15)
        c.setFillColor(black)
        c.setFont("Times-Bold", 7.6)
        c.drawCentredString(cxi, Y(b + 65), f"{d:%d.%m.%Y}")
        c.drawCentredString(cxi, Y(b + 74), f"{d:%A}")
    c.setStrokeColor(HexColor("#404040"))
    c.line(45, Y(b + 81), 550, Y(b + 81))

    # hours table
    tt = b + 107
    c.setFillColor(black)
    c.setFont("Times-Bold", 10.5)
    t = "Daily AQI Across the Hours"
    c.drawString(45, Y(b + 99), t)
    c.setStrokeColor(black)
    c.line(45, Y(b + 100.5), 45 + stringWidth(t, "Times-Bold", 10.5), Y(b + 100.5))
    rowh = 40.5
    th = 40 + rowh * 7 + 3
    c.setLineWidth(0.8)
    c.rect(45, Y(tt + th), 504, th, stroke=1, fill=0)
    c.line(45, Y(tt + 40), 549, Y(tt + 40))
    col = [91, 205, 342, 480]
    c.setFont("Times-Bold", 10.5)
    c.drawCentredString(col[0], Y(tt + 23), "Date and Day")
    for j, (pk, lab, _, rng) in enumerate(PERIODS):
        cx_ = col[j + 1]
        wlab = stringWidth(lab, "Times-Bold", 10.5)
        c.setFillColor(black)
        c.setFont("Times-Bold", 10.5)
        c.drawString(cx_ - wlab / 2 + 6, Y(tt + 17), lab)
        ix = cx_ - wlab / 2 - 5
        if pk == "morning":
            icon_sun(c, ix, Y(tt + 13.5), True)
        elif pk == "evening":
            icon_sun(c, ix, Y(tt + 13.5), False)
        else:
            icon_moon(c, ix, Y(tt + 13.5))
        c.setFillColor(black)
        c.setFont("Times-Roman", 8)
        c.drawCentredString(cx_, Y(tt + 31), rng)
    for i, d in enumerate(days):
        rc = tt + 40 + 8 + i * rowh + 12  # centre of the value box
        c.setFillColor(black)
        c.setFont("Times-Bold", 7.8)
        c.drawCentredString(col[0], Y(rc - 2), f"{d:%d.%m.%Y}")
        c.drawCentredString(col[0], Y(rc + 8), f"{d:%A}")
        for j, (pk, *_rest) in enumerate(PERIODS):
            value_box(c, col[j + 1], rc - 12, 58, 24, r["periods"][(d, pk)], size=15)
    # note
    note_y = tt + th + 11
    n = r["stations"]
    ptxt = ""
    if r["peak"] is not None:
        pv, pt = r["peak"]
        ptxt = f"; peak hourly AQI {rnd(pv)} on {pt:%d.%m.%Y} at {pt:%H:00}"
    c.setFillColor(C_GREY_TXT)
    c.setFont("Helvetica-Oblique", 7.8)
    basis = ("Based on supplied district-average readings" if ctx["source_mode"] == "district_aggregate"
             else f"Based on {n} station{'s' if n != 1 else ''}")
    c.drawString(45, Y(note_y), f"{basis}{ptxt}.")
    draw_legend(c, note_y + 14)
    footer(c, page_no, ctx["plain_dates"])


# ---- static guidance pages
DC_RESPONSE = {
    "Moderate": [
        "Issue AQI forecasts (DG EPA, Central Control Room).",
        "Monitor industries, issue legal notices (EPA Field Offices, DO Industries).",
        "Awareness campaigns for industries/public (DG EPA, DG PR).",
        "Check vehicle emissions/ETS/VICS (DRTA, Traffic Police, EPA).",
        "Awareness for farmers on crop burning (Agri. Extension Dept., Market Committees).",
        "Train healthcare staff &amp; prepare hospitals (District Health Officer, Social Security Dept.).",
        "Sprinkling roads, control trash burning, timely waste disposal (Municipal Corporations/WMCs, C&amp;W).",
        "Compliance of SOPs for sand / clay / soil carrying trolleys (DD Mines &amp; Minerals, Traffic Police).",
        "Identification of traffic hotspots and development of a congestion management strategy (Traffic Police).",
    ],
    "Unhealthy for Sensitive Groups": [
        "Strict enforcement against polluting units (EPA, DO Industries, DSP).",
        "Traffic management, illegal parking &amp; removal of encroachments (Traffic Police, District Administration).",
        "Public awareness along roads (DG EPA, Agriculture Department, DG PR).",
        "Schools to sensitize children, avoid hotspots (Education Dept., EPA).",
        "In case of high concentration of criteria pollutants, evacuate / avoid inflow towards hotspots "
        "(Education, EPA, Traffic).",
        "Hospitals: designate CAPE wards, ensure medicines &amp; R&amp;D (Health Dept., Social Welfare Hospitals).",
        "Impound smoke-emitting vehicles (DRTA, Traffic Police).",
        "Register FIRs for crop burning (Assistant Commissioner, Agri. Extension).",
        "Reduce work hours for vulnerable workers (Labour Dept., Industries, Social Welfare).",
    ],
    "Unhealthy": [
        "Issue CAPE-warning (DG EPA, PCC).",
        "Stop major construction (EPA Field Offices, District Admin).",
        "Cease non-compliant industries (EPA, DO Industries).",
        "Road sprinkling (at least 2x / day), enforce municipal laws to prevent solid waste burning "
        "(Municipal Corporations/WMCs).",
        "Ban vehicles without VICS (DRTA, Traffic Police).",
        "Shift school/work timings during peak traffic hours (Education Dept., District Admin).",
        "Establish health camps, ensure supplies (District Health Officer, Social Welfare Hospitals).",
        "Crop burning surveillance (Agri. Extension, Assistant Commissioner).",
    ],
    "Very Unhealthy": [
        "Stop all construction (EPA Field Offices, District Admin, C&amp;W, Development Authorities).",
        "Reduce work hours/production in industries (Dist. Admin, EPA, DO Industries).",
        "Zero tolerance for crop burning (Agri. Extension, Assistant Commissioner).",
        "Road sprinkling (2x / day) &amp; enforce municipal laws to prevent open burning of waste "
        "(Municipal Corporations/WMCs).",
        "Hybrid/home study for primary schools and alternative for middle school (DCC, Education Dept.).",
        "Close emission-intensive industries (EPA, DO Industries, DSP).",
        "Daily health reporting, free medical camps (District Health Officer, Social Welfare Hospitals).",
    ],
    "Hazardous": [
        "Close all schools in CAPE-hit areas (DCC, Education Dept.).",
        "Relieve vulnerable workers with paid leave (Labour Dept., Industries).",
        "Ban unfit vehicles, impound excessive smoke emitters (DRTA, Traffic Police).",
        "Declare health emergency in hospitals, special wards for vulnerable groups (District Health Officer, "
        "Social Security/Welfare Hospitals).",
        "Strict zero tolerance to crop burning (Agri. Extension, Assistant Commissioner).",
        "Continue daily reporting &amp; free health camps (District Health Officer, DCC).",
    ],
}
HEALTH = {
    "Moderate": ["Check AQI before outdoor activities.", "Monitor health vitals (oxygen, BP, etc.).",
                 "Consult doctor if respiratory issues.", "Eat healthy, avoid smoking.",
                 "Reduce outdoor exertion.", "Keep nebulizers/emergency kits ready."],
    "Unhealthy for Sensitive Groups": ["Wear face masks outdoors.", "Restrict children from playing outdoors.",
                                       "Avoid unnecessary travel.", "Elderly minimize outdoor exposure.",
                                       "Keep doors/windows closed.",
                                       "COPD &amp; CVD patients use masks as per doctor’s advice."],
    "Unhealthy": ["Wear N95 masks outdoors.", "Stay at home as much as possible.", "Avoid outdoor exertion.",
                  "Regularly check AQI &amp; health vitals.", "Bar children from outdoor activities.",
                  "COPD &amp; CVD patients use prescribed masks."],
    "Very Unhealthy": ["Stay indoors.", "Limit exercise; shift to indoor workouts.",
                       "Use N95 masks &amp; goggles if going out.", "Regularly check AQI &amp; vitals.",
                       "COPD &amp; CVD patients use prescribed masks."],
    "Hazardous": ["Stay indoors.", "Use <b>N95 masks</b> &amp; protective goggles when outside is unavoidable.",
                  "Use air purifiers at home.", "Frequently monitor health vitals (oxygen, BP, etc.)."],
}


def _col_header(c, x0, x1, top, text_lines, font="Helvetica-Bold", size=10):
    c.setFillColor(HexColor("#404040"))
    c.setFont(font, size)
    cx = (x0 + x1) / 2
    for i, t in enumerate(text_lines):
        c.drawCentredString(cx, Y(top + i * 12), t)
    return cx


def _bar_row(c, cols, top):
    """cols: list of (x0, x1, colour). Draws a single rounded bar split in colours."""
    x0, x1 = cols[0][0], cols[-1][1]
    c.setFillColor(HexColor(cols[0][2]))
    c.roundRect(x0, Y(top + 15), x1 - x0, 15, 4, stroke=0, fill=1)
    for a, b, col in cols:
        c.setFillColor(HexColor(col))
        if a == x0:
            c.roundRect(a, Y(top + 15), b - a, 15, 4, stroke=0, fill=1)
            c.rect(a + 6, Y(top + 15), b - a - 6, 15, stroke=0, fill=1)
        elif b == x1:
            c.roundRect(a, Y(top + 15), b - a, 15, 4, stroke=0, fill=1)
            c.rect(a, Y(top + 15), b - a - 6, 15, stroke=0, fill=1)
        else:
            c.rect(a, Y(top + 15), b - a, 15, stroke=0, fill=1)


def _numbered(c, items, x, top, width, size=8.6, leading=13):
    st = ParagraphStyle("n", fontName="Times-Roman", fontSize=size, leading=leading, leftIndent=12,
                        bulletIndent=0, bulletFontName="Times-Roman", bulletFontSize=size)
    y = top
    for i, t in enumerate(items, 1):
        p = Paragraph(t, st, bulletText=f"{i}.")
        _, h = p.wrap(width, 1000)
        p.drawOn(c, x, Y(y) - h)
        y += h + 1.2
    return y


def _title(c, title, sub, font="Times-Bold"):
    c.setFillColor(black)
    c.setFont(font, 14)
    c.drawCentredString(PW / 2, Y(68), title)
    c.setFont("Times-Italic", 12)
    c.drawCentredString(PW / 2, Y(85), sub)
    c.setStrokeColor(C_LINE)
    c.setLineWidth(2)
    c.line(35, Y(96), 560, Y(96))


def page_dc1(c, ctx, page_no):
    _title(c, "Desired Response from Deputy Commissioner", "(based on AQI)")
    xs = [35, 123, 210, 385, 560]
    heads = [["0-50", "(Good)"], ["51-100", "(Satisfactory)"], ["", "101-150 (Moderate)"],
             ["151-200 (Unhealthy for sensitive", "groups)"]]
    for i, h in enumerate(heads):
        _col_header(c, xs[i], xs[i + 1], 123, h)
    _bar_row(c, [(xs[i], xs[i + 1], CATEGORIES[i][3]) for i in range(4)], 140)
    _numbered(c, DC_RESPONSE["Moderate"], 222, 163, 158)
    _numbered(c, DC_RESPONSE["Unhealthy for Sensitive Groups"], 397, 163, 160)
    footer(c, page_no, ctx["plain_dates"])


def page_dc2(c, ctx, page_no):
    _title(c, "Desired Response from Deputy Commissioner", "(based on AQI)")
    xs = [35, 210, 385, 560]
    for i, (k, lab) in enumerate([("Unhealthy", "201-300 (Unhealthy)"), ("Very Unhealthy", "301-400 (Very Unhealthy)"),
                                  ("Hazardous", "400+ (Hazardous)")]):
        _col_header(c, xs[i], xs[i + 1], 123, [lab])
        _numbered(c, DC_RESPONSE[k], xs[i] + 5, 150, 165)
    _bar_row(c, [(xs[i], xs[i + 1], CATEGORIES[4 + i][3]) for i in range(3)], 128)
    footer(c, page_no, ctx["plain_dates"])


def page_health(c, ctx, page_no):
    _title(c, "Health Guidelines for Citizens", "(based on AQI)", font="Helvetica-Bold")
    xs = [35, 123, 210, 385, 560]
    heads = [["0-50", "(Good)"], ["51-100", "(Satisfactory)"], ["", "101-150 (Moderate)"],
             ["151-200 (Unhealthy for sensitive", "groups)"]]
    for i, h in enumerate(heads):
        _col_header(c, xs[i], xs[i + 1], 123, h)
    _bar_row(c, [(xs[i], xs[i + 1], CATEGORIES[i][3]) for i in range(4)], 140)
    y1 = _numbered(c, HEALTH["Moderate"], 222, 163, 158)
    y2 = _numbered(c, HEALTH["Unhealthy for Sensitive Groups"], 397, 163, 160)
    t2 = max(y1, y2) + 22
    xs2 = [35, 210, 385, 560]
    for i, (k, lab) in enumerate([("Unhealthy", "201-300 (Unhealthy)"), ("Very Unhealthy", "301-400 (Very Unhealthy)"),
                                  ("Hazardous", "400+ (Hazardous)")]):
        _col_header(c, xs2[i], xs2[i + 1], t2, [lab])
        _numbered(c, HEALTH[k], xs2[i] + 5, t2 + 28, 165)
    _bar_row(c, [(xs2[i], xs2[i + 1], CATEGORIES[4 + i][3]) for i in range(3)], t2 + 5)
    footer(c, page_no, ctx["plain_dates"])


# ----------------------------------------------------------------------------- Excel of results
def write_excel(path, ctx, res, dh, st_hr, match_df):
    days = ctx["days"]
    aggregate_mode = ctx["source_mode"] == "district_aggregate"
    basis_column = "Input basis" if aggregate_mode else "Stations"
    rows = []
    for r in ctx["ranked"] + ctx["unranked"]:
        row = {"Rank": r["rank"] or "not ranked", "District": r["district"],
               "Weekly AQI": rnd(r["weekly"]), "Weekly AQI (unrounded)": round(r["weekly"], 2),
               "Category": cat_of(r["weekly"])[2], "Valid days": r["valid_days"],
               basis_column: "District aggregate" if aggregate_mode else r["stations"],
               "Page": ctx["page_of"][r["district"]]}
        for d in days:
            v = r["daily"][d]
            row[f"{d:%d.%m.%Y}"] = rnd(v) if v is not None else "No data"
        rows.append(row)
    summary = pd.DataFrame(rows)
    per = []
    for r in ctx["ranked"] + ctx["unranked"]:
        for d in days:
            row = {"District": r["district"], "Date": d.strftime("%d.%m.%Y"), "Day": d.strftime("%A")}
            for pk, lab, *_ in PERIODS:
                v = r["periods"][(d, pk)]
                row[lab] = rnd(v) if v is not None else "No data"
            v = r["daily"][d]
            row["Daily average"] = rnd(v) if v is not None else "No data"
            per.append(row)
    hourly = dh.rename(columns={"district": "District", "hour_ts": "Hour", "aqi": "District hourly AQI",
                                "n_st": "Stations reporting"})
    hourly_columns = ["District", "Hour", "District hourly AQI"]
    if not aggregate_mode:
        hourly_columns.append("Stations reporting")
    hourly = hourly[hourly_columns]
    hourly["District hourly AQI"] = hourly["District hourly AQI"].round(1)
    if aggregate_mode:
        st = st_hr.rename(columns={"district": "District", "hour_ts": "Hour", "aqi": "District-average AQI"})[
            ["District", "Hour", "District-average AQI"]]
        input_sheet = "District aggregate input"
        mapping_sheet = "District mapping"
    else:
        st = st_hr.rename(columns={"district": "District", "station": "Station", "hour_ts": "Hour", "aqi": "AQI"})
        input_sheet = "Station hourly (valid)"
        mapping_sheet = "Station matching"
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="Weekly summary", index=False)
        pd.DataFrame(per).to_excel(xw, sheet_name="Daily & periods", index=False)
        hourly.to_excel(xw, sheet_name="District hourly", index=False)
        st.to_excel(xw, sheet_name=input_sheet, index=False)
        match_df.to_excel(xw, sheet_name=mapping_sheet, index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                w = max(len(str(c.value)) if c.value is not None else 0 for c in col[:200])
                ws.column_dimensions[col[0].column_letter].width = min(max(10, w + 2), 45)


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Weekly AQI report of Punjab")
    ap.add_argument("--start", help="first day of the week, YYYY-MM-DD (default: last 7 days in data)")
    ap.add_argument("--input", type=Path, help="one raw data file (default: read files from raw_data/)")
    ap.add_argument("--output-dir", type=Path, help="directory for generated report files")
    args = ap.parse_args()

    global OUTPUT_DIR
    if args.output_dir:
        OUTPUT_DIR = args.output_dir.resolve()

    log("Weekly AQI Report generator")
    log("-" * 60)
    OUTPUT_DIR.mkdir(exist_ok=True)
    dshp, ashp = find_shapefiles()
    districts = load_districts(dshp)
    aqms = load_aqms(ashp, districts)
    raw, source_mode = load_raw([args.input] if args.input else None)
    raw, match_df = assign_districts(raw, districts, aqms, source_mode)

    start = args.start or WEEK_START
    if start:
        week_start = pd.Timestamp(start).normalize().to_pydatetime()
    else:
        last = raw["dt"].max().normalize()
        week_start = (last - pd.Timedelta(days=6)).to_pydatetime()
    res, days, dh, st_hr = compute(raw, week_start)
    ranked, unranked = rank_districts(res)
    if not ranked and not unranked:
        sys.exit("ERROR: no district has valid data in the selected week.")
    rich, plain = range_texts(days)
    log(f"  Week           : {plain}")
    log(f"  Districts with data: {len(ranked) + len(unranked)}  (ranked {len(ranked)}, not ranked {len(unranked)})")

    order = ranked + unranked
    page_of = {r["district"]: 4 + i for i, r in enumerate(order)}
    dc1 = 4 + len(order)
    avg_src = ranked if ranked else unranked
    ctx = {
        "days": days, "rich_dates": rich, "plain_dates": plain, "ranked": ranked, "unranked": unranked,
        "page_of": page_of, "dc_pages": (dc1, dc1 + 1), "dc_pages_txt": f"{dc1}-{dc1 + 1}",
        "health_page": dc1 + 2,
        "punjab_avg": rnd(np.mean([r["weekly"] for r in avg_src])),
        "n_aqms": len(aqms), "n_aqms_districts": int(aqms["sp_district"].nunique()),
        "source_mode": source_mode,
    }
    tag = f"{days[0]:%d%b}-{days[-1]:%d%b%Y}"
    tmp = OUTPUT_DIR / "_maps"
    tmp.mkdir(exist_ok=True)
    ctx["cover_map"] = str(tmp / "cover_map.png")
    ctx["aqms_map"] = str(tmp / "aqms_map.png")
    log("  Drawing maps ...")
    render_cover_map(districts, res, ctx["cover_map"], 350, 390)
    render_aqms_map(districts, aqms, ctx["aqms_map"], 380, 414)

    pdf_path = OUTPUT_DIR / f"Weekly_AQI_Report_{tag}.pdf"
    log("  Writing PDF ...")
    c = rl_canvas.Canvas(str(pdf_path), pagesize=A4)
    c.setTitle(f"Weekly Air Quality Index Report of Punjab | {plain}")
    c.setAuthor(ORG_LINE)
    page_cover(c, ctx); c.showPage()
    page_ranking(c, ctx); c.showPage()
    page_method(c, ctx); c.showPage()
    for r in order:
        page_district(c, ctx, r, page_of[r["district"]]); c.showPage()
    page_dc1(c, ctx, dc1); c.showPage()
    page_dc2(c, ctx, dc1 + 1); c.showPage()
    page_health(c, ctx, dc1 + 2); c.showPage()
    c.save()

    xlsx_path = OUTPUT_DIR / f"Weekly_AQI_Stats_{tag}.xlsx"
    write_excel(xlsx_path, ctx, res, dh, st_hr, match_df)
    log("-" * 60)
    log(f"  Punjab average AQI: {ctx['punjab_avg']}")
    log(f"  PDF   : {pdf_path}")
    log(f"  Excel : {xlsx_path}")
    no_data = sorted(set(districts["dname"]) - set(page_of))
    if no_data:
        log(f"  Districts shown grey (no data this week): {', '.join(no_data)}")


if __name__ == "__main__":
    main()
