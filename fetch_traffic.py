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
        # Filter stale records: skip incidents whose described date is in the past
        today = datetime.now(timezone.utc)
        def not_stale(inc):
            desc = inc["description"]
            # Parse any date in the description (e.g. "nächste Sperre: 11.05.2026")
            d_m = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', desc)
            if d_m:
                try:
                    inc_date = datetime(int(d_m.group(3)), int(d_m.group(2)), int(d_m.group(1)),
                                        tzinfo=timezone.utc)
                    # If the incident date is more than 1 day in the past, skip it
                    if (today - inc_date).days > 1:
                        return False
                except ValueError:
                    pass
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

    # Global stale filter: remove incidents with past dates from the main list
    today = datetime.now(timezone.utc)
    def globally_not_stale(inc):
        d_m = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', inc["description"])
        if d_m:
            try:
                inc_date = datetime(int(d_m.group(3)), int(d_m.group(2)), int(d_m.group(1)),
                                    tzinfo=timezone.utc)
                if (today - inc_date).days > 1:
                    return False
            except ValueError:
                pass
        return True
    incidents = [i for i in incidents if globally_not_stale(i)]
    incidents.sort(key=lambda x: {"critical":0,"high":1,"medium":2,"info":3}.get(x["severity"],4))

    # ── Traffic counter data (real-time queue/speed) ─────────────
    counter_data = {}
    if COUNTER_KEY:
        north_msr, south_msr = auto_discover_msrs()
        if north_msr and south_msr:
            counter_data = fetch_counter_data([north_msr, south_msr])
        # Enrich portal summaries with sensor-derived queue/wait
        for portal, msr_id in [("north", north_msr or ""), ("south", south_msr or "")]:
            cd = counter_data.get(msr_id, {})
            if cd:
                q_m, w_min = derive_queue(cd.get("speed_kmh"), cd.get("flow_per_min"))
                summary = tnorth if portal == "north" else tsouth
                if q_m is not None:
                    summary["queue_m"]    = q_m
                    summary["wait_min"]   = w_min
                    summary["speed_kmh"]  = round(cd["speed_kmh"], 1)
                    # Override delay_min from sensor data if more accurate
                    if w_min is not None:
                        summary["delay_min"] = w_min if w_min > 0 else None

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


# ══════════════════════════════════════════════════════════════════
# TRAFFIC COUNTER MODULE — adds queue_m + wait_min to tunnel portals
# Requires separate TRAFFIC_COUNTER_KEY from api-manager.opentransportdata.swiss
# MSR IDs: run find_gotthard_counters.py once to verify
# ══════════════════════════════════════════════════════════════════

COUNTER_KEY      = os.environ.get("TRAFFIC_COUNTER_KEY", "")
COUNTER_ENDPOINT = "https://api.opentransportdata.swiss/TDP/Soap_Datex2/Pull"
COUNTER_ACTION   = "http://opentransportdata.swiss/TDP/Soap_Datex2/Pull/v1/pullMeasuredData"

# Gotthard portal coordinates for auto-discovery
PORTAL_NORTH = (46.6655, 8.5855)   # Göschenen
PORTAL_SOUTH = (46.5277, 8.6175)   # Airolo
SITES_CACHE  = "counter_sites.json"  # saved in repo root

def auto_discover_msrs():
    """Find nearest traffic counter IDs to Gotthard portals. Cached in counter_sites.json."""
    import math, json as _json, os
    NS = "http://datex2.eu/schema/2/2_0"

    # Load from cache if available
    if os.path.exists(SITES_CACHE):
        with open(SITES_CACHE) as f:
            cached = _json.load(f)
        print(f"Counter sites loaded from cache: north={cached['north_msr']} south={cached['south_msr']}")
        return cached['north_msr'], cached['south_msr']

    print("Auto-discovering Gotthard counter IDs...")
    headers = {
        "Authorization": f"Bearer {COUNTER_KEY}",
        "SOAPAction": "http://opentransportdata.swiss/TDP/Soap_Datex2/Pull/v1/pullMeasurementSiteTable",
        "Content-Type": "text/xml; charset=utf-8",
    }
    try:
        r = requests.post(COUNTER_ENDPOINT, data=BODY_MST_REQ, headers=headers, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"MST discovery failed: {e}")
        return None, None

    def haversine(lat1, lon1, lat2, lon2):
        R = 6371000
        dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    sites = []
    for msr in root.iter(f"{{{NS}}}measurementSiteRecord"):
        msr_id = msr.get("id","")
        lat_el = msr.find(f".//{{{NS}}}latitude")
        lon_el = msr.find(f".//{{{NS}}}longitude")
        if lat_el is None or lon_el is None: continue
        try:
            sites.append((msr_id, float(lat_el.text), float(lon_el.text)))
        except: pass

    def nearest(portal_lat, portal_lon):
        dists = [(s[0], haversine(portal_lat, portal_lon, s[1], s[2])) for s in sites]
        dists.sort(key=lambda x: x[1])
        # Skip H-road detectors (they're on pass roads, not A2)
        for sid, dist in dists[:20]:
            if dist > 3000: break  # >3km = not at the portal
            print(f"  Candidate: {sid} @ {dist:.0f}m")
            return sid
        return dists[0][0] if dists else None

    print(f"
Total MSR sites: {len(sites)}")
    print("Finding nearest to Göschenen (north):")
    north_msr = nearest(*PORTAL_NORTH)
    print(f"→ North MSR: {north_msr}")
    print("Finding nearest to Airolo (south):")
    south_msr = nearest(*PORTAL_SOUTH)
    print(f"→ South MSR: {south_msr}")

    # Save to cache
    with open(SITES_CACHE, "w") as f:
        _json.dump({"north_msr": north_msr, "south_msr": south_msr,
                    "total_sites": len(sites)}, f, indent=2)
    print(f"Saved to {SITES_CACHE}")
    return north_msr, south_msr


BODY_MST_REQ = b"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
<soap:Body>
  <d2LogicalModel modelBaseVersion="2" xmlns="http://datex2.eu/schema/2/2_0">
    <exchange><supplierIdentification>
      <country>ch</country>
      <nationalIdentifier>cannobio-gotthard</nationalIdentifier>
    </supplierIdentification></exchange>
  </d2LogicalModel>
</soap:Body>
</soap:Envelope>"""

FREE_FLOW_SPEED = 80   # km/h — Gotthard tunnel speed limit
VEHICLE_GAP_M   = 10   # metres per vehicle (approx. at low speed)


def counter_soap_body(msr_ids):
    """Build SOAP request for specific MSR IDs."""
    filters = "\n".join(
        f'    <dx223:siteRequestReference xsi:type="dx223:_MeasurementSiteRecordVersionedReference"'
        f' targetClass="MeasurementSiteRecord" id="{m}" version="0"/>'
        for m in msr_ids
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
<soap:Body>
  <d2LogicalModel modelBaseVersion="2"
      xmlns="http://datex2.eu/schema/2/2_0"
      xmlns:dx223="http://datex2.eu/schema/2/2_0"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
    <exchange>
      <supplierIdentification>
        <country>ch</country>
        <nationalIdentifier>cannobio-gotthard</nationalIdentifier>
      </supplierIdentification>
    </exchange>
    <payloadPublication xsi:type="dx223:ElaboratedDataPublication">
      <dx223:measuredDataFilter xsi:type="dx223:MeasuredDataFilter">
        <dx223:measurementSiteTableReference xsi:type="dx223:_MeasurementSiteTableVersionedReference"
          targetClass="MeasurementSiteTable" id="OTD:TrafficData" version="0"/>
{filters}
      </dx223:measuredDataFilter>
    </dx223:payloadPublication>
  </d2LogicalModel>
</soap:Body>
</soap:Envelope>""".encode("utf-8")


def fetch_counter_data(msr_ids):
    """Fetch speed + volume from traffic counters. Returns dict {msr_id: {speed, volume}}."""
    if not COUNTER_KEY:
        return {}
    NS = "http://datex2.eu/schema/2/2_0"
    headers = {
        "Authorization": f"Bearer {COUNTER_KEY}",
        "SOAPAction":    COUNTER_ACTION,
        "Content-Type":  "text/xml; charset=utf-8",
    }
    try:
        r = requests.post(COUNTER_ENDPOINT, data=counter_soap_body(msr_ids),
                          headers=headers, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"Counter API error: {e}")
        return {}

    try:
        root = ET.fromstring(r.content)
    except Exception:
        return {}

    results = {}
    for msr_data in root.iter(f"{{{NS}}}measuredValue"):
        # Find parent MSR reference
        parent = msr_data
        msr_id = None
        for ancestor in root.iter():
            if f"{{{NS}}}measurementSiteReference" in [c.tag for c in ancestor]:
                ref = ancestor.find(f"{{{NS}}}measurementSiteReference")
                if ref is not None:
                    msr_id = ref.get("id","")

        speed_el  = msr_data.find(f".//{{{NS}}}speed")
        count_el  = msr_data.find(f".//{{{NS}}}vehicleFlowRate")
        if msr_id and speed_el is not None:
            try:
                results[msr_id] = {
                    "speed_kmh":  float(speed_el.text),
                    "flow_per_min": float(count_el.text)/60 if count_el is not None else None,
                }
            except (TypeError, ValueError):
                pass
    return results


def derive_queue(speed_kmh, flow_per_min):
    """
    Derive approximate queue length (m) and wait time (min) from sensor data.
    Uses speed-flow relationship: when speed < free-flow, vehicles are queuing.
    """
    if speed_kmh is None or speed_kmh <= 0:
        return None, None

    if speed_kmh >= FREE_FLOW_SPEED * 0.85:
        # Free flow — no queue
        return 0, 0

    # Speed reduction ratio
    congestion_ratio = 1 - (speed_kmh / FREE_FLOW_SPEED)

    # Estimate queue: the lower the speed, the more vehicles per km
    # At 10 km/h: ~100 vehicles/km; at 40 km/h: ~50 vehicles/km
    density_veh_per_km = 1000 / max(speed_kmh, 5) * 0.8

    # Queue length: proportional to congestion and flow
    if flow_per_min:
        # Vehicles accumulating = (arrival rate) - (discharge rate through tunnel)
        # Tunnel discharge rate ≈ 25 vehicles/min at 80 km/h in 1-lane
        discharge = 25
        net_accumulation = max(0, flow_per_min - discharge)
        # Queue grows at net_accumulation per minute; estimate over 10-minute horizon
        queue_veh = net_accumulation * 10
        queue_m   = int(queue_veh * VEHICLE_GAP_M)
    else:
        # Fallback: speed-based estimate
        queue_m = int(congestion_ratio * 500)  # rough estimate

    # Wait time: distance / speed
    wait_min = round((queue_m / 1000) / max(speed_kmh / 60, 0.1)) if queue_m > 0 else 0

    return queue_m, wait_min
