"""
lag_usb_kart.py
===============
VALGFRITT. Appen kan finne dette selv (nettsiden -> USB-pinne -> «Finn
dimmere»). Dette skriptet er for deg som heller vil lage USB-kartet fra
MRF-eksporten: hvilke dimmere (radio-serienumre) som hører til hvert lys.

Bruker to filer:
  1. MRF-eksporten (Fil -> Oversikt prosjektkonfigurasjon -> Eksporter).
     Skriptet forventer norsk MRF (komponenttypen «Dimmeaktuator»).
  2. En backup av xSetup-prosjektet (JSON med "setup" -> "controllers" og
     "devices", slik xSetup/eoptHome-skyen leverer det).

MRF-eksporten inneholder MRF-passordet i klartekst. Skriptet leser bare
komponentlista og forbindelsene, og skriver ALDRI passordet videre. Ikke
legg eksporten i et offentlig repo.

Kjøring:
    python lag_usb_kart.py <MRF-eksport.txt> <xsetup_backup.json> [serienr-til-hovedkontroller]

Resultat: usb_kart.json i mappen du står i. Last den opp i appen under
USB-pinne -> «Last opp et kart fra fil».
"""

import collections
import json
import re
import sys

DIMMER_TYPES = ("Dimmeaktuator",)  # ikke analoge aktuatorer (f.eks. shunt til varme)


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
    # Hovedkontrolleren er den første i xSetup; MRF kaller den «Master».
    ctrls = setup["controllers"]
    roles = {}
    for i, c in enumerate(ctrls):
        cid = c["controllerId"]["id"] if isinstance(c["controllerId"], dict) else c["controllerId"]
        roles[cid] = "Master" if i == 0 else "Slave"
    if len(sys.argv) > 3:  # valgfritt: serienummer (MAC) til hovedkontrolleren, f.eks. 9802840128B2
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
    print(f"Laget usb_kart.json med {len(out)} lys.")
    for k, v in sorted(out.items(), key=lambda kv: kv[1]["navn"] or ""):
        print(f"  {v['navn']:<28} -> {', '.join(v['dimmere'])}")
    print("Sjekk at navnene passer sammen før du laster den opp i appen.")


if __name__ == "__main__":
    main()
