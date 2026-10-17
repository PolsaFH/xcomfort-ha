"""
xcomfort_local_control.py
==========================
Styrer xComfort-lamper direkte over ditt lokale nettverk, HELT UTEN aa
bruke skyen eller eoptHome-appen -- ved aa snakke det samme raa
TCP-protokollen som appen selv bruker mot Home Manager-kontrolleren din,
paa port 10023.

Oppdaget ved aa fange trafikken mellom telefonen og kontrolleren (Wireshark
+ mobil-hotspot-triks) mens "Taklampe fin stue" ble dimmet og skrudd av i
eoptHome-appen.

=== Protokollen (raatekst over TCP, INGEN kryptering) ===

Hver melding -- begge veier -- er pakket inn i to usynlige kontrollbyte:
0x01 (start) foerst, 0x02 (slutt) sist, rundt selve teksten. Det er IKKE
vanlig linjeskift som skiller meldinger (det saa vi foerst feil paa --
derfor virket ikke forrige versjon).

  <- \x01<connect sn="..." ip="..." mac="..." .../>\x02   Kontrolleren
        sender denne foerst, rett etter at vi kobler til.
  -> \x01d_obj <id>\x02                             Abonner paa en variabel
                                                      (kontrolleren sender da
                                                      gjeldende verdi).
  <- \x01SSN <id> <navn> <type> <flag> <flag> <verdi>\x02\x00
                                                      Svar/oppdatering fra
                                                      kontrolleren.
  -> \x01set_value <id> <verdi>\x02                  Sett en verdi -- for en
                                                      dimbar lampe: 0-100
                                                      (0 = av, 100 = full
                                                      lysstyrke).
  -> \x01new_state <id> <verdi>\x02                  "Utfoer" -- trigger at
                                                      verdien faktisk skal tas
                                                      i bruk.

=== Hvorfor tre ulike ID-er per lampe? ===

Hver dimbar lampe har flere interne objekter bak seg i kontrolleren:
  D_<Navn>          selve enheten -- dette er "smuxId" du finner igjen i
                     eoptHome sin sky-database (se eopthome_api_test.py).
  M_D_<Navn>_Val     oensket/gjeldende lysstyrke, 0-100.
  B_D_<Navn>_SET     "utfoer"-knappen -- send new_state hit for at
                      lysstyrken i M_..._Val faktisk skal settes.

Disse tre ID-ene er IKKE forutsigbare tall (ingen fast avstand fra
smuxId -- vi sjekket, det varierer fra lampe til lampe). Derfor maa
scriptet "oppdage" Val- og SET-id for hver ny lampe FOERSTE gang du
styrer den (ved aa abonnere paa mange ID-er rundt smuxId og se etter
riktig navn i svarene). Etter det lagres de i xcomfort_object_cache.json
ved siden av dette scriptet, saa neste gang er det momentant.

=== Bruk ===

    python xcomfort_local_control.py "taklampe fin stue" 60      (paa, 60%)
    python xcomfort_local_control.py "taklampe fin stue" 0       (av)
    python xcomfort_local_control.py 41512 100                   (via smuxId)

Ingen pip install noedvendig -- bruker kun Python sin standardbibliotek.
"""

import json
import logging
import os
import re
import socket
import sys
import time

# Felles logger. Broen (xcomfort_mqtt_bridge.py) setter opp format med
# millisekunder og loggnivaa; naar denne fila kjoeres som CLI settes et
# enkelt format opp i main().
_log = logging.getLogger("xcomfort.protokoll")

CONTROLLER_IP = os.environ.get("XCOMFORT_IP", "192.168.50.169")   # Home Manager sin lokale IP paa nettet ditt
CONTROLLER_PORT = int(os.environ.get("XCOMFORT_PORT", "10023"))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Naar dette kjoerer som Home Assistant-tillegg ligger koden i en Docker-
# beholder som nullstilles ved oppdateringer -- /data er derimot en mappe
# tillegget faar beholde paa tvers av restarter/oppdateringer, saa vi lagrer
# cachen der hvis den finnes (dvs. naar vi kjoerer som tillegg).
if os.path.isdir("/data"):
    CACHE_FILE = "/data/xcomfort_object_cache.json"
else:
    CACHE_FILE = os.path.join(SCRIPT_DIR, "xcomfort_object_cache.json")

SCAN_RANGE = 1000    # hvor langt vi leter rundt smuxId etter Val/SET-id
BATCH_SIZE = 40       # antall d_obj-abonnementer per "bolk"

# Vi har observert at avstanden fra en lampes egen ID (D_...) til dens
# Val- og SET-ID er den SAMME for lamper av samme "type" i anlegget --
# f.eks. alle "Taklampe"-dimmere bruker +171/+219, alle "Downlight"-
# dimmere bruker +118/+166. Dette er IKKE garantert universelt (det kan
# godt finnes unntak), saa vi bruker det kun som et raskt foerste gjett --
# og verifiserer alltid at navnet faktisk stemmer foer vi stoler paa det.
# Om gjettet er feil, faller vi automatisk tilbake til det trege men
# sikre full-vindu-soeket.
KNOWN_OFFSETS = [(171, 219), (118, 166)]

STX = b"\x01"   # start-byte foran hver melding
ETX = b"\x02"   # slutt-byte etter hver melding

# Finn "SSN <id> <navn>" eller "RSN <id> <navn>" hvor som helst i den raa
# byte-stroemmen -- selv om det ligger kontrollbyte/oid-GUID-er rundt.
SSN_RE = re.compile(r"(?:SSN|RSN) (\d+) (\S+)")


class XComfortConn:
    def __init__(self, ip=None, port=None, timeout=5.0):
        # Leses ved hvert kall (ikke ved import), saa broen kan sette
        # CONTROLLER_IP/PORT fra appens innstillinger.
        ip = ip or CONTROLLER_IP
        port = port or CONTROLLER_PORT
        self.sock = socket.create_connection((ip, port), timeout=timeout)
        self.buf = b""
        # Vent paa <connect .../> handshake fra kontrolleren foerst.
        # 2026-10 (v2): sjekker naa at handshaken faktisk kom. Foer gikk vi
        # videre uansett etter 5 sek, og sendte da kommandoer til en
        # kontroller som kanskje ikke var klar -- de forsvant stille.
        self._read_until(b"/>", max_wait=timeout)
        if b"connect" not in self.buf:
            got = self.buf[:120]
            self.close()
            raise ConnectionError(f"Fikk ikke <connect .../> fra Home Manager innen {timeout:.0f}s (mottok {got!r})")
        self.buf = b""  # ferdig med handshaken, ikke la den forstyrre parsing senere
        # Kort timeout paa recv() fremover -- ellers blokkerer hvert kall i
        # helt opp til "timeout" sekunder naar kontrolleren rett og slett
        # ikke svarer (f.eks. paa en ID som ikke finnes), og hele
        # oppdagelses-soekingen blir treig som melasse.
        self.sock.settimeout(0.15)

    def _read_chunk(self):
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return b""
        if not data:
            return b""
        return data

    def _read_until(self, marker, max_wait=5.0):
        start = time.time()
        while marker not in self.buf and time.time() - start < max_wait:
            self.buf += self._read_chunk()
        return self.buf

    def send_line(self, line):
        """Sender en kommando riktig innpakket: 0x01 <tekst> 0x02"""
        _log.debug("-> %s", line)
        self.sock.sendall(STX + line.encode("ascii") + ETX)

    def drain(self, wait=1.0):
        """Les alt kontrolleren sender de neste 'wait' sekundene, som tekst."""
        start = time.time()
        while time.time() - start < wait:
            chunk = self._read_chunk()
            if chunk:
                self.buf += chunk
            else:
                time.sleep(0.05)
        out, self.buf = self.buf, b""
        text = out.decode("latin-1", errors="replace")
        if text and _log.isEnabledFor(logging.DEBUG):
            _log.debug("<- %s", readable(text))
        return text

    def close(self):
        """Lukker pent: si fra at vi er ferdige med aa sende (FIN), les det
        som evt. fortsatt er paa vei inn, og lukk. Lukker man en socket med
        uleste data i mottaksbufferet sender Linux RST i stedet for FIN, og
        da kan motparten forkaste data den ikke har rukket aa lese enda."""
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except Exception:
            pass
        try:
            self.sock.settimeout(0.2)
            end = time.time() + 0.3
            while time.time() < end:
                if not self.sock.recv(65536):
                    break
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def readable(text):
    """Gjoer raa protokolltekst lesbar i loggen (kontrollbyte -> | )."""
    return text.replace("\x01", "").replace("\x02", " | ").replace("\x00", "").strip(" |")


def parse_ssn_names(text):
    """Plukk ut alle (id, navn)-par fra SSN- og RSN-meldinger i mottatt tekst."""
    found = {}
    for oid_str, name in SSN_RE.findall(text):
        found[int(oid_str)] = name
    return found


# Noen lampenavn er saa lange at kontrolleren KUTTER AV navnet ved 30
# tegn naar den rapporterer det tilbake -- da blir f.eks.
# "M_D_TaklampePaviljongPaviljong_Val" til "M_D_TaklampePaviljongPaviljon"
# (uten "g_Val" paa slutten), og den eksakte navnesammenligningen i
# discover_device_objects() feiler, sjoelv om avstanden fra smuxId er
# helt vanlig (171/219). "Taklampe Paviljong" (smuxId 38534) er et slikt
# tilfelle -- bekreftet 2026-08-28 med wide_scan.py (fant riktig id-er
# paa noeyaktig +171/+219, som var lengre unna enn vanlig SCAN_RANGE
# turte lete siden navnesammenligningen feilet foer avstanden ble
# vurdert). Lagt inn haardkodet her saa vi slipper aa gjette/skanne paa
# nytt -- samme moenster som MANUAL_SWITCH_OVERRIDES under.
MANUAL_LIGHT_OVERRIDES = {
    38534: {"name": "D_TaklampePaviljongPaviljong", "val_id": 38705, "set_id": 38753},
}


def discover_device_objects(conn, smux_id, quiet=False):
    """Finn Val- og SET-objekt-ID for en gitt lampe (smuxId). Proever
    foerst de kjente avstandene (raskt), og faller tilbake til aa
    abonnere paa et helt vindu av ID-er rundt smuxId hvis ingen av
    gjettene stemte."""
    if smux_id in MANUAL_LIGHT_OVERRIDES:
        return dict(MANUAL_LIGHT_OVERRIDES[smux_id])

    conn.send_line(f"d_obj {smux_id}")
    text = conn.drain(wait=1.0)
    names = parse_ssn_names(text)
    base_name = names.get(smux_id)
    if not base_name or not base_name.startswith("D_"):
        return None
    key = base_name[2:]  # fjern "D_"-prefiks
    want_val = f"M_D_{key}_Val"
    want_set = f"B_D_{key}_SET"

    # -- Raskt forsoek: proev kjente avstander foerst --
    candidate_ids = []
    for off_val, off_set in KNOWN_OFFSETS:
        candidate_ids += [smux_id + off_val, smux_id + off_set]
    cmd = b"".join(STX + f"d_obj {oid}".encode("ascii") + ETX for oid in candidate_ids)
    conn.sock.sendall(cmd)
    text = conn.drain(wait=0.4)
    names = parse_ssn_names(text)
    val_id = next((oid for oid, n in names.items() if n == want_val), None)
    set_id = next((oid for oid, n in names.items() if n == want_set), None)
    if val_id and set_id:
        return {"name": base_name, "val_id": val_id, "set_id": set_id}

    if not quiet:
        print(f"  Enhetsnavn funnet: {base_name}  (raskt gjett feilet, leter grundigere etter "
              f"{want_val} / {want_set} ...)")

    lo = max(1, smux_id - SCAN_RANGE)
    hi = smux_id + SCAN_RANGE
    all_ids = list(range(lo, hi + 1))
    val_id = None
    set_id = None
    for i in range(0, len(all_ids), BATCH_SIZE):
        if val_id and set_id:
            break
        batch = all_ids[i:i + BATCH_SIZE]
        cmd = b"".join(STX + f"d_obj {oid}".encode("ascii") + ETX for oid in batch)
        conn.sock.sendall(cmd)
        text = conn.drain(wait=0.5)
        names = parse_ssn_names(text)
        for oid, name in names.items():
            if name == f"M_D_{key}_Val":
                val_id = oid
            elif name == f"B_D_{key}_SET":
                set_id = oid
    return {"name": base_name, "val_id": val_id, "set_id": set_id}


def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(cache):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"(Klarte ikke lagre cache: {e})")


def get_device_objects(conn, smux_id, cache):
    key = str(smux_id)
    if key in cache and cache[key].get("val_id") and cache[key].get("set_id"):
        return cache[key]
    _log.info("Ny lampe (smuxId=%s) -- leter etter styre-objekter (skjer kun foerste gang)...", smux_id)
    info = discover_device_objects(conn, smux_id, quiet=True)
    if not info or not info.get("val_id") or not info.get("set_id"):
        _log.warning("Fant IKKE styre-objekter for lampe %s (utenfor +/-%s rundt smuxId, eller ikke en dimmer).",
                     smux_id, SCAN_RANGE)
        return None
    cache[key] = info
    save_cache(cache)
    _log.info("  Funnet: Val-id=%s  SET-id=%s  (lagret til neste gang)", info["val_id"], info["set_id"])
    return info


def set_light(conn, smux_id, brightness, cache):
    """brightness: 0-100 (0 = av)"""
    info = get_device_objects(conn, smux_id, cache)
    if not info:
        return False
    brightness = max(0, min(100, int(brightness)))
    conn.send_line(f"set_value {info['val_id']} {brightness}")
    time.sleep(0.1)
    conn.send_line(f"new_state {info['set_id']} 0")
    conn.drain(wait=0.5)
    return True


# Noen enheter har saa lange navn at kontrolleren KUTTER AV suffikset
# (_On/_Off) naar den rapporterer navnet tilbake -- da ser vi bare
# "B_R_...ys_" og kan ikke lenger kjenne igjen hvilken knapp som er hvem
# ut fra navnet. "Gaardsplass lyskaster" (smuxId 101812) er et slikt
# tilfelle -- oppdaget og bekreftet FYSISK av Filip 2026-08-28 ved aa
# proeve begge knappene direkte (se raw_probe.py): 102021 = paa, 102097 =
# av. Lagt inn haardkodet her saa vi slipper aa gjette/teste paa nytt.
MANUAL_SWITCH_OVERRIDES = {
    101812: {"name": "R_GrdsplasslyskasterUtelys", "on_id": 102021, "off_id": 102097},
}


def scan_objects_window(conn, center_id, window=1000, batch_size=BATCH_SIZE, name_filter=None):
    """Feilsoekings-/oppdagelsesverktoy: abonnerer paa ALLE ID-er i vinduet
    [center_id-window, center_id+window] og returnerer {id: navn} for hver
    som faktisk svarer med et navn. Brukes til aa finne en ENDA UKJENT
    xComfort-objekt-ID (f.eks. et temperatur-boerverdi-objekt for
    ettervarme) naar vi bare vet at det ligger et sted naer en allerede
    kjent enhet (f.eks. paa/av-bryteren for samme funksjon).

    name_filter (valgfri): kun behold treff der navnet (uavhengig av store/
    smaa bokstaver) inneholder minst ett av ordene i denne listen -- gjoer
    output lesbar naar vinduet er stort og fullt av andre enheters
    objekter. Uten filter returneres ALT som ble funnet."""
    lo = max(1, center_id - window)
    hi = center_id + window
    all_ids = list(range(lo, hi + 1))
    found = {}
    for i in range(0, len(all_ids), batch_size):
        batch = all_ids[i:i + batch_size]
        cmd = b"".join(STX + f"d_obj {oid}".encode("ascii") + ETX for oid in batch)
        conn.sock.sendall(cmd)
        text = conn.drain(wait=0.5)
        found.update(parse_ssn_names(text))
    if name_filter:
        words = [w.lower() for w in name_filter]
        found = {oid: name for oid, name in found.items()
                 if any(w in name.lower() for w in words)}
    return found


def discover_switch_objects(conn, smux_id, quiet=False):
    """Brytere (terrassevarmer/vifte/lyskaster) viste seg IKKE aa foelge
    samme D_/M_D_..._Val/B_..._SET-moenster som lysene: selve smuxId-en
    ER status-objektet (et 'TS_...'-objekt som rapporterer 0/1 av seg
    selv, akkurat som sensorene), og styringen skjer med to separate
    "knapper" et sted i naerheten -- B_..._On og B_..._Off -- som vi
    trigger direkte med new_state (som om noen trykket paa knappen).

    Siden det ofte finnes FLERE _On/_Off-objekter i skannevinduet (f.eks.
    fra helt andre enheter), matcher vi kandidatene mot navnet paa selve
    TS_-objektet (uten TS_-prefikset) for aa finne riktig par -- ikke
    bare naermeste ID."""
    if smux_id in MANUAL_SWITCH_OVERRIDES:
        return dict(MANUAL_SWITCH_OVERRIDES[smux_id])

    conn.send_line(f"d_obj {smux_id}")
    text = conn.drain(wait=1.0)
    names = parse_ssn_names(text)
    ts_name = names.get(smux_id)
    if not ts_name:
        return None
    core = ts_name.split("_", 1)[1] if "_" in ts_name else ts_name
    core_lower = core.lower()

    lo = max(1, smux_id - SCAN_RANGE)
    hi = smux_id + SCAN_RANGE
    all_ids = list(range(lo, hi + 1))
    found = {}
    for i in range(0, len(all_ids), BATCH_SIZE):
        batch = all_ids[i:i + BATCH_SIZE]
        cmd = b"".join(STX + f"d_obj {oid}".encode("ascii") + ETX for oid in batch)
        conn.sock.sendall(cmd)
        t = conn.drain(wait=0.5)
        found.update(parse_ssn_names(t))

    def matches(name, suffix):
        # Suffikset kan komme som "_On"/"_Off" (terrassevarmer/vifte) ELLER
        # "_ON"/"_OFF" med store bokstaver (mange av lysene) -- sjekk
        # uavhengig av store/smaa bokstaver.
        if not name.lower().endswith(suffix.lower()):
            return False
        body = name[:-len(suffix)]
        if body.startswith("B_"):
            body = body[2:]
        # Mange av knappene har ogsaa et "R_"-prefiks etter "B_" (f.eks.
        # "B_R_SpeilBadlite_ON") -- fjern det ogsaa, ellers kan den loese
        # "R"-bokstaven feilaktig IKKE matche naar kjernenavnet ikke
        # tilfeldigvis inneholder bokstaven "r" (som skjedde med "Speil").
        if body.startswith("R_"):
            body = body[2:]
        parts = [p for p in body.split("_") if p]
        return bool(parts) and all(p.lower() in core_lower for p in parts)

    on_candidates = [oid for oid, n in found.items() if matches(n, "_On")]
    off_candidates = [oid for oid, n in found.items() if matches(n, "_Off")]
    if not on_candidates or not off_candidates:
        if not quiet:
            print(f"  Fant ikke entydig On/Off-knapp for {ts_name} "
                  f"(kandidater: on={on_candidates}, off={off_candidates})")
            print("  Tips: hvis navnet er langt, kan kontrolleren ha kuttet av")
            print("  _On/_Off-suffikset i svaret (se raw_probe.py for aa teste")
            print("  kandidat-ID-er direkte og legge dem til i MANUAL_SWITCH_OVERRIDES).")
        return None
    on_id = min(on_candidates, key=lambda oid: abs(oid - smux_id))
    off_id = min(off_candidates, key=lambda oid: abs(oid - smux_id))
    return {"name": ts_name, "on_id": on_id, "off_id": off_id}


def get_switch_objects(conn, smux_id, cache):
    key = str(smux_id)
    if key in cache and cache[key].get("on_id") and cache[key].get("off_id"):
        return cache[key]
    _log.info("Ny bryter (smuxId=%s) -- leter etter av/paa-knapper (skjer kun foerste gang)...", smux_id)
    info = discover_switch_objects(conn, smux_id, quiet=True)
    if not info or not info.get("on_id") or not info.get("off_id"):
        _log.warning("Fant IKKE av/paa-knapper for bryter %s. Se naermere paa den med debug_switch.py.", smux_id)
        return None
    cache[key] = info
    save_cache(cache)
    _log.info("  Funnet: On-id=%s  Off-id=%s  (lagret til neste gang)", info["on_id"], info["off_id"])
    return info


def set_switch(conn, smux_id, on, cache):
    """Trigger av/paa-knappen direkte -- ingen set_value her, bare
    new_state paa riktig knapp-id, akkurat som et fysisk knappetrykk."""
    info = get_switch_objects(conn, smux_id, cache)
    if not info:
        return False
    target_id = info["on_id"] if on else info["off_id"]
    conn.send_line(f"new_state {target_id} 0")
    conn.drain(wait=0.5)
    return True


def read_switch_state(conn, smux_id, wait=1.0):
    """Les naavaerende av/paa-tilstand direkte fra smuxId (TS_-objektet
    rapporterer tilstanden sin selv, akkurat som sensorene gjoer).

    VIKTIG: dette er IKKE samme feltposisjon som read_sensor_value bruker.
    Bekreftet 2026-08-28 ved aa sammenligne raatt svar av/paa for
    TS_Hast2Ventilasjon:
        av:  "8 1 0 0"
        paa: "8 1 1 0"
    Det er det TREDJE tallet som endrer seg (0/1) -- det siste tallet
    stod fast paa 0 i begge tilfeller. read_sensor_value fanger derimot
    det SISTE tallet (riktig for kontinuerlige sensorer som Sol/Vind/
    Utetemperatur, som rapporterer en desimalverdi helt til slutt), saa
    den kan IKKE gjenbrukes direkte for brytere -- vi maa lese det tredje
    feltet spesifikt her."""
    conn.send_line(f"d_obj {smux_id}")
    text = conn.drain(wait=wait)
    m = re.search(rf"(?:SSN|RSN) {smux_id} \S+ \d+ \d+ (-?\d+(?:\.\d+)?)", text)
    if not m:
        return None
    return float(m.group(1)) > 0


def read_sensor_value(conn, smux_id, wait=1.5):
    """Sensorer (Sol foeler, Vind foeler) er markert 'report: true' -- de
    sender verdien sin med en gang vi abonnerer paa smuxId direkte, det
    finnes ingen egen Val-/SET-id aa lete etter slik lysene har."""
    conn.send_line(f"d_obj {smux_id}")
    text = conn.drain(wait=wait)
    m = re.search(rf"(?:SSN|RSN) {smux_id} \S+ \d+ \d+ \d+ (-?\d+(?:\.\d+)?)", text)
    if m:
        return float(m.group(1))
    return None


def read_named_sensor(conn, smux_id, unit, wait=None):
    """Leser en oppfoert SENSORS-rad RIKTIG uansett hvilken av de to
    familiene den tilhoerer -- IKKE bare kall read_sensor_value direkte
    paa alt i SENSORS.

    Alarm-/statussensorene (Drift/Tett filter/Overoppheting -- de med tom
    enhet "" i SENSORS) er samme type boolske TS_-objekt som bryterne, og
    har DERFOR samme feltposisjons-avvik vi fant og fikset for
    read_switch_state 2026-08-28 (verdien staar i det TREDJE feltet, ikke
    det siste). De kontinuerlige sensorene (Sol/Vind/Utetemperatur/Retur/
    Tur -- de MED enhet) rapporterer derimot en desimalverdi helt til
    slutt, som read_sensor_value leser riktig.

    Bruk denne i stedet for aa velge lesefunksjon manuelt, saa kan ikke
    dashboard/CLI/bro utilsiktet drifte fra hverandre igjen paa dette."""
    if unit == "":
        v = read_switch_state(conn, smux_id, wait=wait if wait is not None else 1.0)
        if v is None:
            return None
        return 1.0 if v else 0.0
    return read_sensor_value(conn, smux_id, wait=wait if wait is not None else 1.5)


# ---- Alle 50 dimbare lys i huset, hentet fra prosjekt-eksporten (setup-
# kallet i eopthome_api_test.py) -- (Sone, Lampenavn, smuxId). Sonene er
# NOEYAKTIG de samme som "Sone"-kolonnen i xSetup sin Datapunkter-liste
# (Ute, Hus, Paviljong, Stue, Filip, Bad stort, Bad lite, ...). Alle
# "GV ..."-enheter er varme-innganger, ikke lys, og er ikke med her.
LIGHTS = [
    ("Bad kjeller", "Downlight", 93975),
    ("Bad lite", "Downlight", 74113),
    ("Bad stort", "Downlight", 95291),
    ("Bad stort", "Vegglys", 92659),
    ("Filip", "Downlight gr. 1", 123038),
    ("Filip", "Downlight gr. 2", 43001),
    ("Gang 2.etg", "Spottskinner", 80569),
    ("Garderobe", "Downlight", 69155),
    ("Hall", "Downlight", 113518),
    ("Hall kjeller", "Downlight", 60167),
    ("Hall kjeller", "Kjøkken", 100448),
    ("Kjellerstue", "Downlight", 91343),
    ("Kjøkken", "Downlight gr. 1", 110886),
    ("Kjøkken", "Downlight gr. 2", 116150),
    ("Kjøkken", "Downlight gr. 3", 87213),
    ("Kjøkken", "Lamper o/benk", 96607),
    ("Kjøkken", "Lamper o/bord", 88529),
    ("Kjøkken", "Skap", 71787),
    ("Kontor", "Downlight", 120406),
    ("Lillian", "Downlight gr. 1", 56326),
    ("Lillian", "Downlight gr. 2", 102774),
    ("Loftstue", "Downlight gr. 1", 121722),
    ("Loftstue", "Downlight gr. 2", 118908),
    ("Loftstue", "Lamper trapp", 112202),
    ("Paviljong", "Taklampe Paviljong", 38534),
    ("Sov hoved", "Downlight", 78243),
    ("Spisestue", "Downlight", 66829),
    ("Spisestue", "Lamper o/bord", 84517),
    ("Stue", "Downlight gr. 1", 81885),
    ("Stue", "Downlight gr. 2", 53694),
    ("Stue", "Taklampe TV", 40023),
    ("Stue", "Taklampe fin stue", 41512),
    ("Utelys", "DL 2.etg", 114834),
    ("Utelys", "DL Garasje", 55010),
    ("Utelys", "DL gavel", 109570),
    ("Utelys", "DL inngang", 52378),
    ("Utelys", "DL sørvest", 61483),
    ("Utelys", "DL sørøst", 75611),
    ("Utelys", "DL terasse", 70471),
    ("Utelys", "DL uthus", 126687),
    ("Utelys", "Innkjøring", 62799),
    ("Utelys", "VL 2.etg", 104719),
    ("Utelys", "VL Garasje", 108254),
    ("Utelys", "VL Gårdsplasss", 49465),
    ("Utelys", "VL nordvest", 125172),
    ("Utelys", "VL nordøst", 98122),
    ("Utelys", "VL sørvest", 58851),
    ("Utelys", "VL uthus", 65513),
    ("Vaskerom", "Downlight", 76927),
    ("WC", "Downlight", 89845),
]

# ---- Brytere (av/paa, ingen dimming): terrassevarmere, vifte, lyskaster,
# og en gjeng ikke-dimbare lys (samme "deviceType"-familie -- de var ikke
# med i LIGHTS-lista over fordi de er av/paa-lys, ikke dimmere). Alle
# foelger IKKE samme moenster som de dimbare lysene (se
# discover_switch_objects over) -- selve smuxId-en er et status-objekt,
# og av/paa skjer via egne On-/Off-knapper vi finner/lagrer foerste gang.
SWITCHES = [
    ("Ute", "Terassevarmer1", 104289),
    ("Ute", "Terassevarmer2", 124742),
    ("Ute", "Vifte", 126488),
    ("Ute", "Gårdsplass lyskaster", 101812),
    # ---- Av/paa-lys (ikke dimbare) -- lagt til 2026-08-28, sone hentet
    # direkte fra anleggets sone-liste:
    ("Utelys", "LED Trapp", 106282),
    ("Hall kjeller", "Trapp", 117514),
    ("Hall", "Vegg trapp", 79607),
    ("Garasje", "Taklys", 68193),
    ("Bad stort", "LED", 86251),
    ("Bad lite", "Speil", 99486),
    ("Bad kjeller", "Speil", 64163),
    ("Bod m/hems", "Bod m/hems", 107292),
    ("Bod nord", "Bod nord", 57690),
    ("Bod liten", "Bod liten 2.etg", 51416),
    # ---- Flere brytere (varme/annet) -- lagt til 2026-08-28:
    ("Ventilasjon", "Ettervarme", 128003),
    ("Ute", "Fontenehage", 73151),
    # ---- Viftehastighet: DISSE (ikke "Vent hast"-verdien) er den ekte
    # styringen -- de to reléene eoptHome-appen selv har egne av/på-
    # brytere for. Legg til som vanlige brytere og test hver for seg.
    ("Ventilasjon", "Hast. 2", 58652),
    ("Ventilasjon", "Hast. 3", 97923),
    # ---- Ettervarme-temperatur (17/20/23 grader i eoptHome) -- funnet via
    # eoptHome sitt eget sky-API (finn_ettervarme_cloud.py) 2026-09-20,
    # etter at den gamle Xstudio-macroen ("vent_temp.txt") viste at de tre
    # knappene IKKE er en fri temperaturverdi, men to reléer som settes i
    # kombinasjon (nøyaktig samme oppskrift som "Vent hast" brukte for
    # vifthastighet, der de virkelige reléene -- Hast. 2/Hast. 3 over --
    # viste seg aa vaere den ekte styringen mens selve "Memory"-verdien var
    # doed/frakoblet). "Vent temp"-verdien (smuxId 91161, samme deviceType
    # som "Vent hast") er derfor IKKE lagt til her -- vi styrer reléene
    # direkte, akkurat som med vifthastigheten:
    #   17°: Temp. 1 = PAA, Temp. 2 = AV
    #   20°: Temp. 1 = AV,  Temp. 2 = AV
    #   23°: Temp. 1 = AV,  Temp. 2 = PAA
    # Se ETTERVARME_TEMP_RELAY_STATE lenger ned og HA-helperen/automasjonen
    # "Ettervarme temperatur" som bruker disse to bryterne.
    ("Ventilasjon", "Ettervarme Temp. 1", 106035),
    ("Ventilasjon", "Ettervarme Temp. 2", 104090),
]

# a==0 -> 17°, a==1 -> 20°, a==2 -> 23° (samme rekkefølge som den gamle
# macroen brukte for "Vent temp:Memory"). Brukes bare som dokumentasjon/
# oppslag her -- selve settingen skjer nå fra Home Assistant-siden (se
# automasjonen "Ettervarme temperatur"), som kaller de to bryterne over
# direkte via MQTT, akkurat som alle andre brytere.
ETTERVARME_TEMP_RELAY_STATE = {
    17: (True, False),
    20: (False, False),
    23: (False, True),
}

# ---- Sensorer (kun avlesning): Sol foeler, Vind foeler. Disse rapporterer
# verdien sin selv naar vi abonnerer -- ingen Val-/SET-id-oppdagelse
# noedvendig, bare (Sone, Navn, smuxId, Enhet).
SENSORS = [
    ("Ute", "Sol føler", 19974, "lux"),
    ("Ute", "Vind føler", 20027, "m/s"),
    # Utetemperaturen (TS_Ute) sitter fysisk ved ventilasjonsinntaket, men
    # er en ekte, generell utemåling -- bekreftet 18.2 grader 2026-08-28.
    ("Ute", "Utetemperatur", 28861, "°C"),
    # Ventilasjonsanleggets egne alarmer/status -- rapporterer 0 (inaktiv)
    # eller 1 (aktiv), akkurat som de andre sensorene.
    ("Ventilasjon", "Drift", 234899, ""),
    ("Ventilasjon", "Tett filter", 231939, ""),
    ("Ventilasjon", "Overoppheting", 232677, ""),
    # Tilluft/avtrekk-temperatur i selve ventilasjonsanlegget (rene
    # informasjonssensorer, samme fysiske enhet -- serienr. 2194897).
    ("Ventilasjon", "Retur", 29019, "°C"),
    ("Ventilasjon", "Tur", 19921, "°C"),
    # Markisenes egne "Kjøretid"-innstillinger (sekunder) -- eksponeres som
    # vanlige sensorer slik at en Home Assistant-automasjon kan lese SAMME
    # tall som xComfort-kontrolleren selv bruker, i stedet for at vi
    # dupliserer tallet i en egen HA-helper som kan drifte ut av synk.
    # Id-ene er de samme som KJORETID_PAVILJON_ID/KJORETID_BALKONG_ID/
    # KJORETID_BOBLEBAD_ID lenger ned i fila.
    ("Ute", "Kjøretid Paviljon", 47931, "s"),
    ("Ute", "Kjøretid Balkong", 48113, "s"),
    ("Ute", "Kjøretid Boblebad", 47671, "s"),
]

# ---- Viftehastighet: DEN GAMLE koden her brukte en løs, "innstilling"-
# verdi (deviceType 0, "Vent hast", smuxId 75429) som viste seg aa vaere
# en dod/frakoblet konfigurasjonsverdi -- bekreftet 2026-08-28 baade med
# direkte set_value OG med Inc/Dec-knappetrykk: TALLET endret seg, men
# INGENTING fysisk skjedde (ingen forskjell i eoptHome-appen eller lyden
# fra vifta). Fjernet.
#
# Den EKTE styringen er de to vanlige bryterne "Hast. 2" (58652) og
# "Hast. 3" (97923) i SWITCHES-listen over -- de samme to som eoptHome-
# appen selv viser som "Hast 2"/"Hast 3"-brytere. Bekreftet fysisk
# 2026-08-28 (hoerbar forskjell i ventilasjonen ved av/paa). De styres
# helt vanlig med de vanlige bryter-kommandoene, f.eks.:
#   python xcomfort_local_control.py "hast. 2" på
#   python xcomfort_local_control.py "hast. 3" av
#
# Vi har IKKE faatt bekreftet den noeyaktige kombinasjonen->navngitt
# nivaa-tabellen (hva "begge av" / "bare 2 paa" / "bare 3 paa" betyr i
# lav/normal/hoey-termer), saa "viftehastighet"-kommandoen under viser
# bare status for begge bryterne raatt i stedet for aa gjette et navn.


def read_vent_speed(conn, wait=1.0):
    """Leser status for begge de ekte fartsbryterne (Hast. 2 / Hast. 3)."""
    hast2_id = KNOWN_SWITCHES.get("hast. 2")
    hast3_id = KNOWN_SWITCHES.get("hast. 3")
    if hast2_id is None or hast3_id is None:
        return None
    hast2 = read_switch_state(conn, hast2_id, wait=wait)
    hast3 = read_switch_state(conn, hast3_id, wait=wait)

    def fmt(v):
        if v is None:
            return "ukjent"
        return "på" if v else "av"

    return f"Hast. 2={fmt(hast2)}, Hast. 3={fmt(hast3)}"

# ---- Markiser: tidsstyrt motor uten posisjonstilbakemelding -- ingen
# 0-100-verdi slik lysene har. Alle tre har PRESIS samme avstand fra sin
# D_-id til knappene sine (bekreftet ved direkte testing 2026-08-28):
#   +61  = "Open"-knappen  -- trekker FAKTISK markisen INN (forvirrende
#          fabrikknavn -- vi kaller den "inn" i koden vaar)
#   +126 = "Stop"-knappen  -- stopper motoren med en gang
#   +191 = "Close"-knappen -- kjoerer FAKTISK markisen UT ("ut" hos oss)
#   +256 = "Up"-knappen    -- ukjent funksjon, ikke testet, IKKE i bruk
MARKISE_OFFSETS = {"inn": 61, "stopp": 126, "ut": 191}

MARKISER = [
    ("Ute", "Markise paviljong", 124354),
    ("Ute", "Markise boblebad", 65125),
    ("Ute", "Markise balkong", 50980),
]


def markise_action(conn, smux_id, action):
    """Send ett enkelt "knappetrykk" til en markise -- 'ut' (kjoer ut),
    'inn' (trekk inn) eller 'stopp'. Ingen automatisk stopp her -- kall
    'stopp' selv naar du vil at den skal slutte aa kjoere (dashbordet
    gjoer dette automatisk naar du slipper museknappen)."""
    if action not in MARKISE_OFFSETS:
        return False
    oid = smux_id + MARKISE_OFFSETS[action]
    conn.send_line(f"new_state {oid} 0")
    conn.drain(wait=0.3)
    return True


def markise_button_id(smux_id, action):
    """Objekt-ID-en til "knappen" som utfoerer en markise-handling."""
    return smux_id + MARKISE_OFFSETS[action]


# ---- Markiser: "resett"-sekvens (alle tre, SAMTIDIG) ----------------
# Markisene har INGEN fysisk posisjonstilbakemelding (bekreftet -- se
# AUTOMASJONER.md/LES_MEG_FORST.md). Den gamle xSetup-makroen ("Kjøretid
# Balkong - 6 sekunder") anslo bare posisjonen ut fra en antatt kjøretid,
# noe som gradvis kan drifte feil. Denne sekvensen gjør i stedet dette:
#   - Paviljong, boblebad og balkong kjøres HELT inn SAMTIDIG (ikke én og
#     én -- kommandoene sendes rett etter hverandre paa samme tilkobling,
#     saa alle tre motorene starter nesten samtidig), akkurat som
#     markauth-makroen gjør ved vind-nødstopp.
#   - Deretter kjøres balkong UT igjen ALENE i BALKONG_RESET_OUT_SECONDS
#     sekunder, slik at den havner i vanlig hvileposisjon (samme idé som
#     gammel "stopp 1m før bunn", men alltid regnet fra en kjent
#     nullposisjon i stedet for et anslag).
KJORETID_PAVILJON_ID = 47931  # Memory-objekt "Kjøretid Paviljon" (sekunder)
KJORETID_BOBLEBAD_ID = 47671  # Memory-objekt "Kjøretid Boblebad" (sekunder)
KJORETID_BALKONG_ID = 48113   # Memory-objekt "Kjøretid Balkong" (sekunder)

# 2026-09-18: Filip testet reset-knappen.
#  - Balkongen trengte aa gaa litt lenger ut -- justert 6 -> 9 -> 12 -> 16 sekunder.
#  - Boblebad stoppet for tidlig (kom ikke helt inn) -- lagt til en
#    sikkerhetsmargin paa "kjør helt inn"-steget, se KJORETID_INN_MARGIN_SECONDS.
#  - Selve resett-KNAPPEN i Home Assistant kjoerer ikke lenger denne
#    Python-funksjonen -- den bruker naa et HA-script ("Nullstill markiser")
#    med tallene styrt av input_number-helpere ("Markise balkong reset
#    ut-tid" / "Markise reset margin"), saa Filip kan justere dem uten
#    kodeendring. Konstantene her brukes bare av CLI-kommandoen
#    "markise-reset" for manuell feilsoeking rett mot kontrolleren.
BALKONG_RESET_OUT_SECONDS = 16.0
BALKONG_RESET_FALLBACK_SECONDS = 30.0
PAVILJON_RESET_FALLBACK_SECONDS = 30.0
BOBLEBAD_RESET_FALLBACK_SECONDS = 30.0

# Ekstra tid vi venter UTOVER lest/reserve "Kjøretid" før vi sender "stopp"
# paa "kjør helt inn"-steget. Uten denne stoppet boblebad for tidlig (den
# lagrede Kjøretid-verdien var trolig litt for lav i forhold til hvor lang
# tid motoren faktisk bruker) -- aa stoppe LITT for sent er ufarlig (motoren
# har uansett et fysisk endestopp), mens aa stoppe for tidlig etterlater
# markisen ikke helt inne, som er akkurat det denne knappen skal fikse.
KJORETID_INN_MARGIN_SECONDS = 5.0


def _les_kjoretid(conn, kjoretid_id, fallback_seconds, visningsnavn, log):
    """Les en markises 'Kjøretid'-verdi live, eller bruk en reserveverdi
    hvis den ikke kan leses."""
    kjoretid = read_sensor_value(conn, kjoretid_id, wait=1.5)
    if kjoretid is None or kjoretid <= 0:
        kjoretid = fallback_seconds
        log(f"  Advarsel: klarte ikke lese 'Kjøretid {visningsnavn}' live -- bruker reserve-verdi paa {kjoretid:.0f} sekunder i stedet.")
    else:
        log(f"  Leste 'Kjøretid {visningsnavn}' = {kjoretid:.0f} sekunder.")
    return kjoretid


def markise_reset_sekvens(conn, balkong_out_seconds=BALKONG_RESET_OUT_SECONDS, log=print):
    """Nullstill ALLE tre markisene til kjent posisjon, SAMTIDIG:
      - Paviljong, boblebad og balkong kjøres helt inn parallelt (ett
        "inn"-signal til hver, rett etter hverandre, saa venter vi til den
        LENGSTE av de tre kjøretidene + en sikkerhetsmargin er over, og
        stopper alle tre).
      - Balkong kjøres deretter ut igjen ALENE i balkong_out_seconds
        sekunder (hvileposisjon).
    Blokkerende -- kalles fra en egen tråd naar den trigges fra MQTT-
    knappen (tar nå typisk 40-60 sekunder totalt siden de tre kjøres
    samtidig i stedet for etter hverandre)."""
    markiser_info = []  # [(visningsnavn, smux_id, kjoretid), ...]
    for visningsnavn, markise_navn, kjoretid_id, fallback in (
        ("Paviljong", "Markise paviljong", KJORETID_PAVILJON_ID, PAVILJON_RESET_FALLBACK_SECONDS),
        ("Boblebad", "Markise boblebad", KJORETID_BOBLEBAD_ID, BOBLEBAD_RESET_FALLBACK_SECONDS),
        ("Balkong", "Markise balkong", KJORETID_BALKONG_ID, BALKONG_RESET_FALLBACK_SECONDS),
    ):
        smux_id = next((s for z, n, s in MARKISER if n.lower() == markise_navn.lower()), None)
        if smux_id is None:
            log(f"  Advarsel: fant ikke '{markise_navn}' i MARKISER -- hopper over.")
            continue
        kjoretid = _les_kjoretid(conn, kjoretid_id, fallback, visningsnavn, log)
        markiser_info.append((visningsnavn, smux_id, kjoretid))

    if not markiser_info:
        return {"ok": False, "error": "Fant ingen markiser i MARKISER."}

    log("  Kjører alle markiser helt inn SAMTIDIG ...")
    for visningsnavn, smux_id, _ in markiser_info:
        markise_action(conn, smux_id, "inn")

    lengste_kjoretid = max(kj for _, _, kj in markiser_info)
    total_vent = lengste_kjoretid + KJORETID_INN_MARGIN_SECONDS
    log(f"  Venter {total_vent:.0f} sekunder (lengste kjøretid {lengste_kjoretid:.0f}s + {KJORETID_INN_MARGIN_SECONDS:.0f}s margin) ...")
    time.sleep(total_vent)

    for visningsnavn, smux_id, _ in markiser_info:
        markise_action(conn, smux_id, "stopp")
    log("  Alle tre er naa helt inne.")

    result = {"ok": True}
    for visningsnavn, _, kjoretid in markiser_info:
        result[f"{visningsnavn.lower()}_kjoretid"] = kjoretid

    balkong_entry = next((e for e in markiser_info if e[0] == "Balkong"), None)
    if balkong_entry is None:
        result["ok"] = False
        result["error"] = "Fant ikke 'Markise balkong' i MARKISER."
        return result
    balkong_smux_id = balkong_entry[1]

    time.sleep(1.0)
    log(f"  Balkong: kjører UT i {balkong_out_seconds:.0f} sekunder (tilbake til hvileposisjon) ...")
    markise_action(conn, balkong_smux_id, "ut")
    time.sleep(balkong_out_seconds)
    markise_action(conn, balkong_smux_id, "stopp")

    log("  Ferdig -- alle tre markisene er nullstilt.")
    result["balkong_ut_sekunder"] = balkong_out_seconds
    return result


# ---- Termostater (gulvvarme-styring via vannbaarne ventiler, IKKE
# varmekabler): 11 rom, hver med sin egen fysiske termostat (deviceType 5
# i anleggsdataene, "type": 7, minTemp=5.0, maxTemp=35.0). Disse styrer
# automatisk de 10 "GV ..."-ventilaktuatorene bak seg -- vi setter bare
# oensket temperatur her, akkurat som paa selve termostat-hjulet/appen.
#
# Termostatene er ENKLERE enn baade lys og brytere: ingen egen Val-/SET-
# id aa lete etter -- "set_value <smuxId> <grader>" virker DIREKTE paa
# selve smuxId-en, bekreftet fysisk 2026-08-28 (satt WC til 23 grader,
# vist igjen i eoptHome-appen med en gang).
#
# Naar vi abonnerer faar vi tilbake TRE tall i én melding (i stedet for
# ett, slik lys/sensorer har): "<oensket temp> <maalt temp> <fysisk styring>"
# f.eks. "22.50 24.70 -0.90".
THERMOSTAT_MIN = 5.0
THERMOSTAT_MAX = 35.0

THERMOSTATS = [
    ("Bad kjeller", "Bad kjeller", 5098),
    ("Bad lite", "Bad lite", 23016),
    ("Bad stort", "Bad stort", 31743),
    ("Hall", "Hall", 11127),
    ("Hall kjeller", "Hall kjeller", 2246),
    ("Kjellerstue", "Kjellerstue", 26063),
    ("Kjøkken", "Kjøkken", 16987),
    ("Spisestue", "Spisestue", 20080),
    ("Stue", "Stue", 7896),
    ("Vaskerom", "Vaskerom", 34689),
    ("WC", "WC", 14058),
]

THERMOSTAT_RE = re.compile(
    r"(?:SSN|RSN) (\d+) \S+ \d+ \d+ \d+ (-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)"
)


def read_thermostat(conn, smux_id, wait=1.5):
    """Les (oensket temp, maalt temp, fysisk styring) for en termostat."""
    conn.send_line(f"d_obj {smux_id}")
    text = conn.drain(wait=wait)
    for oid_str, setpoint, current, offset in THERMOSTAT_RE.findall(text):
        if int(oid_str) == smux_id:
            return {
                "setpoint": float(setpoint),
                "current": float(current),
                "offset": float(offset),
            }
    return None


def set_thermostat(conn, smux_id, target_temp):
    """Sett oensket temperatur direkte -- ingen egen SET-knapp trengs."""
    target_temp = max(THERMOSTAT_MIN, min(THERMOSTAT_MAX, float(target_temp)))
    conn.send_line(f"set_value {smux_id} {target_temp}")
    conn.drain(wait=0.5)
    return True


ZONES = sorted(set(zone for zone, _, _ in LIGHTS))


def zone_key(zone):
    return zone.lower()


def name_key(zone, name):
    return f"{zone.lower()}|{name.lower()}"


# Oppslag: "sone|navn" (full noeyaktig traeff) og bare "navn" naar det er
# entydig i hele huset (f.eks. "taklampe fin stue" uten "stue|" foran).
KNOWN_LIGHTS = {}
_name_counts = {}
for _zone, _name, _smux in LIGHTS:
    KNOWN_LIGHTS[name_key(_zone, _name)] = _smux
    _nk = _name.lower()
    _name_counts[_nk] = _name_counts.get(_nk, 0) + 1
for _zone, _name, _smux in LIGHTS:
    _nk = _name.lower()
    if _name_counts[_nk] == 1:
        KNOWN_LIGHTS[_nk] = _smux

# Oppslag for brytere -- samme prinsipp som KNOWN_LIGHTS over.
KNOWN_SWITCHES = {}
_switch_name_counts = {}
for _zone, _name, _smux in SWITCHES:
    KNOWN_SWITCHES[name_key(_zone, _name)] = _smux
    _nk = _name.lower()
    _switch_name_counts[_nk] = _switch_name_counts.get(_nk, 0) + 1
for _zone, _name, _smux in SWITCHES:
    _nk = _name.lower()
    if _switch_name_counts[_nk] == 1:
        KNOWN_SWITCHES[_nk] = _smux

# Oppslag for sensorer -- navn -> (smuxId, enhet).
KNOWN_SENSORS = {}
for _zone, _name, _smux, _unit in SENSORS:
    KNOWN_SENSORS[name_key(_zone, _name)] = (_smux, _unit)
    KNOWN_SENSORS[_name.lower()] = (_smux, _unit)

# Oppslag for markiser -- samme prinsipp som KNOWN_SWITCHES.
KNOWN_MARKISER = {}
_markise_name_counts = {}
for _zone, _name, _smux in MARKISER:
    KNOWN_MARKISER[name_key(_zone, _name)] = _smux
    _nk = _name.lower()
    _markise_name_counts[_nk] = _markise_name_counts.get(_nk, 0) + 1
for _zone, _name, _smux in MARKISER:
    _nk = _name.lower()
    if _markise_name_counts[_nk] == 1:
        KNOWN_MARKISER[_nk] = _smux

# Oppslag for termostater -- samme prinsipp som de andre (KNOWN_LIGHTS osv).
KNOWN_THERMOSTATS = {}
for _zone, _name, _smux in THERMOSTATS:
    KNOWN_THERMOSTATS[name_key(_zone, _name)] = _smux
    KNOWN_THERMOSTATS[_name.lower()] = _smux

GROUP_WORDS = {"alle", "hus", "alle lys"}


def lights_in_group(target):
    """Returner [(sone, navn, smuxId), ...] for en gruppe -- 'alle'/'hus' =
    hele huset, eller et sonenavn (f.eks. 'stue', 'bad stort') = bare den
    sonen (soner med flere ord maa skrives fullt ut, akkurat som i xSetup)."""
    if target in GROUP_WORDS:
        return list(LIGHTS)
    for zone in ZONES:
        if zone_key(zone) == target:
            return [t for t in LIGHTS if t[0] == zone]
    return None


def preload_all(conn, cache):
    """Gaar gjennom ALLE lys i huset og finner/lagrer Val- og SET-ID for
    hver, UTEN aa endre lysstyrken paa noen av dem. Kjoer denne en gang
    (f.eks. over natta) saa er hele huset momentant styrbart etterpaa."""
    total = len(LIGHTS)
    for i, (zone, name, smux_id) in enumerate(LIGHTS, 1):
        label = f"{zone} / {name}"
        key = str(smux_id)
        if key in cache and cache[key].get("val_id") and cache[key].get("set_id"):
            print(f"[{i}/{total}] {label:35s} allerede i cache, hopper over")
            continue
        print(f"[{i}/{total}] {label:35s} leter ...", end=" ", flush=True)
        info = discover_device_objects(conn, smux_id, quiet=True)
        if info and info.get("val_id") and info.get("set_id"):
            cache[key] = info
            save_cache(cache)
            print(f"OK (Val={info['val_id']}, SET={info['set_id']})")
        else:
            print("FANT IKKE -- se naermere paa denne senere")
    print("\nFerdig med forhaandslasting.")


ON_WORDS = {"on", "på", "pa", "1", "100"}
OFF_WORDS = {"off", "av", "0"}


def main():
    if len(sys.argv) < 2:
        print("Bruk:")
        print(f"  python {os.path.basename(__file__)} <lampenavn eller smuxId> <lysstyrke 0-100>")
        print(f"  python {os.path.basename(__file__)} <sonenavn> <lysstyrke>       # alle lys i sonen")
        print(f"  python {os.path.basename(__file__)} alle <lysstyrke>            # ALLE lys i huset")
        print(f"  python {os.path.basename(__file__)} forhaandslast               # klargjoer hele huset")
        print(f"  python {os.path.basename(__file__)} <bryternavn> på/av         # terrassevarmer/vifte/lyskaster")
        print(f"  python {os.path.basename(__file__)} les <sensornavn>            # les Sol/Vind-foeler")
        print(f"  python {os.path.basename(__file__)} markise <navn> ut/inn/stopp [sekunder]  # markiser")
        print(f"  python {os.path.basename(__file__)} markise-reset                  # nullstill ALLE markiser (paviljong+boblebad inn, balkong inn+ut {BALKONG_RESET_OUT_SECONDS:.0f}s)")
        print(f"  python {os.path.basename(__file__)} termostat <rom> [grader]        # les/sett temperatur")
        print(f"  python {os.path.basename(__file__)} viftehastighet             # les status Hast.2/Hast.3")
        print(f"  python {os.path.basename(__file__)} finn-ettervarme            # (engangs) finn temperatur-objektet for ettervarme")
        print(f"  python {os.path.basename(__file__)} les-raw <id>               # (feilsøking) les et raatt objekt-ID direkte")
        print()
        print("Eksempler:")
        print(f"  python {os.path.basename(__file__)} \"taklampe fin stue\" 60")
        print(f"  python {os.path.basename(__file__)} stue 50")
        print(f"  python {os.path.basename(__file__)} \"bad stort\" 0")
        print(f"  python {os.path.basename(__file__)} vifte på")
        print(f"  python {os.path.basename(__file__)} terassevarmer1 av")
        print(f"  python {os.path.basename(__file__)} les \"sol føler\"")
        print(f"  python {os.path.basename(__file__)} markise \"markise paviljong\" ut 3")
        print(f"  python {os.path.basename(__file__)} termostat wc 23")
        print()
        print("Kjente soner:", ", ".join(ZONES))
        print("Kjente brytere:", ", ".join(name for _, name, _ in SWITCHES))
        print("Kjente sensorer:", ", ".join(name for _, name, _, _ in SENSORS))
        print("Kjente markiser:", ", ".join(name for _, name, _ in MARKISER))
        print("Kjente termostater:", ", ".join(name for _, name, _ in THERMOSTATS))
        sys.exit(1)

    target = sys.argv[1].strip().lower()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(f"Kobler til kontrolleren paa {CONTROLLER_IP}:{CONTROLLER_PORT} ...")
    conn = XComfortConn()
    print("Tilkoblet.\n")
    cache = load_cache()

    if target in ("forhaandslast", "forhåndslast", "preload"):
        preload_all(conn, cache)
        conn.close()
        return

    if target == "les":
        if len(sys.argv) < 3:
            print("Mangler sensornavn. Kjente sensorer:", ", ".join(name for _, name, _, _ in SENSORS))
            conn.close()
            sys.exit(1)
        sensor_target = sys.argv[2].strip().lower()
        if sensor_target not in KNOWN_SENSORS:
            print(f"Kjenner ikke sensoren '{sensor_target}'.")
            print("Kjente sensorer:", ", ".join(name for _, name, _, _ in SENSORS))
            conn.close()
            sys.exit(1)
        smux_id, unit = KNOWN_SENSORS[sensor_target]
        value = read_named_sensor(conn, smux_id, unit)
        conn.close()
        if value is None:
            print(f"Fikk ikke noe svar fra sensoren (smuxId={smux_id}).")
        elif unit:
            print(f"{sys.argv[2]}: {value} {unit}")
        else:
            print(f"{sys.argv[2]}: {'PÅ' if value > 0 else 'av'}")
        return

    if target == "markise":
        if len(sys.argv) < 4:
            print("Bruk: markise <navn> ut/inn/stopp [sekunder]")
            print("Kjente markiser:", ", ".join(name for _, name, _ in MARKISER))
            conn.close()
            sys.exit(1)
        markise_target = sys.argv[2].strip().lower()
        action = sys.argv[3].strip().lower()
        if markise_target not in KNOWN_MARKISER:
            print(f"Kjenner ikke markisen '{markise_target}'.")
            print("Kjente markiser:", ", ".join(name for _, name, _ in MARKISER))
            conn.close()
            sys.exit(1)
        if action not in MARKISE_OFFSETS:
            print("Ukjent handling -- bruk ut, inn eller stopp.")
            conn.close()
            sys.exit(1)
        smux_id = KNOWN_MARKISER[markise_target]
        seconds = None
        if len(sys.argv) >= 5:
            seconds = float(sys.argv[4])
            if seconds > 30:
                print("Av sikkerhetshensyn tillater ikke scriptet mer enn 30 sekunder om gangen.")
                conn.close()
                sys.exit(1)
        ok = markise_action(conn, smux_id, action)
        if ok and seconds and action != "stopp":
            print(f"Kjører '{action}' i {seconds} sekund(er) ...")
            time.sleep(seconds)
            markise_action(conn, smux_id, "stopp")
            print("Stoppet automatisk.")
        conn.close()
        if ok:
            print(f"\nFerdig -- sendte '{action}' til {sys.argv[2]}.")
        else:
            print("\nFeilet -- se meldinger over.")
        return

    if target in ("markise-reset", "markise_reset", "resett-markiser", "balkong-reset", "balkong_reset", "resett-balkong"):
        print("Nullstiller alle tre markisene (paviljong+boblebad helt inn, balkong helt inn og saa ut igjen) ...")
        result = markise_reset_sekvens(conn, log=print)
        conn.close()
        if result.get("ok"):
            print(f"\nFerdig -- balkong brukte {result['balkong_kjoretid']:.0f}s inn, {result['balkong_ut_sekunder']:.0f}s ut.")
        else:
            print(f"\nFeilet -- {result.get('error', 'ukjent feil')}")
        return

    if target == "termostat":
        if len(sys.argv) < 3:
            print("Bruk: termostat <rom> [ønsket grader]")
            print("Kjente termostater:", ", ".join(name for _, name, _ in THERMOSTATS))
            conn.close()
            sys.exit(1)
        th_target = sys.argv[2].strip().lower()
        if th_target not in KNOWN_THERMOSTATS:
            print(f"Kjenner ikke termostaten '{th_target}'.")
            print("Kjente termostater:", ", ".join(name for _, name, _ in THERMOSTATS))
            conn.close()
            sys.exit(1)
        smux_id = KNOWN_THERMOSTATS[th_target]
        if len(sys.argv) >= 4:
            ok = set_thermostat(conn, smux_id, sys.argv[3])
            info = read_thermostat(conn, smux_id)
            conn.close()
            if ok and info:
                print(f"\nFerdig -- {sys.argv[2]}: ønsket={info['setpoint']}°  målt={info['current']}°")
            elif ok:
                print(f"\nFerdig -- sendte ny temperatur, men fikk ikke lest tilbake bekreftelse.")
            else:
                print("\nFeilet -- se meldinger over.")
        else:
            info = read_thermostat(conn, smux_id)
            conn.close()
            if info:
                print(f"{sys.argv[2]}: ønsket={info['setpoint']}°  målt={info['current']}°  "
                      f"fysisk styring={info['offset']}°")
            else:
                print(f"Fikk ikke noe svar fra termostaten (smuxId={smux_id}).")
        return

    if target in ("finn-ettervarme", "finn_ettervarme", "sok-ettervarme"):
        # Runde 1 (allerede proevd) -- skanning rundt selve av/paa-bryteren
        # (128003, "TS_EttervarmeVentilasjon") etter "etter"/"varme" fant
        # ingenting nytt. Grunnen: temperaturvalget (17/20/23) styres IKKE
        # av ettervarme-bryteren i det hele tatt -- den gamle Xstudio-
        # macroen ("vent_temp.txt") viser at det er en HELT EGEN, gammel
        # "Vent temp:Memory"-verdi (0/1/2) som saetter to reléer, "Temp. 1"
        # og "Temp. 2" -- akkurat samme oppskrift som den (nå daarlige/
        # frakoblede) "Vent hast:Memory"-verdien (smuxId 75429) brukte for
        # viftehastighet foer vi fant ut at Hast. 2/Hast. 3-bryterne var de
        # ekte reléene. "Vent temp"-systemet kan vaere den ekte styringen
        # (ingen grunn til aa anta det ogsaa er dødt), men objekt-ID-ene er
        # ukjente. Runde 2: skann et bredt vindu rundt 75429 (der den
        # beslektede "Vent hast"-verdien ligger -- samme ventilasjons-
        # modul, sannsynligvis kommisjonert samtidig) etter "temp" i
        # navnet, i tillegg til et nytt, bredere soek rundt selve
        # ettervarme-bryteren.
        print("Skanner rundt 'Vent hast'-verdien (smuxId 75429) og ettervarme-bryteren (128003) etter 'temp' i navnet ...")
        found = {}
        found.update(scan_objects_window(conn, 75429, window=5000, name_filter=["temp"]))
        found.update(scan_objects_window(conn, 128003, window=3000, name_filter=["temp", "etter", "varme"]))
        conn.close()
        if not found:
            print("Fant fortsatt ingenting. Neste steg blir da aa fange trafikken med "
                  "Wireshark mens du trykker 17/20/23-knappen for ettervarme i eoptHome "
                  "(samme triks som ble brukt for aa knekke selve lys-protokollen).")
        else:
            print(f"\nFunnet ({len(found)} stk) -- se etter noe som heter i retning av")
            print("'Vent temp', 'Temp1'/'Temp. 1' eller 'Temp2'/'Temp. 2':\n")
            for oid in sorted(found):
                print(f"  {oid:>8}  {found[oid]}")
            print("\nTips: proev  python xcomfort_local_control.py les-raw <id>  "
                  "(se under) paa de mest lovende ID-ene for aa se hva de faktisk rapporterer.")
        return

    if target == "les-raw":
        if len(sys.argv) < 3:
            print("Bruk: les-raw <objekt-id>")
            conn.close()
            sys.exit(1)
        try:
            raw_id = int(sys.argv[2])
        except ValueError:
            print("Objekt-ID maa vaere et tall.")
            conn.close()
            sys.exit(1)
        conn.send_line(f"d_obj {raw_id}")
        text = conn.drain(wait=1.0)
        conn.close()
        print(text.strip() or "(ingen svar)")
        return

    if target in ("viftehastighet", "vifthastighet"):
        # Viser status for de to ekte fartsbryterne. Selve styringen skjer
        # med de vanlige bryter-kommandoene, f.eks.:
        #   python xcomfort_local_control.py "hast. 2" på
        #   python xcomfort_local_control.py "hast. 3" av
        status = read_vent_speed(conn)
        conn.close()
        if status:
            print(f"Viftehastighet ({status})")
            print("Styr med: \"hast. 2\" på/av  og  \"hast. 3\" på/av")
        else:
            print("Fikk ikke lest status for fartsbryterne.")
        return

    if target in KNOWN_SWITCHES:
        if len(sys.argv) < 3:
            print("Mangler på/av. Se bruk over.")
            conn.close()
            sys.exit(1)
        state_word = sys.argv[2].strip().lower()
        if state_word in ON_WORDS:
            on = True
        elif state_word in OFF_WORDS:
            on = False
        else:
            print(f"Skjønner ikke '{sys.argv[2]}' -- bruk på/av (eller on/off, 1/0).")
            conn.close()
            sys.exit(1)
        smux_id = KNOWN_SWITCHES[target]
        ok = set_switch(conn, smux_id, on, cache)
        conn.close()
        if ok:
            print(f"\nFerdig -- {sys.argv[1]} er nå {'PÅ' if on else 'AV'}.")
        else:
            print("\nFeilet -- se meldinger over.")
        return

    if len(sys.argv) < 3:
        print("Mangler lysstyrke (0-100). Se bruk over.")
        conn.close()
        sys.exit(1)
    brightness = sys.argv[2]

    group = lights_in_group(target)
    if group is not None:
        all_ok = True
        for zone, name, smux_id in group:
            print(f"--- {zone} / {name} (smuxId={smux_id}) ---")
            ok = set_light(conn, smux_id, brightness, cache)
            all_ok = all_ok and ok
        conn.close()
        if all_ok:
            print(f"\nFerdig -- satte lysstyrke til {brightness}% paa {len(group)} lys.")
        else:
            print("\nNoen lamper feilet -- se meldinger over.")
        return

    if target in KNOWN_LIGHTS:
        smux_id = KNOWN_LIGHTS[target]
    elif target.isdigit():
        smux_id = int(target)
    else:
        print(f"Kjenner ikke lampen/sonen '{target}'.")
        print("Kjente soner:", ", ".join(ZONES))
        print("(Eller bruk smuxId direkte som tall -- se eopthome_api_test.py.)")
        conn.close()
        sys.exit(1)

    ok = set_light(conn, smux_id, brightness, cache)
    conn.close()

    if ok:
        print(f"\nFerdig -- satte lysstyrke til {brightness}%.")
    else:
        print("\nFeilet -- se meldinger over.")


if __name__ == "__main__":
    main()
