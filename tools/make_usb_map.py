"""
make_usb_map.py
===============
OPTIONAL. The app can find this by itself (web page -> USB stick -> «Find
dimmers»). This script is for those who would rather build the USB map from
the MRF export: which dimmers (radio serial numbers) belong to each light.

Uses two files:
  1. The MRF export (File -> Project configuration overview -> Export).
     The script expects a Norwegian MRF (component type «Dimmeaktuator»);
     add your language's name for dimming actuators to DIMMER_TYPES.
  2. A backup of the xSetup project (JSON with "setup" -> "controllers" and
     "devices", as delivered by xSetup / the eoptHome cloud).

The MRF export contains the MRF password in plain text. This script only
reads the component list and the links, and NEVER writes the password
anywhere. Do not put the export in a public repository.

Usage:
    python make_usb_map.py <MRF-export.txt> <xsetup_backup.json> [serial-of-main-controller]

Result: usb_kart.json in the current folder. Upload it in the app under
USB stick -> «Upload a map from a file».
"""

import collections
import json
import re
import sys

DIMMER_TYPES = ("Dimmeaktuator",)  # not analogue actuators (e.g. heating valves)


def read_mrf(path):
    raw = open(path, "rb").read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    lines = [ln for ln in text.splitlines() if not ln.lower().startswith(("passord", "passwort", "password"))]
    comp = {}
    for line in lines:
        p = line.split("\t")
        if len(p) > 6 and p[0].strip().isdigit() and p[4].strip().isdigit():
            comp[p[1].strip()] = (p[2].strip(), int(p[4].strip()))
    links = collections.defaultdict(list)
    for line in lines:
        m = re.match(r"\t(Master|Slave) \(DP: (\d+)\) => (.+?)(?: \((?:Dimmeaktuator|Aktuator|Sjalteaktuator)\))?\t",
                     line)
        if m:
            links[(m.group(1), int(m.group(2)))].append(m.group(3).strip())
    return comp, links


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    comp, links = read_mrf(sys.argv[1])
    setup = json.load(open(sys.argv[2], encoding="utf-8"))["setup"]
    # The main controller is the first one in xSetup; MRF calls it «Master».
    ctrls = setup["controllers"]
    roles = {}
    for i, c in enumerate(ctrls):
        cid = c["controllerId"]["id"] if isinstance(c["controllerId"], dict) else c["controllerId"]
        roles[cid] = "Master" if i == 0 else "Slave"
    if len(sys.argv) > 3:  # optional: serial (MAC) of the main controller
        for c in ctrls:
            cid = c["controllerId"]["id"] if isinstance(c["controllerId"], dict) else c["controllerId"]
            roles[cid] = "Master" if c.get("serial") == sys.argv[3] else "Slave"
    out, skipped = {}, []
    for d in setup["devices"]:
        m = re.match(r"\d+-(\d+)$", d.get("address") or "")
        cid = d.get("controllerId")
        cid = cid["id"] if isinstance(cid, dict) else cid
        if not m or cid not in roles:
            continue
        acts = links.get((roles[cid], int(m.group(1))), [])
        serials = [comp[a][1] for a in acts if a in comp and comp[a][0] in DIMMER_TYPES]
        if serials:
            out[str(d["smuxId"])] = {"navn": d.get("name"), "dimmere": [a for a in acts if a in comp],
                                     "serienr": serials}
        elif acts:
            skipped.append(d.get("name"))
    json.dump({"lys": out}, open("usb_kart.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"Created usb_kart.json with {len(out)} lights.")
    for k, v in sorted(out.items(), key=lambda kv: kv[1]["navn"] or ""):
        print(f"  {v['navn']:<28} -> {', '.join(v['dimmere'])}")
    print("Check that the names match before you upload it to the app.")


if __name__ == "__main__":
    main()
