"""
idmap.py
========
Holder styr på objekter som kan få NYTT NUMMER i Home Manager.

Bekreftet 18.10.2026: Når noe lastes opp fra xSetup, kan Home Manager
nummerere om mange objekter (506 stykker den gangen), spesielt innganger
(IN_...). Lys, brytere og termostater beholdt numrene sine.

Hvert objekt vi følger med på har en fast NØKKEL (det opprinnelige
nummeret, som også brukes i unique_id og MQTT-topics i HA, så ingenting
endres der) og et forventet NAVN i Home Manager. Denne modulen finner hvilket
nummer objektet har NÅ:

  1. Spør Home Manager om nummeret vi sist kjente. Stemmer navnet, er alt OK.
  2. Hvis ikke: let etter navnet i siste fulle søk (Verktøy i appen).
  3. Hvis fortsatt ikke funnet: søk i et område rundt det gamle nummeret.
"""

import json
import logging
import os
import re
import threading
import time

import xcomfort_local_control as xc

log = logging.getLogger("xcomfort.idmap")

NAME_RE = re.compile(r"(?:SSN|RSN) (\d+) (\S+) ")
WINDOW = 25000
BATCH = 100
WAIT_S = 0.25


def name_key(name):
    """Sammenligningsnøkkel for et objektnavn. Home Manager setter SONEN inn i
    navnet på innganger (IN_<sone>_<navn>), så et sonebytte i xSetup gjør
    IN_Hus_BRstue1 til IN_Stue_BRstue1. Vi sammenligner derfor bare første
    og siste del: ("IN", "BRstue1")."""
    parts = (name or "").split("_")
    return (parts[0], parts[-1]) if len(parts) >= 2 else (name, "")


def _unique_by_key(pairs):
    """{nøkkel: id} for nøkler som bare finnes én gang."""
    seen, dup = {}, set()
    for oid, name in pairs:
        k = name_key(name)
        if k in seen and seen[k] != oid:
            dup.add(k)
        seen.setdefault(k, oid)
    return {k: v for k, v in seen.items() if k not in dup}


class IdMap:
    def __init__(self, path):
        self.path = path
        self.expected = {}      # nøkkel -> forventet objektnavn
        self.current = {}       # nøkkel -> nåværende nummer
        self.lock = threading.Lock()
        self.quiet_until = 0.0  # svar på våre egne spørsmål er ikke hendelser
        try:
            with open(path, encoding="utf-8") as f:
                self.current = {int(k): int(v) for k, v in json.load(f).items()}
        except Exception:  # noqa: BLE001
            self.current = {}

    def expect(self, key, name):
        if name:
            self.expected[int(key)] = name

    def cur(self, key):
        return self.current.get(key, key)

    def alias(self):
        """{nåværende nummer: nøkkel} for alle objekter vi følger med på."""
        return {self.cur(k): k for k in self.expected}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in sorted(self.current.items()) if k != v}, f, indent=1)
        os.replace(tmp, self.path)

    @staticmethod
    def _ask(conn, ids):
        found = {}
        ids = list(ids)
        for i in range(0, len(ids), BATCH):
            chunk = ids[i:i + BATCH]
            conn.sock.sendall(b"".join(xc.STX + f"d_obj {oid}".encode("ascii") + xc.ETX for oid in chunk))
            for oid, name in NAME_RE.findall(conn.drain(wait=WAIT_S) + " "):
                found[int(oid)] = name
        return found

    def verify(self, scanned=None, connect=None):
        """Sjekker alle objektene. Returnerer liste med endringer
        [(nøkkel, navn, gammelt nr, nytt nr)]. 'scanned' er siste fulle søk
        {id: (navn, type)}."""
        connect = connect or xc.XComfortConn
        with self.lock:
            if not self.expected:
                return []
            self.quiet_until = float("inf")
            try:
                return self._verify(scanned, connect)
            finally:
                self.quiet_until = time.time() + 3.0

    def quiet(self):
        return time.time() < self.quiet_until

    def _verify(self, scanned, connect):
        if True:
            conn = connect()
            try:
                now = self._ask(conn, {self.cur(k) for k in self.expected})
                missing = {}
                for k, n in self.expected.items():
                    got = now.get(self.cur(k))
                    if got == n:
                        continue
                    if got is not None and name_key(got) == name_key(n):
                        log.info("%s heter nå %s i Home Manager (samme nummer)", n, got)
                        self.expected[k] = got
                        continue
                    missing[k] = n
                changes = []
                if missing and scanned:
                    by_key = _unique_by_key((oid, name) for oid, (name, _t) in scanned.items())
                    candidates = {k: by_key[name_key(n)] for k, n in missing.items() if name_key(n) in by_key}
                    if candidates:
                        check = self._ask(conn, set(candidates.values()))
                        for k, oid in candidates.items():
                            if name_key(check.get(oid)) == name_key(missing[k]):
                                changes.append((k, check[oid], self.cur(k), oid))
                                del missing[k]
                if missing:
                    lo = max(1, min(self.cur(k) for k in missing) - WINDOW)
                    hi = max(self.cur(k) for k in missing) + WINDOW
                    log.info("Leter etter %d objekt(er) med nytt nummer (nr. %d-%d) ...", len(missing), lo, hi)
                    seen = self._ask(conn, range(lo, hi + 1))
                    by_key = _unique_by_key(seen.items())
                    for k in list(missing):
                        oid = by_key.get(name_key(missing[k]))
                        if oid is not None:
                            changes.append((k, seen[oid], self.cur(k), oid))
                            del missing[k]
            finally:
                conn.close()
            for k, name, old, new in changes:
                self.current[k] = new
                self.expected[k] = name
                log.warning("%s har fått nytt nummer i Home Manager: %d -> %d (fulgt opp automatisk)",
                            name, old, new)
            for k, name in missing.items():
                log.warning("Fant ikke %s i Home Manager (sist kjent som nr. %d). Er den slettet i xSetup?",
                            name, self.cur(k))
            if changes:
                self._save()
            return changes

