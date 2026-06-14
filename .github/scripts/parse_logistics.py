#!/usr/bin/env python3
"""
260614-cannobio-parse-fahrplan-v1.1.py
Ablage: scripts/cannobio_parse_fahrplan.py

Parst vorheruntergeladene FART/VCO PDFs aus _cache/ und schreibt fahrplan.json.
Die SHA-256-Werte und URLs werden via Umgebungsvariablen aus dem GitHub-Actions-
Workflow übergeben (FART316_SHA, FART316_URL, VCO3_SHA, VCO1_SHA).

Kein eigener Download: Der Workflow-Step lädt mit curl + Browser-Headers.

Exit-Codes:
  0 = JSON erfolgreich geschrieben
  1 = Fataler Fehler (kein gecachtes PDF, Parse komplett fehlgeschlagen)
  2 = Konfidenz < MIN_CONF – JSON NICHT überschrieben (Workflow erstellt Issue)
"""
import json, os, re, sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    print("[FATAL] pip install pdfplumber", file=sys.stderr); sys.exit(1)

# ─── KONFIGURATION ────────────────────────────────────────────────────────────
DATA_DIR   = Path("cannobio/anreise/data")
CACHE_DIR  = DATA_DIR / "_cache"
JSON_OUT   = DATA_DIR / "fahrplan.json"
DIAG_OUT   = DATA_DIR / "parse-diagnostic.json"
MIN_CONF   = 0.65

SOURCES_CFG = {
    "fart316": {
        "url_env": "FART316_URL",
        "url_default": "https://fartiamo.ch/wp-content/uploads/2025/12/62316.pdf",
        "sha_env": "FART316_SHA",
    },
    "vco3": {
        "url_env": None,
        "url_default": "https://www.vcotrasporti.it/userdata/banner/VERBANIA-BRISSAGO.pdf",
        "sha_env": "VCO3_SHA",
    },
    "vco1": {
        "url_env": None,
        "url_default": "https://www.vcotrasporti.it/userdata/banner/VERBANIA%20-%20OMEGNA.pdf",
        "sha_env": "VCO1_SHA",
    },
    "saf": {
        "url_env": "SAF_URL",
        "url_default": "https://www.safduemila.com/wp-content/uploads/2019/01/alibus-2026-web.pdf",
        "sha_env": "SAF_SHA",
    },
}

# ─── HILFSFUNKTIONEN ──────────────────────────────────────────────────────────
def to_hhmm(raw) -> str | None:
    if not raw: return None
    s = str(raw).strip().replace(",", ":").replace(".", ":")
    if re.match(r"^[-–—−\s]+$", s): return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m: return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.match(r"^(\d{1,2})\s+(\d{2})$", s)
    if m: return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None

def valid_time(t) -> bool:
    if not t: return False
    try:
        h, mn = map(int, t.split(":")); return 0 <= h <= 23 and 0 <= mn <= 59
    except: return False

def expand_range(start: str, end_: str, step: int = 30) -> list[str]:
    out = []
    try:
        sh, sm = map(int, start.split(":")); eh, em = map(int, end_.split(":"))
        cur = sh * 60 + sm; fin = eh * 60 + em
        while cur <= fin:
            out.append(f"{cur // 60:02d}:{cur % 60:02d}"); cur += step
    except: pass
    return out

# ─── VCO LINIE 3 PARSER ───────────────────────────────────────────────────────
_SVC_RAW = {"fer":"fer","fer ▼":"fer_mofr","▼":"fer_mofr","fest":"fest","FEST":"fest",
            "scol":"scol","SCOL":"scol","#":"sab","sab":"sab"}

def _svc(cell: str) -> str:
    for k, v in _SVC_RAW.items():
        if k in cell: return v
    return "fer" if cell.strip() and cell.strip().upper() not in ("","NAN") else "all"

def _headers(row: list) -> list[dict]:
    return [{"trip": (re.match(r"^(\w+)", str(c).strip()) or type("", (), {"group": lambda _,x: "?"})()).group(1),
             "svc": _svc(str(c or ""))} for c in (row[1:] if row else [])]

def _row_times(row: list, trips: list[dict]) -> list[dict]:
    return [{"time": t, "trip": trips[i]["trip"], "svc": trips[i]["svc"]}
            for i, cell in enumerate(row[1:]) if i < len(trips) and (t := to_hhmm(cell)) and valid_time(t)]

def _find_row(table, kw: str, nth=0):
    n = 0
    for row in (table or []):
        if row and kw.lower() in str(row[0] or "").lower():
            if n == nth: return row
            n += 1
    return None

def _build(dep, arr) -> list[dict]:
    am = {x["trip"]: x["time"] for x in arr}
    return [{"dep": d["time"], "arr": am[d["trip"]], "svc": d["svc"], "trip": d["trip"]}
            for d in dep if d["trip"] in am]

def parse_vco3(path: Path) -> dict | None:
    try:
        with pdfplumber.open(path) as pdf:
            tables = [t for p in pdf.pages for t in (p.extract_tables() or [])]
        if len(tables) < 2: return None
        tb_out, tb_ret = tables[0], tables[1]
        h_out = _headers(tb_out[0] if tb_out else [])
        h_ret = _headers(tb_ret[0] if tb_ret else [])
        return {
            "VCO3_BR_CAN": _build(_row_times(_find_row(tb_ret, "Brissago") or [], h_ret),
                                   _row_times(_find_row(tb_ret, "Cannobio") or [], h_ret)),
            "VCO3_CAN_BR": _build(_row_times(_find_row(tb_out, "Cannobio", 1) or [], h_out),
                                   _row_times(_find_row(tb_out, "Brissago") or [], h_out)),
            "VCO3_IN_CAN": _build(_row_times(_find_row(tb_out, "Intra") or [], h_out),
                                   _row_times(_find_row(tb_out, "Cannobio", 0) or [], h_out)),
            "VCO3_CAN_IN": _build(_row_times(_find_row(tb_ret, "Cannobio") or [], h_ret),
                                   _row_times(_find_row(tb_ret, "Intra") or [], h_ret)),
        }
    except Exception as e:
        print(f"[ERROR] parse_vco3: {e}", file=sys.stderr); return None

# ─── FART 316 PARSER ──────────────────────────────────────────────────────────
def _times_from_text(text: str, keywords: list[str]) -> list[str]:
    found = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not any(k.lower() in line.lower() for k in keywords): continue
        ctx = " ".join(lines[i:i+3])
        for h, mn in re.findall(r"\b([0-2]?\d)[:\s]([0-5]\d)\b", ctx):
            t = f"{int(h):02d}:{mn}"
            if valid_time(t) and t not in found: found.append(t)
        step = 30 if re.search(r"ogni\s+30|30 min", ctx, re.I) else 60
        for sh, sm, eh, em in re.findall(r"(\d{2})\s+(\d{2})\s*[-–]\s*(\d{2})\s+(\d{2})", ctx):
            for t in expand_range(f"{sh}:{sm}", f"{eh}:{em}", step):
                if t not in found: found.append(t)
    return sorted(set(found))

def parse_fart316(path: Path) -> dict | None:
    try:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        split = len(text)
        for m in ["Brissago - Locarno","Brissago – Locarno","62.316 Brissago"]:
            i = text.find(m)
            if 0 < i < split: split = i
        lc = _times_from_text(text[:split], ["Debarcadero","Stazione","Locarno"])
        br = _times_from_text(text[split:], ["Piazza del Sole","Brissago"])
        if len(lc) < 10 or len(br) < 10:
            print(f"[WARN] fart316: LC={len(lc)}, BR={len(br)} – zu wenige Abfahrten.", file=sys.stderr)
            return None
        return {"FART_LC": lc, "FART_BR": br}
    except Exception as e:
        print(f"[ERROR] parse_fart316: {e}", file=sys.stderr); return None

# ─── VCO LINIE 1 PARSER ───────────────────────────────────────────────────────
def parse_vco1(path: Path) -> dict | None:
    try:
        with pdfplumber.open(path) as pdf:
            tables = [t for p in pdf.pages for t in (p.extract_tables() or [])]
        if not tables: return None
        main = max(tables, key=lambda t: sum(len(r) for r in t) if t else 0)
        trips = _headers(main[0])
        vf   = _row_times(_find_row(main, "Verbania Ferrovia", 0) or [], trips)
        intr = _row_times(_find_row(main, "Intra",             0) or [], trips)
        vf2  = _row_times(_find_row(main, "Verbania Ferrovia", 1) or [], trips)
        in2  = _row_times(_find_row(main, "Intra",             1) or [], trips)
        is_out = lambda tid: (int(tid) % 2 != 0) if tid.isdigit() else True
        return {
            "VCO1_VF_IN": _build([x for x in vf   if is_out(x["trip"])], [x for x in intr if is_out(x["trip"])]),
            "VCO1_IN_VF": _build([x for x in in2  if not is_out(x["trip"])], [x for x in vf2 if not is_out(x["trip"])]),
        }
    except Exception as e:
        print(f"[ERROR] parse_vco1: {e}", file=sys.stderr); return None

# ─── SAF ALIBUS PARSER ───────────────────────────────────────────────────────
def parse_saf(path: Path) -> dict | None:
    """
    Parst das SAF Alibus PDF (gültig April–Oktober).
    Extrahiert Abfahrtszeiten ab Malpensa T.1 und ab Verbania-Intra Bar Lucini.
    """
    try:
        import re as _re
        with pdfplumber.open(path) as pdf:
            tables = [t for p in pdf.pages for t in (p.extract_tables() or [])]
            full_text = " ".join(p.extract_text() or "" for p in pdf.pages)

        mxp_deps, intra_arrs, intra_deps, mxp_arrs = [], [], [], []

        for table in tables:
            for row in table:
                if not row: continue
                first = str(row[0] or "").upper().strip()
                times = [x for x in (to_hhmm(c) for c in row[1:]) if x and valid_time(x)]
                if not times: continue

                if "MALPENSA" in first and "T.1" in first and not mxp_arrs:
                    if not mxp_deps:
                        mxp_deps = times    # erste Tabelle = Hinfahrt
                    else:
                        mxp_arrs = times    # zweite Tabelle = Rückfahrt
                elif "IMBARCADERO" in first and "INTRA" in first:
                    intra_arrs = times
                elif "LUCINI" in first or ("INTRA" in first and intra_deps == [] and not mxp_deps):
                    intra_deps = times
                elif "INTRA" in first and "BAR" in first:
                    intra_deps = times

        # Fallback: aus Volltext extrahieren wenn Tabelle nicht funktioniert
        if not mxp_deps:
            m = _re.search(r"MALPENSA.*?T\.1[^\d]+([\d:]+(?:\s+[\d:]+)*)", full_text, _re.I)
            if m:
                mxp_deps = [x for x in _re.findall(r"\d{1,2}:\d{2}", m.group(1)) if valid_time(x)]

        if not intra_deps:
            m = _re.search(r"LUCINI.*?((?:\d{1,2}[:\s]\d{2}\s*)+)", full_text, _re.I)
            if m:
                intra_deps = [to_hhmm(x) for x in _re.findall(r"\d{1,2}:\d{2}", m.group(1))
                              if to_hhmm(x) and valid_time(to_hhmm(x))]

        if not mxp_deps or not intra_deps:
            print(f"[WARN] saf: MXP={len(mxp_deps)}, INTRA={len(intra_deps)} – Zeiten nicht gefunden.",
                  file=sys.stderr)
            return None

        # Fahrzeit berechnen
        mxp_t = 100  # Default
        if intra_arrs and mxp_deps:
            try:
                diff = sum(toMin(a)-toMin(d) for a,d in zip(intra_arrs, mxp_deps)) // len(mxp_deps)
                if 60 <= diff <= 150: mxp_t = diff
            except: pass

        intra_t = 100  # Default
        if mxp_arrs and intra_deps:
            try:
                diff = sum(toMin(a)-toMin(d) for a,d in zip(mxp_arrs, intra_deps)) // len(intra_deps)
                if 60 <= diff <= 150: intra_t = diff
            except: pass

        print(f"[OK] saf: {len(mxp_deps)} MXP-Abf., {len(intra_deps)} Intra-Abf., "
              f"MXP→Intra {mxp_t} Min., Intra→MXP {intra_t} Min.")
        return {
            "SAF_MXP_DEP":   sorted(set(mxp_deps)),
            "SAF_MXP_T":     mxp_t,
            "SAF_INTRA_DEP": sorted(set(intra_deps)),
            "SAF_INTRA_T":   intra_t,
        }
    except Exception as e:
        print(f"[ERROR] parse_saf: {e}", file=sys.stderr)
        return None

# Kleine Hilfsfunktionen für Python (analog zu JS im Frontend)
def toMin(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m

# ─── KONFIDENZ ────────────────────────────────────────────────────────────────
_EXP = {"FART_LC":(12,35),"FART_BR":(12,36),"VCO3_BR_CAN":(8,15),"VCO3_CAN_BR":(8,14),
        "VCO3_IN_CAN":(10,18),"VCO3_CAN_IN":(10,18),"VCO1_VF_IN":(18,35),"VCO1_IN_VF":(18,35)}

def score(data: dict) -> float:
    s = []
    for k,(mn,ex) in _EXP.items():
        n = len(data.get(k,[]))
        s.append(min(1.0,n/ex) if n>=mn else max(0.0,n/mn)*0.5)
    return round(sum(s)/len(s),3) if s else 0.0

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = {}
    if JSON_OUT.exists():
        try: existing = json.loads(JSON_OUT.read_text())
        except: pass

    # SHA-256 und URLs aus Umgebungsvariablen (gesetzt vom Workflow-Step)
    checksums: dict[str, dict] = dict(existing.get("sources", {}))
    today = datetime.now(timezone.utc).date().isoformat()
    for key, cfg in SOURCES_CFG.items():
        sha = os.environ.get(cfg["sha_env"] or "", "")
        url = os.environ.get(cfg["url_env"] or "", "") if cfg["url_env"] else ""
        if sha:
            checksums[key] = {
                "url":  url or cfg["url_default"],
                "sha256": sha,
                "last_fetched": today,
            }

    data: dict = {}
    notes: list[str] = []

    def try_parse(key, parser_fn, fallback_keys):
        path = CACHE_DIR / f"{key}.pdf"
        if path.exists():
            result = parser_fn(path)
            if result:
                data.update(result); notes.append(f"{key}:OK")
            else:
                notes.append(f"{key}:FAILED(fallback)")
                for k in fallback_keys: data[k] = existing.get(k, [])
        else:
            notes.append(f"{key}:NO_CACHE")
            for k in fallback_keys: data[k] = existing.get(k, [])

    try_parse("fart316", parse_fart316, ["FART_LC","FART_BR"])
    try_parse("vco3",    parse_vco3,    ["VCO3_BR_CAN","VCO3_CAN_BR","VCO3_IN_CAN","VCO3_CAN_IN"])
    try_parse("vco1",    parse_vco1,    ["VCO1_VF_IN","VCO1_IN_VF"])
    try_parse("saf",     parse_saf,     ["SAF_MXP_DEP","SAF_MXP_T","SAF_INTRA_DEP","SAF_INTRA_T"])

    data["FART_LC_T"] = 21
    data["FART_BR_T"] = 30
    # SAF Defaults (falls Parser fehlschlägt und kein Fallback existiert)
    data.setdefault("SAF_MXP_DEP",  ["10:00","12:00","14:00","16:00","18:00","20:00"])
    data.setdefault("SAF_MXP_T",    100)
    data.setdefault("SAF_INTRA_DEP",["06:00","09:00","11:00","13:00","15:00","17:00"])
    data.setdefault("SAF_INTRA_T",  100)

    confidence = score(data)
    print(f"[SCORE] Konfidenz: {confidence:.2f} (Minimum: {MIN_CONF})")

    output = {
        "meta": {"generated": datetime.now(timezone.utc).isoformat(),
                 "v":"1.1","confidence":confidence,"notes":" | ".join(notes)},
        "sources": checksums,
        **data,
    }

    if confidence < MIN_CONF:
        DIAG_OUT.write_text(json.dumps(output,indent=2,ensure_ascii=False))
        print(f"[WARN] Konfidenz {confidence} < {MIN_CONF}. JSON NICHT aktualisiert.", file=sys.stderr)
        sys.exit(2)

    JSON_OUT.write_text(json.dumps(output,indent=2,ensure_ascii=False))
    print(f"[OK] {JSON_OUT} geschrieben (Konfidenz: {confidence})")

if __name__ == "__main__":
    main()
