#!/usr/bin/env python3
"""
fetch_traffic.py
================
Fetches real-time A2 traffic situations (Gotthard corridor, Basel–Chiasso)
from the ASTRA opentransportdata.swiss DATEX II SOAP API.

Writes two output files:
  traffic.json       — filtered A2 incidents (Gotthard focus)
  traffic_raw.xml    — raw DATEX II XML response (for debugging)

Usage (local):
  export OPENTRANSPORT_API_KEY="your-bearer-token"
  python3 fetch_traffic.py

GitHub Actions: set OPENTRANSPORT_API_KEY as repository secret.

API key: free registration at https://api-manager.opentransportdata.swiss/
  → "Traffic Situations" API → "Access with this plan"
"""

import os, sys, json, re, textwrap
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

try:
    import requests
except ImportError:
    print("Missing: pip install requests")
    sys.exit(1)

# ── CONFIG ──────────────────────────────────────────────────────
API_KEY   = os.environ.get("OPENTRANSPORT_API_KEY", "")
ENDPOINT  = "https://api.opentransportdata.swiss/TDP/Soap_Datex2/TrafficSituations/Pull"
SOAP_ACTION = "http://opentransportdata.swiss/TDP/Soap_Datex2/Pull/v1/pullTrafficMessages"

OUT_JSON  = "traffic.json"
OUT_XML   = "traffic_raw.xml"

# A2 / Gotthard corridor keywords (DE/FR/IT)
A2_KEYWORDS = [
    "A2", "N2", "Gotthard", "Gotthardtunnel", "Gotthard-Tunnel",
    "Göschenen", "Airolo", "Amsteg", "Erstfeld", "Flüelen", "Altdorf",
    "Brunnen", "Schwyz", "Luzern", "Bellinzona", "Lugano", "Mendrisio",
    "Chiasso", "Basel", "Birsfelden", "Liestal",
    "Tunnel du Saint-Gothard", "Galleria del San Gottardo",
]

DIRECTION_SOUTH = [
    "richtung süd", "richtung airolo", "richtung tessin", "richtung lugano",
    "richtung chiasso", "richtung mailand", "richtung bellinzona",
    "verso sud", "direzione sud", "direction sud", "southbound",
    "göschenen → airolo",
]
DIRECTION_NORTH = [
    "richtung nord", "richtung göschenen", "richtung basel",
    "richtung zürich", "richtung luzern",
    "verso nord", "direzione nord", "direction nord", "northbound",
    "airolo → göschenen",
]

# SOAP request body (minimal — pulls all current situations)
SOAP_BODY = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
      <soap:Body>
        <d2LogicalModel
          modelBaseVersion="2"
          xmlns="http://datex2.eu/schema/2/2_0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
          <exchange>
            <supplierIdentification>
              <country>ch</country>
              <nationalIdentifier>cannobio-gotthard-monitor</nationalIdentifier>
            </supplierIdentification>
          </exchange>
        </d2LogicalModel>
      </soap:Body>
    </soap:Envelope>
""").encode("utf-8")

NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
NS_D2   = "http://datex2.eu/schema/2/2_0"

# ── HELPERS ─────────────────────────────────────────────────────

def iter_texts(element, *tag_localnames):
    """Yield text content of descendant elements matching any of the local names."""
    for localname in tag_localnames:
        for el in element.iter(f"{{{NS_D2}}}{localname}"):
            if el.text and el.text.strip():
                yield el.text.strip()


def road_number(situation_record):
    roads = []
    for rn in situation_record.iter(f"{{{NS_D2}}}roadNumber"):
        if rn.text:
            roads.append(rn.text.strip())
    return ", ".join(dict.fromkeys(roads))  # deduplicated


def detect_direction(text: str) -> str:
    t = text.lower()
    is_s = any(k in t for k in DIRECTION_SOUTH)
    is_n = any(k in t for k in DIRECTION_NORTH)
    if is_s and not is_n:   return "south"
    if is_n and not is_s:   return "north"
    return "both"


def classify_severity(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ["gesperrt", "sperre", "closed", "chiuso", "fermé", "vollsperre"]):
        return "critical"
    if any(k in t for k in ["stau", "coda", "bouchon", "congestion", "km", "kilometer"]):
        return "high"
    if any(k in t for k in ["stockend", "verlangsamt", "slow", "rallentato", "baustelle", "chantier"]):
        return "medium"
    return "low"


def extract_delay_minutes(text: str):
    m = re.search(r"(\d{1,3})\s*(?:minuten|min(?:utes?)?|minute)", text, re.I)
    return int(m.group(1)) if m else None


def extract_queue_km(text: str):
    m = re.search(r"(\d{1,2}(?:[.,]\d)?)\s*km", text, re.I)
    return float(m.group(1).replace(",", ".")) if m else None


# ── FETCH ────────────────────────────────────────────────────────

def fetch_situations() -> list[dict]:
    if not API_KEY:
        print("WARNING: OPENTRANSPORT_API_KEY not set — writing empty result.")
        return []

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "SOAPAction":    SOAP_ACTION,
        "Content-Type":  "text/xml; charset=utf-8",
        "Accept":        "text/xml",
    }

    try:
        resp = requests.post(
            ENDPOINT,
            data=SOAP_BODY,
            headers=headers,
            timeout=25,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"HTTP error: {exc}")
        return []

    # Save raw XML for debugging
    with open(OUT_XML, "wb") as f:
        f.write(resp.content)
    print(f"Raw XML saved to {OUT_XML} ({len(resp.content):,} bytes)")

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        print(f"XML parse error: {exc}")
        return []

    situations = []

    for sit in root.iter(f"{{{NS_D2}}}situationRecord"):
        # Gather all text content from this record
        texts = list(iter_texts(sit,
            "value",                   # multilingual strings
            "locationDescription",
            "comment",
            "nonGeneralPublicComment",
        ))
        road = road_number(sit)
        full_text = " | ".join(texts)
        combined  = f"{road} {full_text}"

        # ── A2 corridor filter ────────────────────────────────
        if not any(kw.lower() in combined.lower() for kw in A2_KEYWORDS):
            continue

        # ── Extract structured fields ────────────────────────
        rec_id    = sit.get("id", "")
        direction = detect_direction(combined)
        severity  = classify_severity(combined)
        delay     = extract_delay_minutes(combined)
        queue_km  = extract_queue_km(combined)

        # Validity period
        valid_start = next(iter_texts(sit, "overallStartTime"), None)
        valid_end   = next(iter_texts(sit, "overallEndTime"),   None)

        # Short description (first value text, max 300 chars)
        description = texts[0][:300] if texts else combined[:300]

        situations.append({
            "id":          rec_id,
            "road":        road or "A2",
            "description": description,
            "full_text":   full_text[:500],
            "direction":   direction,
            "severity":    severity,
            "delay_min":   delay,
            "queue_km":    queue_km,
            "valid_from":  valid_start,
            "valid_to":    valid_end,
        })

    # Sort: critical first, then high, medium, low
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    situations.sort(key=lambda x: order.get(x["severity"], 4))
    return situations


# ── MAIN ─────────────────────────────────────────────────────────

def main():
    now_utc = datetime.now(timezone.utc)
    print(f"Fetching A2/Gotthard traffic data at {now_utc.isoformat()} ...")

    situations = fetch_situations()

    south  = [s for s in situations if s["direction"] in ("south", "both")]
    north  = [s for s in situations if s["direction"] in ("north", "both")]
    tunnel = [s for s in situations if any(k.lower() in (s["description"] + s["road"]).lower()
                                          for k in ["Gotthard", "Tunnel", "Göschenen", "Airolo"])]

    # ── Derive tunnel wait / queue summary ───────────────────
    tunnel_south = next((s for s in tunnel if s["direction"] in ("south","both")), None)
    tunnel_north = next((s for s in tunnel if s["direction"] in ("north","both")), None)

    output = {
        "updated":        now_utc.isoformat(),
        "source":         "ASTRA / opentransportdata.swiss · DATEX II · A2 Gotthard",
        "total_incidents": len(situations),
        # Tunnel-specific summary (for the portal cards)
        "tunnel": {
            "south": {
                "delay_min": tunnel_south["delay_min"] if tunnel_south else None,
                "queue_km":  tunnel_south["queue_km"]  if tunnel_south else None,
                "severity":  tunnel_south["severity"]  if tunnel_south else "unknown",
                "text":      tunnel_south["description"][:120] if tunnel_south else None,
            },
            "north": {
                "delay_min": tunnel_north["delay_min"] if tunnel_north else None,
                "queue_km":  tunnel_north["queue_km"]  if tunnel_north else None,
                "severity":  tunnel_north["severity"]  if tunnel_north else "unknown",
                "text":      tunnel_north["description"][:120] if tunnel_north else None,
            },
        },
        # Full filtered incident lists
        "south":   south,
        "north":   north,
        "all":     situations,
    }

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Written {OUT_JSON}: {len(south)} southbound, {len(north)} northbound incidents")
    print(f"Tunnel south: delay={output['tunnel']['south']['delay_min']} min, "
          f"queue={output['tunnel']['south']['queue_km']} km")
    print(f"Tunnel north: delay={output['tunnel']['north']['delay_min']} min, "
          f"queue={output['tunnel']['north']['queue_km']} km")

    if not situations:
        print("No A2 incidents found (API returned 0 matches, or no API key set).")


if __name__ == "__main__":
    main()
