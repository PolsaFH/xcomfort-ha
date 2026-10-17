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

log = logging.getLogger("xcomfort.skann")

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
        log.info("Starter søk etter alle objekter i Home Manager (ID 1–%d og videre) ...", max_id)
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
            log.info("Søket er ferdig: fant %d objekter", len(found))
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            log.error("Søket stoppet: %s", e)
        finally:
            if conn:
                conn.close()
            self.running = False
            self.progress = 1.0 if not self.error else self.progress
            self.finished = time.strftime("%Y-%m-%d %H:%M:%S")


def candidates(objects, known_ids):
    """Lager forslag til nye enheter ut fra en objektliste {id: (navn, type)}.
    Returnerer {"lys": [...], "brytere": [...], "knapper": [...]}."""
    by_name = {name: oid for oid, (name, _t) in objects.items()}
    out = {"lys": [], "brytere": [], "knapper": []}
    for oid, (name, typ) in sorted(objects.items()):
        if oid in known_ids:
            continue
        if name.startswith("D_") and typ == 21:
            key = name[2:]
            val_id = by_name.get(f"M_D_{key}_Val")
            set_id = by_name.get(f"B_D_{key}_SET")
            if val_id and set_id:
                out["lys"].append({"id": oid, "navn": pretty(key), "objekt": name,
                                   "val_id": val_id, "set_id": set_id})
        elif typ == 8 and (name.startswith("R_") or name.startswith("TS_")):
            core = name.split("_", 1)[1]
            on_id = next((i for n, i in by_name.items()
                          if n.lower() in (f"b_{core}_on".lower(), f"b_r_{core}_on".lower())), None)
            off_id = next((i for n, i in by_name.items()
                           if n.lower() in (f"b_{core}_off".lower(), f"b_r_{core}_off".lower())), None)
            out["brytere"].append({"id": oid, "navn": pretty(core), "objekt": name,
                                   "on_id": on_id, "off_id": off_id})
        elif typ == 11 and name.startswith("IN_"):
            out["knapper"].append({"id": oid, "navn": pretty(name[3:]), "objekt": name})
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
