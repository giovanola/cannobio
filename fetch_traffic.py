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

# ── Tunnel-specific keywords (only Göschenen ↔ Airolo on A2) ──
TUNNEL_KEYWORDS   = ["Gotthard-Strassentunnel", "Gotthard-Tunnel",
                     "Gotthardtunnel", "Gotthard Tunnel"]
TUNNEL_PAIR       = ("göschenen", "airolo")   # both must appear → tunnel incident

# ── A2 corridor but NOT the tunnel (for general A2 list) ──
A2_KEYWORDS = ["A2 ", "N2 ", "Gotthard", "Göschenen", "Airolo", "Amsteg",
               "Erstfeld", "Altdorf", "Flüelen", "Brunnen", "Schwyz",
               "Bellinzona", "Lugano", "Chiasso", "Basel", "Luzern",
               "Liestal", "Kaiseraugst"]

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


def detect_direction(text):
    t = text.lower()
    s = any(k in t for k in ["→ airolo","richtung airolo","richtung süd","richtung tessin",
                               "richtung lugano","richtung chiasso","verso sud","southbound"])
    n = any(k in t for k in ["→ göschenen","richtung göschenen","richtung nord","richtung basel",
                               "richtung zürich","richtung luzern","verso nord","northbound"])
    if s and not n: return "south"
    if n and not s: return "north"
    return "both"


def severity(text):
    t = text.lower()
    if "aufgehoben" in t:   return "resolved"
    if any(k in t for k in ["gesperrt","sperre","closed","chiuso"]): return "critical"
    if any(k in t for k in ["stau","coda","congestion","km stau"]):  return "high"
    if any(k in t for k in ["stockend","verlangsamt","slow","lavori"]): return "medium"
    return "info"


def extract_delay(text):
    m = re.search(r"(\d{1,3})\s*(?:minuten|min)", text, re.I)
    return int(m.group(1)) if m else None


def extract_queue(text):
    m = re.search(r"(\d{1,2}(?:[.,]\d)?)\s*km", text, re.I)
    return float(m.group(1).replace(",", ".")) if m else None


def is_tunnel_incident(text):
    t = text.lower()
    if any(k.lower() in t for k in TUNNEL_KEYWORDS): return True
    return TUNNEL_PAIR[0] in t and TUNNEL_PAIR[1] in t


def is_side_road(text):
    """Göschenenalp, Sustenpass etc. — not the main tunnel"""
    t = text.lower()
    return any(k in t for k in ["göschenenalp","sustenpass","oberalp","furka","grimsel"])


def main():
    now = datetime.now(timezone.utc)
    print(f"Fetching at {now.isoformat()} ...")

    if not API_KEY:
        print("WARNING: no API key — writing empty result")
        result = empty_result(now)
    else:
        result = fetch_and_parse(now)

    with open("traffic.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    t = result["tunnel"]
    print(f"Done: {result['total_incidents']} A2 incidents "
          f"({result['tunnel_incidents']} tunnel-specific)")
    print(f"  Tunnel N: sev={t['north']['severity']}  delay={t['north']['delay_min']} min")
    print(f"  Tunnel S: sev={t['south']['severity']}  delay={t['south']['delay_min']} min")


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


def fetch_and_parse(now):
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
    tunnel_north_candidates = []
    tunnel_south_candidates = []

    for rec in root.iter(f"{{{NS_D2}}}situationRecord"):
        texts = list(iter_text(rec, "value", "locationDescription", "comment"))
        road  = road_number(rec)
        full  = " | ".join(texts)
        combined = f"{road} {full}"

        # Must be on A2 corridor
        if not any(k.lower() in combined.lower() for k in A2_KEYWORDS):
            continue

        # Skip "Aufgehoben" (revoked incidents)
        if "aufgehoben" in combined.lower():
            continue

        sev   = severity(combined)
        if sev == "resolved":
            continue

        delay  = extract_delay(combined)
        queue  = extract_queue(combined)
        dirn   = detect_direction(combined)
        is_tun = is_tunnel_incident(combined) and not is_side_road(combined)
        desc   = texts[0][:300] if texts else combined[:300]

        inc = {
            "road":        road or "A2",
            "description": desc,
            "direction":   dirn,
            "severity":    sev,
            "delay_min":   delay,
            "queue_km":    queue,
            "is_tunnel":   is_tun,
        }
        incidents.append(inc)

        if is_tun:
            if dirn in ("north", "both"): tunnel_north_candidates.append(inc)
            if dirn in ("south", "both"): tunnel_south_candidates.append(inc)

    # ── Derive overall tunnel status ──────────────────────
    def portal_summary(candidates):
        if not candidates:
            return {"severity": "clear", "delay_min": None, "queue_km": None, "text": None}
        worst = sorted(candidates, key=lambda x: {"critical":0,"high":1,"medium":2,"info":3}.get(x["severity"],4))[0]
        return {
            "severity":  worst["severity"],
            "delay_min": worst["delay_min"],
            "queue_km":  worst["queue_km"],
            "text":      worst["description"][:150],
        }

    tnorth = portal_summary(tunnel_north_candidates)
    tsouth = portal_summary(tunnel_south_candidates)

    # Overall tunnel status
    sev_order = ["critical","high","medium","info","clear","unknown"]
    worst_both = min([tnorth["severity"], tsouth["severity"]],
                     key=lambda s: sev_order.index(s) if s in sev_order else 9)
    tunnel_status = "clear" if worst_both == "clear" else worst_both

    # Sort incidents: critical first, then by direction
    sev_key = lambda x: {"critical":0,"high":1,"medium":2,"info":3}.get(x["severity"],4)
    incidents.sort(key=sev_key)

    return {
        "updated":          now.isoformat(),
        "source":           "ASTRA / opentransportdata.swiss · DATEX II · A2 Gotthard",
        "total_incidents":  len(incidents),
        "tunnel_incidents": len(tunnel_north_candidates) + len(tunnel_south_candidates),
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
