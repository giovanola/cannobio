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

    with open("traffic.json","w",encoding="utf-8") as f:
        json.dump(result,f,ensure_ascii=False,indent=2)

    print(f"Done: {len(incidents)} incidents | tunnel={status} | N={tn['severity']} S={ts['severity']}")

if __name__ == "__main__":
    main()
