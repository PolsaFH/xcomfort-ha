"""
usbstick.py
===========
Styrer xComfort-lys DIREKTE over radio med USB-pinnen CKOZ-00/13 (den samme
pinnen som MRF bruker), uten å gå via Home Manager. Lysene reagerer da med
en gang.

Protokollen er funnet ved å ta opp trafikken mellom MRF og pinnen
(30.10.2026). Bare kommandoene MRF selv bruker når et lys testes, brukes:
    0x50 = på (forrige nivå), 0x51 = av, 0x5A + 01 00 <nivå 0-255> = sett nivå (prosent x 2,55)

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

import collections
import logging
import os
import random
import stat
import threading
import time

log = logging.getLogger("xcomfort.usb")

VID, PID = 0x188A, 0x1102
PACKET = 62
STICK_ADDR = 128               # pinnens egen adresse på radio
RETRY_OPEN_S = 10.0             # hvor ofte vi ser etter pinnen når den mangler
FRAME_GAP_S = 0.06             # minste pause mellom radiomeldinger
ACK_WAIT_S = 0.35              # så lenge vi venter på kvittering før neste dimmer
ACK_BUDGET_S = 2.5             # maks total ventetid per bolk (resten sendes uten å vente)

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


def percent_to_raw(percent):
    """Dimmerne bruker 0-255 på radio (MRF sendte 0x80 for halv styrke, og
    «på» rapporteres som 0xFF). HA og Home Manager bruker 0-100 %."""
    percent = max(0, min(100, float(percent)))
    return 0 if percent <= 0 else max(1, min(255, int(round(percent * 255 / 100))))


def raw_to_percent(raw):
    return int(round(raw * 100 / 255))


def level_frame(serial, percent, seq):
    """Ramme som setter en lampe til `percent` (0-100, 0 = av)."""
    raw = percent_to_raw(percent)
    if raw == 0:
        return rf_frame(CMD_OFF, serial, b"", seq)
    return rf_frame(CMD_LEVEL, serial, bytes([1, 0, raw]), seq)


def parse_radio(frame):
    """Mottatt radiomelding (kode 0x03) -> (type, fra, til) eller None."""
    if len(frame) < 20 or frame[0] != 0x5A or frame[2] != 0x03:
        return None
    src = int.from_bytes(frame[10:14], "little")
    dst = int.from_bytes(frame[15:19], "little")
    return frame[3], src, dst


class TransientUsbError(IOError):
    """En enkelt lesing feilet, men pinnen virker (f.eks. overflow)."""


def _sysfs_sticks(root="/sys/bus/usb/devices"):
    """Finner pinnen i sysfs (det kjernen ser akkurat nå). Returnerer liste
    med (bus, enhet, major, minor)."""
    found = []
    try:
        names = os.listdir(root)
    except OSError:
        return found

    def rd(d, f):
        with open(os.path.join(root, d, f), encoding="ascii") as fh:
            return fh.read().strip()

    for d in names:
        try:
            if int(rd(d, "idVendor"), 16) != VID or int(rd(d, "idProduct"), 16) != PID:
                continue
            major, minor = (int(x) for x in rd(d, "dev").split(":"))
            found.append((int(rd(d, "busnum")), int(rd(d, "devnum")), major, minor))
        except (OSError, ValueError):
            continue
    return found


def ensure_dev_node(sysfs_root="/sys/bus/usb/devices", dev_root="/dev/bus/usb"):
    """Når pinnen settes inn mens appen kjører, får den et nytt nummer, og
    filen /dev/bus/usb/<bus>/<enhet> finnes ikke alltid inne i appen. Da
    lager vi den selv ut fra sysfs. Returnerer antall filer som ble laget."""
    made = 0
    for bus, devnum, major, minor in _sysfs_sticks(sysfs_root):
        path = os.path.join(dev_root, f"{bus:03d}", f"{devnum:03d}")
        if os.path.exists(path):
            continue
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            os.mknod(path, 0o660 | stat.S_IFCHR, os.makedev(major, minor))
            made += 1
            log.info("Created %s for the USB stick (plugged in after the app started)", path)
        except OSError as e:
            log.warning("The USB stick is plugged in, but the app could not access it (%s). "
                        "Restart the app.", e)
    return made


def _fresh_backend():
    """Ny libusb-sesjon for hvert søk. libusb husker hvilke enheter som fantes
    da den startet, og inne i appen får den ikke beskjed når en ny enhet
    kobles til. Uten dette ble pinnen ikke funnet igjen før omstart."""
    try:
        import usb.backend.libusb1 as lb
        if lb.get_backend() is None:
            return None
        return lb._LibUSB(lb._lib)   # noqa: SLF001
    except Exception as e:  # noqa: BLE001
        log.debug("Could not create a new libusb session: %s", e)
        return None


def _free_backend(backend):
    """Lukker libusb-sesjonen. finalize() i pyusb kan kalles flere ganger
    (libusb_exit direkte ga dobbel lukking og krasj når Python ryddet opp)."""
    try:
        backend.finalize()
    except Exception:  # noqa: BLE001
        pass


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
            log.info("pyusb is not installed -- the USB stick is not used")
            return None
        ensure_dev_node()
        backend = _fresh_backend()
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
        if dev is None:
            if backend is not None:
                _free_backend(backend)   # ingen enheter holder på den
            return None
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError) as e:
            log.debug("Could not detach the kernel driver: %s", e)
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
            raise IOError("could not find the endpoints on the USB stick")
        return cls(dev, ep_in, ep_out)

    def write(self, data):
        self.ep_out.write(data, timeout=1000)

    def read(self, timeout_ms):
        import usb.core
        # Les NØYAKTIG én pakke (62 bytes). Ber man om 64, venter libusb på en
        # pakke til etter en full 62-byte-pakke: enkeltmeldinger (som
        # kvitteringer) blir da borte, og to meldinger tett på hverandre gir
        # "[Errno 75] Overflow". Det var feilen i 2.5.0-2.6.1.
        size = getattr(self.ep_in, "wMaxPacketSize", 0) or PACKET
        timeout_error = getattr(usb.core, "USBTimeoutError", ())   # finnes i pyusb 1.2+
        try:
            return bytes(self.ep_in.read(size, timeout=timeout_ms))
        except timeout_error:
            return None
        except usb.core.USBError as e:
            errno = getattr(e, "errno", None)
            if errno == 110 or "timed out" in str(e).lower():
                return None
            if errno == 75 or "overflow" in str(e).lower():
                raise TransientUsbError(str(e))
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
        self._logged_open_fail = False
        self.rx_frames = 0
        self.rx_radio = 0
        self.rx_acks = 0
        self.recent = collections.deque(maxlen=20)   # siste rammer fra pinnen (for feilsøking)
        self.connected_at = 0.0
        self._warned_deaf = False
        self._read_errors = 0
        self._ack_cv = threading.Condition()
        self.retries = 0

    # -- tilkobling --
    def connect(self):
        """Prøver å åpne pinnen én gang. True hvis den er klar."""
        try:
            t = self.factory()
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            if not self._logged_open_fail:   # ikke spam loggen hvert 10. sekund
                self._logged_open_fail = True
                log.warning("The USB stick was found but could not be opened: %s", e)
            return False
        if t is None:
            if not self._logged_missing:
                log.info("No USB stick (CKOZ-00/13) found -- lights are controlled via Home Manager")
                self._logged_missing = True
            return False
        try:
            for f in INIT:
                t.write(self._pad(bytes.fromhex(f)))
                time.sleep(0.02)
            end = time.time() + 1.0
            while time.time() < end:
                try:
                    data = t.read(100)
                except TransientUsbError:
                    continue
                if data and b"CKOZ" in data:
                    self.name = data[3:data.index(b"\x00", 3)].decode("ascii", "replace") \
                        if b"\x00" in data[3:] else "CKOZ"
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            log.warning("The USB stick did not answer correctly at startup: %s", e)
            t.close()
            return False
        with self.lock:
            self.transport = t
            self.ok = True
            self.connected_at = time.time()
            self._warned_deaf = False
        self._logged_missing = False
        self._logged_open_fail = False
        log.info("USB stick ready (%s). Lights are controlled directly over radio, with Home Manager as fallback.",
                 self.name or "CKOZ-00/13")
        return True

    def run(self):
        """Holder pinnen åpen og leser alt den hører. Kjøres i egen tråd."""
        while not self.stopping:
            if not self.ok and not self.connect():
                time.sleep(RETRY_OPEN_S)
                continue
            t = self.transport
            if t is None:       # pinnen ble nettopp koblet fra av sendingen
                continue
            try:
                data = t.read(500)
                self._read_errors = 0
            except TransientUsbError as e:
                self._read_errors += 1
                self.last_error = f"read error: {e}"
                if self._read_errors >= 20:
                    self._broken(f"many read errors in a row: {e}")
                continue
            except Exception as e:  # noqa: BLE001
                self._broken(f"read error: {e}")
                continue
            if data:
                self.feed(data)
            elif (not self._warned_deaf and self.rx_radio == 0 and self.connected_at
                  and time.time() - self.connected_at > 120):
                self._warned_deaf = True
                log.warning("The USB stick has not heard any radio messages for 2 minutes. Commands can still "
                            "be sent, but acknowledgements are not received -- Home Manager is used to confirm.")

    def feed(self, data):
        if not data:
            return
        self.rx_frames += 1
        self.recent.append((time.strftime("%H:%M:%S"), bytes(data[:64]).rstrip(b"\x00").hex(" ")))
        if data[0] != 0x5A and len(data) > 1 and data[1] == 0x5A:
            data = data[1:]  # rapport-ID foran
        if data[0] != 0x5A or len(data) < 3:
            return
        frame = data[:data[1]]
        info = parse_radio(frame)
        if not info:
            return
        self.rx_radio += 1
        if self.rx_radio <= 3:
            log.info("The USB stick hears radio: %s", frame.hex(" "))
        _kind, src, dst = info
        if dst == STICK_ADDR:
            self.rx_acks += 1
            with self._ack_cv:
                self.last_ack[src] = time.time()
                self._ack_cv.notify_all()
        if self.on_radio:
            try:
                self.on_radio(frame, info)
            except Exception:  # noqa: BLE001
                log.exception("Error while handling a radio message")

    def _broken(self, why):
        log.warning("The USB stick stopped working (%s) -- using Home Manager until it is back", why)
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

    def _wait_ack(self, serial, since, timeout):
        with self._ack_cv:
            return self._ack_cv.wait_for(lambda: self.acked_since(serial, since), timeout)

    def set_levels(self, targets):
        """Sender [(serienr, prosent), ...] én og én. Etter hver melding venter
        vi kort på kvitteringen fra dimmeren før neste sendes, og prøver én
        gang til hvis den ikke kom. Det gir færre kollisjoner på radio enn å
        sende alt på en gang (pinnen kan ikke høre mens den sender).

        Returnerer (t0, {serienr som kvitterte}) eller None hvis pinnen ikke
        virker (da skal Home Manager brukes)."""
        if not self.healthy():
            return None
        t0 = time.time()
        acked = set()
        budget_end = t0 + ACK_BUDGET_S
        with self.lock:
            try:
                for i, (serial, pct) in enumerate(targets):
                    for attempt in (1, 2):
                        sent_at = time.time()
                        self.transport.write(self._pad(level_frame(serial, pct, self._next_seq())))
                        self.sent += 1
                        wait = min(ACK_WAIT_S, budget_end - time.time())
                        if wait > 0 and self._wait_ack(serial, sent_at, wait):
                            acked.add(int(serial))
                            break
                        if attempt == 1 and time.time() < budget_end:
                            self.retries += 1
                            continue
                        break
                    if i < len(targets) - 1:
                        time.sleep(FRAME_GAP_S)
            except Exception as e:  # noqa: BLE001
                self.failed += 1
                broken = str(e)
            else:
                return t0, acked
        self._broken(f"write error: {broken}")
        return None

    def acked_since(self, serial, t0):
        t = self.last_ack.get(int(serial))
        return t is not None and t >= t0

    def snapshot(self):
        return {"tilkoblet": self.healthy(), "navn": self.name, "sendt": self.sent,
                "feilet": self.failed, "siste_feil": self.last_error,
                "omsendt": self.retries, "mottatt": self.rx_frames, "radio_mottatt": self.rx_radio, "kvitteringer": self.rx_acks,
                "siste_rammer": list(self.recent)[-10:]}
