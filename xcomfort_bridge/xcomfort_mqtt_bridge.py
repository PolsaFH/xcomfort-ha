"""
xcomfort_mqtt_bridge.py  (v2)
=============================
Kobler HELE xComfort-anlegget (lys, brytere, sensorer, markiser og
termostater) til Home Assistant via MQTT, med automatisk enhets-oppdagelse
(MQTT Discovery).

VIKTIG FOR VEDLIKEHOLD: unique_id-er og discovery-emner er UENDRET fra v1
(xcomfort_light_<id>, xcomfort_switch_<id>, xcomfort_cover_<id> osv.), saa
entity_id-er, automasjoner, scener og dashbord fortsetter aa virke. Nye
ting (diagnostikk) har egne, nye unique_id-er (xcomfort_diag_...).

Hva er nytt i v2 (se CHANGELOG.md for detaljer):
  - Markiser har egen koe og egen traad. Kommandoene blokkerer ikke lenger
    MQTT-traaden, de venter ikke paa pollingen, og det er en fast minste
    pause mellom radiotelegrammer til markisene. "Stopp" sendes to ganger
    (alltid trygt). Tidsbasert posisjon (0-100 %) med set_position.
  - Lys/brytere/termostater: samme raske bolk-sending som foer, men bare
    SISTE kommando per enhet sendes (f.eks. naar noen drar i en slider).
  - Polling holder ikke laasen i flere sekunder i strekk, og et lys som
    ikke kan finnes leter vi ikke etter paa nytt hver 20. sekund.
  - Logging med millisekunder, loggnivaa i innstillingene, og raatt svar
    fra Home Manager paa hver markise-kommando.
  - Diagnostikk-entiteter, ny publisering naar Home Assistant restarter,
    QoS 1 paa kommandoer, helsesjekk for Supervisor sin watchdog.
  - Alle innstillinger hentes fra appens options (/data/options.json).
"""

import collections
import datetime as _dt
import json
import logging
import os
import queue
import random
import re
import sys
import threading
import time

import paho.mqtt.client as mqtt

import hm_link
import scanner
import web
import xcomfort_local_control as xc

BRIDGE_VERSION = "2.1.0"

# ------------------------------------------------------------ innstillinger --

DEFAULT_OPTIONS = {
    "xcomfort_ip": xc.CONTROLLER_IP,
    "xcomfort_port": xc.CONTROLLER_PORT,
    "mqtt_host": "core-mosquitto",
    "mqtt_port": 1883,
    "mqtt_username": "xcomfort",
    "mqtt_password": "",
    "log_level": "info",
    "poll_interval": 20,
    "markise_posisjon": True,
    "markise_kjoretid_balkong": 20.0,
    "markise_kjoretid_paviljong": 34.0,
    "markise_kjoretid_boblebad": 25.0,
    "markise_pause_mellom_kommandoer": 0.5,
    "markise_dobbel_stopp": True,
    "markise_gjenta_bevegelse": False,
    "hurtig_status": True,
    "lysbrytere_som_lys": False,
}

OPTIONS_FILE = "/data/options.json"


def load_options():
    """Leser appens innstillinger. Manglende noekler (f.eks. rett etter en
    oppdatering) faar standardverdien, saa en gammel options.json aldri
    stopper broen. Utenfor HA (paa PC) brukes miljoevariabler."""
    opts = dict(DEFAULT_OPTIONS)
    if os.path.exists(OPTIONS_FILE):
        try:
            with open(OPTIONS_FILE, encoding="utf-8") as f:
                opts.update({k: v for k, v in json.load(f).items() if v is not None})
        except Exception as e:  # noqa: BLE001
            print(f"Klarte ikke lese {OPTIONS_FILE}: {e} -- bruker standardverdier")
    else:
        env_map = {"xcomfort_ip": "XCOMFORT_IP", "xcomfort_port": "XCOMFORT_PORT",
                   "mqtt_host": "MQTT_HOST", "mqtt_port": "MQTT_PORT",
                   "mqtt_username": "MQTT_USERNAME", "mqtt_password": "MQTT_PASSWORD",
                   "log_level": "LOG_LEVEL"}
        for key, env in env_map.items():
            if os.environ.get(env):
                opts[key] = os.environ[env]
    for key in ("xcomfort_port", "mqtt_port", "poll_interval"):
        opts[key] = int(opts[key])
    for key in ("markise_kjoretid_balkong", "markise_kjoretid_paviljong",
                "markise_kjoretid_boblebad", "markise_pause_mellom_kommandoer"):
        opts[key] = float(opts[key])
    return opts


OPTS = load_options()

# Kontroller-adressen settes i biblioteket (brukes av XComfortConn).
xc.CONTROLLER_IP = OPTS["xcomfort_ip"]
xc.CONTROLLER_PORT = OPTS["xcomfort_port"]

# ------------------------------------------------------------------ logging --


def setup_logging(level_name):
    level = {"debug": logging.DEBUG, "info": logging.INFO,
             "warning": logging.WARNING}.get(str(level_name).lower(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    web.LOG_RING.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler, web.LOG_RING]
    root.setLevel(level)
    logging.getLogger("paho").setLevel(logging.WARNING)


log = logging.getLogger("xcomfort.bro")
mlog = logging.getLogger("xcomfort.markise")

# ------------------------------------------------------------- konstanter --

DISCOVERY_PREFIX = "homeassistant"
AVAILABILITY_TOPIC = "xcomfort/bridge/status"
DIAG_TOPIC = "xcomfort/bridge/diag"
HA_STATUS_TOPIC = "homeassistant/status"
HEALTH_PORT = 8099

ORIGIN = {"name": "xComfort lokal bro", "sw_version": BRIDGE_VERSION}

CONTINUOUS_SENSORS = [s for s in xc.SENSORS if s[3]]
BINARY_SENSORS = [s for s in xc.SENSORS if not s[3]]

MARKISE_COMMAND_TO_ACTION = {"OPEN": "ut", "CLOSE": "inn", "STOP": "stopp"}
BALKONG_RESET_BUTTON_ID = "balkong_reset"

# Kjoeretid fra helt inn til helt ut per markise (fra appens innstillinger).
MARKISE_RUNTIME_OPTION = {
    50980: "markise_kjoretid_balkong",
    124354: "markise_kjoretid_paviljong",
    65125: "markise_kjoretid_boblebad",
}

# Ekstra tid vi lar motoren gaa naar maalet er helt inn/helt ut, saa den
# garantert naar endestoppet (motoren stopper uansett selv der).
ENDESTOPP_MARGIN_S = 3.0
# Pause foer "stopp" nr. 2 naar dobbel stopp er paa.
DOBBEL_STOPP_PAUSE_S = 0.8
# Ventetid foer en bevegelseskommando gjentas (bare hvis det er slaatt paa).
GJENTA_BEVEGELSE_PAUSE_S = 1.5
# Hvor lenge vi venter paa svar fra Home Manager etter en markise-kommando.
MARKISE_SVAR_VENT_S = 0.3
# Markise-forbindelsen lukkes etter saa mange sekunder uten trafikk.
MARKISE_CONN_IDLE_S = 8.0
# Hvor lenge vi venter paa at en lampe bekrefter ny lysstyrke.
LYS_BEKREFT_S = 15.0
# Naar lytte-forbindelsen virker, leses alt likevel av saa ofte (sekunder),
# som en ekstra sikkerhet. Uten lytte-forbindelse brukes poll_interval.
FULL_AVLESNING_S = 300

# Et lys/bryter som ikke kan finnes proeves ikke paa nytt foer dette har gaatt.
DISCOVERY_BACKOFF_S = 1800

# ------------------------------------------------------------ felles tilstand --

conn_lock = threading.Lock()


class Stats:
    """Tellere for diagnostikk-entitetene. Traadsikker."""

    def __init__(self):
        self.lock = threading.Lock()
        self.hm_ok = False
        self.last_contact = None
        self.sent = 0
        self.failed = 0
        self.last_error = "ingen"
        self.poll_seconds = 0.0
        self.markise = {sid: {"sendt": 0, "feilet": 0, "siste": "ingen"} for _, _, sid in xc.MARKISER}
        self.bekreftet = 0
        self.ubekreftet = 0
        self.responstider = collections.deque(maxlen=50)   # dict(navn, sek, tid)
        self.radio = collections.deque(maxlen=2000)          # tidspunkt for radiokvitteringer
        self.poll_modus = "vanlig"

    def contact_ok(self):
        with self.lock:
            self.hm_ok = True
            self.last_contact = _dt.datetime.now(_dt.timezone.utc)

    def contact_failed(self, err):
        with self.lock:
            self.hm_ok = False
            self.last_error = f"{now_str()} {err}"[:250]

    def add(self, sent=0, failed=0):
        with self.lock:
            self.sent += sent
            self.failed += failed

    def snapshot(self, queue_len):
        with self.lock:
            data = {
                "hm_ok": "ON" if self.hm_ok else "OFF",
                "last_contact": self.last_contact.isoformat() if self.last_contact else None,
                "sent": self.sent,
                "failed": self.failed,
                "last_error": self.last_error,
                "poll_seconds": round(self.poll_seconds, 2),
                "markise_queue": queue_len,
                "version": BRIDGE_VERSION,
                "listener_ok": "ON" if (LINK and LINK.healthy()) else "OFF",
                "confirmed": self.bekreftet,
                "unconfirmed": self.ubekreftet,
                "resp_last": round(self.responstider[-1]["sek"], 2) if self.responstider else None,
                "resp_avg": self.avg_response(),
                "radio_5min": self.radio_recent(),
            }
            for sid, m in self.markise.items():
                data[f"m{sid}_sendt"] = m["sendt"]
                data[f"m{sid}_feilet"] = m["feilet"]
                data[f"m{sid}_siste"] = m["siste"]
            return data


    def avg_response(self):
        if not self.responstider:
            return None
        return round(sum(r["sek"] for r in self.responstider) / len(self.responstider), 2)

    def radio_recent(self, seconds=300):
        cutoff = time.time() - seconds
        return sum(1 for t in self.radio if t > cutoff)


STATS = Stats()
LINK = None  # settes i main() -- hm_link.HMLink

# Siste publiserte retained-tilstand per emne -- sendes paa nytt naar Home
# Assistant restarter (homeassistant/status = online).
_state_cache = {}
_state_cache_lock = threading.Lock()

# Hjerteslag for helsesjekken (watchdog).
HEARTBEAT = {"poll": time.time(), "batch": time.time(), "markise": time.time()}
MQTT_STATE = {"connected": False, "client": None}


def now_str():
    return _dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


_state_sent_at = {}


def publish_state(client, topic, payload, retain=True, qos=0, force=False):
    """Publiserer tilstand. Samme verdi som sist sendes ikke paa nytt
    oftere enn hvert 5. minutt (Home Manager sender mye om igjen)."""
    if retain:
        with _state_cache_lock:
            same = _state_cache.get(topic) == payload
            if same and not force and time.time() - _state_sent_at.get(topic, 0) < 300:
                return
            _state_cache[topic] = payload
            _state_sent_at[topic] = time.time()
    client.publish(topic, payload, qos=qos, retain=retain)


def open_hm(purpose, attempts=3):
    """Aapner en forbindelse til Home Manager, med inntil `attempts` forsoek
    og oekende ventetid (0.5s, 1s, 2s). Kaster ConnectionError hvis alle
    feiler -- da logges det som FEIL hos den som kalte, aldri stille."""
    delay = 0.5
    last = None
    for attempt in range(1, attempts + 1):
        try:
            conn = xc.XComfortConn()
            STATS.contact_ok()
            return conn
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("Tilkobling til Home Manager feilet (%s, forsoek %d/%d): %s",
                        purpose, attempt, attempts, e)
            if attempt < attempts:
                time.sleep(delay)
                delay *= 2
    STATS.contact_failed(f"{purpose}: {last}")
    raise ConnectionError(f"Home Manager svarer ikke ({purpose}): {last}")


# ---------------------------------------------------------------- topics --

def zone_slug(zone):
    out = [ch if ch.isalnum() else "_" for ch in zone.lower()]
    slug = "".join(out)
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_")


def device_block(zone):
    # "identifiers" er det HA kjenner igjen enheten paa -- UENDRET fra v1.
    return {
        "identifiers": [f"xcomfort_zone_{zone_slug(zone)}"],
        "name": zone,
        "suggested_area": zone,
        "manufacturer": "Eaton / Sensio",
        "model": "Home Manager (lokal bro)",
    }


BRIDGE_DEVICE = {
    "identifiers": ["xcomfort_bridge_diag"],
    "name": "xComfort bro",
    "manufacturer": "Eaton / Sensio",
    "model": "Lokal MQTT-bro",
    "sw_version": BRIDGE_VERSION,
}


def light_state_topic(i):
    return f"xcomfort/light/{i}/state"


def light_command_topic(i):
    return f"xcomfort/light/{i}/set"


def switch_state_topic(i):
    return f"xcomfort/switch/{i}/state"


def switch_command_topic(i):
    return f"xcomfort/switch/{i}/set"


def sensor_state_topic(i):
    return f"xcomfort/sensor/{i}/state"


def binary_sensor_state_topic(i):
    return f"xcomfort/binary_sensor/{i}/state"


def cover_command_topic(i):
    return f"xcomfort/cover/{i}/set"


def cover_state_topic(i):
    return f"xcomfort/cover/{i}/state"


def cover_position_topic(i):
    return f"xcomfort/cover/{i}/position"


def cover_set_position_topic(i):
    return f"xcomfort/cover/{i}/set_position"


def climate_current_topic(i):
    return f"xcomfort/climate/{i}/current_temp"


def climate_temp_state_topic(i):
    return f"xcomfort/climate/{i}/temp_state"


def climate_temp_command_topic(i):
    return f"xcomfort/climate/{i}/temp_set"


def climate_mode_topic(i):
    return f"xcomfort/climate/{i}/mode_state"


def climate_valve_topic(i):
    return f"xcomfort/sensor/{i}_valve/state"


def balkong_reset_button_topic():
    return f"xcomfort/button/{BALKONG_RESET_BUTTON_ID}/press"


def with_availability(payload):
    payload["availability_topic"] = AVAILABILITY_TOPIC
    payload["payload_available"] = "online"
    payload["payload_not_available"] = "offline"
    payload["origin"] = ORIGIN
    return payload


def _pub_disc(client, component, object_id, payload):
    client.publish(f"{DISCOVERY_PREFIX}/{component}/{object_id}/config",
                   json.dumps(with_availability(payload)), qos=1, retain=True)


# ------------------------------------------------------------- discovery --

def publish_light_discovery(client):
    for zone, name, smux_id in xc.LIGHTS:
        _pub_disc(client, "light", f"xcomfort_{smux_id}", {
            "name": name,
            "unique_id": f"xcomfort_light_{smux_id}",
            "state_topic": light_state_topic(smux_id),
            "command_topic": light_command_topic(smux_id),
            "schema": "json",
            "brightness": True,
            "brightness_scale": 100,
            "optimistic": False,
            "qos": 1,
            "device": device_block(zone),
        })


def publish_switch_discovery(client):
    config_names = {"ettervarme temp. 1", "ettervarme temp. 2"}
    for zone, name, smux_id in xc.SWITCHES:
        payload = {
            "name": name,
            "unique_id": f"xcomfort_switch_{smux_id}",
            "state_topic": switch_state_topic(smux_id),
            "command_topic": switch_command_topic(smux_id),
            "payload_on": "ON",
            "payload_off": "OFF",
            "state_on": "ON",
            "state_off": "OFF",
            "optimistic": False,
            "qos": 1,
            "device": device_block(zone),
        }
        if name.lower() in config_names:
            payload["entity_category"] = "config"
        _pub_disc(client, "switch", f"xcomfort_{smux_id}", payload)


def publish_sensor_discovery(client):
    device_classes = {"°C": "temperature", "lux": "illuminance", "m/s": "wind_speed", "s": "duration"}
    ha_unit_overrides = {"lux": "lx"}  # HA godtar bare "lx" for illuminance
    diagnostic_names = {"retur", "tur", "kjøretid paviljon", "kjøretid balkong", "kjøretid boblebad"}
    for zone, name, smux_id, unit in CONTINUOUS_SENSORS:
        payload = {
            "name": name,
            "unique_id": f"xcomfort_sensor_{smux_id}",
            "state_topic": sensor_state_topic(smux_id),
            "unit_of_measurement": ha_unit_overrides.get(unit, unit),
            "state_class": "measurement",
            "device": device_block(zone),
        }
        if device_classes.get(unit):
            payload["device_class"] = device_classes[unit]
        if name.lower() in diagnostic_names:
            payload["entity_category"] = "diagnostic"
        _pub_disc(client, "sensor", f"xcomfort_{smux_id}", payload)


def publish_binary_sensor_discovery(client):
    problem_names = {"tett filter", "overoppheting"}
    for zone, name, smux_id, _unit in BINARY_SENSORS:
        payload = {
            "name": name,
            "unique_id": f"xcomfort_binary_sensor_{smux_id}",
            "state_topic": binary_sensor_state_topic(smux_id),
            "payload_on": "ON",
            "payload_off": "OFF",
            "entity_category": "diagnostic",
            "device": device_block(zone),
        }
        if name.lower() in problem_names:
            payload["device_class"] = "problem"
        _pub_disc(client, "binary_sensor", f"xcomfort_{smux_id}", payload)


def publish_cover_discovery(client):
    for zone, name, smux_id in xc.MARKISER:
        payload = {
            "name": name,
            "unique_id": f"xcomfort_cover_{smux_id}",
            "command_topic": cover_command_topic(smux_id),
            "payload_open": "OPEN",
            "payload_close": "CLOSE",
            "payload_stop": "STOP",
            "device_class": "awning",
            "qos": 1,
            "device": device_block(zone),
        }
        if OPTS["markise_posisjon"]:
            payload.update({
                "state_topic": cover_state_topic(smux_id),
                "position_topic": cover_position_topic(smux_id),
                "set_position_topic": cover_set_position_topic(smux_id),
                "position_open": 100,
                "position_closed": 0,
                "optimistic": False,
            })
        else:
            payload["optimistic"] = True
        _pub_disc(client, "cover", f"xcomfort_{smux_id}", payload)


def publish_climate_discovery(client):
    for zone, name, smux_id in xc.THERMOSTATS:
        _pub_disc(client, "climate", f"xcomfort_{smux_id}", {
            "name": name,
            "unique_id": f"xcomfort_climate_{smux_id}",
            "current_temperature_topic": climate_current_topic(smux_id),
            "temperature_state_topic": climate_temp_state_topic(smux_id),
            "temperature_command_topic": climate_temp_command_topic(smux_id),
            "mode_state_topic": climate_mode_topic(smux_id),
            "modes": ["heat"],
            "min_temp": xc.THERMOSTAT_MIN,
            "max_temp": xc.THERMOSTAT_MAX,
            "temp_step": 0.5,
            "temperature_unit": "C",
            "qos": 1,
            "device": device_block(zone),
        })
        publish_state(client, climate_mode_topic(smux_id), "heat")
        _pub_disc(client, "sensor", f"xcomfort_valve_{smux_id}", {
            "name": f"{name} fysisk styring",
            "unique_id": f"xcomfort_valve_{smux_id}",
            "state_topic": climate_valve_topic(smux_id),
            "unit_of_measurement": "°",
            "entity_category": "diagnostic",
            "device": device_block(zone),
        })


def publish_button_discovery(client):
    # Selve resett-logikken ligger i et HA-script; dette er bare knappen.
    _pub_disc(client, "button", f"xcomfort_{BALKONG_RESET_BUTTON_ID}", {
        "name": "Resett markiser",
        "unique_id": f"xcomfort_button_{BALKONG_RESET_BUTTON_ID}",
        "command_topic": balkong_reset_button_topic(),
        "payload_press": "PRESS",
        "device": device_block("Ute"),
        "entity_category": "config",
    })


def publish_diag_discovery(client):
    """Diagnostikk for selve broen (ny enhet "xComfort bro"). Alt leses fra
    ett JSON-emne. Disse kan brukes i varsler, f.eks. "boblebad-markisen
    fikk ikke svar" eller "broen har mistet kontakten med Home Manager"."""
    base = {"state_topic": DIAG_TOPIC, "entity_category": "diagnostic", "device": BRIDGE_DEVICE}

    def sensor(uid, name, template, **extra):
        p = dict(base, name=name, unique_id=f"xcomfort_diag_{uid}", value_template=template)
        p.update(extra)
        _pub_disc(client, "sensor", f"xcomfort_diag_{uid}", p)

    _pub_disc(client, "binary_sensor", "xcomfort_diag_hm_tilkoblet", dict(
        base, name="Home Manager tilkoblet", unique_id="xcomfort_diag_hm_tilkoblet",
        value_template="{{ value_json.hm_ok }}", payload_on="ON", payload_off="OFF",
        device_class="connectivity"))
    sensor("siste_kontakt", "Siste kontakt med Home Manager",
           "{{ value_json.last_contact }}", device_class="timestamp")
    sensor("kommandoer_sendt", "Kommandoer sendt", "{{ value_json.sent }}", state_class="total_increasing")
    sensor("kommandoer_feilet", "Kommandoer feilet", "{{ value_json.failed }}", state_class="total_increasing")
    sensor("siste_feil", "Siste feil", "{{ value_json.last_error }}")
    sensor("pollrunde", "Varighet pollrunde", "{{ value_json.poll_seconds }}",
           unit_of_measurement="s", state_class="measurement")
    sensor("markise_koe", "Markise-kø", "{{ value_json.markise_queue }}", state_class="measurement")
    sensor("versjon", "Versjon", "{{ value_json.version }}")
    for _zone, name, sid in xc.MARKISER:
        short = name.replace("Markise ", "").capitalize()
        sensor(f"markise_{sid}_sendt", f"{short}: kommandoer sendt",
               f"{{{{ value_json.m{sid}_sendt }}}}", state_class="total_increasing")
        sensor(f"markise_{sid}_feilet", f"{short}: kommandoer feilet",
               f"{{{{ value_json.m{sid}_feilet }}}}", state_class="total_increasing")
        sensor(f"markise_{sid}_siste", f"{short}: siste kommando",
               f"{{{{ value_json.m{sid}_siste }}}}")
        # Fjern sensorer fra 2.0.x som ikke finnes lenger (tom config = slett)
        for old in ("uten_svar", "kvittert", "uten_kvittering"):
            client.publish(f"{DISCOVERY_PREFIX}/sensor/xcomfort_diag_markise_{sid}_{old}/config",
                           "", qos=1, retain=True)

    _pub_disc(client, "binary_sensor", "xcomfort_diag_lytter", dict(
        base, name="Øyeblikkelig status (lytte-forbindelse)", unique_id="xcomfort_diag_lytter",
        value_template="{{ value_json.listener_ok }}", payload_on="ON", payload_off="OFF",
        device_class="connectivity"))
    sensor("responstid_siste", "Responstid lys (siste)", "{{ value_json.resp_last }}",
           unit_of_measurement="s", state_class="measurement")
    sensor("responstid_snitt", "Responstid lys (snitt siste 50)", "{{ value_json.resp_avg }}",
           unit_of_measurement="s", state_class="measurement")
    sensor("lys_bekreftet", "Lyskommandoer bekreftet av lampa", "{{ value_json.confirmed }}",
           state_class="total_increasing")
    sensor("lys_ubekreftet", "Lyskommandoer uten bekreftelse", "{{ value_json.unconfirmed }}",
           state_class="total_increasing")
    sensor("radio_5min", "Radiokvitteringer siste 5 min", "{{ value_json.radio_5min }}",
           state_class="measurement")


def publish_discovery(client):
    log.info("Sender discovery for %d lys, %d brytere, %d sensorer, %d alarmer, %d markiser, %d termostater",
             len(xc.LIGHTS), len(xc.SWITCHES), len(CONTINUOUS_SENSORS), len(BINARY_SENSORS),
             len(xc.MARKISER), len(xc.THERMOSTATS))
    publish_light_discovery(client)
    publish_switch_discovery(client)
    publish_sensor_discovery(client)
    publish_binary_sensor_discovery(client)
    publish_cover_discovery(client)
    publish_climate_discovery(client)
    publish_button_discovery(client)
    publish_diag_discovery(client)
    publish_event_discovery(client)
    publish_onoff_light_discovery(client)


def publish_diag(client):
    publish_state(client, DIAG_TOPIC, json.dumps(STATS.snapshot(MARKISER_CTRL.queue_length())))


# ------------------------------------------------- lys/bryter/termostat-bolk --
#
# Kommandoer samles i COMMAND_BATCH_WINDOW sekunder og sendes over EN
# forbindelse: foerst alle set_value, saa (etter en felles kort pause) alle
# new_state tett etter hverandre. Den synlige "boelgen" som gjenstaar naar
# mange lys styres samtidig kommer fra Home Manager, som sender ett
# radiotelegram per aktuator etter hverandre -- det kan ikke broen endre.
#
# v2: Flere kommandoer til SAMME enhet i samme bolk -> bare den siste
# sendes (rekkefoelgen er ellers som HA sendte dem).

COMMAND_BATCH_WINDOW = 0.15

_pending_commands = []
_pending_lock = threading.Lock()
_pending_event = threading.Event()


def _queue_command(kind, smux_id, payload_bytes):
    with _pending_lock:
        _pending_commands.append((kind, smux_id, payload_bytes))
    _pending_event.set()


def dedupe_last_wins(batch):
    """Behold bare siste kommando per (type, enhet), i rekkefoelgen enheten
    foerst dukket opp."""
    order = []
    latest = {}
    for kind, smux_id, payload in batch:
        key = (kind, smux_id)
        if key not in latest:
            order.append(key)
        latest[key] = payload
    return [(k, s, latest[(k, s)]) for k, s in order]


def _parse_light_brightness(payload_bytes):
    try:
        data = json.loads(payload_bytes.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("Klarte ikke tolke lys-kommando %r: %s", payload_bytes, e)
        return None
    if data.get("state", "ON") == "OFF":
        return 0
    brightness = data.get("brightness")
    if brightness is None:
        brightness = 100
    return max(0, min(100, int(brightness)))


def publish_light_state(client, smux_id, brightness, force=False):
    payload = {"state": "ON" if brightness > 0 else "OFF"}
    if brightness > 0:
        payload["brightness"] = brightness
    publish_state(client, light_state_topic(smux_id), json.dumps(payload), force=force)


# Lys vi har sendt kommando til, og venter paa at lampa selv bekrefter
# (via D_-objektet paa lytte-forbindelsen): smux_id -> (maal %, sendetid)
PENDING_CONFIRM = {}


_discovery_retry_after = {}


def cached_objects(conn, smux_id, cache, finder, for_command=False):
    """Slaar opp (og finner om noedvendig) styre-objektene for et lys eller
    en bryter. Et oppslag som feiler proeves ikke paa nytt av pollingen foer
    DISCOVERY_BACKOFF_S har gaatt -- ellers ville et "umulig" lys koste et
    25-sekunders soek i HVER pollrunde og holde alle andre kommandoer igjen.
    Kommandoer fra HA proever alltid (brukeren vil jo at noe skal skje)."""
    key = str(smux_id)
    if key in cache and (cache[key].get("val_id") or cache[key].get("on_id")):
        return finder(conn, smux_id, cache)
    if not for_command and time.time() < _discovery_retry_after.get(smux_id, 0):
        return None
    info = finder(conn, smux_id, cache)
    if not info:
        _discovery_retry_after[smux_id] = time.time() + DISCOVERY_BACKOFF_S
        log.warning("Hopper over %s i pollingen de neste %d min (styre-objekter ikke funnet).",
                    smux_id, DISCOVERY_BACKOFF_S // 60)
    return info


def _process_command_batch(client, batch):
    batch = dedupe_last_wins(batch)
    cache = xc.load_cache()
    armed_lights, armed_switches, applied_climate = [], [], []
    t_start = time.time()

    with conn_lock:
        try:
            conn = open_hm("kommando-bolk")
        except ConnectionError as e:
            log.error("Kommando-bolk med %d kommando(er) ble IKKE sendt: %s", len(batch), e)
            STATS.add(failed=len(batch))
            return
        try:
            for kind, smux_id, payload in batch:
                try:
                    if kind == "light":
                        brightness = _parse_light_brightness(payload)
                        if brightness is None:
                            continue
                        info = cached_objects(conn, smux_id, cache, xc.get_device_objects, for_command=True)
                        if not info:
                            log.error("Lys %s: fant ikke styre-objekter, kommando ikke sendt", smux_id)
                            STATS.add(failed=1)
                            continue
                        conn.send_line(f"set_value {info['val_id']} {brightness}")
                        armed_lights.append((smux_id, brightness, info["set_id"]))
                    elif kind == "switch":
                        on = payload.decode("utf-8").strip().upper() == "ON"
                        info = cached_objects(conn, smux_id, cache, xc.get_switch_objects, for_command=True)
                        if not info:
                            log.error("Bryter %s: fant ikke av/paa-knapp, kommando ikke sendt", smux_id)
                            STATS.add(failed=1)
                            continue
                        armed_switches.append((smux_id, on, info["on_id"] if on else info["off_id"]))
                    elif kind == "climate":
                        try:
                            target = float(payload.decode("utf-8").strip())
                        except (ValueError, UnicodeDecodeError):
                            log.warning("Ugyldig temperatur-kommando for termostat %s: %r", smux_id, payload)
                            continue
                        target = max(xc.THERMOSTAT_MIN, min(xc.THERMOSTAT_MAX, target))
                        conn.send_line(f"set_value {smux_id} {target}")
                        applied_climate.append((smux_id, target))
                except Exception as e:  # noqa: BLE001
                    log.error("Feil ved %s-kommando for %s: %s", kind, smux_id, e)
                    STATS.add(failed=1)

            if armed_lights or armed_switches:
                time.sleep(0.1)
            for _s, _b, set_id in armed_lights:
                conn.send_line(f"new_state {set_id} 0")
            for _s, _o, target_id in armed_switches:
                conn.send_line(f"new_state {target_id} 0")
            reply = conn.drain(wait=0.5)
        except Exception as e:  # noqa: BLE001
            log.error("Kommando-bolk avbrutt: %s", e)
            STATS.contact_failed(f"kommando-bolk: {e}")
            STATS.add(failed=len(batch))
            return
        finally:
            conn.close()

    n = len(armed_lights) + len(armed_switches) + len(applied_climate)
    STATS.add(sent=n)
    if n:
        names = ", ".join(
            [f"lys {s}->{b}%" for s, b, _ in armed_lights]
            + [f"bryter {s}->{'PÅ' if o else 'AV'}" for s, o, _ in armed_switches]
            + [f"termostat {s}->{t}°" for s, t in applied_climate])
        log.info("Kommando-bolk (%d sendt, %.2fs): %s", n, time.time() - t_start, names)
        log.debug("Svar paa bolk: %s", xc.readable(reply) or "(ingen)")

    now = time.time()
    for smux_id, brightness, _ in armed_lights:
        publish_light_state(client, smux_id, brightness, force=True)
        PENDING_CONFIRM[smux_id] = (brightness, now)
    for smux_id, on, _ in armed_switches:
        publish_switch_state(client, smux_id, on, force=True)
    for smux_id, target in applied_climate:
        publish_state(client, climate_temp_state_topic(smux_id), f"{target:.1f}")


def command_batch_worker(client):
    while True:
        HEARTBEAT["batch"] = time.time()
        if not _pending_event.wait(timeout=10):
            continue
        time.sleep(COMMAND_BATCH_WINDOW)
        with _pending_lock:
            batch = _pending_commands[:]
            _pending_commands.clear()
            _pending_event.clear()
        if not batch:
            continue
        try:
            _process_command_batch(client, batch)
        except Exception:  # noqa: BLE001
            log.exception("Kommando-bolk feilet uventet")
        publish_diag(client)


# ------------------------------------------------------------- markiser --
#
# Hver markise har EN ting som kan gaa galt: radiotelegrammet fra Home
# Manager naar ikke fram (de to eldste aktuatorene kvitterer ikke, saa Home
# Manager vet ikke om det). Broen kan ikke se det direkte, men kan:
#   - sende markise-kommandoene i en egen koe, med fast minste pause mellom
#     hvert telegram (ikke tre tett paa hverandre),
#   - aldri la dem vente paa pollingen,
#   - sende "stopp" to ganger (en ekstra stopp er alltid ufarlig),
#   - (valgfritt, av som standard) gjenta "ut"/"inn" en gang,
#   - logge raatt svar fra Home Manager og telle opp i diagnostikken.
#
# Tidsbasert posisjon: broen regner ut omtrent hvor markisen er ut fra hvor
# lenge motoren har gaatt i hvilken retning (0 = helt inne, 100 = helt
# ute). "Resett markiser" (helt inn) nullstiller anslaget.

POSITION_FILE = "/data/markise_posisjon.json" if os.path.isdir("/data") else \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "markise_posisjon.json")


class Markise:
    def __init__(self, smux_id, name, runtime):
        self.smux_id = smux_id
        self.name = name
        self.runtime = max(1.0, float(runtime))
        self.position = 0.0
        self.moving = None       # None, "ut" eller "inn"
        self.t0 = 0.0
        self.pos0 = 0.0
        self.stop_at = None      # planlagt "stopp" (set_position)

    def current_position(self, now=None):
        now = now or time.time()
        if not self.moving:
            return self.position
        delta = (now - self.t0) / self.runtime * 100.0
        pos = self.pos0 + delta if self.moving == "ut" else self.pos0 - delta
        return max(0.0, min(100.0, pos))

    def settle(self, now=None):
        self.position = self.current_position(now)

    def start(self, direction, now=None):
        now = now or time.time()
        self.settle(now)
        self.moving = direction
        self.t0 = now
        self.pos0 = self.position

    def stop(self, now=None):
        self.settle(now)
        self.moving = None
        self.stop_at = None

    def ha_state(self):
        if self.moving == "ut":
            return "opening"
        if self.moving == "inn":
            return "closing"
        if self.position >= 99.5:
            return "open"
        if self.position <= 0.5:
            return "closed"
        return "stopped"


def plan_set_position(markise, target, now=None):
    """Returnerer (retning, sekunder) for aa kjoere til `target` %, eller
    None hvis den allerede er der. Mot helt inn/helt ut legges en margin
    paa, saa motoren garantert naar endestoppet (og anslaget kalibreres)."""
    now = now or time.time()
    target = max(0.0, min(100.0, float(target)))
    pos = markise.current_position(now)
    diff = target - pos
    if target in (0.0, 100.0):
        if abs(diff) < 0.5 and not markise.moving:
            return None
        secs = abs(diff) / 100.0 * markise.runtime + ENDESTOPP_MARGIN_S
        return ("ut" if target == 100.0 else "inn"), secs
    if abs(diff) < 2.0:
        return None
    return ("ut" if diff > 0 else "inn"), abs(diff) / 100.0 * markise.runtime


class MarkiseController:
    def __init__(self):
        self.q = queue.Queue()
        self.markiser = {}
        for _zone, name, sid in xc.MARKISER:
            runtime = OPTS.get(MARKISE_RUNTIME_OPTION.get(sid, ""), 30.0)
            self.markiser[sid] = Markise(sid, name, runtime)
        self.scheduled = []        # [(tidspunkt, smux_id, handling, grunn)]
        self.last_tx = 0.0
        self.conn = None
        self.conn_used = 0.0
        self.client = None
        self._last_pos_publish = 0.0
        self._load_positions()

    # -- innkommende (fra MQTT-traaden): bare legg i koen, aldri blokker --
    def enqueue(self, smux_id, action, value=None):
        self.q.put((time.time(), smux_id, action, value))

    def queue_length(self):
        return self.q.qsize() + len(self.scheduled)

    # -- posisjon lagres saa den overlever restart --
    def _load_positions(self):
        try:
            with open(POSITION_FILE, encoding="utf-8") as f:
                data = json.load(f)
            for sid, m in self.markiser.items():
                if str(sid) in data:
                    m.position = float(data[str(sid)])
            mlog.info("Leste lagrede markiseposisjoner: %s",
                      {m.name: round(m.position) for m in self.markiser.values()})
        except FileNotFoundError:
            mlog.info("Ingen lagrede markiseposisjoner -- antar at alle er helt inne (0 %). "
                      "Kjoer 'Resett markiser' for aa kalibrere.")
        except Exception as e:  # noqa: BLE001
            mlog.warning("Klarte ikke lese markiseposisjoner: %s", e)

    def _save_positions(self):
        try:
            with open(POSITION_FILE, "w", encoding="utf-8") as f:
                json.dump({str(sid): round(m.current_position(), 1) for sid, m in self.markiser.items()}, f)
        except Exception as e:  # noqa: BLE001
            mlog.warning("Klarte ikke lagre markiseposisjoner: %s", e)

    def publish(self, m):
        if not (self.client and OPTS["markise_posisjon"]):
            return
        publish_state(self.client, cover_position_topic(m.smux_id), str(int(round(m.current_position()))))
        publish_state(self.client, cover_state_topic(m.smux_id), m.ha_state())

    def publish_all(self):
        for m in self.markiser.values():
            self.publish(m)

    # -- selve sendingen --
    def _send(self, m, action, reason):
        """Sender ett markise-telegram. Returnerer tidspunktet det ble sendt
        (eller None hvis det IKKE ble sendt).

        Venter til minste pause siden forrige markise-telegram har gaatt.
        Markise-traaden holder sin egen forbindelse aapen mens det er
        markise-trafikk (lukkes etter noen sekunder uten), slik eoptHome-
        appen ogsaa gjoer. Feiler sendingen paa en gammel forbindelse,
        aapnes en ny og telegrammet sendes paa nytt EN gang."""
        gap = OPTS["markise_pause_mellom_kommandoer"]
        wait = self.last_tx + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        oid = xc.markise_button_id(m.smux_id, action)
        stats = STATS.markise[m.smux_id]
        t_sent = None
        reply = ""
        error = None
        # Markisene bruker sin EGEN forbindelse og venter IKKE paa laasen som
        # pollingen og lys-bolkene deler -- en "stopp" skal aldri bli staaende
        # og vente paa at pollingen blir ferdig. (Home Manager haandterer
        # flere samtidige forbindelser; eoptHome-appen har ogsaa sin egen.)
        for attempt in (1, 2):
            try:
                if self.conn is None:
                    self.conn = open_hm(f"markise {m.name}")
                self.conn.send_line(f"new_state {oid} 0")
                t_sent = time.time()
                reply = self.conn.drain(wait=MARKISE_SVAR_VENT_S)
                self.conn_used = time.time()
                error = None
                break
            except Exception as e:  # noqa: BLE001
                error = e
                self._close_conn()
                if t_sent is not None:
                    break  # sendt, men lesing av svar feilet -- ikke send dobbelt
                if attempt == 1 and not isinstance(e, ConnectionError):
                    mlog.warning("%s: forbindelsen var brutt (%s) -- proever paa nytt", m.name, e)
        if t_sent is None:
            stats["feilet"] += 1
            STATS.add(failed=1)
            STATS.contact_failed(f"markise {m.name}: {error}")
            stats["siste"] = f"{now_str()} {action} FEILET"
            mlog.error("%s (%s): '%s' ble IKKE sendt (%s): %s", m.name, m.smux_id, action, reason, error)
            return None
        self.last_tx = t_sent
        stats["sendt"] += 1
        STATS.add(sent=1)
        readable = xc.readable(reply)
        stats["siste"] = f"{now_str()} {action}"
        mlog.info("%s (%s): %s -> new_state %s 0 [%s]", m.name, m.smux_id, action, oid, reason)
        mlog.debug("%s: svar fra Home Manager: %s", m.name, readable or "(ingen)")
        return t_sent

    def _close_conn(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _do(self, m, action, reason):
        """Sender handlingen og oppdaterer posisjonsanslaget fra det
        tidspunktet telegrammet faktisk gikk. Returnerer sendetidspunktet
        eller None."""
        t_sent = self._send(m, action, reason)
        if t_sent is None:
            return None
        if action == "stopp":
            m.stop(t_sent)
            # fjern ventende "stopp" fra set_position for denne markisen
            self.scheduled = [s for s in self.scheduled
                              if not (s[1] == m.smux_id and s[3] == "set_position")]
            if OPTS["markise_dobbel_stopp"] and reason != "ekstra stopp":
                self.scheduled.append((t_sent + DOBBEL_STOPP_PAUSE_S, m.smux_id, "stopp", "ekstra stopp"))
        elif reason == "gjentatt" and m.moving == action:
            pass  # samme bevegelse paa nytt: anslaget fortsetter uendret
        else:
            m.start(action, t_sent)
            if OPTS["markise_gjenta_bevegelse"] and reason != "gjentatt":
                self.scheduled.append((t_sent + GJENTA_BEVEGELSE_PAUSE_S, m.smux_id, action, "gjentatt"))
        self._save_positions()
        self.publish(m)
        return t_sent

    def _handle(self, smux_id, action, value, queued_at):
        m = self.markiser.get(smux_id)
        if not m:
            mlog.warning("Ukjent markise %s", smux_id)
            return
        age = time.time() - queued_at
        if age > 2:
            mlog.warning("%s: kommando '%s' laa %.1fs i koe foer sending", m.name, action, age)
        if action == "set_position":
            plan = plan_set_position(m, value)
            mlog.info("%s: set_position %s %% (naa ca. %d %%) -> %s", m.name, value,
                      round(m.current_position()), f"{plan[0]} i {plan[1]:.1f}s" if plan else "ingen bevegelse")
            if not plan:
                self.publish(m)
                return
            direction, secs = plan
            # avbryt tidligere planlagte stopp for denne markisen
            self.scheduled = [s for s in self.scheduled if s[1] != smux_id]
            t_sent = self._do(m, direction, "set_position")
            if t_sent is not None:
                self.scheduled.append((t_sent + secs, smux_id, "stopp", "set_position"))
            return
        # En ny kommando fra HA overstyrer ALT som var planlagt for denne
        # markisen (ekstra stopp, gjentatt bevegelse, set_position-stopp).
        # Uten dette kunne den ekstra "stopp"-en fra en tidligere kommando
        # komme rett ETTER en ny "inn" og stoppe markisen igjen (skjedde
        # 17.10 kl. 19:36 med boblebad da "Resett markiser" ble trykket
        # mens "Kjoer ut markiser" holdt paa aa avslutte).
        cancelled = [s for s in self.scheduled if s[1] == smux_id]
        if cancelled:
            self.scheduled = [s for s in self.scheduled if s[1] != smux_id]
            mlog.info("%s: ny kommando '%s' -- avbryter planlagt %s", m.name, action,
                      ", ".join(f"'{c[2]}' ({c[3]})" for c in cancelled))
        self._do(m, action, "fra HA")

    def run(self, client):
        self.client = client
        while True:
            HEARTBEAT["markise"] = time.time()
            # planlagte handlinger som er forfalt
            now = time.time()
            due = sorted(s for s in self.scheduled if s[0] <= now)
            if due:
                self.scheduled = [s for s in self.scheduled if s[0] > now]
                for _t, sid, action, reason in due:
                    try:
                        self._do(self.markiser[sid], action, reason)
                    except Exception:  # noqa: BLE001
                        mlog.exception("Planlagt markise-handling feilet")
                publish_diag(client)
                continue
            # neste kommando fra HA
            timeout = 0.25
            if self.scheduled:
                timeout = max(0.01, min(timeout, min(s[0] for s in self.scheduled) - now))
            try:
                queued_at, sid, action, value = self.q.get(timeout=timeout)
            except queue.Empty:
                self._tick()
                continue
            try:
                self._handle(sid, action, value, queued_at)
            except Exception:  # noqa: BLE001
                mlog.exception("Markise-kommando feilet uventet")
            publish_diag(client)

    def _tick(self):
        """Mens en markise gaar: publiser posisjon hvert sekund, og merk den
        som stoppet naar anslaget har naadd endestoppet."""
        now = time.time()
        if self.conn is not None:
            # Home Manager sender alt til alle forbindelser -- les og kast
            # det, saa bufferet ikke fylles opp (lytte-forbindelsen tar seg
            # av det som er interessant).
            try:
                self.conn._read_chunk()
            except Exception:  # noqa: BLE001
                self._close_conn()
        if self.conn is not None and now - self.conn_used > MARKISE_CONN_IDLE_S:
            self._close_conn()
        moving = [m for m in self.markiser.values() if m.moving]
        for m in moving:
            pos = m.current_position(now)
            if (m.moving == "ut" and pos >= 100.0) or (m.moving == "inn" and pos <= 0.0):
                m.stop(now)
                self._save_positions()
                self.publish(m)
        if moving and now - self._last_pos_publish >= 1.0:
            self._last_pos_publish = now
            for m in moving:
                self.publish(m)


MARKISER_CTRL = MarkiseController()


def handle_cover_command(client, smux_id, payload_bytes):
    word = payload_bytes.decode("utf-8", errors="replace").strip().upper()
    action = MARKISE_COMMAND_TO_ACTION.get(word)
    if not action:
        mlog.warning("Ukjent markise-kommando %r for %s", word, smux_id)
        return
    mlog.info("Mottatt fra HA: markise %s -> %s", smux_id, action)
    MARKISER_CTRL.enqueue(smux_id, action)


def handle_cover_position(client, smux_id, payload_bytes):
    try:
        value = float(payload_bytes.decode("utf-8").strip())
    except (ValueError, UnicodeDecodeError):
        mlog.warning("Ugyldig posisjon %r for markise %s", payload_bytes, smux_id)
        return
    mlog.info("Mottatt fra HA: markise %s -> posisjon %s %%", smux_id, value)
    MARKISER_CTRL.enqueue(smux_id, "set_position", value)


# ------------------------------------------------------------------ MQTT --

LIGHT_SET_RE = re.compile(r"^xcomfort/light/(\d+)/set$")
SWITCH_SET_RE = re.compile(r"^xcomfort/switch/(\d+)/set$")
COVER_SET_RE = re.compile(r"^xcomfort/cover/(\d+)/set$")
COVER_POS_RE = re.compile(r"^xcomfort/cover/(\d+)/set_position$")
CLIMATE_SET_RE = re.compile(r"^xcomfort/climate/(\d+)/temp_set$")
ONOFF_SET_RE = re.compile(r"^xcomfort/onofflight/(\d+)/set$")


def _onoff_light_command(client, smux_id, payload):
    try:
        on = json.loads(payload.decode("utf-8")).get("state", "ON") == "ON"
    except Exception:  # noqa: BLE001
        on = payload.decode("utf-8", errors="replace").strip().upper() == "ON"
    _queue_command("switch", smux_id, b"ON" if on else b"OFF")


def _republish_after_ha_restart(client):
    time.sleep(random.uniform(2, 5))  # la HA bli ferdig med aa starte MQTT
    log.info("Home Assistant er startet paa nytt -- sender discovery og tilstand paa nytt")
    publish_discovery(client)
    with _state_cache_lock:
        cached = list(_state_cache.items())
    for topic, payload in cached:
        client.publish(topic, payload, qos=0, retain=True)
    client.publish(AVAILABILITY_TOPIC, "online", qos=1, retain=True)


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code != 0:
        log.error("Klarte ikke koble til MQTT-megleren (reason_code=%s). Sjekk brukernavn/passord.", reason_code)
        return
    MQTT_STATE["connected"] = True
    log.info("Tilkoblet MQTT-megleren")
    client.publish(AVAILABILITY_TOPIC, "online", qos=1, retain=True)
    publish_discovery(client)
    for topic in ("xcomfort/light/+/set", "xcomfort/switch/+/set", "xcomfort/cover/+/set",
                  "xcomfort/cover/+/set_position", "xcomfort/climate/+/temp_set",
                  "xcomfort/onofflight/+/set"):
        client.subscribe(topic, qos=1)
    client.subscribe(HA_STATUS_TOPIC, qos=1)
    MARKISER_CTRL.publish_all()
    publish_diag(client)


def on_disconnect(client, userdata, flags, reason_code, properties):
    MQTT_STATE["connected"] = False
    log.warning("Mistet forbindelsen til MQTT-megleren (reason_code=%s) -- kobler til igjen automatisk",
                reason_code)


def on_message(client, userdata, message):
    try:
        topic = message.topic
        if topic == HA_STATUS_TOPIC:
            if message.payload.decode("utf-8", errors="replace").strip() == "online":
                threading.Thread(target=_republish_after_ha_restart, args=(client,), daemon=True).start()
            return
        for regex, handler in ((LIGHT_SET_RE, lambda c, i, p: _queue_command("light", i, p)),
                               (SWITCH_SET_RE, lambda c, i, p: _queue_command("switch", i, p)),
                               (COVER_SET_RE, handle_cover_command),
                               (COVER_POS_RE, handle_cover_position),
                               (CLIMATE_SET_RE, lambda c, i, p: _queue_command("climate", i, p)),
                               (ONOFF_SET_RE, _onoff_light_command)):
            m = regex.match(topic)
            if m:
                handler(client, int(m.group(1)), message.payload)
                return
    except Exception:  # noqa: BLE001
        log.exception("Feil ved behandling av MQTT-melding paa %s", message.topic)


# ------------------------------------------------------------------ poll --
#
# v2: Pollingen bruker EN forbindelse per runde som foer, men tar bare
# laasen for ett avsnitt om gangen (lys, brytere, ...), saa markiser og
# lys-bolker aldri maa vente mer enn rundt ett sekund paa pollingen.

SWITCH_STATE_RE = re.compile(r"(?:SSN|RSN) (\d+) \S+ \d+ \d+ (-?\d+(?:\.\d+)?)")
SENSOR_VALUE_RE = re.compile(r"(?:SSN|RSN) (\d+) \S+ \d+ \d+ \d+ (-?\d+(?:\.\d+)?)")


def _batch_d_obj(conn, ids, wait):
    if not ids:
        return ""
    cmd = b"".join(xc.STX + f"d_obj {oid}".encode("ascii") + xc.ETX for oid in ids)
    conn.sock.sendall(cmd)
    return conn.drain(wait=wait)


def discover_one_missing_light(conn):
    """Leter etter styre-objektene til ETT lys som mangler i cachen (et soek
    kan ta opptil ~25 sek). Kjoeres paa pollingens egen forbindelse og UTEN
    den delte laasen, saa lys-bolker og markiser aldri venter paa det."""
    cache = xc.load_cache()
    for _, _, smux_id in xc.LIGHTS:
        key = str(smux_id)
        if key in cache and cache[key].get("val_id") and cache[key].get("set_id"):
            continue
        if time.time() < _discovery_retry_after.get(smux_id, 0):
            continue
        cached_objects(conn, smux_id, cache, xc.get_device_objects)
        return


def poll_lights(client, conn):
    """Leser lysstyrken paa alle lys som er i cachen."""
    cache = xc.load_cache()
    known = [(s, cache[str(s)]) for _, _, s in xc.LIGHTS
             if cache.get(str(s), {}).get("val_id") and cache.get(str(s), {}).get("set_id")]
    if not known:
        return
    text = _batch_d_obj(conn, [info["val_id"] for _, info in known], wait=0.8)
    found = {int(o): float(v) for o, v in SENSOR_VALUE_RE.findall(text)}
    for smux_id, info in known:
        v = found.get(info["val_id"])
        if v is not None:
            publish_light_state(client, smux_id, max(0, min(100, round(v))))


def poll_switches(client, conn):
    ids = [s for _, _, s in xc.SWITCHES]
    found = {int(o): float(v) for o, v in SWITCH_STATE_RE.findall(_batch_d_obj(conn, ids, wait=0.8))}
    for smux_id in ids:
        if smux_id in found:
            publish_switch_state(client, smux_id, found[smux_id] > 0)


def poll_binary_sensors(client, conn):
    ids = [s for _, _, s, _ in BINARY_SENSORS]
    found = {int(o): float(v) for o, v in SWITCH_STATE_RE.findall(_batch_d_obj(conn, ids, wait=0.8))}
    for smux_id in ids:
        if smux_id in found:
            publish_state(client, binary_sensor_state_topic(smux_id), "ON" if found[smux_id] > 0 else "OFF")


def poll_continuous_sensors(client, conn):
    ids = [s for _, _, s, _ in CONTINUOUS_SENSORS]
    found = {int(o): float(v) for o, v in SENSOR_VALUE_RE.findall(_batch_d_obj(conn, ids, wait=0.8))}
    for smux_id in ids:
        if smux_id in found:
            publish_state(client, sensor_state_topic(smux_id), str(found[smux_id]))


def poll_thermostats(client, conn):
    ids = [s for _, _, s in xc.THERMOSTATS]
    for oid_str, setpoint, current, offset in xc.THERMOSTAT_RE.findall(_batch_d_obj(conn, ids, wait=1.0)):
        smux_id = int(oid_str)
        if smux_id in ids:
            publish_state(client, climate_current_topic(smux_id), current)
            publish_state(client, climate_temp_state_topic(smux_id), setpoint)
            publish_state(client, climate_valve_topic(smux_id), offset)


def poll_all(client):
    t0 = time.time()
    with conn_lock:
        try:
            conn = open_hm("polling", attempts=1)
        except ConnectionError as e:
            log.warning("Pollrunde hoppet over: %s", e)
            return
    try:
        discover_one_missing_light(conn)
        # Pollingen bruker sin egen forbindelse. Laasen tas bare per avsnitt
        # (under 1-2 sek), saa en lys-bolk aldri venter lenge.
        for section in (poll_lights, poll_switches, poll_binary_sensors,
                        poll_continuous_sensors, poll_thermostats):
            with conn_lock:
                section(client, conn)
        STATS.contact_ok()
    except Exception as e:  # noqa: BLE001
        log.warning("Pollrunde avbrutt: %s", e)
        STATS.contact_failed(f"polling: {e}")
    finally:
        conn.close()
    STATS.poll_seconds = time.time() - t0
    if STATS.poll_seconds > 10:
        log.warning("Pollrunden tok %.1fs (vanlig er ca. 5s)", STATS.poll_seconds)
    else:
        log.debug("Pollrunde ferdig paa %.2fs", STATS.poll_seconds)


def poll_loop(client):
    """Leser alt av jevnlig. Med frisk lytte-forbindelse holder det med en
    full avlesning hvert 5. minutt; ellers brukes poll_interval."""
    last = 0.0
    while True:
        HEARTBEAT["poll"] = time.time()
        time.sleep(1.0)
        fast = LINK is not None and LINK.healthy()
        interval = FULL_AVLESNING_S if fast else OPTS["poll_interval"]
        STATS.poll_modus = "øyeblikkelig (+ full avlesning hvert 5. min)" if fast else \
            f"hvert {OPTS['poll_interval']}. sekund"
        if time.time() - last < interval:
            continue
        last = time.time()
        HEARTBEAT["poll"] = time.time()
        try:
            poll_all(client)
        except Exception:  # noqa: BLE001
            log.exception("Pollrunde feilet")
        publish_diag(client)


# ------------------------------------------------- lytte-forbindelse (2.1) --
#
# Én vedvarende forbindelse som bare lytter. Home Manager sender endringer
# av seg selv (bekreftet 17.10.2026), saa HA faar ny tilstand innen et
# sekund i stedet for etter neste pollrunde. Pollingen gaar fortsatt, men
# sjeldnere (FULL_AVLESNING_S) saa lenge lytte-forbindelsen er frisk.

REG = {
    "val_to_light": {},   # Val-id -> lys-smux
    "light_ids": set(),   # D_-objekter for lys (bekreftelse/responstid)
    "switch_ids": set(),
    "binary_ids": set(),
    "sensor_ids": set(),
    "thermo_ids": set(),
    "button_ids": {},     # IN_-id -> navn
    "markise_ids": set(),
    "names": {},          # smux -> "Sone Navn" (for logg/nettside)
}

# Knapper/innganger som blir hendelser (event) i HA. Markiseknappene er
# kjent fra sky-oppslaget 17.10.2026; flere kan legges til fra nettsiden.
BUTTONS = [
    ("Ute", "Markiseknapp 1", 232304),
    ("Ute", "Markiseknapp 2", 234213),
    ("Ute", "Markiseknapp 3", 233840),
]
EVENT_TYPES = [f"kode_{i}" for i in range(10)]

# Av/paa-brytere som egentlig er lys. Med innstillingen "lysbrytere_som_lys"
# faar disse i tillegg en ekte lys-entitet i HA (bryteren beholdes).
LIGHT_SWITCH_IDS = {106282, 117514, 79607, 68193, 86251, 99486, 64163, 107292, 57690, 51416, 101812}

EXTRA_FILE = "/data/ekstra_enheter.json" if os.path.isdir("/data") else \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ekstra_enheter.json")
SCAN_FILE = "/data/objekter.json" if os.path.isdir("/data") else \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "objekter.json")
SEED_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_cache.json")

SCANNER = scanner.Scanner(SCAN_FILE)


def event_topic(i):
    return f"xcomfort/event/{i}"


def onoff_state_topic(i):
    return f"xcomfort/onofflight/{i}/state"


def onoff_command_topic(i):
    return f"xcomfort/onofflight/{i}/set"


def publish_switch_state(client, smux_id, on, force=False):
    publish_state(client, switch_state_topic(smux_id), "ON" if on else "OFF", force=force)
    if OPTS["lysbrytere_som_lys"] and smux_id in LIGHT_SWITCH_IDS:
        publish_state(client, onoff_state_topic(smux_id), json.dumps({"state": "ON" if on else "OFF"}), force=force)


def publish_event_discovery(client):
    for zone, name, oid in BUTTONS:
        _pub_disc(client, "event", f"xcomfort_in_{oid}", {
            "name": name,
            "unique_id": f"xcomfort_event_{oid}",
            "state_topic": event_topic(oid),
            "event_types": EVENT_TYPES,
            "device_class": "button",
            "device": device_block(zone),
        })


def publish_onoff_light_discovery(client):
    for zone, name, smux_id in xc.SWITCHES:
        if smux_id not in LIGHT_SWITCH_IDS:
            continue
        topic = f"{DISCOVERY_PREFIX}/light/xcomfort_onoff_{smux_id}/config"
        if not OPTS["lysbrytere_som_lys"]:
            client.publish(topic, "", qos=1, retain=True)
            continue
        _pub_disc(client, "light", f"xcomfort_onoff_{smux_id}", {
            "name": f"{name} (lys)",
            "unique_id": f"xcomfort_onofflight_{smux_id}",
            "state_topic": onoff_state_topic(smux_id),
            "command_topic": onoff_command_topic(smux_id),
            "schema": "json",
            "supported_color_modes": ["onoff"],
            "optimistic": False,
            "qos": 1,
            "device": device_block(zone),
        })


def load_extras():
    """Enheter som er lagt til fra nettsiden (Verktoey -> Soek etter nye
    enheter). Legges til listene foer discovery, med samme unique_id-moenster
    som de faste enhetene."""
    try:
        with open(EXTRA_FILE, encoding="utf-8") as f:
            extra = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:  # noqa: BLE001
        log.warning("Klarte ikke lese %s: %s", EXTRA_FILE, e)
        return
    cache = xc.load_cache()
    known = {s for _, _, s in xc.LIGHTS} | {s for _, _, s in xc.SWITCHES} | {b[2] for b in BUTTONS}
    changed = False
    for item in extra.get("lys", []):
        if item["id"] in known:
            continue
        xc.LIGHTS.append((item.get("sone", "Nye enheter"), item["navn"], item["id"]))
        if item.get("val_id") and item.get("set_id") and str(item["id"]) not in cache:
            cache[str(item["id"])] = {"name": item.get("objekt"), "val_id": item["val_id"], "set_id": item["set_id"]}
            changed = True
    for item in extra.get("brytere", []):
        if item["id"] in known:
            continue
        xc.SWITCHES.append((item.get("sone", "Nye enheter"), item["navn"], item["id"]))
        if item.get("on_id") and item.get("off_id") and str(item["id"]) not in cache:
            cache[str(item["id"])] = {"name": item.get("objekt"), "on_id": item["on_id"], "off_id": item["off_id"]}
            changed = True
    for item in extra.get("knapper", []):
        if item["id"] in known:
            continue
        BUTTONS.append((item.get("sone", "Knapper"), item["navn"], item["id"]))
    if changed:
        xc.save_cache(cache)
    log.info("Ekstra enheter fra nettsiden: %d lys, %d brytere, %d knapper",
             len(extra.get("lys", [])), len(extra.get("brytere", [])), len(extra.get("knapper", [])))


def merge_seed_cache():
    """Fyller inn styre-objekter som mangler i /data-cachen fra fila som
    foelger med appen, saa en ny installasjon ikke maa lete etter alt."""
    try:
        with open(SEED_CACHE_FILE, encoding="utf-8") as f:
            seed = json.load(f)
    except Exception:  # noqa: BLE001
        return
    cache = xc.load_cache()
    added = {k: v for k, v in seed.items() if k not in cache}
    if added:
        cache.update(added)
        xc.save_cache(cache)
        log.info("La inn %d kjente styre-objekter fra appens startliste", len(added))


def rebuild_registry():
    cache = xc.load_cache()
    REG["val_to_light"] = {}
    for zone, name, smux in xc.LIGHTS:
        REG["names"][smux] = f"{zone} {name}"
        info = cache.get(str(smux)) or {}
        if info.get("val_id"):
            REG["val_to_light"][info["val_id"]] = smux
    REG["light_ids"] = {s for _, _, s in xc.LIGHTS}
    REG["switch_ids"] = {s for _, _, s in xc.SWITCHES}
    REG["binary_ids"] = {s for _, _, s, _ in BINARY_SENSORS}
    REG["sensor_ids"] = {s for _, _, s, _ in CONTINUOUS_SENSORS}
    REG["thermo_ids"] = {s for _, _, s in xc.THERMOSTATS}
    REG["button_ids"] = {oid: f"{zone} {name}" for zone, name, oid in BUTTONS}
    REG["markise_ids"] = {s for _, _, s in xc.MARKISER}
    for zone, name, smux in xc.SWITCHES + xc.MARKISER:
        REG["names"][smux] = f"{zone} {name}"
    ids = (set(REG["val_to_light"]) | REG["light_ids"] | REG["switch_ids"] | REG["binary_ids"]
           | REG["sensor_ids"] | REG["thermo_ids"] | set(REG["button_ids"]) | REG["markise_ids"])
    return ids


def _num(fields, idx):
    try:
        return float(fields[idx])
    except (IndexError, ValueError):
        return None


def on_hm_object(oid, name, fields):
    """Kalles for hver SSN/RSN-melding paa lytte-forbindelsen."""
    client = MQTT_STATE["client"]
    web.RAW_RING.add(f"{web.ts()}  {oid:>7} {name:<32} {' '.join(fields)}")
    if client is None:
        return
    STATS.contact_ok()
    if oid in REG["val_to_light"]:
        v = _num(fields, 3)
        if v is not None:
            publish_light_state(client, REG["val_to_light"][oid], max(0, min(100, round(v))))
    if oid in REG["light_ids"]:
        level = _num(fields, 2)
        pending = PENDING_CONFIRM.get(oid)
        if pending and level is not None and abs(level - pending[0]) <= 2:
            PENDING_CONFIRM.pop(oid, None)
            secs = time.time() - pending[1]
            with STATS.lock:
                STATS.bekreftet += 1
                STATS.responstider.append({"navn": REG["names"].get(oid, str(oid)), "sek": secs,
                                           "tid": time.strftime("%H:%M:%S")})
            log.info("Lys %s bekreftet %d %% etter %.1fs", REG["names"].get(oid, oid), level, secs)
    if oid in REG["switch_ids"]:
        v = _num(fields, 2)
        if v is not None:
            publish_switch_state(client, oid, v > 0)
    if oid in REG["binary_ids"]:
        v = _num(fields, 2)
        if v is not None:
            publish_state(client, binary_sensor_state_topic(oid), "ON" if v > 0 else "OFF")
    if oid in REG["sensor_ids"]:
        v = _num(fields, 3)
        if v is not None:
            publish_state(client, sensor_state_topic(oid), str(v))
    if oid in REG["thermo_ids"] and len(fields) >= 6:
        publish_state(client, climate_temp_state_topic(oid), fields[3])
        publish_state(client, climate_current_topic(oid), fields[4])
        publish_state(client, climate_valve_topic(oid), fields[5])
    # Et svar paa et d_obj-spoersmaal (fra skanneren eller en annen klient)
    # ser ut som en hendelse -- ignorer knapper mens skanneren gaar.
    quiet = LINK is None or LINK.recently_subscribed(oid) or SCANNER.running
    if oid in REG["button_ids"]:
        code = _num(fields, 2)
        if code is not None and not quiet:
            kode = int(code)
            log.info("Knapp %s: kode %d", REG["button_ids"][oid], kode)
            client.publish(event_topic(oid), json.dumps({"event_type": f"kode_{min(kode, 9)}", "kode": kode}),
                           qos=0, retain=False)
    elif name.startswith("IN_") and oid not in REG["binary_ids"] and not quiet:
        log.info("Ukjent knapp/inngang %s (%d) sendte %s -- kan legges til under Verktøy i appen",
                 name, oid, " ".join(fields))
    if oid in REG["markise_ids"]:
        mlog.debug("Markise-objekt %s: %s", REG["names"].get(oid, oid), " ".join(fields))


def on_hm_ack(kind):
    STATS.radio.append(time.time())
    web.RAW_RING.add(f"{web.ts()}  radiokvittering {kind}")


def on_hm_other(text):
    web.RAW_RING.add(f"{web.ts()}  {text}")


def housekeeping(client):
    """Hvert sekund: lys som ikke ble bekreftet i tide, og jevnlig diagnostikk."""
    last_diag = 0.0
    while True:
        time.sleep(1.0)
        now = time.time()
        for smux, (target, t0) in list(PENDING_CONFIRM.items()):
            if now - t0 > LYS_BEKREFT_S:
                PENDING_CONFIRM.pop(smux, None)
                with STATS.lock:
                    STATS.ubekreftet += 1
                log.info("Lys %s: lampa bekreftet ikke %d %% innen %.0fs (enten treg radio, "
                         "eller en lampe som ikke rapporterer status)", REG["names"].get(smux, smux),
                         target, LYS_BEKREFT_S)
        if now - last_diag > 30:
            last_diag = now
            publish_diag(client)


# ----------------------------------------------------- nettside / API (2.1) --

class Api:
    """Det nettsiden kan spoerre om og be broen gjoere."""

    def health(self):
        now = time.time()
        problems = []
        if not MQTT_STATE["connected"]:
            problems.append("ikke koblet til MQTT")
        if now - HEARTBEAT["poll"] > max(OPTS["poll_interval"], FULL_AVLESNING_S) * 2 + 120:
            problems.append("polling henger")
        if now - HEARTBEAT["batch"] > 60:
            problems.append("kommando-traaden henger")
        if now - HEARTBEAT["markise"] > 60:
            problems.append("markise-traaden henger")
        return problems

    def status(self):
        snap = STATS.snapshot(MARKISER_CTRL.queue_length())
        return {
            "versjon": BRIDGE_VERSION,
            "mqtt": MQTT_STATE["connected"],
            "mqtt_host": OPTS["mqtt_host"],
            "lytter_ok": bool(LINK and LINK.healthy()),
            "siste_melding_sek": round(time.time() - LINK.last_rx) if LINK and LINK.last_rx else None,
            "sendt": snap["sent"], "feilet": snap["failed"], "siste_feil": snap["last_error"],
            "bekreftet": snap["confirmed"], "ubekreftet": snap["unconfirmed"],
            "snitt_respons": snap["resp_avg"], "radio_5min": snap["radio_5min"],
            "poll_modus": STATS.poll_modus, "poll_sek": round(STATS.poll_seconds, 1),
            "responstider": list(STATS.responstider),
            "markiser": [{"navn": m.name, "posisjon": round(m.current_position()), "tilstand": m.ha_state()}
                         for m in MARKISER_CTRL.markiser.values()],
        }

    def devices(self):
        with _state_cache_lock:
            cache = dict(_state_cache)
        out = []
        for zone, name, smux in xc.LIGHTS:
            st = json.loads(cache.get(light_state_topic(smux), "{}") or "{}")
            on = st.get("state") == "ON"
            out.append({"type": "lys", "id": smux, "sone": zone, "navn": name, "på": on,
                        "tilstand": f"{st.get('brightness', 100)} %" if on else ("av" if st else "?")})
        for zone, name, smux in xc.SWITCHES:
            v = cache.get(switch_state_topic(smux))
            out.append({"type": "bryter", "id": smux, "sone": zone, "navn": name, "på": v == "ON",
                        "tilstand": {"ON": "på", "OFF": "av"}.get(v, "?")})
        for zone, name, smux in xc.MARKISER:
            m = MARKISER_CTRL.markiser[smux]
            out.append({"type": "markise", "id": smux, "sone": zone, "navn": name, "på": m.position > 1,
                        "tilstand": f"{round(m.current_position())} % ({m.ha_state()})"})
        return out

    def command(self, body):
        kind, smux, value = body.get("type"), int(body.get("id")), str(body.get("value"))
        log.info("Kommando fra appens nettside: %s %s -> %s", kind, smux, value)
        if kind == "lys":
            pct = max(0, min(100, int(float(value))))
            payload = {"state": "OFF"} if pct == 0 else {"state": "ON", "brightness": pct}
            _queue_command("light", smux, json.dumps(payload).encode())
        elif kind == "bryter":
            _queue_command("switch", smux, b"ON" if value.upper() == "ON" else b"OFF")
        elif kind == "markise":
            if value not in ("ut", "inn", "stopp"):
                return {"ok": False, "feil": "ukjent markise-handling"}
            MARKISER_CTRL.enqueue(smux, value)
        else:
            return {"ok": False, "feil": "ukjent type"}
        return {"ok": True}

    def scan_start(self):
        return {"ok": SCANNER.start()}

    def scan_info(self):
        known = REG["light_ids"] | REG["switch_ids"] | REG["binary_ids"] | REG["sensor_ids"] | \
            REG["thermo_ids"] | set(REG["button_ids"]) | REG["markise_ids"]
        cands = scanner.candidates(SCANNER.objects, known)
        try:
            with open(EXTRA_FILE, encoding="utf-8") as f:
                extra = json.load(f)
        except Exception:  # noqa: BLE001
            extra = {}
        for kind in cands:
            added = {x["id"] for x in extra.get(kind, [])}
            for item in cands[kind]:
                item["lagt_til"] = item["id"] in added
        return {"status": SCANNER.status(), "kandidater": cands}

    def add_extra(self, body):
        try:
            with open(EXTRA_FILE, encoding="utf-8") as f:
                extra = json.load(f)
        except Exception:  # noqa: BLE001
            extra = {}
        known = REG["light_ids"] | REG["switch_ids"] | REG["binary_ids"] | REG["sensor_ids"] | \
            REG["thermo_ids"] | set(REG["button_ids"]) | REG["markise_ids"]
        cands = scanner.candidates(SCANNER.objects, known)
        added = []
        for kind in ("lys", "brytere", "knapper"):
            for oid in body.get(kind, []):
                item = next((c for c in cands[kind] if c["id"] == int(oid)), None)
                if item and not any(x["id"] == item["id"] for x in extra.get(kind, [])):
                    extra.setdefault(kind, []).append(item)
                    added.append(item["navn"])
        with open(EXTRA_FILE, "w", encoding="utf-8") as f:
            json.dump(extra, f, indent=2, ensure_ascii=False)
        if added:
            load_extras()
            ids = rebuild_registry()
            if LINK:
                LINK.subscribe(ids)
            client = MQTT_STATE["client"]
            if client:
                publish_discovery(client)
            log.info("La til fra nettsiden: %s", ", ".join(added))
        return {"ok": True, "lagt_til": added}

    def cache(self):
        return xc.load_cache()


# ------------------------------------------------------------------ main --

def main():
    setup_logging(OPTS["log_level"])
    log.info("Starter xComfort MQTT-bro v%s (Home Manager %s:%s, MQTT %s:%s, loggnivaa %s)",
             BRIDGE_VERSION, OPTS["xcomfort_ip"], OPTS["xcomfort_port"],
             OPTS["mqtt_host"], OPTS["mqtt_port"], OPTS["log_level"])
    log.info("Markiser: posisjon=%s, kjoeretider=%s, pause=%.1fs, dobbel stopp=%s, gjenta bevegelse=%s",
             OPTS["markise_posisjon"],
             {m.name: m.runtime for m in MARKISER_CTRL.markiser.values()},
             OPTS["markise_pause_mellom_kommandoer"], OPTS["markise_dobbel_stopp"],
             OPTS["markise_gjenta_bevegelse"])

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="xcomfort-bridge")
    MQTT_STATE["client"] = client
    client.suppress_exceptions = True
    client.username_pw_set(OPTS["mqtt_username"], OPTS["mqtt_password"])
    client.will_set(AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    MARKISER_CTRL.client = client

    global LINK
    merge_seed_cache()
    load_extras()
    ids = rebuild_registry()
    if OPTS["hurtig_status"]:
        LINK = hm_link.HMLink(on_hm_object, on_hm_ack, on_hm_other)
        LINK.subscribe(ids)
        threading.Thread(target=LINK.run, daemon=True, name="lytter").start()
        log.info("Hurtig status er PAA: lytter paa %d objekter i Home Manager", len(ids))
    else:
        log.info("Hurtig status er AV: bruker bare polling hvert %ss", OPTS["poll_interval"])
    threading.Thread(target=housekeeping, args=(client,), daemon=True, name="husholdning").start()
    web.start(Api(), HEALTH_PORT)
    threading.Thread(target=poll_loop, args=(client,), daemon=True, name="poll").start()
    threading.Thread(target=command_batch_worker, args=(client,), daemon=True, name="bolk").start()
    threading.Thread(target=MARKISER_CTRL.run, args=(client,), daemon=True, name="markise").start()

    log.info("Kobler til MQTT-megler paa %s:%s ...", OPTS["mqtt_host"], OPTS["mqtt_port"])
    client.connect_async(OPTS["mqtt_host"], OPTS["mqtt_port"], keepalive=60)
    try:
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
