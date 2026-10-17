"""
hm_link.py
==========
Én VEDVARENDE forbindelse til Home Manager som bare LYTTER.

Bekreftet 17.10.2026 med verktoy/markise_diagnose.py:
  - Når en forbindelse står åpen, sender Home Manager av seg selv
    oppdateringer ("push") for objekter som endrer seg, f.eks. en downlight
    som skrus av på veggen (under ett sekund etter trykket).
  - Home Manager sender i tillegg svarene på ALLE klienters d_obj-spørsmål
    til alle forbindelser, og en "PANEL_BRIGHTNESS"-melding hvert minutt.
    Det siste bruker vi som hjerteslag: kommer det ingenting på flere
    minutter, er forbindelsen død og vi kobler til på nytt.

Denne modulen kjenner ikke til MQTT eller Home Assistant. Den gir hver
mottatt melding videre til en callback som broen bestemmer over.
"""

import logging
import re
import socket
import threading
import time

import xcomfort_local_control as xc

log = logging.getLogger("xcomfort.lytter")

FRAME_RE = re.compile(r"(?:oid \S+ )?(SSN|RSN) (\d+) (\S+) ?(.*)")
ACK_RE = re.compile(r"x_bm_st (\w+)")

SUBSCRIBE_BATCH = 50
SILENCE_RECONNECT_S = 300   # Home Manager sender noe minst hvert minutt


def parse_frame(text):
    """Tolker én ramme. Returnerer en av:
       ("obj", oid, navn, [felter])   for SSN/RSN-meldinger
       ("ack", type, tekst)           for radiokvitteringer (x_bm_st ...)
       ("annet", tekst, None)         for alt annet
    """
    text = text.replace("\x07", "").replace("\x00", "").strip()
    if not text:
        return None
    m = FRAME_RE.search(text)
    if m:
        return ("obj", int(m.group(2)), m.group(3), m.group(4).split())
    m = ACK_RE.search(text)
    if m:
        return ("ack", m.group(1), text)
    return ("annet", text, None)


class HMLink:
    """Vedvarende lytte-forbindelse med automatisk gjenoppkobling."""

    def __init__(self, on_object, on_ack=None, on_other=None):
        self.on_object = on_object
        self.on_ack = on_ack
        self.on_other = on_other
        self.ids = set()
        self.subscribed_at = {}
        self.conn = None
        self.lock = threading.Lock()
        self.connected = False
        self.last_rx = 0.0
        self.connects = 0
        self.frames = 0
        self.stopping = False
        self._buf = b""

    # -- abonnement --
    def subscribe(self, ids):
        ids = {int(i) for i in ids}
        new = ids - self.ids
        self.ids |= ids
        if new and self.connected:
            self._send_subscriptions(sorted(new))

    def _send_subscriptions(self, ids):
        now = time.time()
        for i in range(0, len(ids), SUBSCRIBE_BATCH):
            chunk = ids[i:i + SUBSCRIBE_BATCH]
            data = b"".join(xc.STX + f"d_obj {oid}".encode("ascii") + xc.ETX for oid in chunk)
            with self.lock:
                if not self.conn:
                    return
                self.conn.sock.sendall(data)
            for oid in chunk:
                self.subscribed_at[oid] = now
            time.sleep(0.2)

    def recently_subscribed(self, oid, seconds=5.0):
        """True rett etter at vi abonnerte (svaret er da startverdien, ikke
        en ny hendelse)."""
        return time.time() - self.subscribed_at.get(oid, 0) < seconds

    def healthy(self):
        return self.connected and time.time() - self.last_rx < SILENCE_RECONNECT_S

    # -- hovedløkke --
    def run(self):
        delay = 1.0
        while not self.stopping:
            try:
                conn = xc.XComfortConn()
            except Exception as e:  # noqa: BLE001
                self.connected = False
                log.warning("Lytte-forbindelsen fikk ikke kontakt med Home Manager: %s (prøver igjen om %.0fs)",
                            e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            delay = 1.0
            conn.sock.settimeout(1.0)
            with self.lock:
                self.conn = conn
            self.connected = True
            self.connects += 1
            self.last_rx = time.time()
            self._buf = b""
            log.info("Lytte-forbindelse til Home Manager er oppe (abonnerer på %d objekter)", len(self.ids))
            try:
                self._send_subscriptions(sorted(self.ids))
                self._read_loop(conn)
            except Exception as e:  # noqa: BLE001
                log.warning("Lytte-forbindelsen ble brutt: %s -- kobler til igjen", e)
            finally:
                self.connected = False
                with self.lock:
                    self.conn = None
                conn.close()
            time.sleep(1.0)

    def _read_loop(self, conn):
        while not self.stopping:
            try:
                data = conn.sock.recv(65536)
            except socket.timeout:
                if time.time() - self.last_rx > SILENCE_RECONNECT_S:
                    raise ConnectionError(f"ingenting mottatt på {SILENCE_RECONNECT_S}s")
                continue
            if not data:
                raise ConnectionError("Home Manager lukket forbindelsen")
            self.last_rx = time.time()
            self.feed(data)

    def feed(self, data):
        """Deler opp byte-strømmen i rammer (0x01 ... 0x02) og sender dem
        videre. Egen metode så den kan testes uten nettverk."""
        self._buf += data
        while xc.ETX in self._buf:
            frame, self._buf = self._buf.split(xc.ETX, 1)
            text = frame.replace(xc.STX, b"").decode("latin-1", errors="replace")
            parsed = parse_frame(text)
            if not parsed:
                continue
            self.frames += 1
            try:
                if parsed[0] == "obj":
                    self.on_object(parsed[1], parsed[2], parsed[3])
                elif parsed[0] == "ack" and self.on_ack:
                    self.on_ack(parsed[1])
                elif parsed[0] == "annet" and self.on_other:
                    self.on_other(parsed[1])
            except Exception:  # noqa: BLE001
                log.exception("Feil ved behandling av melding fra Home Manager: %r", text[:200])
        if len(self._buf) > 65536:  # søppel uten sluttbyte -- kast
            self._buf = b""
