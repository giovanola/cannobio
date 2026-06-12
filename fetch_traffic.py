#!/usr/bin/env python3
"""
fetch_traffic.py — A2 Gotthard traffic data fetcher
Source: ASTRA / opentransportdata.swiss · DATEX II SOAP
Output: traffic.json (cannobio repo root)
"""

import os, sys, json, re
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

try:
    import requests
except ImportError:
    sys.exit("Missing: pip install requests")

API_KEY     = os.environ.get("OPENTRANSPORT_API_KEY", "")
ENDPOINT    = "https://api.opentransportdata.swiss/TDP/Soap_Datex2/TrafficSituations/Pull"
SOAP_ACTION = "http://opentransportdata.swiss/TDP/Soap_Datex2/Pull/v1/pullTrafficMessages"
NS_D2       = "http://datex2.eu/schema/2/2_0"

# Main Gotthard tunnel = A2, between Göschenen and Airolo
TUNNEL_KEYWORDS = ["Gotthard-Strassentunnel", "Gotthard-Tunnel", "Gotthardtunnel"]
TUNNEL_PAIR     = ("göschenen", "airolo")   # both must appear on A2

A2_KEYWORDS = ["A2 ", "N2 ", "Gotthard", "Göschenen", "Airolo",
               "Amsteg", "Erstfeld", "Altdorf", "Flüelen", "Brunnen",
               "Bellinzona", "Lugano", "Chiasso", "Basel", "Luzern"]

SOAP_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <d2LogicalModel modelBaseVersion="2"
      xmlns="http://datex2.eu/schema/2/2_0">
      <exchange>
        <supplierIdentification>
          <country>ch</country>
          <nationalIdentifier>cannobio-gotthard</nationalIdentifier>
        </supplierIdentification>
      </exchange>
    </d2LogicalModel>
  </soap:Body>
</soap:Envelope>"""


def iter_text(el, *localnames):
    for ln in localnames:
        for e in el.iter(f"{{{NS_D2}}}{ln}"):
            if e.text and e.text.strip():
                yield e.text.strip()


def road_number(record):
    return ", ".join(dict.fromkeys(
        e.text.strip() for e in record.iter(f"{{{NS_D2}}}roadNumber")
        if e.text
    ))


def clean_description(raw):
    """Extract the 'Sachlage:' portion and remove DATEX II boilerplate."""
    text = raw

    # Remove "Freigegeben: <route info>" prefix (DATEX II admin status)
    text = re.sub(r'^Freigegeben:\s*[^\n]+?(?=Sachlage:|$)', '', text).strip()

    # Extract Sachlage: ... (the actual situation)
    m = re.search(r'Sachlage:\s*(.+?)(?:Baustelle|Dauer:|$)', text, re.IGNORECASE | re.DOTALL)
    if m:
        situation = m.group(1).strip().rstrip(',').strip()
    else:
        situation = text

    # Append next closure date if present
    date_m = re.search(r'voraussichtlich\s+(\d{1,2}\.\d{2}\.\d{4})', raw)
    next_date = date_m.group(1) if date_m else None

    return situation.strip(), next_date


def is_night_only(text):
    """True if incident is explicitly limited to night hours."""
    return bool(re.search(r'Dauer:\s*nachts', text, re.IGNORECASE))


def is_side_road(road, text):
    """True for H2, H19, pass roads — not the main A2 Gotthard tunnel."""
    t = text.lower()
    if road and re.match(r'^H\d', road.strip()):
        return True
    return any(k in t for k in [
        'göschenenalp', 'sustenpass', 'oberalp', 'furka', 'grimsel',
        'andermatt', 'hospental', 'airolo-nufenen'
    ])


def detect_direction(text):
    t = text.lower()
    s = any(k in t for k in ['airolo', 'richtung süd', 'richtung tessin', 'richtung lugano', 'richtung chiasso'])
    n = any(k in t for k in ['göschenen', 'richtung nord', 'richtung basel', 'richtung zürich', 'richtung luzern'])
    if s and not n: return "south"
    if n and not s: return "north"
    return "both"


def severity_from_situation(situation_text, raw_text, is_night, now_cest_hour):
    """Determine severity considering time of day and night-only restrictions."""
    t = situation_text.lower()
    raw_lower = raw_text.lower()

    # Night-only incidents during daytime = info (upcoming, not active)
    if is_night and 5 <= now_cest_hour < 20:
        return "info"

    if any(k in t for k in ['gesperrt', 'sperre', 'closed', 'chiuso']):
        return "critical"
    if any(k in raw_lower for k in ['stau', 'coda', 'congestion', 'km stau']):
        return "high"
    if any(k in t for k in ['stockend', 'verlangsamt', 'behinderung', 'slow', 'lavori']):
        return "medium"
    return "info"


def extract_delay(text):
    m = re.search(r'(\d{1,3})\s*(?:minuten|min)', text, re.I)
    return int(m.group(1)) if m else None


def extract_queue(text):
    m = re.search(r'(\d{1,2}(?:[.,]\d)?)\s*km', text, re.I)
    return float(m.group(1).replace(",", ".")) if m else None


def is_tunnel_incident(road, text):
    """Only the A2 main tunnel Göschenen↔Airolo, no side roads."""
    if is_side_road(road, text): return False
    t = text.lower()
    if any(k.lower() in t for k in TUNNEL_KEYWORDS): return True
    if TUNNEL_PAIR[0] in t and TUNNEL_PAIR[1] in t: return True
    return False


def main():
    now = datetime.now(timezone.utc)
    now_cest_hour = (now.hour + 2) % 24  # CEST = UTC+2

    if not API_KEY:
        print("WARNING: no API key — writing empty result")
        result = empty_result(now)
    else:
        result = fetch_and_parse(now, now_cest_hour)

    with open("traffic.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    t = result["tunnel"]
    print(f"Done: {result['total_incidents']} A2 incidents "
          f"({result['tunnel_incidents']} tunnel)")
    print(f"  Status: {t['status']}")
    print(f"  Tunnel N: sev={t['north']['severity']}")
    print(f"  Tunnel S: sev={t['south']['severity']}")


def empty_result(now):
    return {
        "updated": now.isoformat(),
        "source":  "ASTRA / opentransportdata.swiss · DATEX II · A2 Gotthard",
        "total_incidents": 0,
        "tunnel_incidents": 0,
        "tunnel": {
            "status": "unknown",
            "north": {"severity":"unknown","delay_min":None,"queue_km":None,"text":None},
            "south": {"severity":"unknown","delay_min":None,"queue_km":None,"text":None},
        },
        "incidents_north": [],
        "incidents_south": [],
        "incidents_all":   [],
    }


def fetch_and_parse(now, now_cest_hour):
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "SOAPAction":    SOAP_ACTION,
        "Content-Type":  "text/xml; charset=utf-8",
    }
    try:
        r = requests.post(ENDPOINT, data=SOAP_BODY, headers=headers, timeout=25)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"HTTP error: {e}")
        return empty_result(now)

    with open("traffic_raw.xml", "wb") as f:
        f.write(r.content)

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        print(f"XML error: {e}")
        return empty_result(now)

    incidents = []
    tunnel_north_cands = []
    tunnel_south_cands = []

    for rec in root.iter(f"{{{NS_D2}}}situationRecord"):
        texts = list(iter_text(rec, "value", "locationDescription", "comment"))
        road  = road_number(rec)
        raw   = " | ".join(texts)
        combined = f"{road} {raw}"

        # Must be on A2 corridor
        if not any(k.lower() in combined.lower() for k in A2_KEYWORDS):
            continue

        night_only = is_night_only(combined)
        situation, next_date = clean_description(raw)

        # Determine actual severity (day/night aware)
        sev = severity_from_situation(situation, combined, night_only, now_cest_hour)

        # During daytime, skip pure-night incidents if they're just "info"
        # But keep them with "info" to show planned upcoming closures
        if sev == "info" and night_only and 5 <= now_cest_hour < 20:
            pass  # keep as info — upcoming night closure

        delay  = extract_delay(combined)
        queue  = extract_queue(combined)
        dirn   = detect_direction(combined)
        is_tun = is_tunnel_incident(road, combined)

        # Build clean display text
        display = situation
        if next_date:
            display = f"{situation} (nächste Sperre: {next_date})"
        if len(display) > 200:
            display = display[:197] + "…"

        inc = {
            "road":        road or "A2",
            "description": display,
            "direction":   dirn,
            "severity":    sev,
            "delay_min":   delay,
            "queue_km":    queue,
            "is_tunnel":   is_tun,
            "night_only":  night_only,
        }
        incidents.append(inc)

        if is_tun:
            if dirn in ("north", "both"): tunnel_north_cands.append(inc)
            if dirn in ("south", "both"): tunnel_south_cands.append(inc)

    # ── Portal summaries (exclude night-only during day) ──────
    def portal_summary(cands):
        # Filter out obviously stale/ancient records (dates before 2025)
        def not_stale(inc):
            d_m = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', inc["description"])
            if d_m and int(d_m.group(3)) < 2025:
                return False
            return True
        active = [c for c in cands
                  if not (c["night_only"] and 5 <= now_cest_hour < 20)
                  and not_stale(c)]
        if not active:
            # Check for upcoming night closures
            upcoming = [c for c in cands if c["night_only"] and c["severity"] == "info"]
            if upcoming:
                u = upcoming[0]
                return {"severity":"info","delay_min":None,"queue_km":None,
                        "text": u["description"]}
            return {"severity":"clear","delay_min":None,"queue_km":None,"text":None}
        sev_order = {"critical":0,"high":1,"medium":2,"info":3}
        worst = sorted(active, key=lambda x: sev_order.get(x["severity"],4))[0]
        return {
            "severity":  worst["severity"],
            "delay_min": worst["delay_min"],
            "queue_km":  worst["queue_km"],
            "text":      worst["description"][:150],
        }

    tnorth = portal_summary(tunnel_north_cands)
    tsouth = portal_summary(tunnel_south_cands)

    sev_order = ["critical","high","medium","info","clear","unknown"]
    worst_sev = min([tnorth["severity"], tsouth["severity"]],
                    key=lambda s: sev_order.index(s) if s in sev_order else 9)
    tunnel_status = "clear" if worst_sev == "clear" else worst_sev

    incidents.sort(key=lambda x: {"critical":0,"high":1,"medium":2,"info":3}.get(x["severity"],4))

    return {
        "updated":          now.isoformat(),
        "source":           "ASTRA / opentransportdata.swiss · DATEX II · A2 Gotthard",
        "total_incidents":  len(incidents),
        "tunnel_incidents": len(set(id(i) for i in tunnel_north_cands + tunnel_south_cands)),
        "tunnel": {
            "status": tunnel_status,
            "north": tnorth,
            "south": tsouth,
        },
        "incidents_north": [i for i in incidents if i["direction"] in ("north","both")],
        "incidents_south": [i for i in incidents if i["direction"] in ("south","both")],
        "incidents_all":   incidents,
    }


if __name__ == "__main__":
    main()
