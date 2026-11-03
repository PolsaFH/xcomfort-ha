"""
scanner.py
==========
Finner ALLE objekter i Home Manager ved å spørre etter hver eneste ID
(d_obj) i bolker, og lager en liste over enheter som broen ikke kjenner
fra før: dimbare lys, av/på-brytere og trykknapper/innganger.

Kjøres i bakgrunnen fra appens nettside ("Søk etter nye enheter"). Tar
typisk 10–20 minutter for hele ID-området, men belaster bare TCP-
forbindelsen til Home Manager, ikke radioen.
"""

import json
import logging
import os
import re
import threading
import time

import xcomfort_local_control as xc

log = logging.getLogger("xcomfort.scan")

NAME_TYPE_RE = re.compile(r"(?:SSN|RSN) (\d+) (\S+) (\d+)")

MAX_ID = 270000
EMPTY_GAP = 30000
HARD_LIMIT = 1000000
BATCH = 100
WAIT_S = 0.25


class Scanner:
    def __init__(self, result_file):
        self.result_file = result_file
        self.running = False
        self.progress = 0.0
        self.found = 0
        self.started = None
        self.finished = None
        self.error = None
        self.objects = self._load()

    def _load(self):
        try:
            with open(self.result_file, encoding="utf-8") as f:
                return {int(k): tuple(v) for k, v in json.load(f).items()}
        except Exception:  # noqa: BLE001
            return {}

    def _save(self):
        tmp = self.result_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): list(v) for k, v in sorted(self.objects.items())}, f)
        os.replace(tmp, self.result_file)

    def status(self):
        return {"running": self.running, "progress": round(self.progress, 3), "found": self.found,
                "objects": len(self.objects), "started": self.started, "finished": self.finished,
                "error": self.error}

    def start(self, max_id=MAX_ID):
        if self.running:
            return False
        threading.Thread(target=self._run, args=(max_id,), daemon=True, name="skann").start()
        return True

    def _run(self, max_id):
        self.running = True
        self.error = None
        self.progress = 0.0
        self.found = 0
        self.started = time.strftime("%Y-%m-%d %H:%M:%S")
        found = {}
        log.info("Searching for all objects in Home Manager (ID 1-%d and beyond) ...", max_id)
        conn = None
        try:
            conn = xc.XComfortConn()
            # Nye objekter fra MRF faar de hoeyeste numrene, saa vi fortsetter
            # forbi max_id helt til det har vaert EMPTY_GAP tomme numre paa rad.
            start, last_hit = 1, 0
            while start <= HARD_LIMIT and (start <= max_id or start - last_hit < EMPTY_GAP):
                ids = range(start, start + BATCH)
                conn.sock.sendall(b"".join(xc.STX + f"d_obj {i}".encode("ascii") + xc.ETX for i in ids))
                text = conn.drain(wait=WAIT_S)
                for oid, name, typ in NAME_TYPE_RE.findall(text):
                    found[int(oid)] = (name, int(typ))
                    last_hit = max(last_hit, int(oid))
                self.found = len(found)
                end = max(max_id, last_hit + EMPTY_GAP)
                self.progress = min(0.99, start / end)
                start += BATCH
            self.objects = found
            self._save()
            log.info("Search finished: found %d objects", len(found))
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            log.error("Search stopped: %s", e)
        finally:
            if conn:
                conn.close()
            self.running = False
            self.progress = 1.0 if not self.error else self.progress
            self.finished = time.strftime("%Y-%m-%d %H:%M:%S")


# Home Manager kutter lange objektnavn (sett i ekte anlegg: ved 29-30 tegn,
# og ikke alltid likt). Et navn som er minst så langt og er starten på det
# ventede navnet, regnes som kuttet.
NAME_MAX = 30
NAME_CUT_MIN = 24
# Typenumre fra Home Manager (de samme som «type» i xSetup).
T_BUTTON_OBJ, T_THERMOSTAT, T_SWITCH, T_SENSOR, T_INPUT, T_DIMMER, T_VALUE = 6, 7, 8, 10, 11, 21, 23
# Faste avstander fra hovedobjektet til styre-objektene, sett i ekte anlegg.
DIMMER_OFFSETS = [(171, 219), (118, 166)]          # (M_D_<navn>_Val, B_D_<navn>_SET)
AWNING_OFFSETS = {"open": 61, "stop": 126, "close": 191}
SEARCH_WINDOW = 320                                  # hvor langt etter hovedobjektet vi leter


def _same(found, wanted):
    """Er `found` lik `wanted`, også når Home Manager har kuttet navnet?"""
    return found == wanted or (len(found) >= NAME_CUT_MIN and wanted.startswith(found))


def _following(objects, oid, typ=None):
    """Objekter rett etter `oid` (bare av typen `typ` hvis gitt), i nummerrekkefølge."""
    return [(i, objects[i][0]) for i in range(oid + 1, oid + SEARCH_WINDOW + 1)
            if i in objects and (typ is None or objects[i][1] == typ)]


def _dimmer_objects(objects, oid, key):
    """(val_id, set_id) for en dimmer, eller None."""
    want_val, want_set = f"M_D_{key}_Val", f"B_D_{key}_SET"
    for off_val, off_set in DIMMER_OFFSETS:
        v, st = objects.get(oid + off_val), objects.get(oid + off_set)
        if v and st and v[1] == T_VALUE and st[1] == T_BUTTON_OBJ \
                and _same(v[0], want_val) and _same(st[0], want_set):
            return oid + off_val, oid + off_set
    # Andre avstander: bare når navnene er hele (ikke kuttet), så det er entydig.
    after = _following(objects, oid)
    val = next((i for i, n in after if n == want_val), None)
    set_ = next((i for i, n in after if n == want_set), None)
    return (val, set_) if val and set_ else None


def _awning_objects(objects, oid, key):
    """{"open", "stop", "close"} -> objekt-id for en markise (B_<navn>_Open osv.), eller None."""
    found = {}
    for i, n in _following(objects, oid, T_BUTTON_OBJ):
        if not n.startswith("B_") or n.startswith("B_D_"):
            continue
        base, _, suffix = n[2:].rpartition("_")
        if base and key.startswith(base) and suffix.lower() in AWNING_OFFSETS and suffix.lower() not in found:
            found[suffix.lower()] = i
    if len(found) < 3:
        # Kuttede navn: bruk de faste avstandene, men bare når alle tre ser riktige ut.
        found = {}
        for what, off in AWNING_OFFSETS.items():
            o = objects.get(oid + off)
            if not (o and o[1] == T_BUTTON_OBJ and o[0].startswith("B_") and not o[0].startswith("B_D_")):
                return None
            found[what] = oid + off
    return found


def _switch_objects(objects, oid, name):
    """(on_id, off_id) for en av/på-aktuator, eller None. To mønstre er sett:
    R_<navn>  -> B_R_<navn>_ON / B_R_<navn>_OFF (navnene kan være kuttet)
    TS_<navn><sone> -> B_<sone>_<navn>_On / B_<sone>_<navn>_Off"""
    core = name.split("_", 1)[1]
    hits = []
    for i, n in _following(objects, oid):
        low = n.lower()
        if low in (f"b_{core}_on".lower(), f"b_{core}_off".lower(),
                   f"b_r_{core}_on".lower(), f"b_r_{core}_off".lower()):
            hits.append((i, n))      # hele navnet stemmer (uansett type)
        elif objects[i][1] != T_BUTTON_OBJ:
            continue
        elif name.startswith("R_") and _same(n, f"B_R_{core}_"):
            hits.append((i, n))      # kuttet navn
        else:
            parts = n.split("_")
            if len(parts) == 4 and parts[0] == "B" and (parts[2] + parts[1]).lower() == core.lower():
                hits.append((i, n))  # B_<sone>_<navn>_On
    on = next((i for i, n in hits if n.lower().endswith("_on")), None)
    off = next((i for i, n in hits if n.lower().endswith("_off")), None)
    if on is None or off is None:
        # Kuttede navn (B_R_<langt navn>_): første er ON, andre er OFF.
        cut = [i for i, n in hits if not n.lower().endswith(("_on", "_off"))]
        if len(cut) >= 2 and on is None and off is None:
            on, off = cut[0], cut[1]
    return (on, off) if on and off else None


def guess_unit(name):
    n = name.lower()
    if "temp" in n or n.endswith(("ute", "tur", "retur")):
        return "°C"
    if "sol" in n or "lux" in n:
        return "lux"
    if "vind" in n or "wind" in n:
        return "m/s"
    if "fukt" in n or "humid" in n:
        return "%"
    return ""


def candidates(objects, known_ids):
    """Lager forslag til nye enheter ut fra en objektliste {id: (navn, type)}.
    Kjenner igjen enhetene på typenummer og faste avstander mellom objektene,
    ikke bare på navn (Home Manager kutter lange navn).
    Returnerer {"lys", "brytere", "knapper", "markiser", "termostater", "sensorer"}."""
    out = {"lys": [], "brytere": [], "knapper": [], "markiser": [], "termostater": [], "sensorer": []}
    for oid, (name, typ) in sorted(objects.items()):
        if oid in known_ids:
            continue
        if typ == T_DIMMER and name.startswith("D_"):
            key = name[2:]
            dim = _dimmer_objects(objects, oid, key)
            if dim:
                out["lys"].append({"id": oid, "navn": pretty(key), "objekt": name,
                                   "val_id": dim[0], "set_id": dim[1]})
                continue
            aw = _awning_objects(objects, oid, key)
            if aw:
                out["markiser"].append({"id": oid, "navn": pretty(key), "objekt": name,
                                        "open_id": aw["open"], "stop_id": aw["stop"], "close_id": aw["close"]})
        elif typ == T_SWITCH and (name.startswith("R_") or name.startswith("TS_")):
            sw = _switch_objects(objects, oid, name)
            if sw:   # uten av/på-objekter kan den ikke styres (f.eks. interne varmereleer)
                out["brytere"].append({"id": oid, "navn": pretty(name.split("_", 1)[1]), "objekt": name,
                                       "on_id": sw[0], "off_id": sw[1]})
        elif typ == T_INPUT and name.startswith("IN_"):
            out["knapper"].append({"id": oid, "navn": pretty(name[3:]), "objekt": name})
        elif typ == T_THERMOSTAT and name.startswith("TH_"):
            room = pretty(name[3:].rstrip("0123456789"))
            out["termostater"].append({"id": oid, "navn": room or pretty(name[3:]), "objekt": name, "sone": room})
        elif typ == T_SENSOR and (name.startswith("TS_") or name.startswith("S_")):
            raw = name.split("_", 1)[1]
            out["sensorer"].append({"id": oid, "navn": pretty(raw), "objekt": name, "enhet": guess_unit(raw)})
    return out


def pretty(raw):
    """'DownlightKjellerstue' -> 'Downlight Kjellerstue' (enkelt forslag til navn)."""
    raw = raw.replace("_", " ")
    out = []
    for i, ch in enumerate(raw):
        if i and ch.isupper() and raw[i - 1].islower():
            out.append(" ")
        out.append(ch)
    return "".join(out).strip()
