"""
usbstick.py
===========
Styrer xComfort-lys DIREKTE over radio med USB-pinnen CKOZ-00/13 (den samme
pinnen som MRF bruker), uten å gå via Home Manager. Lysene reagerer da med
en gang.

Protokollen er funnet ved å ta opp trafikken mellom MRF og pinnen
(30.10.2026). Bare kommandoene MRF selv bruker når et lys testes, brukes:
    0x50 = på (forrige nivå), 0x51 = av, 0x5A + 01 00 <prosent> = sett nivå

Ramme til/fra pinnen: 5A <lengde> <kode> <data> A5, sendt som 62 bytes
over USB (interrupt-endepunkt). "Send radiomelding" er kode 0x1B:
    1B <cmd> 00 <len> <seq> 82 07 00 80 00 00 00 00 <serienr LE> <data> <CRC16> A5
CRC-en er CRC-16/Kermit over alt fra <cmd> til og med <data>.

Lampa kvitterer på radio til pinnen (adresse 128) med en gang. Den
kvitteringen brukes til å sjekke at kommandoen kom fram. Kom den ikke fram,
sender broen samme kommando via Home Manager i stedet.

Pinnen er valgfri. Finnes den ikke, eller slutter den å virke, bruker broen
Home Manager som før.
"""

import logging
import random
import threading
import time

log = logging.getLogger("xcomfort.usb")

VID, PID = 0x188A, 0x1102
PACKET = 62
STICK_ADDR = 128               # pinnens egen adresse på radio
RETRY_OPEN_S = 30.0
FRAME_GAP_S = 0.06             # pause mellom radiomeldinger til flere lamper

INIT = ["5a 04 0b a5", "5a 05 1a 00 a5", "5a 04 44 a5", "5a 04 42 a5",
        "5a 05 33 01 a5", "5a 05 1a 01 a5", "5a 06 34 ff ff a5", "5a 04 31 a5", "5a 04 36 a5"]

CMD_ON, CMD_OFF, CMD_LEVEL = 0x50, 0x51, 0x5A


def crc16_kermit(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc


def rf_frame(cmd, serial, extra=b"", seq=0x10):
    body = bytes([cmd, 0x00, 0, seq, 0x82, 0x07, 0x00, 0x80, 0, 0, 0, 0])
    body += int(serial).to_bytes(4, "little") + (extra or b"\x00")
    body = body[:2] + bytes([len(body) + 2]) + body[3:]
    inner = bytes([0x1B]) + body + crc16_kermit(body).to_bytes(2, "little") + b"\xA5"
    return bytes([0x5A, len(inner) + 2]) + inner


def level_frame(serial, percent, seq):
    """Ramme som setter en lampe til `percent` (0 = av)."""
    percent = max(0, min(100, int(round(percent))))
    if percent == 0:
        return rf_frame(CMD_OFF, serial, b"", seq)
    return rf_frame(CMD_LEVEL, serial, bytes([1, 0, percent]), seq)


def parse_radio(frame):
    """Mottatt radiomelding (kode 0x03) -> (type, fra, til) eller None."""
    if len(frame) < 20 or frame[0] != 0x5A or frame[2] != 0x03:
        return None
    src = int.from_bytes(frame[10:14], "little")
    dst = int.from_bytes(frame[15:19], "little")
    return frame[3], src, dst


class PyUsbTransport:
    """Den ekte USB-forbindelsen (pyusb/libusb). Brukes i appen."""

    def __init__(self, dev, ep_in, ep_out):
        self.dev, self.ep_in, self.ep_out = dev, ep_in, ep_out

    @classmethod
    def open(cls):
        try:
            import usb.core
            import usb.util
        except ImportError:
            log.info("pyusb er ikke installert -- USB-pinnen brukes ikke")
            return None
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            return None
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError) as e:
            log.debug("Kunne ikke koble fra kjernedriveren: %s", e)
        try:
            dev.set_configuration()
        except usb.core.USBError as e:
            log.debug("set_configuration: %s", e)
        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]
        usb.util.claim_interface(dev, intf.bInterfaceNumber)
        ep_in = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        ep_out = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        if ep_in is None or ep_out is None:
            raise IOError("fant ikke endepunktene på USB-pinnen")
        return cls(dev, ep_in, ep_out)

    def write(self, data):
        self.ep_out.write(data, timeout=1000)

    def read(self, timeout_ms):
        import usb.core
        try:
            return bytes(self.ep_in.read(64, timeout=timeout_ms))
        except usb.core.USBTimeoutError:
            return None
        except usb.core.USBError as e:
            if getattr(e, "errno", None) == 110 or "timed out" in str(e).lower():
                return None
            raise

    def close(self):
        try:
            import usb.util
            usb.util.dispose_resources(self.dev)
        except Exception:  # noqa: BLE001
            pass


class UsbStick:
    def __init__(self, transport_factory=PyUsbTransport.open, on_radio=None):
        self.factory = transport_factory
        self.on_radio = on_radio
        self.transport = None
        self.lock = threading.Lock()
        self.seq = 0x10 + random.randrange(16)
        self.last_ack = {}          # serienr -> tid for siste kvittering
        self.ok = False
        self.stopping = False
        self.name = ""
        self.sent = 0
        self.failed = 0
        self.last_error = ""
        self._logged_missing = False

    # -- tilkobling --
    def connect(self):
        """Prøver å åpne pinnen én gang. True hvis den er klar."""
        try:
            t = self.factory()
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            log.warning("USB-pinnen ble funnet, men kunne ikke åpnes: %s", e)
            return False
        if t is None:
            if not self._logged_missing:
                log.info("Ingen USB-pinne (CKOZ-00/13) funnet -- lys styres via Home Manager")
                self._logged_missing = True
            return False
        try:
            for f in INIT:
                t.write(self._pad(bytes.fromhex(f)))
                time.sleep(0.02)
            end = time.time() + 1.0
            while time.time() < end:
                data = t.read(100)
                if data and b"CKOZ" in data:
                    self.name = data[3:data.index(b"\x00", 3)].decode("ascii", "replace") \
                        if b"\x00" in data[3:] else "CKOZ"
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            log.warning("USB-pinnen svarte ikke riktig ved oppstart: %s", e)
            t.close()
            return False
        with self.lock:
            self.transport = t
            self.ok = True
        self._logged_missing = False
        log.info("USB-pinne klar (%s). Lys styres direkte over radio, Home Manager er reserve.",
                 self.name or "CKOZ-00/13")
        return True

    def run(self):
        """Holder pinnen åpen og leser alt den hører. Kjøres i egen tråd."""
        while not self.stopping:
            if not self.ok and not self.connect():
                time.sleep(RETRY_OPEN_S)
                continue
            try:
                data = self.transport.read(500)
            except Exception as e:  # noqa: BLE001
                self._broken(f"lesefeil: {e}")
                continue
            if data:
                self.feed(data)

    def feed(self, data):
        if not data or data[0] != 0x5A or len(data) < 3:
            return
        frame = data[:data[1]]
        info = parse_radio(frame)
        if not info:
            return
        _kind, src, dst = info
        if dst == STICK_ADDR:
            self.last_ack[src] = time.time()
        if self.on_radio:
            try:
                self.on_radio(frame, info)
            except Exception:  # noqa: BLE001
                log.exception("Feil i behandling av radiomelding")

    def _broken(self, why):
        log.warning("USB-pinnen sluttet å virke (%s) -- bruker Home Manager til den er tilbake", why)
        self.last_error = why
        with self.lock:
            self.ok = False
            t, self.transport = self.transport, None
        if t:
            t.close()

    def healthy(self):
        return self.ok and self.transport is not None

    # -- sending --
    @staticmethod
    def _pad(frame):
        return frame + bytes(PACKET - len(frame))

    def _next_seq(self):
        s = self.seq
        self.seq = 0x10 + ((self.seq - 0x10 + 1) % 16)
        return s

    def set_levels(self, targets):
        """Sender [(serienr, prosent), ...]. Returnerer tidspunktet sendingen
        startet, eller None hvis pinnen ikke virker (da skal Home Manager
        brukes)."""
        if not self.healthy():
            return None
        t0 = time.time()
        with self.lock:
            try:
                for i, (serial, pct) in enumerate(targets):
                    if i:
                        time.sleep(FRAME_GAP_S)
                    self.transport.write(self._pad(level_frame(serial, pct, self._next_seq())))
                    self.sent += 1
            except Exception as e:  # noqa: BLE001
                self.failed += 1
                broken = str(e)
            else:
                return t0
        self._broken(f"skrivefeil: {broken}")
        return None

    def acked_since(self, serial, t0):
        t = self.last_ack.get(int(serial))
        return t is not None and t >= t0

    def snapshot(self):
        return {"tilkoblet": self.healthy(), "navn": self.name, "sendt": self.sent,
                "feilet": self.failed, "siste_feil": self.last_error}
