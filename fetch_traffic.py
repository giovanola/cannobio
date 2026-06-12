#!/usr/bin/env python3
"""fetch_traffic.py — A2 Gotthard · ASTRA DATEX II · cannobio repo"""

import os, sys, json, re
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

API_KEY     = os.environ.get("OPENTRANSPORT_API_KEY", "")
ENDPOINT    = "https://api.opentransportdata.swiss/TDP/Soap_Datex2/TrafficSituations/Pull"
SOAP_ACTION = "http://opentransportdata.swiss/TDP/Soap_Datex2/Pull/v1/pullTrafficMessages"
NS          = "http://datex2.eu/schema/2/2_0"

SOAP_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
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

A2_KEYWORDS = ["A2 ","N2 ","Gotthard","Göschenen","Airolo",
               "Amsteg","Erstfeld","Altdorf","Flüelen","Bellinzona","Lugano","Chiasso"]
TUNNEL_KEYWORDS = ["Gotthard-Strassentunnel","Gotthard-Tunnel","Gotthardtunnel"]

def iter_text(el, *tags):
    for t in tags:
        for e in el.iter(f"{{{NS}}}{t}"):
            if e.text and e.text.strip():
                yield e.text.strip()

def road_num(rec):
    return ", ".join(dict.fromkeys(
        e.text.strip() for e in rec.iter(f"{{{NS}}}roadNumber") if e.text))

def clean_desc(raw):
    t = re.sub(r'^Freigegeben:\s*[^\n]+?(?=Sachlage:|$)', '', raw).strip()
    m = re.search(r'Sachlage:\s*(.+?)(?:Baustelle|Dauer:|$)', t, re.I|re.DOTALL)
    sit = m.group(1).strip().rstrip(',') if m else t
    dm  = re.search(r'voraussichtlich\s+(\d{1,2}\.\d{2}\.\d{4})', raw)
    return (sit.strip() + (f" (ab {dm.group(1)})" if dm else ""))[:200]

def is_stale(desc, now):
    dm = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', desc)
    if dm:
        try:
            d = datetime(int(dm.group(3)),int(dm.group(2)),int(dm.group(1)),tzinfo=timezone.utc)
            return (now - d).days > 1
        except: pass
    return False

def is_night_only(text):
    return bool(re.search(r'Dauer:\s*nachts', text, re.I))

def is_side_road(road, text):
    t = text.lower()
    if road and re.match(r'^H\d', road.strip()): return True
    return any(k in t for k in ['göschenenalp','sustenpass','oberalp','furka','grimsel',
                                  'andermatt','hospental','airolo-nufenen'])

def severity(sit, raw, night, cest_h):
    if night and 5 <= cest_h < 20: return "info"
    # Future events ("ab DD.MM.YYYY" or "voraussichtlich DD.MM.YYYY") → info
    dm = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', sit)
    if dm:
        try:
            from datetime import datetime as _dt, timezone as _tz
            ev_date = _dt(int(dm.group(3)),int(dm.group(2)),int(dm.group(1)),tzinfo=_tz.utc)
            if ev_date > _dt.now(_tz.utc): return "info"
        except: pass
    t = sit.lower()
    if any(k in t for k in ['gesperrt','sperre','closed']): return "critical"
    if any(k in raw.lower() for k in ['stau','coda','congestion']): return "high"
    if any(k in t for k in ['stockend','verlangsamt','behinderung']): return "medium"
    return "info"

def direction(text):
    t = text.lower()
    s = any(k in t for k in ['airolo','richtung süd','richtung tessin','richtung lugano'])
    n = any(k in t for k in ['göschenen','richtung nord','richtung basel','richtung zürich'])
    if s and not n: return "south"
    if n and not s: return "north"
    return "both"

def is_tunnel(road, text):
    if is_side_road(road, text): return False
    t = text.lower()
    if any(k.lower() in t for k in TUNNEL_KEYWORDS): return True
    return 'göschenen' in t and 'airolo' in t

def empty(now):
    return {"updated":now.isoformat(),"source":"ASTRA/opentransportdata.swiss · DATEX II · A2",
            "total_incidents":0,"tunnel_incidents":0,
            "tunnel":{"status":"unknown",
                      "north":{"severity":"unknown","delay_min":None,"queue_km":None,"text":None},
                      "south":{"severity":"unknown","delay_min":None,"queue_km":None,"text":None}},
            "incidents_north":[],"incidents_south":[],"incidents_all":[]}

def portal_summary(cands, cest_h, now):
    active = [c for c in cands
              if not (is_stale(c["description"], now))
              and not (c["night_only"] and 5 <= cest_h < 20)]
    if not active:
        upcoming = [c for c in cands if c["night_only"] and not is_stale(c["description"],now)]
        if upcoming:
            return {"severity":"info","delay_min":None,"queue_km":None,"text":upcoming[0]["description"]}
        return {"severity":"clear","delay_min":None,"queue_km":None,"text":None}
    sev_ord = {"critical":0,"high":1,"medium":2,"info":3}
    w = sorted(active, key=lambda x: sev_ord.get(x["severity"],4))[0]
    return {"severity":w["severity"],"delay_min":w["delay_min"],"queue_km":w["queue_km"],"text":w["description"][:150]}

def main():
    now     = datetime.now(timezone.utc)
    cest_h  = (now.hour + 2) % 24

    if not API_KEY:
        print("WARNING: OPENTRANSPORT_API_KEY not set")
        result = empty(now)
        with open("traffic.json","w") as f: json.dump(result,f,ensure_ascii=False,indent=2)
        return

    headers = {"Authorization":f"Bearer {API_KEY}","SOAPAction":SOAP_ACTION,
               "Content-Type":"text/xml; charset=utf-8"}
    try:
        r = requests.post(ENDPOINT, data=SOAP_BODY, headers=headers, timeout=25)
        r.raise_for_status()
    except Exception as e:
        print(f"API error: {e}")
        result = empty(now)
        with open("traffic.json","w") as f: json.dump(result,f,ensure_ascii=False,indent=2)
        return

    try:
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"XML parse error: {e}")
        result = empty(now)
        with open("traffic.json","w") as f: json.dump(result,f,ensure_ascii=False,indent=2)
        return

    incidents = []
    t_north, t_south = [], []

    for rec in root.iter(f"{{{NS}}}situationRecord"):
        texts = list(iter_text(rec,"value","locationDescription","comment"))
        road  = road_num(rec)
        raw   = " | ".join(texts)
        combined = f"{road} {raw}"

        if not any(k.lower() in combined.lower() for k in A2_KEYWORDS): continue

        desc    = clean_desc(raw)
        night   = is_night_only(combined)
        stale   = is_stale(desc, now)
        if stale and not night: continue  # keep upcoming night closures

        sev  = severity(desc, combined, night, cest_h)
        dirn = direction(combined)
        tun  = is_tunnel(road, combined)

        delay_m = re.search(r'(\d{1,3})\s*min', combined, re.I)
        queue_m = re.search(r'(\d{1,2}(?:[.,]\d)?)\s*km', combined, re.I)

        inc = {"road":road or "A2","description":desc,"direction":dirn,"severity":sev,
               "delay_min":int(delay_m.group(1)) if delay_m else None,
               "queue_km":float(queue_m.group(1).replace(",",".")) if queue_m else None,
               "is_tunnel":tun,"night_only":night}
        incidents.append(inc)
        if tun:
            if dirn in ("north","both"): t_north.append(inc)
            if dirn in ("south","both"): t_south.append(inc)

    incidents.sort(key=lambda x: {"critical":0,"high":1,"medium":2,"info":3}.get(x["severity"],4))

    tn = portal_summary(t_north, cest_h, now)
    ts = portal_summary(t_south, cest_h, now)
    sev_ord = ["critical","high","medium","info","clear","unknown"]
    worst = min([tn["severity"],ts["severity"]], key=lambda s: sev_ord.index(s) if s in sev_ord else 9)
    status = "clear" if worst == "clear" else worst

    result = {"updated":now.isoformat(),
              "source":"ASTRA/opentransportdata.swiss · DATEX II · A2 Gotthard",
              "total_incidents":len(incidents),
              "tunnel_incidents":len(set(id(i) for i in t_north+t_south)),
              "tunnel":{"status":status,"north":tn,"south":ts},
              "incidents_north":[i for i in incidents if i["direction"] in ("north","both")],
              "incidents_south":[i for i in incidents if i["direction"] in ("south","both")],
              "incidents_all":incidents}

    # Enrich with Traffic Counter sensor data (real sensor queue/wait)
    enrich_with_counter_data(result)

    # Also commit counter_sites.json if discovered
    if os.path.exists(SITES_CACHE_FILE):
        pass  # already written by enrich_with_counter_data

    with open("traffic.json","w",encoding="utf-8") as f:
        json.dump(result,f,ensure_ascii=False,indent=2)

    print(f"Done: {len(incidents)} incidents | tunnel={status} | N={tn['severity']} S={ts['severity']}")

if __name__ == "__main__":
    main()


# ── Traffic Counter API (FEDRO) ─────────────────────────────────────────────
# Provides real sensor data: vehicle count + speed → derive queue_m + delay_min
# Endpoint: https://api.opentransportdata.swiss/TDP/Soap_Datex2/Pull
# Requires TRAFFIC_COUNTER_KEY from api-manager.opentransportdata.swiss

COUNTER_KEY      = os.environ.get("TRAFFIC_COUNTER_KEY", "")
COUNTER_ENDPOINT = "https://api.opentransportdata.swiss/TDP/Soap_Datex2/Pull"
COUNTER_ACTION   = "http://opentransportdata.swiss/TDP/Soap_Datex2/Pull/v1/pullMeasuredData"
COUNTER_MST      = "http://opentransportdata.swiss/TDP/Soap_Datex2/Pull/v1/pullMeasurementSiteTable"
SITES_CACHE      = "counter_sites.json"

# Gotthard portal coordinates
PORTAL_NORTH = (46.6655, 8.5855)   # Göschenen
PORTAL_SOUTH = (46.5277, 8.6175)   # Airolo

COUNTER_NS = "http://datex2.eu/schema/2/2_0"

MST_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <d2LogicalModel modelBaseVersion="2" xmlns="http://datex2.eu/schema/2/2_0">
      <exchange><supplierIdentification>
        <country>ch</country><nationalIdentifier>cannobio</nationalIdentifier>
      </supplierIdentification></exchange>
    </d2LogicalModel>
  </soap:Body>
</soap:Envelope>"""


def haversine_m(lat1, lon1, lat2, lon2):
    import math
    R = 6371000
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def discover_gotthard_msrs():
    """Find nearest MSR IDs to Gotthard portals. Cached in counter_sites.json."""
    import json as _json
    if os.path.exists(SITES_CACHE):
        with open(SITES_CACHE) as f:
            d = _json.load(f)
        if d.get("north_msr") and d.get("south_msr"):
            return d["north_msr"], d["south_msr"]

    print("Discovering Gotthard counter IDs from MST...")
    hdrs = {
        "Authorization": f"Bearer {COUNTER_KEY}",
        "SOAPAction":    COUNTER_MST,
        "Content-Type":  "text/xml; charset=utf-8",
    }
    try:
        r = requests.post(COUNTER_ENDPOINT, data=MST_BODY, headers=hdrs, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"MST error: {e}")
        return None, None

    sites = []
    for msr in root.iter(f"{{{COUNTER_NS}}}measurementSiteRecord"):
        mid  = msr.get("id", "")
        lat  = msr.find(f".//{{{COUNTER_NS}}}latitude")
        lon  = msr.find(f".//{{{COUNTER_NS}}}longitude")
        if lat is None or lon is None: continue
        try:
            sites.append((mid, float(lat.text), float(lon.text)))
        except: pass

    print(f"Found {len(sites)} MSR sites")

    def nearest_msr(plat, plon, max_dist_m=2000):
        dists = [(s[0], haversine_m(plat, plon, s[1], s[2])) for s in sites]
        dists.sort(key=lambda x: x[1])
        for sid, dist in dists[:5]:
            print(f"  {dist:6.0f}m  {sid}")
        if dists and dists[0][1] <= max_dist_m:
            return dists[0][0]
        return None

    print(f"Nearest to Göschenen (north portal {PORTAL_NORTH}):")
    north = nearest_msr(*PORTAL_NORTH)
    print(f"Nearest to Airolo (south portal {PORTAL_SOUTH}):")
    south = nearest_msr(*PORTAL_SOUTH)

    import json as _json2
    with open(SITES_CACHE, "w") as f:
        _json2.dump({"north_msr": north, "south_msr": south, "total": len(sites)}, f, indent=2)
    print(f"Saved to {SITES_CACHE}: north={north} south={south}")
    return north, south


def counter_soap_body(msr_ids):
    filters = "".join(
        f'      <dx223:siteRequestReference xsi:type="dx223:_MeasurementSiteRecordVersionedReference" '
        f'targetClass="MeasurementSiteRecord" id="{m}" version="0"/>\n'
        for m in msr_ids
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <d2LogicalModel modelBaseVersion="2"
        xmlns="http://datex2.eu/schema/2/2_0"
        xmlns:dx223="http://datex2.eu/schema/2/2_0"
        xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <exchange><supplierIdentification>
        <country>ch</country><nationalIdentifier>cannobio</nationalIdentifier>
      </supplierIdentification></exchange>
      <payloadPublication xsi:type="dx223:ElaboratedDataPublication">
        <dx223:measuredDataFilter xsi:type="dx223:MeasuredDataFilter">
          <dx223:measurementSiteTableReference xsi:type="dx223:_MeasurementSiteTableVersionedReference"
            targetClass="MeasurementSiteTable" id="OTD:TrafficData" version="0"/>
{filters}        </dx223:measuredDataFilter>
      </dx223:payloadPublication>
    </d2LogicalModel>
  </soap:Body>
</soap:Envelope>""".encode("utf-8")


def fetch_counter_readings(msr_ids):
    """Returns dict {msr_id: {speed_kmh, flow_per_min}} from FEDRO counters."""
    hdrs = {
        "Authorization": f"Bearer {COUNTER_KEY}",
        "SOAPAction":    COUNTER_ACTION,
        "Content-Type":  "text/xml; charset=utf-8",
    }
    try:
        r = requests.post(COUNTER_ENDPOINT, data=counter_soap_body(msr_ids),
                          headers=hdrs, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"Counter API error: {e}")
        return {}

    results = {}
    for pub in root.iter(f"{{{COUNTER_NS}}}siteMeasurements"):
        ref = pub.find(f".//{{{COUNTER_NS}}}measurementSiteReference")
        if ref is None: continue
        mid = ref.get("id", "")
        speed_vals = [float(e.text) for e in pub.iter(f"{{{COUNTER_NS}}}speed") if e.text]
        flow_vals  = [float(e.text) for e in pub.iter(f"{{{COUNTER_NS}}}vehicleFlowRate") if e.text]
        if speed_vals:
            results[mid] = {
                "speed_kmh":    sum(speed_vals) / len(speed_vals),
                "flow_per_min": sum(flow_vals) / len(flow_vals) / 60 if flow_vals else None,
            }
    return results


def derive_queue_from_sensor(speed_kmh, flow_per_min, free_flow_kmh=80):
    """Estimate queue_m and delay_min from loop detector data."""
    if speed_kmh is None or speed_kmh <= 0:
        return None, None
    if speed_kmh >= free_flow_kmh * 0.85:
        return 0, 0  # Free flow — no queue
    # Speed reduction indicates queuing upstream
    congestion_factor = 1 - (speed_kmh / free_flow_kmh)
    # Estimate queue length: each vehicle ~10m, density increases as speed drops
    if flow_per_min and flow_per_min > 0:
        # Net accumulation: inflow - tunnel throughput capacity
        throughput = min(flow_per_min, free_flow_kmh / 3.6 / 10)  # veh/min at free flow spacing
        net_accumulation = max(0, flow_per_min - throughput)
        queue_veh = net_accumulation * max(5, 60 * congestion_factor)  # vehicles in queue
        queue_m = int(queue_veh * 10)
    else:
        # Fallback speed-based estimate
        queue_m = int(congestion_factor * 800)
    delay_min = max(0, round(queue_m / 1000 / max(speed_kmh / 60, 0.5)))
    return min(queue_m, 20000), delay_min  # cap at 20km


def enrich_with_counter_data(result):
    """Add real sensor queue/delay to tunnel portals if TRAFFIC_COUNTER_KEY set."""
    if not COUNTER_KEY:
        return result
    north_msr, south_msr = discover_gotthard_msrs()
    if not north_msr or not south_msr:
        return result
    readings = fetch_counter_readings([north_msr, south_msr])
    if not readings:
        return result

    for side, msr_id in [("north", north_msr), ("south", south_msr)]:
        rd = readings.get(msr_id)
        if not rd: continue
        q_m, d_min = derive_queue_from_sensor(rd["speed_kmh"], rd["flow_per_min"])
        if q_m is not None:
            result["tunnel"][side]["queue_m"]   = q_m
            result["tunnel"][side]["delay_min"] = d_min if d_min > 0 else None
            result["tunnel"][side]["speed_kmh"] = round(rd["speed_kmh"], 1)
            print(f"Counter {side}: speed={rd['speed_kmh']:.1f} km/h queue={q_m}m delay={d_min}min")
    return result


# ════════════════════════════════════════════════════════════════
# TRAFFIC COUNTER MODULE  (appended to fetch_traffic.py)
# Source: opentransportdata.swiss · DATEX II · Traffic Counters
# Requires: TRAFFIC_COUNTER_KEY env var (separate from OPENTRANSPORT_API_KEY)
# ════════════════════════════════════════════════════════════════

import math as _math

COUNTER_KEY      = os.environ.get("TRAFFIC_COUNTER_KEY", "")
COUNTER_ENDPOINT = "https://api.opentransportdata.swiss/TDP/Soap_Datex2/Pull"
COUNTER_NS       = "http://datex2.eu/schema/2/2_0"
SITES_CACHE_FILE = "counter_sites.json"

# Gotthard portal coordinates
_PORTAL_N = (46.6655, 8.5855)   # Göschenen
_PORTAL_S = (46.5277, 8.6175)   # Airolo

# Max distance (m) to consider a sensor "at the portal"
_MAX_DIST_M = 5000

_MST_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
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


def _haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat = _math.radians(lat2 - lat1)
    dlon = _math.radians(lon2 - lon1)
    a = (_math.sin(dlat/2)**2
         + _math.cos(_math.radians(lat1)) * _math.cos(_math.radians(lat2))
         * _math.sin(dlon/2)**2)
    return R * 2 * _math.atan2(_math.sqrt(a), _math.sqrt(1-a))


def _discover_msrs():
    """Call pullMeasurementSiteTable, find nearest sensor IDs to portals."""
    headers = {
        "Authorization": f"Bearer {COUNTER_KEY}",
        "SOAPAction": "http://opentransportdata.swiss/TDP/Soap_Datex2/Pull/v1/pullMeasurementSiteTable",
        "Content-Type": "text/xml; charset=utf-8",
    }
    try:
        r = requests.post(COUNTER_ENDPOINT, data=_MST_BODY, headers=headers, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"Counter MST error: {e}")
        return None, None

    sites = []
    for msr in root.iter(f"{{{COUNTER_NS}}}measurementSiteRecord"):
        msr_id  = msr.get("id", "")
        lat_el  = msr.find(f".//{{{COUNTER_NS}}}latitude")
        lon_el  = msr.find(f".//{{{COUNTER_NS}}}longitude")
        name_el = msr.find(f".//{{{COUNTER_NS}}}measurementSiteName//{{{COUNTER_NS}}}value")
        if lat_el is None or lon_el is None:
            continue
        try:
            sites.append({
                "id": msr_id,
                "lat": float(lat_el.text),
                "lon": float(lon_el.text),
                "name": name_el.text.strip() if name_el is not None else "",
            })
        except (TypeError, ValueError):
            pass

    print(f"Counter MST: {len(sites)} sites found")

    def nearest(plat, plon):
        best_id, best_dist, best_name = None, float("inf"), ""
        for s in sites:
            d = _haversine(plat, plon, s["lat"], s["lon"])
            if d < best_dist:
                best_dist, best_id, best_name = d, s["id"], s["name"]
        if best_dist > _MAX_DIST_M:
            print(f"  Warning: nearest site {best_dist:.0f}m away (>{_MAX_DIST_M}m)")
        print(f"  Nearest: {best_id} '{best_name}' @ {best_dist:.0f}m")
        return best_id

    north_msr = nearest(*_PORTAL_N)
    south_msr = nearest(*_PORTAL_S)
    return north_msr, south_msr


def _load_counter_sites():
    """Load cached MSR IDs or discover them."""
    if os.path.exists(SITES_CACHE_FILE):
        try:
            with open(SITES_CACHE_FILE) as f:
                cached = json.load(f)
            n, s = cached.get("north_msr"), cached.get("south_msr")
            if n and s:
                print(f"Counter sites cached: north={n} south={s}")
                return n, s
        except Exception:
            pass

    # Discover
    n, s = _discover_msrs()
    if n and s:
        try:
            with open(SITES_CACHE_FILE, "w") as f:
                json.dump({"north_msr": n, "south_msr": s}, f, indent=2)
            print(f"Counter sites saved: {SITES_CACHE_FILE}")
        except Exception as e:
            print(f"Could not save counter sites: {e}")
    return n, s


def _fetch_counter_data(msr_ids):
    """Fetch speed + flow for given MSR IDs. Returns {id: {speed_kmh, flow_per_min}}."""
    filters = "\n".join(
        f'      <dx223:siteRequestReference xsi:type="dx223:_MeasurementSiteRecordVersionedReference"'
        f' targetClass="MeasurementSiteRecord" id="{m}" version="0"/>'
        for m in msr_ids if m
    )
    if not filters:
        return {}

    body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <d2LogicalModel modelBaseVersion="2"
        xmlns="http://datex2.eu/schema/2/2_0"
        xmlns:dx223="http://datex2.eu/schema/2/2_0"
        xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <exchange><supplierIdentification>
        <country>ch</country>
        <nationalIdentifier>cannobio-gotthard</nationalIdentifier>
      </supplierIdentification></exchange>
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

    headers = {
        "Authorization": f"Bearer {COUNTER_KEY}",
        "SOAPAction": "http://opentransportdata.swiss/TDP/Soap_Datex2/Pull/v1/pullMeasuredData",
        "Content-Type": "text/xml; charset=utf-8",
    }
    try:
        r = requests.post(COUNTER_ENDPOINT, data=body, headers=headers, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"Counter data error: {e}")
        return {}

    NS = COUNTER_NS
    results = {}
    for pub in root.iter(f"{{{NS}}}elaboratedData"):
        ref_el = pub.find(f".//{{{NS}}}measurementSiteReference")
        if ref_el is None:
            continue
        msr_id = ref_el.get("id", "")
        speed_el = pub.find(f".//{{{NS}}}speed")
        flow_el  = pub.find(f".//{{{NS}}}vehicleFlowRate")
        if speed_el is not None:
            try:
                results[msr_id] = {
                    "speed_kmh":    float(speed_el.text),
                    "flow_per_min": float(flow_el.text) / 60 if flow_el is not None else None,
                }
            except (TypeError, ValueError):
                pass
    return results


def _derive_queue(speed_kmh, flow_per_min):
    """Estimate queue_m and wait_min from sensor data."""
    if speed_kmh is None or speed_kmh <= 0:
        return None, None

    FREE_FLOW = 80.0  # km/h — Gotthard limit
    if speed_kmh >= FREE_FLOW * 0.90:
        return 0, 0   # Free flow

    # Speed-based congestion estimate
    # Rule of thumb: vehicles/km = 1000 / speed (km/h) × density factor
    congestion_ratio = 1.0 - (speed_kmh / FREE_FLOW)

    if flow_per_min and flow_per_min > 0:
        # Tunnel capacity: ~25 vehicles/min at normal flow
        TUNNEL_CAP = 25.0
        # Net accumulation rate (vehicles queuing per minute)
        net_acc = max(0.0, flow_per_min - TUNNEL_CAP * (speed_kmh / FREE_FLOW))
        # Estimate queue over 10 min horizon
        queue_veh = net_acc * 10
        queue_m   = int(queue_veh * 10)   # ~10m per vehicle at low speed
    else:
        # Fallback: speed-only estimate
        queue_m = int(congestion_ratio * 1000)

    wait_min = max(1, round(queue_m / 1000 / max(speed_kmh / 60, 0.1))) if queue_m > 50 else 0
    return min(queue_m, 15000), min(wait_min, 180)


def enrich_with_counter_data(tunnel_data):
    """
    Enrich tunnel portal data with real sensor measurements.
    Called from main() after fetch_and_parse() if COUNTER_KEY is set.
    Modifies tunnel_data['tunnel']['north'] and ['south'] in place.
    """
    if not COUNTER_KEY:
        return

    north_msr, south_msr = _load_counter_sites()
    if not north_msr and not south_msr:
        print("Counter: no MSR IDs available")
        return

    sensor_data = _fetch_counter_data([m for m in [north_msr, south_msr] if m])

    for portal, msr_id in [("north", north_msr), ("south", south_msr)]:
        if not msr_id:
            continue
        sd = sensor_data.get(msr_id)
        if not sd:
            print(f"Counter: no data for {portal} ({msr_id})")
            continue

        q_m, w_min = _derive_queue(sd["speed_kmh"], sd.get("flow_per_min"))
        p = tunnel_data["tunnel"][portal]

        if q_m is not None:
            p["queue_m"]   = q_m
            p["wait_min"]  = w_min
            p["speed_kmh"] = round(sd["speed_kmh"], 1)
            # Override delay_min from sensor (more accurate than incident-based)
            if w_min is not None:
                p["delay_min"] = w_min if w_min > 0 else None
            print(f"Counter {portal}: {sd['speed_kmh']:.0f} km/h → {w_min} min | {q_m} m")
