#!/usr/bin/env python3
"""
fetch_traffic.py — A2 Gotthard · ASTRA DATEX II + Traffic Counters
Schreibt traffic.json in das aktuelle Verzeichnis (Repo-Root).
"""

import os, sys, json, re, math
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

# ─── ASTRA TrafficSituations API ──────────────────────────────────────────────
API_KEY  = os.environ.get("OPENTRANSPORT_API_KEY", "")
TS_URL   = "https://api.opentransportdata.swiss/TDP/Soap_Datex2/TrafficSituations/Pull"
TS_ACT   = "http://opentransportdata.swiss/TDP/Soap_Datex2/Pull/v1/pullTrafficMessages"
NS       = "http://datex2.eu/schema/2/2_0"

# ─── Traffic Counter API (FEDRO) ──────────────────────────────────────────────
CTR_KEY  = os.environ.get("TRAFFIC_COUNTER_KEY", "")
CTR_URL  = "https://api.opentransportdata.swiss/TDP/Soap_Datex2/Pull"
CTR_NS   = "http://datex2.eu/schema/2/2_0"
CTR_MST  = "http://opentransportdata.swiss/TDP/Soap_Datex2/Pull/v1/pullMeasurementSiteTable"
CTR_DATA = "http://opentransportdata.swiss/TDP/Soap_Datex2/Pull/v1/pullMeasuredData"
CTR_CACHE = "counter_sites.json"

# Gotthard portal coordinates
PORTAL_N = (46.6655, 8.5855)   # Göschenen Nord
PORTAL_S = (46.5277, 8.6175)   # Airolo Süd

SOAP_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <d2LogicalModel modelBaseVersion="2" xmlns="http://datex2.eu/schema/2/2_0">
      <exchange><supplierIdentification>
        <country>ch</country><nationalIdentifier>cannobio</nationalIdentifier>
      </supplierIdentification></exchange>
    </d2LogicalModel>
  </soap:Body>
</soap:Envelope>"""

A2_KW = ["A2 ","N2 ","Gotthard","Göschenen","Airolo","Amsteg","Erstfeld",
          "Altdorf","Flüelen","Bellinzona","Lugano","Chiasso"]
TUN_KW = ["Gotthard-Strassentunnel","Gotthard-Tunnel","Gotthardtunnel"]

# ─── Helpers ──────────────────────────────────────────────────────────────────
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
    return any(k in t for k in ['göschenenalp','sustenpass','oberalp','furka',
                                  'grimsel','andermatt','hospental','airolo-nufenen'])

def calc_severity(sit, raw, night, cest_h):
    if night and 5 <= cest_h < 20: return "info"
    dm = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', sit)
    if dm:
        try:
            ev = datetime(int(dm.group(3)),int(dm.group(2)),int(dm.group(1)),tzinfo=timezone.utc)
            if ev > datetime.now(timezone.utc): return "info"
        except: pass
    t = sit.lower()
    if any(k in t for k in ['gesperrt','sperre','closed']): return "critical"
    if any(k in raw.lower() for k in ['stau','coda','congestion']): return "high"
    if any(k in t for k in ['stockend','verlangsamt','behinderung']): return "medium"
    return "info"

def detect_dir(text):
    t = text.lower()
    s = any(k in t for k in ['airolo','richtung süd','richtung tessin'])
    n = any(k in t for k in ['göschenen','richtung nord','richtung basel','richtung zürich'])
    if s and not n: return "south"
    if n and not s: return "north"
    return "both"

def is_tunnel(road, text):
    if is_side_road(road, text): return False
    t = text.lower()
    if any(k.lower() in t for k in TUN_KW): return True
    return 'göschenen' in t and 'airolo' in t

def empty_result(now):
    return {
        "updated": now.isoformat(),
        "source": "ASTRA / opentransportdata.swiss · DATEX II · A2 Gotthard",
        "total_incidents": 0, "tunnel_incidents": 0,
        "tunnel": {
            "status": "unknown",
            "north": {"severity":"unknown","delay_min":None,"queue_m":None,"wait_min":None,"text":None},
            "south": {"severity":"unknown","delay_min":None,"queue_m":None,"wait_min":None,"text":None},
        },
        "incidents_north": [], "incidents_south": [], "incidents_all": []
    }

def portal_summary(cands, cest_h, now):
    active = [c for c in cands
              if not is_stale(c["description"], now)
              and not (c["night_only"] and 5 <= cest_h < 20)]
    if not active:
        upcoming = [c for c in cands
                    if c["night_only"] and not is_stale(c["description"], now)]
        if upcoming:
            return {"severity":"info","delay_min":None,"queue_m":None,"wait_min":None,
                    "text": upcoming[0]["description"]}
        return {"severity":"clear","delay_min":None,"queue_m":None,"wait_min":None,"text":None}
    sev_ord = {"critical":0,"high":1,"medium":2,"info":3}
    w = sorted(active, key=lambda x: sev_ord.get(x["severity"],4))[0]
    return {"severity":w["severity"],"delay_min":w["delay_min"],"queue_m":None,"wait_min":None,
            "text":w["description"][:150]}

# ─── ASTRA Fetch ──────────────────────────────────────────────────────────────
def fetch_situations(now, cest_h):
    if not API_KEY:
        print("WARNING: OPENTRANSPORT_API_KEY not set")
        return None

    headers = {"Authorization":f"Bearer {API_KEY}","SOAPAction":TS_ACT,
               "Content-Type":"text/xml; charset=utf-8"}
    try:
        r = requests.post(TS_URL, data=SOAP_BODY, headers=headers, timeout=25)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"ASTRA API error: {e}")
        return None

    incidents, t_north, t_south = [], [], []

    for rec in root.iter(f"{{{NS}}}situationRecord"):
        texts = list(iter_text(rec, "value", "locationDescription", "comment"))
        road  = road_num(rec)
        raw   = " | ".join(texts)
        combined = f"{road} {raw}"

        if not any(k.lower() in combined.lower() for k in A2_KW):
            continue

        desc   = clean_desc(raw)
        night  = is_night_only(combined)
        stale  = is_stale(desc, now)
        if stale and not night: continue

        sev  = calc_severity(desc, combined, night, cest_h)
        dirn = detect_dir(combined)
        tun  = is_tunnel(road, combined)

        dm = re.search(r'(\d{1,3})\s*min', combined, re.I)
        qm = re.search(r'(\d{1,2}(?:[.,]\d)?)\s*km', combined, re.I)

        inc = {"road":road or "A2","description":desc,"direction":dirn,"severity":sev,
               "delay_min":int(dm.group(1)) if dm else None,
               "queue_km":float(qm.group(1).replace(",",".")) if qm else None,
               "is_tunnel":tun,"night_only":night}
        incidents.append(inc)
        if tun:
            if dirn in ("north","both"): t_north.append(inc)
            if dirn in ("south","both"): t_south.append(inc)

    incidents.sort(key=lambda x:{"critical":0,"high":1,"medium":2,"info":3}.get(x["severity"],4))

    tn = portal_summary(t_north, cest_h, now)
    ts = portal_summary(t_south, cest_h, now)
    sev_ord = ["critical","high","medium","info","clear","unknown"]
    worst = min([tn["severity"],ts["severity"]],
                key=lambda s: sev_ord.index(s) if s in sev_ord else 9)

    return {
        "updated": now.isoformat(),
        "source": "ASTRA / opentransportdata.swiss · DATEX II · A2 Gotthard",
        "total_incidents": len(incidents),
        "tunnel_incidents": len(set(id(i) for i in t_north+t_south)),
        "tunnel": {"status": "clear" if worst=="clear" else worst, "north":tn, "south":ts},
        "incidents_north": [i for i in incidents if i["direction"] in ("north","both")],
        "incidents_south": [i for i in incidents if i["direction"] in ("south","both")],
        "incidents_all": incidents,
    }

# ─── Traffic Counter Functions ─────────────────────────────────────────────────
def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat = math.radians(lat2-lat1); dlon = math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R*2*math.atan2(math.sqrt(a), math.sqrt(1-a))

def discover_msrs():
    """Call pullMeasurementSiteTable and find nearest sensors to Gotthard portals."""
    headers = {"Authorization":f"Bearer {CTR_KEY}","SOAPAction":CTR_MST,
               "Content-Type":"text/xml; charset=utf-8"}
    try:
        r = requests.post(CTR_URL, data=SOAP_BODY, headers=headers, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"Counter MST error: {e}")
        return None, None

    sites = []
    for msr in root.iter(f"{{{CTR_NS}}}measurementSiteRecord"):
        lat_el = msr.find(f".//{{{CTR_NS}}}latitude")
        lon_el = msr.find(f".//{{{CTR_NS}}}longitude")
        if lat_el is None or lon_el is None: continue
        try:
            sites.append({"id": msr.get("id",""), "lat": float(lat_el.text),
                          "lon": float(lon_el.text)})
        except: pass

    print(f"Counter MST: {len(sites)} sites")

    def nearest(plat, plon):
        best, dist = None, float("inf")
        for s in sites:
            d = haversine(plat, plon, s["lat"], s["lon"])
            if d < dist: dist, best = d, s["id"]
        print(f"  Nearest to {plat:.4f},{plon:.4f}: {best} @ {dist:.0f}m")
        return best

    n = nearest(*PORTAL_N)
    s = nearest(*PORTAL_S)
    return n, s

def load_counter_sites():
    """Load cached MSR IDs or discover them."""
    if os.path.exists(CTR_CACHE):
        try:
            with open(CTR_CACHE) as f:
                c = json.load(f)
            n, s = c.get("north_msr"), c.get("south_msr")
            if n and s:
                print(f"Counter cache: N={n} S={s}")
                return n, s
        except: pass
    n, s = discover_msrs()
    if n and s:
        try:
            with open(CTR_CACHE,"w") as f:
                json.dump({"north_msr":n,"south_msr":s},f,indent=2)
        except Exception as e:
            print(f"Counter cache write error: {e}")
    return n, s

def fetch_counter_readings(north_msr, south_msr):
    """Fetch speed+flow for two MSR IDs. Returns {id: {speed_kmh, flow_per_min}}."""
    ids = [m for m in [north_msr, south_msr] if m]
    if not ids: return {}

    filters = "\n".join(
        f'      <dx223:siteRequestReference'
        f' xsi:type="dx223:_MeasurementSiteRecordVersionedReference"'
        f' targetClass="MeasurementSiteRecord" id="{m}" version="0"/>'
        for m in ids)

    body = f"""<?xml version="1.0" encoding="utf-8"?>
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
          <dx223:measurementSiteTableReference
            xsi:type="dx223:_MeasurementSiteTableVersionedReference"
            targetClass="MeasurementSiteTable" id="OTD:TrafficData" version="0"/>
{filters}
        </dx223:measuredDataFilter>
      </dx223:payloadPublication>
    </d2LogicalModel>
  </soap:Body>
</soap:Envelope>""".encode("utf-8")

    headers = {"Authorization":f"Bearer {CTR_KEY}","SOAPAction":CTR_DATA,
               "Content-Type":"text/xml; charset=utf-8"}
    try:
        r = requests.post(CTR_URL, data=body, headers=headers, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"Counter readings error: {e}")
        return {}

    results = {}
    for sm in root.iter(f"{{{CTR_NS}}}siteMeasurements"):
        ref = sm.find(f".//{{{CTR_NS}}}measurementSiteReference")
        if ref is None: continue
        msr_id = ref.get("id","")
        speeds = [float(e.text) for e in sm.iter(f"{{{CTR_NS}}}speed") if e.text]
        flows  = [float(e.text) for e in sm.iter(f"{{{CTR_NS}}}vehicleFlowRate") if e.text]
        if speeds:
            results[msr_id] = {
                "speed_kmh":    sum(speeds)/len(speeds),
                "flow_per_min": sum(flows)/len(flows)/60 if flows else None,
            }
    return results

def derive_queue(speed_kmh, flow_per_min):
    """Estimate queue_m and wait_min from loop detector data."""
    if speed_kmh is None or speed_kmh <= 0:
        return None, None
    FREE_FLOW = 80.0
    if speed_kmh >= FREE_FLOW * 0.9:
        return 0, 0
    congestion = 1.0 - speed_kmh / FREE_FLOW
    if flow_per_min and flow_per_min > 0:
        TUNNEL_CAP = 25.0
        net_acc  = max(0.0, flow_per_min - TUNNEL_CAP * (speed_kmh / FREE_FLOW))
        queue_m  = int(net_acc * 10 * 10)
    else:
        queue_m = int(congestion * 1000)
    wait_min = max(1, round(queue_m / 1000 / max(speed_kmh/60, 0.1))) if queue_m > 50 else 0
    return min(queue_m, 15000), min(wait_min, 180)

def enrich_with_counters(result):
    """Add real sensor data (speed, queue, wait) from FEDRO traffic counters."""
    if not CTR_KEY:
        return
    try:
        north_msr, south_msr = load_counter_sites()
        if not north_msr and not south_msr:
            print("Counter: no MSR IDs available")
            return
        readings = fetch_counter_readings(north_msr, south_msr)
        for side, msr_id in [("north", north_msr), ("south", south_msr)]:
            if not msr_id: continue
            rd = readings.get(msr_id)
            if not rd:
                print(f"Counter: no data for {side} ({msr_id})")
                continue
            q_m, w_min = derive_queue(rd["speed_kmh"], rd.get("flow_per_min"))
            p = result["tunnel"][side]
            if q_m is not None:
                p["queue_m"]   = q_m
                p["wait_min"]  = w_min
                p["speed_kmh"] = round(rd["speed_kmh"], 1)
                if w_min is not None and w_min > 0:
                    p["delay_min"] = w_min
            print(f"Counter {side}: {rd['speed_kmh']:.0f} km/h → {w_min}min {q_m}m")
    except Exception as e:
        print(f"Counter enrichment error (non-fatal): {e}")

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    now    = datetime.now(timezone.utc)
    cest_h = (now.hour + 2) % 24

    result = fetch_situations(now, cest_h)
    if result is None:
        result = empty_result(now)

    enrich_with_counters(result)

    with open("traffic.json","w",encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    t = result["tunnel"]
    print(f"Done: {result['total_incidents']} incidents | status={t['status']}")
    print(f"  N: sev={t['north']['severity']} delay={t['north']['delay_min']} queue={t['north'].get('queue_m')}")
    print(f"  S: sev={t['south']['severity']} delay={t['south']['delay_min']} queue={t['south'].get('queue_m')}")

if __name__ == "__main__":
    main()
