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
import idmap
import scanner
import usbstick
import web
import xcomfort_local_control as xc

BRIDGE_VERSION = "3.1.3"

# ------------------------------------------------------------ innstillinger --

DEFAULT_OPTIONS = {
    "xcomfort_ip": xc.CONTROLLER_IP,
    "xcomfort_port": xc.CONTROLLER_PORT,
    "mqtt_host": "core-mosquitto",
    "mqtt_port": 1883,
    "mqtt_username": "",
    "mqtt_password": "",
    "log_level": "info",
    "poll_interval": 20,
    "awning_position": True,
    "awning_runtime_balcony": 20.0,
    "awning_runtime_pavilion": 34.0,
    "awning_runtime_hot_tub": 25.0,
    "awning_command_gap": 0.5,
    "awning_double_stop": True,
    "awning_repeat_move": False,
    "instant_status": True,
    "switches_as_lights": False,
    "light_control": "hybrid",
}

# Innstillinger som het noe annet før 3.0.0 (norske navn). Leses fortsatt.
LEGACY_OPTIONS = {
    "markise_posisjon": "awning_position",
    "markise_kjoretid_balkong": "awning_runtime_balcony",
    "markise_kjoretid_paviljong": "awning_runtime_pavilion",
    "markise_kjoretid_boblebad": "awning_runtime_hot_tub",
    "markise_pause_mellom_kommandoer": "awning_command_gap",
    "markise_dobbel_stopp": "awning_double_stop",
    "markise_gjenta_bevegelse": "awning_repeat_move",
    "hurtig_status": "instant_status",
    "lysbrytere_som_lys": "switches_as_lights",
    "lysstyring": "light_control",
}
LEGACY_VALUES = {"light_control": {"pinne": "usb_stick"}}

OPTIONS_FILE = "/data/options.json"


def load_options():
    """Leser appens innstillinger. Manglende noekler (f.eks. rett etter en
    oppdatering) faar standardverdien, saa en gammel options.json aldri
    stopper broen. Utenfor HA (paa PC) brukes miljoevariabler."""
    opts = dict(DEFAULT_OPTIONS)
    if os.path.exists(OPTIONS_FILE):
        try:
            with open(OPTIONS_FILE, encoding="utf-8") as f:
                raw = {k: v for k, v in json.load(f).items() if v is not None}
            for old, new in LEGACY_OPTIONS.items():
                if old in raw and new not in raw:
                    raw[new] = raw.pop(old)
            opts.update(raw)
        except Exception as e:  # noqa: BLE001
            print(f"Could not read {OPTIONS_FILE}: {e} -- using defaults")
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
    for key in ("awning_runtime_balcony", "awning_runtime_pavilion",
                "awning_runtime_hot_tub", "awning_command_gap"):
        opts[key] = float(opts[key])
    for key, values in LEGACY_VALUES.items():
        opts[key] = values.get(opts[key], opts[key])
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


log = logging.getLogger("xcomfort.bridge")
mlog = logging.getLogger("xcomfort.awning")

# ------------------------------------------------------------- konstanter --

DISCOVERY_PREFIX = "homeassistant"
AVAILABILITY_TOPIC = "xcomfort/bridge/status"
DIAG_TOPIC = "xcomfort/bridge/diag"
HA_STATUS_TOPIC = "homeassistant/status"
HEALTH_PORT = 8099

ORIGIN = {"name": "xComfort local bridge", "sw_version": BRIDGE_VERSION}

CONTINUOUS_SENSORS = [s for s in xc.SENSORS if s[3]]
BINARY_SENSORS = [s for s in xc.SENSORS if not s[3]]
BINARY_NAMES = {s: f"{z} {n}" for z, n, s, _ in BINARY_SENSORS}

MARKISE_COMMAND_TO_ACTION = {"OPEN": "ut", "CLOSE": "inn", "STOP": "stopp"}
BALKONG_RESET_BUTTON_ID = "balkong_reset"

# Kjoeretid fra helt inn til helt ut per markise (fra appens innstillinger).
MARKISE_RUNTIME_OPTION = {
    50980: "awning_runtime_balcony",
    124354: "awning_runtime_pavilion",
    65125: "awning_runtime_hot_tub",
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
        self.last_error = "none"
        self.poll_seconds = 0.0
        self.markise = {sid: {"sendt": 0, "feilet": 0, "siste": "none"} for _, _, sid in xc.MARKISER}
        self.bekreftet = 0
        self.ubekreftet = 0
        self.responstider = collections.deque(maxlen=50)   # dict(navn, sek, tid)
        self.radio = collections.deque(maxlen=2000)          # tidspunkt for radiokvitteringer
        self.poll_modus = "every few seconds"

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
START_TIME = time.time()

# Siste kjente verdi per ting, så «Hva skjer»-lista bare viser ENDRINGER.
LAST_SEEN = {}
# Siste trykk per knapp: nøkkel -> (event_type, tid)
LAST_BUTTON = {}


def note_change(kind, key, value, text):
    """Legger en linje i hendelseslista på nettsiden når noe faktisk endrer
    seg (ikke ved første avlesning etter oppstart)."""
    old = LAST_SEEN.get((kind, key), "__ny__")
    LAST_SEEN[(kind, key)] = value
    if old != "__ny__" and old != value:
        web.EVENTS.add(text, kind)
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
            log.warning("Connection to Home Manager failed (%s, attempt %d/%d): %s",
                        purpose, attempt, attempts, e)
            if attempt < attempts:
                time.sleep(delay)
                delay *= 2
    STATS.contact_failed(f"{purpose}: {last}")
    raise ConnectionError(f"Home Manager is not responding ({purpose}): {last}")


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
    "name": "xComfort bridge",
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
    device_classes = {"°C": "temperature", "lux": "illuminance", "m/s": "wind_speed", "s": "duration",
                      "%": "humidity"}
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
            "device": device_block(zone),
        }
        if smux_id not in EXTRA_IDS:
            payload["entity_category"] = "diagnostic"
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
        if OPTS["awning_position"]:
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
            "name": f"{name} valve",
            "unique_id": f"xcomfort_valve_{smux_id}",
            "state_topic": climate_valve_topic(smux_id),
            "unit_of_measurement": "°",
            "entity_category": "diagnostic",
            "device": device_block(zone),
        })


def publish_button_discovery(client):
    # Selve resett-logikken ligger i et HA-script i forfatterens anlegg; dette
    # er bare knappen. Andre anlegg får den ikke.
    if not (xc.MARKISER and PROFILE["innebygd"]):
        return
    _pub_disc(client, "button", f"xcomfort_{BALKONG_RESET_BUTTON_ID}", {
        "name": "Reset awnings",
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
        base, name="Home Manager connected", unique_id="xcomfort_diag_hm_tilkoblet",
        value_template="{{ value_json.hm_ok }}", payload_on="ON", payload_off="OFF",
        device_class="connectivity"))
    sensor("siste_kontakt", "Last contact with Home Manager",
           "{{ value_json.last_contact }}", device_class="timestamp")
    sensor("kommandoer_sendt", "Commands sent", "{{ value_json.sent }}", state_class="total_increasing")
    sensor("kommandoer_feilet", "Commands failed", "{{ value_json.failed }}", state_class="total_increasing")
    sensor("siste_feil", "Last error", "{{ value_json.last_error }}")
    sensor("pollrunde", "Poll duration", "{{ value_json.poll_seconds }}",
           unit_of_measurement="s", state_class="measurement")
    sensor("markise_koe", "Awning queue", "{{ value_json.markise_queue }}", state_class="measurement")
    sensor("versjon", "Version", "{{ value_json.version }}")
    for _zone, name, sid in xc.MARKISER:
        short = name.replace("Markise ", "").capitalize()
        sensor(f"markise_{sid}_sendt", f"{short}: commands sent",
               f"{{{{ value_json.m{sid}_sendt }}}}", state_class="total_increasing")
        sensor(f"markise_{sid}_feilet", f"{short}: commands failed",
               f"{{{{ value_json.m{sid}_feilet }}}}", state_class="total_increasing")
        sensor(f"markise_{sid}_siste", f"{short}: last command",
               f"{{{{ value_json.m{sid}_siste }}}}")
        # Fjern sensorer fra 2.0.x som ikke finnes lenger (tom config = slett)
        for old in ("uten_svar", "kvittert", "uten_kvittering"):
            client.publish(f"{DISCOVERY_PREFIX}/sensor/xcomfort_diag_markise_{sid}_{old}/config",
                           "", qos=1, retain=True)

    _pub_disc(client, "binary_sensor", "xcomfort_diag_lytter", dict(
        base, name="Instant status (listener connection)", unique_id="xcomfort_diag_lytter",
        value_template="{{ value_json.listener_ok }}", payload_on="ON", payload_off="OFF",
        device_class="connectivity"))
    sensor("responstid_siste", "Light response time (last)", "{{ value_json.resp_last }}",
           unit_of_measurement="s", state_class="measurement")
    sensor("responstid_snitt", "Light response time (average of last 50)", "{{ value_json.resp_avg }}",
           unit_of_measurement="s", state_class="measurement")
    sensor("lys_bekreftet", "Light commands confirmed by the lamp", "{{ value_json.confirmed }}",
           state_class="total_increasing")
    sensor("lys_ubekreftet", "Light commands without confirmation", "{{ value_json.unconfirmed }}",
           state_class="total_increasing")
    sensor("radio_5min", "Radio acknowledgements, last 5 min", "{{ value_json.radio_5min }}",
           state_class="measurement")


def publish_discovery(client):
    log.info("Publishing discovery for %d lights, %d switches, %d sensors, %d alarms, %d awnings, %d thermostats",
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
#
# v2.4 (hurtigvei): Er lytte-forbindelsen oppe og alle styre-objektene
# kjent, sendes bolken rett på den åpne forbindelsen. Da slipper vi å koble
# til, vente på handshake og lese svar etterpå for hver kommando. Ellers
# brukes den gamle veien med egen forbindelse.

COMMAND_BATCH_WINDOW = 0.05
# Pause mellom set_value og new_state (Home Manager må ha fått verdien først).
SET_TO_EXECUTE_PAUSE_S = 0.1

_pending_commands = []
_pending_lock = threading.Lock()
_pending_event = threading.Event()


_first_queued_at = [0.0]


def _queue_command(kind, smux_id, payload_bytes):
    with _pending_lock:
        if not _pending_commands:
            _first_queued_at[0] = time.time()
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
        log.warning("Could not parse light command %r: %s", payload_bytes, e)
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
# Når hvert lys sist fikk en kommando (uansett vei), så en gammel
# pinne-kommando aldri sendes på nytt etter at en nyere er gitt.
LAST_CMD_AT = {}
LAST_CMD_PCT = {}       # lys-smux -> (prosent, hvilken vei) for siste kommando


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
        log.warning("Skipping %s in polling for the next %d min (control objects not found).",
                    smux_id, DISCOVERY_BACKOFF_S // 60)
    return info


# ------------------------------------------------------------- USB-pinne --
#
# Med USB-pinnen (CKOZ-00/13) i HA-maskinen sendes lyskommandoer rett ut på
# radio til dimmerne (se usbstick.py). Hvert lys i HA kan bestå av flere
# dimmere; hvilke står i /data/usb_kart.json (lastes opp på nettsiden).
# Kvitterer ikke alle dimmerne innen USB_ACK_S, sendes samme kommando via
# Home Manager. Uten pinne eller kart brukes Home Manager som før.

STICK = None
USB_MAP_FILE = "/data/usb_kart.json" if os.path.isdir("/data") else \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "usb_kart.json")
USB_MAP = {}            # lys-smux -> [serienr, ...]
USB_ACK_S = 3.0        # pinnens kvittering ELLER status fra lampa via Home Manager må komme innen dette
USB_GRACE_S = 10.0      # så lenge stoler vi på det vi sendte framfor gamle verdier fra Home Manager
LAST_USB = {}           # lys-smux -> (prosent, tid)
_force_hm = {}          # lys-smux -> tid; neste kommando skal via Home Manager
USB_STATS = {"bekreftet": 0, "bekreftet_hm": 0, "reserve": 0, "ubekreftet": 0, "stor_bolk": 0}
# Hybrid: bolker til flere dimmere enn dette går rett via Home Manager. Pinnen
# sender til én dimmer om gangen og venter på kvittering, så 16 utelys tok
# 3,3 s og de fleste måtte likevel sendes på nytt. Home Manager tar samme
# bolk på 0,15 s.
USB_MAX_DIMMERE_HYBRID = 6


SERIAL_TO_LIGHT = {}    # dimmer-serienr -> lys-smux
RADIO_LOG = collections.deque(maxlen=40)   # radiomeldinger til/fra våre dimmere (feilsøking)


# Radiokommandoer (på, av, sett nivå) som Home Manager sender til dimmere.
RADIO_CMD_KINDS = (usbstick.CMD_ON, usbstick.CMD_OFF, usbstick.CMD_LEVEL)
LEARN = {"kjorer": False, "lys": None, "hort": set(), "ferdig": 0, "totalt": 0,
         "resultat": {}, "uten_svar": [], "melding": ""}
_learning = set()        # lys som læres nå: skal gå via Home Manager, ikke pinnen
LEARN_WINDOW_S = 3.5


def _on_usb_radio(frame, info):
    """Alt pinnen hører. Tar vare på meldinger til og fra dimmerne i kartet,
    så man kan se hva Home Manager sender (f.eks. hvilken verdi den bruker
    for en gitt prosent) og hva dimmerne melder tilbake."""
    kind, src, dst = info
    if LEARN["lys"] is not None and kind in RADIO_CMD_KINDS and usbstick.STICK_ADDR not in (src, dst):
        LEARN["hort"].add(dst)
    if src == usbstick.STICK_ADDR:
        return
    if dst in SERIAL_TO_LIGHT:
        smux, retning = SERIAL_TO_LIGHT[dst], "to dimmer"
    elif src in SERIAL_TO_LIGHT and dst != usbstick.STICK_ADDR:
        smux, retning = SERIAL_TO_LIGHT[src], "from dimmer"
    else:
        return
    pct, how = LAST_CMD_PCT.get(smux, (None, ""))
    RADIO_LOG.append({"tid": time.strftime("%H:%M:%S"), "lys": REG["names"].get(smux, str(smux)),
                      "retning": retning, "fra": src, "til": dst,
                      "siste_kommando": f"{pct} % {how}".strip() if pct is not None else "",
                      "ramme": frame.hex(" ")})


def load_usb_map():
    global USB_MAP
    try:
        with open(USB_MAP_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        USB_MAP = {}
        return USB_MAP
    except Exception as e:  # noqa: BLE001
        log.warning("Could not read the USB map %s: %s", USB_MAP_FILE, e)
        USB_MAP = {}
        return USB_MAP
    lights = {s for _, _, s in xc.LIGHTS}
    USB_MAP = {int(k): [int(x) for x in v] for k, v in (data.get("lys") or {}).items()
               if int(k) in lights and v}
    SERIAL_TO_LIGHT.clear()
    SERIAL_TO_LIGHT.update({s: k for k, v in USB_MAP.items() for s in v})
    return USB_MAP


def save_usb_map(lys):
    tmp = USB_MAP_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"lys": {str(k): v for k, v in lys.items()}}, f, indent=1)
    os.replace(tmp, USB_MAP_FILE)
    return load_usb_map()


def usb_grace(smux_id, pct):
    """True hvis vi nettopp har sendt et ANNET nivå via pinnen. Da er en
    avvikende verdi fra Home Manager trolig gammel og skal ikke vises."""
    last = LAST_USB.get(smux_id)
    return bool(last and time.time() - last[1] < USB_GRACE_S and abs(last[0] - pct) > 2)


LIGHT_MODES = ("hybrid", "usb_stick", "home_manager")


def light_mode():
    """Hvordan lys styres: hybrid (pinne, Home Manager som reserve), bare
    pinne, eller bare Home Manager."""
    m = OPTS.get("light_control")
    if m in LIGHT_MODES:
        return m
    return "home_manager" if OPTS.get("usb_pinne") is False else "hybrid"  # gammel innstilling (2.5.0)


def _usb_confirmed(smux_id, t0, serials=()):
    """Alle dimmerne i lyset kvitterte via pinnen: regn lyset som bekreftet
    (ellers ville det telt som «ubekreftet» etter LYS_BEKREFT_S hvis Home
    Manager ikke får vite om endringen)."""
    USB_STATS["bekreftet"] += 1
    pending = PENDING_CONFIRM.pop(smux_id, None)
    if pending is not None:
        stick = STICK
        acks = [stick.last_ack.get(int(x), 0) for x in serials] if stick else []
        t_ack = max(acks) if acks and min(acks) >= t0 else time.time()
        with STATS.lock:
            STATS.bekreftet += 1
            STATS.responstider.append({"navn": REG["names"].get(smux_id, str(smux_id)),
                                       "sek": max(0.0, t_ack - t0),
                                       "tid": time.strftime("%H:%M:%S")})


def _try_usb(client, batch, t_start):
    """Sender lys som har serienumre via pinnen. Returnerer resten av bolken
    (det som skal via Home Manager)."""
    mode = light_mode()
    stick = STICK
    if mode == "home_manager" or not USB_MAP:
        return batch
    now = time.time()
    rest, lights, targets = [], [], []
    for kind, smux_id, payload in batch:
        serials = USB_MAP.get(smux_id) if kind == "light" else None
        if not serials or smux_id in _learning or (mode == "hybrid" and _force_hm.get(smux_id, 0) > now):
            rest.append((kind, smux_id, payload))
            continue
        brightness = _parse_light_brightness(payload)
        if brightness is None:
            continue
        lights.append((smux_id, brightness, serials))
        targets += [(s, brightness) for s in serials]
    if not lights:
        return batch
    if mode == "hybrid" and len(targets) > USB_MAX_DIMMERE_HYBRID:
        USB_STATS["stor_bolk"] += 1
        log.info("Large batch (%d dimmers) sent via Home Manager, which is faster than the stick", len(targets))
        return batch
    # Vis ny tilstand i HA med en gang; sendingen til mange dimmere kan ta litt tid
    t_pre = time.time()
    for smux_id, brightness, _ in lights:
        LAST_USB[smux_id] = (brightness, t_pre)
        publish_light_state(client, smux_id, brightness, force=True)
    result = stick.set_levels(targets) if stick is not None else None
    if result is None:
        for smux_id, _, _ in lights:
            LAST_USB.pop(smux_id, None)
        if mode == "hybrid":
            return batch  # pinnen virker ikke -- alt går via Home Manager
        log.error("USB stick only is selected, but the stick is not working: %d lights were NOT controlled (%s)",
                  len(lights), ", ".join(str(REG["names"].get(s, s)) for s, _, _ in lights))
        STATS.add(failed=len(lights))
        return rest
    t0, acked = result
    _finish_batch(client, [(s, b, None) for s, b, _ in lights], [], [], t_start, "via the USB stick")
    unconfirmed = []
    for light in lights:
        if all(int(x) in acked for x in light[2]):
            _usb_confirmed(light[0], t0, light[2])
        else:
            unconfirmed.append(light + (LAST_CMD_AT.get(light[0]),))
    if unconfirmed:
        threading.Timer(USB_ACK_S, _check_usb_acks, args=(unconfirmed, t0, mode == "hybrid")).start()
    return rest


def _check_usb_acks(lights, t0, fallback=True):
    stick = STICK
    for light in lights:
        smux_id, brightness, serials = light[:3]
        marker = light[3] if len(light) > 3 else None
        if (LAST_CMD_AT.get(smux_id) != marker) if marker is not None else LAST_CMD_AT.get(smux_id, 0) > t0 + 0.5:
            continue  # en nyere kommando er gitt siden -- denne er utdatert
        missing = [s for s in serials if not (stick and stick.acked_since(s, t0))]
        if not missing:
            _usb_confirmed(smux_id, t0, serials)
            continue
        pending = PENDING_CONFIRM.get(smux_id)
        if pending is None or pending[1] < t0 - 0.5:
            # Lampa har allerede meldt riktig nivå til Home Manager (se on_hm_object)
            USB_STATS["bekreftet_hm"] += 1
            continue
        names = ", ".join(str(s) for s in missing)
        if not fallback:
            USB_STATS["ubekreftet"] += 1
            log.warning("Light %s: %d of %d dimmers did not acknowledge via the USB stick (%s). USB stick only is selected, "
                        "so it is not sent via Home Manager.", REG["names"].get(smux_id, smux_id),
                        len(missing), len(serials), names)
            continue
        USB_STATS["reserve"] += 1
        log.warning("Light %s: %d of %d dimmers did not acknowledge via the USB stick (%s) -- sending via Home Manager",
                    REG["names"].get(smux_id, smux_id), len(missing), len(serials), names)
        _force_hm[smux_id] = time.time() + 5
        LAST_USB.pop(smux_id, None)
        _queue_command("light", smux_id, json.dumps(
            {"state": "ON" if brightness else "OFF", "brightness": brightness}).encode())


def _current_level(smux_id):
    with _state_cache_lock:
        raw = _state_cache.get(light_state_topic(smux_id))
    try:
        st = json.loads(raw or "{}")
    except ValueError:
        return None
    if not st:
        return None
    return int(st.get("brightness", 100)) if st.get("state") == "ON" else 0


def _listen_for(smux_id, level):
    """Sender nivået via Home Manager og returnerer dimmerne pinnen hørte."""
    LEARN["hort"] = set()
    LEARN["lys"] = smux_id
    _queue_command("light", smux_id, json.dumps(
        {"state": "ON", "brightness": level} if level else {"state": "OFF"}).encode())
    time.sleep(LEARN_WINDOW_S)
    LEARN["lys"] = None
    return set(LEARN["hort"])


def _learn_one(smux_id):
    level = _current_level(smux_id)
    if level is None:
        level = 0
    heard = _listen_for(smux_id, level)
    if not heard and level > 1:
        # Home Manager sender kanskje ikke noe når nivået er det samme.
        # Et lite dytt (1 %) som ikke synes, så tilbake.
        heard = _listen_for(smux_id, level - 1)
        _listen_for(smux_id, level)
    if not heard:
        return set()
    # Kontroll: én runde til. Bare dimmere som hørtes begge gangene teller,
    # så en tilfeldig melding i huset (noen trykket på en bryter) ikke blir med.
    again = _listen_for(smux_id, level)
    return heard & again if again else heard


def learn_usb_map(light_ids):
    """Lærer hvilke dimmere hvert lys styrer ved å lytte på radio mens
    Home Manager sender. Kjøres i egen tråd fra nettsiden."""
    LEARN.update(kjorer=True, ferdig=0, totalt=len(light_ids), resultat={}, uten_svar=[],
                 melding="Listening …")
    names = REG["names"]
    try:
        for smux in light_ids:
            if STICK is None or not STICK.healthy():
                LEARN["melding"] = "The USB stick is not connected"
                break
            _learning.add(smux)
            try:
                LEARN["melding"] = f"Listening to {names.get(smux, smux)}"
                taken = {x for k, v in USB_MAP.items() if k != smux for x in v} | \
                    {x for k, v in LEARN["resultat"].items() if k != smux for x in v}
                found = sorted(_learn_one(smux) - taken)
            finally:
                _learning.discard(smux)
            if found:
                LEARN["resultat"][smux] = found
            else:
                LEARN["uten_svar"].append(names.get(smux, str(smux)))
            LEARN["ferdig"] += 1
        if LEARN["resultat"]:
            merged = dict(USB_MAP)
            merged.update(LEARN["resultat"])
            save_usb_map(merged)
        LEARN["melding"] = (f"Done: learned {len(LEARN['resultat'])} lights"
                            + (f", {len(LEARN['uten_svar'])} without an answer" if LEARN["uten_svar"] else ""))
        log.info("USB map learned: %s", LEARN["melding"])
    except Exception as e:  # noqa: BLE001
        LEARN["melding"] = f"Stopped: {e}"
        log.exception("Learning the USB map failed")
    finally:
        LEARN["lys"] = None
        LEARN["kjorer"] = False


def start_usb_stick():
    global STICK
    mode = light_mode()
    if mode == "home_manager":
        log.info("Light control: Home Manager only (the USB stick is not used)")
        return
    log.info("Light control: %s", "USB stick only" if mode == "usb_stick" else
             "hybrid (USB stick, Home Manager as fallback)")
    n = len(load_usb_map())
    STICK = usbstick.UsbStick(on_radio=_on_usb_radio)
    threading.Thread(target=STICK.run, daemon=True, name="usb").start()
    if n:
        log.info("USB map loaded: %d lights have dimmer serial numbers", n)
    else:
        log.info("No USB map yet (use USB stick -> Find dimmers on the web page) -- the stick is not used for lights until then")


def _fast_batch_lines(batch, cache):
    """Lager kommandolinjene for en bolk bare ut fra det vi allerede vet.
    Returnerer None hvis noe mangler (da brukes den vanlige veien)."""
    first, second = [], []
    lights, switches, climates = [], [], []
    for kind, smux_id, payload in batch:
        info = cache.get(str(smux_id), {})
        if kind == "light":
            brightness = _parse_light_brightness(payload)
            if brightness is None:
                continue
            if not (info.get("val_id") and info.get("set_id")):
                return None
            first.append(f"set_value {info['val_id']} {brightness}")
            second.append(f"new_state {info['set_id']} 0")
            lights.append((smux_id, brightness, info["set_id"]))
        elif kind == "switch":
            on = payload.decode("utf-8").strip().upper() == "ON"
            if not (info.get("on_id") and info.get("off_id")):
                return None
            target = info["on_id"] if on else info["off_id"]
            second.append(f"new_state {target} 0")
            switches.append((smux_id, on, target))
        elif kind == "climate":
            try:
                target = float(payload.decode("utf-8").strip())
            except (ValueError, UnicodeDecodeError):
                log.warning("Invalid temperature command for thermostat %s: %r", smux_id, payload)
                continue
            target = max(xc.THERMOSTAT_MIN, min(xc.THERMOSTAT_MAX, target))
            first.append(f"set_value {smux_id} {target}")
            climates.append((smux_id, target))
        else:
            return None
    return first, second, lights, switches, climates


def _finish_batch(client, armed_lights, armed_switches, applied_climate, t_start, how):
    n = len(armed_lights) + len(armed_switches) + len(applied_climate)
    STATS.add(sent=n)
    if n:
        names = ", ".join(
            [f"light {s}->{b}%" for s, b, _ in armed_lights]
            + [f"switch {s}->{'ON' if o else 'OFF'}" for s, o, _ in armed_switches]
            + [f"thermostat {s}->{t}°" for s, t in applied_climate])
        log.info("Command batch (%d sent %s, %.2fs after HA asked): %s",
                 n, how, time.time() - t_start, names)
    now = time.time()
    for smux_id, brightness, _ in armed_lights:
        publish_light_state(client, smux_id, brightness, force=True)
        PENDING_CONFIRM[smux_id] = (brightness, now)
        LAST_CMD_AT[smux_id] = now
        LAST_CMD_PCT[smux_id] = (brightness, how)
    for smux_id, on, _ in armed_switches:
        publish_switch_state(client, smux_id, on, force=True)
    for smux_id, target in applied_climate:
        publish_state(client, climate_temp_state_topic(smux_id), f"{target:.1f}")


def _try_fast_batch(client, batch, cache, t_start):
    link = LINK
    if link is None or not link.healthy():
        return False
    built = _fast_batch_lines(batch, cache)
    if built is None:
        return False
    first, second, lights, switches, climates = built
    if not link.send_lines(first):
        return False
    if first and second:
        time.sleep(SET_TO_EXECUTE_PAUSE_S)
    if not link.send_lines(second):
        log.warning("The fast path failed in the middle of a batch -- resending via a separate connection")
        return False
    _finish_batch(client, lights, switches, climates, t_start, "via the listener connection")
    return True


def _process_command_batch(client, batch, t_start=None):
    batch = dedupe_last_wins(batch)
    cache = xc.load_cache()
    if t_start is None:
        t_start = time.time()
    batch = _try_usb(client, batch, t_start)
    if not batch:
        return
    if _try_fast_batch(client, batch, cache, t_start):
        return
    armed_lights, armed_switches, applied_climate = [], [], []

    with conn_lock:
        try:
            conn = open_hm("command batch")
        except ConnectionError as e:
            log.error("Command batch with %d command(s) was NOT sent: %s", len(batch), e)
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
                            log.error("Light %s: control objects not found, command not sent", smux_id)
                            STATS.add(failed=1)
                            continue
                        conn.send_line(f"set_value {info['val_id']} {brightness}")
                        armed_lights.append((smux_id, brightness, info["set_id"]))
                    elif kind == "switch":
                        on = payload.decode("utf-8").strip().upper() == "ON"
                        info = cached_objects(conn, smux_id, cache, xc.get_switch_objects, for_command=True)
                        if not info:
                            log.error("Switch %s: on/off objects not found, command not sent", smux_id)
                            STATS.add(failed=1)
                            continue
                        armed_switches.append((smux_id, on, info["on_id"] if on else info["off_id"]))
                    elif kind == "climate":
                        try:
                            target = float(payload.decode("utf-8").strip())
                        except (ValueError, UnicodeDecodeError):
                            log.warning("Invalid temperature command for thermostat %s: %r", smux_id, payload)
                            continue
                        target = max(xc.THERMOSTAT_MIN, min(xc.THERMOSTAT_MAX, target))
                        conn.send_line(f"set_value {smux_id} {target}")
                        applied_climate.append((smux_id, target))
                except Exception as e:  # noqa: BLE001
                    log.error("Error in %s command for %s: %s", kind, smux_id, e)
                    STATS.add(failed=1)

            if armed_lights or armed_switches:
                time.sleep(SET_TO_EXECUTE_PAUSE_S)
            for _s, _b, set_id in armed_lights:
                conn.send_line(f"new_state {set_id} 0")
            for _s, _o, target_id in armed_switches:
                conn.send_line(f"new_state {target_id} 0")
            reply = conn.drain(wait=0.2)
        except Exception as e:  # noqa: BLE001
            log.error("Command batch aborted: %s", e)
            STATS.contact_failed(f"command batch: {e}")
            STATS.add(failed=len(batch))
            return
        finally:
            conn.close()

    log.debug("Reply to batch: %s", xc.readable(reply) or "(none)")
    _finish_batch(client, armed_lights, armed_switches, applied_climate, t_start, "via a separate connection")


def command_batch_worker(client):
    while True:
        HEARTBEAT["batch"] = time.time()
        if not _pending_event.wait(timeout=10):
            continue
        time.sleep(COMMAND_BATCH_WINDOW)
        with _pending_lock:
            batch = _pending_commands[:]
            t_first = _first_queued_at[0] or time.time()
            _pending_commands.clear()
            _pending_event.clear()
        if not batch:
            continue
        try:
            _process_command_batch(client, batch, t_first)
        except Exception:  # noqa: BLE001
            log.exception("Command batch failed unexpectedly")
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
    def __init__(self, smux_id, name, runtime, buttons=None, reverse=False):
        self.smux_id = smux_id
        self.name = name
        self.runtime = max(1.0, float(runtime))
        # Objektene som «trykkes» for inn/stopp/ut. Innebygde markiser bruker de
        # faste avstandene (xc.MARKISE_OFFSETS); markiser lagt til fra nettsiden
        # har objektene søket fant.
        self.buttons = dict(buttons) if buttons else \
            {a: xc.markise_button_id(smux_id, a) for a in ("inn", "stopp", "ut")}
        self.reverse = bool(reverse)   # motoren er koblet motsatt vei
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

    def button_id(self, action):
        if self.reverse and action in ("inn", "ut"):
            action = "ut" if action == "inn" else "inn"
        return self.buttons[action]

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
        m = self.markiser.get(smux_id)
        if m is not None:
            txt = {"ut": "extending", "inn": "retracting", "stopp": "stop"}.get(action, str(action))
            if value is not None:
                txt = f"{txt} ({value})"
            web.EVENTS.add(f"{m.name}: {txt}", "markise")

    def queue_length(self):
        return self.q.qsize() + len(self.scheduled)

    # -- posisjon lagres saa den overlever restart --
    def _saved_positions(self):
        try:
            with open(POSITION_FILE, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:  # noqa: BLE001
            mlog.warning("Could not read awning positions: %s", e)
            return {}

    def _load_positions(self):
        data = self._saved_positions()
        for sid, m in self.markiser.items():
            if str(sid) in data:
                m.position = float(data[str(sid)])
        if not self.markiser:
            return
        if data:
            mlog.info("Loaded saved awning positions: %s",
                      {m.name: round(m.position) for m in self.markiser.values()})
        else:
            mlog.info("No saved awning positions -- assuming all are fully retracted (0 %). "
                      "Run each awning fully in once to calibrate.")

    def add(self, smux_id, name, runtime, buttons=None, reverse=False):
        """Legger til (eller oppdaterer) en markise lagt til fra nettsiden."""
        m = self.markiser.get(smux_id)
        if m is None:
            m = Markise(smux_id, name, runtime, buttons, reverse)
            saved = self._saved_positions()
            if str(smux_id) in saved:
                m.position = float(saved[str(smux_id)])
            self.markiser[smux_id] = m
        else:
            m.name, m.runtime = name, max(1.0, float(runtime))
            m.buttons = dict(buttons) if buttons else m.buttons
            m.reverse = bool(reverse)
        STATS.markise.setdefault(smux_id, {"sendt": 0, "feilet": 0, "siste": "none"})
        return m

    def _save_positions(self):
        try:
            with open(POSITION_FILE, "w", encoding="utf-8") as f:
                json.dump({str(sid): round(m.current_position(), 1) for sid, m in self.markiser.items()}, f)
        except Exception as e:  # noqa: BLE001
            mlog.warning("Could not save awning positions: %s", e)

    def publish(self, m):
        if not (self.client and OPTS["awning_position"]):
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
        gap = OPTS["awning_command_gap"]
        wait = self.last_tx + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        oid = m.button_id(action)
        stats = STATS.markise.setdefault(m.smux_id, {"sendt": 0, "feilet": 0, "siste": "none"})
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
                    self.conn = open_hm(f"awning {m.name}")
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
                    mlog.warning("%s: the connection was broken (%s) -- retrying", m.name, e)
        if t_sent is None:
            stats["feilet"] += 1
            STATS.add(failed=1)
            STATS.contact_failed(f"awning {m.name}: {error}")
            stats["siste"] = f"{now_str()} {action} FAILED"
            mlog.error("%s (%s): '%s' was NOT sent (%s): %s", m.name, m.smux_id, action, reason, error)
            return None
        self.last_tx = t_sent
        stats["sendt"] += 1
        STATS.add(sent=1)
        readable = xc.readable(reply)
        stats["siste"] = f"{now_str()} {action}"
        mlog.info("%s (%s): %s -> new_state %s 0 [%s]", m.name, m.smux_id, action, oid, reason)
        mlog.debug("%s: reply from Home Manager: %s", m.name, readable or "(none)")
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
            if OPTS["awning_double_stop"] and reason != "extra stop":
                self.scheduled.append((t_sent + DOBBEL_STOPP_PAUSE_S, m.smux_id, "stopp", "extra stop"))
        elif reason == "repeat" and m.moving == action:
            pass  # samme bevegelse paa nytt: anslaget fortsetter uendret
        else:
            m.start(action, t_sent)
            if OPTS["awning_repeat_move"] and reason != "repeat":
                self.scheduled.append((t_sent + GJENTA_BEVEGELSE_PAUSE_S, m.smux_id, action, "repeat"))
        self._save_positions()
        self.publish(m)
        return t_sent

    def _handle(self, smux_id, action, value, queued_at):
        m = self.markiser.get(smux_id)
        if not m:
            mlog.warning("Unknown awning %s", smux_id)
            return
        age = time.time() - queued_at
        if age > 2:
            mlog.warning("%s: command '%s' waited %.1fs in the queue before sending", m.name, action, age)
        if action == "set_position":
            plan = plan_set_position(m, value)
            mlog.info("%s: set_position %s %% (now about %d %%) -> %s", m.name, value,
                      round(m.current_position()), f"{plan[0]} for {plan[1]:.1f}s" if plan else "no movement")
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
            mlog.info("%s: new command '%s' -- cancelling scheduled %s", m.name, action,
                      ", ".join(f"'{c[2]}' ({c[3]})" for c in cancelled))
        self._do(m, action, "from HA")

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
                        mlog.exception("Scheduled awning action failed")
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
                mlog.exception("Awning command failed unexpectedly")
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
        mlog.warning("Unknown awning command %r for %s", word, smux_id)
        return
    mlog.info("From HA: awning %s -> %s", smux_id, action)
    MARKISER_CTRL.enqueue(smux_id, action)


def handle_cover_position(client, smux_id, payload_bytes):
    try:
        value = float(payload_bytes.decode("utf-8").strip())
    except (ValueError, UnicodeDecodeError):
        mlog.warning("Invalid position %r for awning %s", payload_bytes, smux_id)
        return
    mlog.info("From HA: awning %s -> position %s %%", smux_id, value)
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
    log.info("Home Assistant restarted -- publishing discovery and state again")
    publish_discovery(client)
    with _state_cache_lock:
        cached = list(_state_cache.items())
    for topic, payload in cached:
        client.publish(topic, payload, qos=0, retain=True)
    client.publish(AVAILABILITY_TOPIC, "online", qos=1, retain=True)


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code != 0:
        log.error("Could not connect to the MQTT broker (reason_code=%s). Check the username and password.", reason_code)
        return
    MQTT_STATE["connected"] = True
    log.info("Connected to the MQTT broker")
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
    log.warning("Lost the connection to the MQTT broker (reason_code=%s) -- reconnecting automatically",
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
        log.exception("Error while handling MQTT message on %s", message.topic)


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
        if v is not None and not usb_grace(smux_id, max(0, min(100, round(v)))):
            publish_light_state(client, smux_id, max(0, min(100, round(v))))


def poll_switches(client, conn):
    ids = [s for _, _, s in xc.SWITCHES]
    found = {int(o): float(v) for o, v in SWITCH_STATE_RE.findall(_batch_d_obj(conn, ids, wait=0.8))}
    for smux_id in ids:
        if smux_id in found:
            publish_switch_state(client, smux_id, found[smux_id] > 0)


def poll_binary_sensors(client, conn):
    keys = [s for _, _, s, _ in BINARY_SENSORS]
    ids = [IDMAP.cur(k) for k in keys]
    found = {int(o): float(v) for o, v in SWITCH_STATE_RE.findall(_batch_d_obj(conn, ids, wait=0.8))}
    for key in keys:
        if IDMAP.cur(key) in found:
            publish_state(client, binary_sensor_state_topic(key), "ON" if found[IDMAP.cur(key)] > 0 else "OFF")


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
            log.warning("Poll round skipped: %s", e)
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
        log.warning("Poll round aborted: %s", e)
        STATS.contact_failed(f"polling: {e}")
    finally:
        conn.close()
    STATS.poll_seconds = time.time() - t0
    if STATS.poll_seconds > 10:
        log.warning("The poll round took %.1fs (usually about 5s)", STATS.poll_seconds)
    else:
        log.debug("Poll round finished in %.2fs", STATS.poll_seconds)


def poll_loop(client):
    """Leser alt av jevnlig. Med frisk lytte-forbindelse holder det med en
    full avlesning hvert 5. minutt; ellers brukes poll_interval."""
    last = 0.0
    while True:
        HEARTBEAT["poll"] = time.time()
        time.sleep(1.0)
        fast = LINK is not None and LINK.healthy()
        interval = FULL_AVLESNING_S if fast else OPTS["poll_interval"]
        STATS.poll_modus = "instantly (+ a full read every 5 min)" if fast else \
            f"every {OPTS['poll_interval']} seconds"
        if time.time() - last < interval:
            continue
        last = time.time()
        HEARTBEAT["poll"] = time.time()
        try:
            poll_all(client)
        except Exception:  # noqa: BLE001
            log.exception("Poll round failed")
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
    "alias": {},          # nåværende nr -> nøkkel for innganger (idmap.py)
}

# Knapper/innganger som blir hendelser (event) i HA. Markiseknappene er
# kjent fra sky-oppslaget 17.10.2026; flere kan legges til fra nettsiden.
BUTTONS = [
    ("Ute", "Markiseknapp 1", 232304),
    ("Ute", "Markiseknapp 2", 234213),
    ("Ute", "Markiseknapp 3", 233840),
]
# Koder fra en trykknapp satt opp som "Generell inngang" i xSetup
# (kartlagt med BR stue 18.10.2026): kort trykk gir 1/0, langt trykk gir
# start + slipp.
KNAPP_KODER = {0: "short_down", 1: "short_up", 2: "long_up", 3: "long_up_release",
               4: "langt_ned", 5: "langt_ned_slipp"}
EVENT_TYPES = list(KNAPP_KODER.values()) + [f"code_{i}" for i in range(6, 10)]

# Av/paa-brytere som egentlig er lys. Med innstillingen "lysbrytere_som_lys"
# faar disse i tillegg en ekte lys-entitet i HA (bryteren beholdes).
LIGHT_SWITCH_IDS = {106282, 117514, 79607, 68193, 86251, 99486, 64163, 107292, 57690, 51416, 101812}

EXTRA_FILE = "/data/ekstra_enheter.json" if os.path.isdir("/data") else \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ekstra_enheter.json")
SCAN_FILE = "/data/objekter.json" if os.path.isdir("/data") else \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "objekter.json")
SEED_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_cache.json")

SCANNER = scanner.Scanner(SCAN_FILE)
ID_MAP_FILE = "/data/id_map.json" if os.path.isdir("/data") else \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "id_map.json")
IDMAP = idmap.IdMap(ID_MAP_FILE)

# Navnet i Home Manager for objekter som kan bli nummerert om ved opplasting
# fra xSetup (innganger). Nøkkelen (det opprinnelige nummeret) brukes fortsatt
# i unique_id og MQTT-topics, så HA merker ingenting.
OBJEKT_NAVN = {
    234899: "IN_Ventilasjon_Drift",
    231939: "IN_Ventilasjon_Tettfilter",
    232677: "IN_Ventilasjon_Overoppheting",
    232304: "IN_Ute_BRMarkise1",
    234213: "IN_Ute_BRMarkise2",
    233840: "IN_Ute_BRMarkise3",
}
for _k, _n in OBJEKT_NAVN.items():
    IDMAP.expect(_k, _n)


def event_topic(i):
    return f"xcomfort/event/{i}"


def onoff_state_topic(i):
    return f"xcomfort/onofflight/{i}/state"


def onoff_command_topic(i):
    return f"xcomfort/onofflight/{i}/set"


def publish_switch_state(client, smux_id, on, force=False):
    publish_state(client, switch_state_topic(smux_id), "ON" if on else "OFF", force=force)
    if OPTS["switches_as_lights"] and smux_id in LIGHT_SWITCH_IDS:
        publish_state(client, onoff_state_topic(smux_id), json.dumps({"state": "ON" if on else "OFF"}), force=force)


def button_label(zone, name, oid):
    """(sone, navn) for en knapp. Knapper lagt til fra nettsiden får sone og
    navn fra objektnavnet i Home Manager slik det er NÅ (IN_<sone>_<navn>),
    så IN_Stue_BRstue1 havner på enheten «Stue» som «BRstue1»."""
    if zone == "Knapper":
        parts = (IDMAP.expected.get(oid) or "").split("_")
        if len(parts) >= 3 and parts[0] == "IN":
            return parts[1], parts[-1]
    return zone, name


def publish_event_discovery(client):
    for zone, name, oid in BUTTONS:
        zone2, name2 = button_label(zone, name, oid)
        _pub_disc(client, "event", f"xcomfort_in_{oid}", {
            "name": name2,
            "unique_id": f"xcomfort_event_{oid}",
            "state_topic": event_topic(oid),
            "event_types": EVENT_TYPES,
            "device_class": "button",
            "device": device_block(zone2),
        })


def publish_onoff_light_discovery(client):
    for zone, name, smux_id in xc.SWITCHES:
        if smux_id not in LIGHT_SWITCH_IDS:
            continue
        topic = f"{DISCOVERY_PREFIX}/light/xcomfort_onoff_{smux_id}/config"
        if not OPTS["switches_as_lights"]:
            client.publish(topic, "", qos=1, retain=True)
            continue
        _pub_disc(client, "light", f"xcomfort_onoff_{smux_id}", {
            "name": f"{name} (light)",
            "unique_id": f"xcomfort_onofflight_{smux_id}",
            "state_topic": onoff_state_topic(smux_id),
            "command_topic": onoff_command_topic(smux_id),
            "schema": "json",
            "supported_color_modes": ["onoff"],
            "optimistic": False,
            "qos": 1,
            "device": device_block(zone),
        })


# Hvilke typer enheter som kan legges til fra nettsiden, og hva de heter i
# ekstra_enheter.json. «alarmer» er innganger (IN_) brukt som av/på-sensorer.
EXTRA_KINDS = ("lys", "brytere", "knapper", "markiser", "termostater", "sensorer", "alarmer")
EXTRA_IDS = set()          # id-er for alt som er lagt til fra nettsiden
DEFAULT_ROOM = "New devices"
DEFAULT_AWNING_RUNTIME = 30.0
SENSOR_UNITS = ("°C", "lux", "m/s", "%", "W", "kWh", "s", "")
EXTRA_LABELS = {"lys": "lights", "brytere": "switches", "knapper": "buttons", "markiser": "awnings",
                "termostater": "thermostats", "sensorer": "sensors", "alarmer": "on/off sensors"}


def load_extras():
    """Enheter som er lagt til fra nettsiden (Devices -> Search). Legges til
    listene foer discovery, med samme unique_id-moenster som de faste
    enhetene. Kan kalles flere ganger: det som allerede er lagt inn hoppes over."""
    try:
        with open(EXTRA_FILE, encoding="utf-8") as f:
            extra = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:  # noqa: BLE001
        log.warning("Could not read %s: %s", EXTRA_FILE, e)
        return
    cache = xc.load_cache()
    known = ({s for _, _, s in xc.LIGHTS} | {s for _, _, s in xc.SWITCHES} | {b[2] for b in BUTTONS}
             | {s for _, _, s in xc.MARKISER} | {s for _, _, s in xc.THERMOSTATS}
             | {s[2] for s in CONTINUOUS_SENSORS} | {s[2] for s in BINARY_SENSORS})
    changed = False
    counts = {}
    for kind in EXTRA_KINDS:
        for item in extra.get(kind, []):
            oid = int(item["id"])
            EXTRA_IDS.add(oid)
            counts[kind] = counts.get(kind, 0) + 1
            if oid in known:
                continue
            room = item.get("sone") or (DEFAULT_ROOM if kind != "knapper" else "Knapper")
            name = item["navn"]
            if kind == "lys":
                xc.LIGHTS.append((room, name, oid))
                if item.get("val_id") and item.get("set_id") and str(oid) not in cache:
                    cache[str(oid)] = {"name": item.get("objekt"), "val_id": item["val_id"], "set_id": item["set_id"]}
                    changed = True
            elif kind == "brytere":
                xc.SWITCHES.append((room, name, oid))
                if item.get("on_id") and item.get("off_id") and str(oid) not in cache:
                    cache[str(oid)] = {"name": item.get("objekt"), "on_id": item["on_id"], "off_id": item["off_id"]}
                    changed = True
            elif kind == "knapper":
                BUTTONS.append((room, name, oid))
                IDMAP.expect(oid, item.get("objekt"))
            elif kind == "markiser":
                xc.MARKISER.append((room, name, oid))
                buttons = None
                if item.get("open_id") and item.get("stop_id") and item.get("close_id"):
                    # Samme betydning som de innebygde: Open = inn, Close = ut.
                    buttons = {"inn": item["open_id"], "stopp": item["stop_id"], "ut": item["close_id"]}
                MARKISER_CTRL.add(oid, name, item.get("runtime") or DEFAULT_AWNING_RUNTIME,
                                  buttons, item.get("reverse", False))
            elif kind == "termostater":
                xc.THERMOSTATS.append((room, name, oid))
            elif kind == "sensorer":
                CONTINUOUS_SENSORS.append((room, name, oid, item.get("enhet", "")))
            elif kind == "alarmer":
                BINARY_SENSORS.append((room, name, oid, ""))
                BINARY_NAMES[oid] = f"{room} {name}"
                IDMAP.expect(oid, item.get("objekt"))
    if changed:
        xc.save_cache(cache)
    if counts:
        log.info("Devices added from the web page: %s",
                 ", ".join(f"{n} {EXTRA_LABELS[k]}" for k, n in counts.items()))


# ------------------------------------------------------------ anlegg --
#
# Listene over lys, brytere, markiser, sensorer og termostater i
# xcomfort_local_control.py er laget for ETT bestemt anlegg (det appen ble
# utviklet på). Hos alle andre ville de numrene peke på helt andre ting i
# Home Manager. Derfor sjekker appen ved første oppstart om navnene i Home
# Manager stemmer med listene. Gjør de det, brukes listene. Ellers starter
# appen tom, og enhetene legges til fra nettsiden (Enheter → Søk).
# Valget lagres i /data/anlegg.json og kan overstyres der.

PROFILE_FILE = "/data/anlegg.json" if os.path.isdir("/data") else \
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "anlegg.json")
PROFILE = {"innebygd": False, "grunn": "not checked"}


def _load_seed():
    try:
        with open(SEED_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _names_match_seed(seed, sample=12):
    """Spør Home Manager om navnet på noen av objektene i den innebygde
    lista. True/False, eller None hvis Home Manager ikke svarer. Navnene er
    så spesielle (f.eks. D_TaklampefinstueStue på et bestemt nummer) at to
    treff ikke kan skje tilfeldig, så noen omdøpte objekter gjør ingenting."""
    items = [(int(k), v.get("name")) for k, v in sorted(seed.items()) if v.get("name")][:sample]
    if not items:
        return False
    try:
        conn = xc.XComfortConn()
    except Exception as e:  # noqa: BLE001
        log.info("Could not check the installation right now (%s)", e)
        return None
    try:
        conn.sock.sendall(b"".join(xc.STX + f"d_obj {i}".encode("ascii") + xc.ETX for i, _ in items))
        text = conn.drain(wait=1.0)
    except Exception as e:  # noqa: BLE001
        log.info("Could not check the installation right now (%s)", e)
        return None
    finally:
        conn.close()
    got = {int(o): n for o, n, _t in scanner.NAME_TYPE_RE.findall(text)}
    hits = sum(1 for i, n in items if got.get(i) == n)
    log.info("Installation check: %d of %d known names matched", hits, len(items))
    return hits >= min(2, len(items))


def decide_profile(check=_names_match_seed):
    """Bestemmer om de innebygde enhetslistene skal brukes."""
    try:
        with open(PROFILE_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved.get("innebygd"), bool):
            PROFILE.update(innebygd=saved["innebygd"], grunn="saved choice")
            return PROFILE["innebygd"]
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("Could not read %s: %s", PROFILE_FILE, e)
    had_data = os.path.exists(xc.CACHE_FILE)   # appen har kjørt her før (eldre versjon)
    ok = check(_load_seed())
    if ok is None:
        # Home Manager svarer ikke akkurat nå. En installasjon som har kjørt
        # før (fra før denne sjekken fantes) beholder listene; en ny starter
        # tom. Ingenting lagres, så det sjekkes igjen ved neste oppstart.
        PROFILE.update(innebygd=had_data, grunn="Home Manager did not answer, checking again at next start")
        return had_data
    if not ok and had_data:
        log.warning("This installation has run an older version, but the names in Home Manager do not match "
                    "the built-in device list, so the app starts empty. If this is wrong, write "
                    '{"innebygd": true} to %s and restart the app.', PROFILE_FILE)
    PROFILE.update(innebygd=bool(ok), grunn="the names in Home Manager matched" if ok else "new installation")
    try:
        with open(PROFILE_FILE, "w", encoding="utf-8") as f:
            json.dump({"innebygd": bool(ok)}, f)
    except OSError as e:
        log.warning("Could not save %s: %s", PROFILE_FILE, e)
    return bool(ok)


def clear_builtin_devices():
    """Fjerner de innebygde listene, så bare enheter lagt til fra nettsiden brukes."""
    for lst in (xc.LIGHTS, xc.SWITCHES, xc.SENSORS, xc.MARKISER, xc.THERMOSTATS,
                CONTINUOUS_SENSORS, BINARY_SENSORS, BUTTONS):
        lst[:] = []
    BINARY_NAMES.clear()
    LIGHT_SWITCH_IDS.clear()
    MARKISER_CTRL.markiser.clear()
    STATS.markise.clear()
    for k in OBJEKT_NAVN:
        IDMAP.expected.pop(k, None)


def apply_profile():
    if decide_profile():
        log.info("Known devices for this installation are loaded (%s)", PROFILE["grunn"])
        merge_seed_cache()
    else:
        clear_builtin_devices()
        log.info("New installation (%s): the app only uses devices you add under Devices on the web page",
                 PROFILE["grunn"])


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
        log.info("Added %d known control objects from the built-in list", len(added))


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
    REG["alias"] = IDMAP.alias()
    REG["sensor_ids"] = {s for _, _, s, _ in CONTINUOUS_SENSORS}
    REG["thermo_ids"] = {s for _, _, s in xc.THERMOSTATS}
    REG["button_ids"] = {oid: " ".join(button_label(zone, name, oid)) for zone, name, oid in BUTTONS}
    REG["markise_ids"] = {s for _, _, s in xc.MARKISER}
    for zone, name, smux in xc.SWITCHES + xc.MARKISER:
        REG["names"][smux] = f"{zone} {name}"
    followed = {IDMAP.cur(k) for k in REG["binary_ids"] | set(REG["button_ids"])}
    ids = (set(REG["val_to_light"]) | REG["light_ids"] | REG["switch_ids"] | followed
           | REG["sensor_ids"] | REG["thermo_ids"] | REG["markise_ids"])
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
            smux = REG["val_to_light"][oid]
            pct = max(0, min(100, round(v)))
            if not usb_grace(smux, pct):
                publish_light_state(client, smux, pct)
                note_change("lys", smux, pct, f"{REG['names'].get(smux, smux)}: {pct} %" if pct else
                            f"{REG['names'].get(smux, smux)}: off")
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
            log.info("Light %s confirmed %d %% after %.1fs", REG["names"].get(oid, oid), level, secs)
    if oid in REG["switch_ids"]:
        v = _num(fields, 2)
        if v is not None:
            publish_switch_state(client, oid, v > 0)
            note_change("bryter", oid, v > 0, f"{REG['names'].get(oid, oid)}: {'on' if v > 0 else 'off'}")
    # Innganger kan ha fått nytt nummer -- slå opp nøkkelen (se idmap.py)
    fkey = REG["alias"].get(oid, oid) if oid in REG["alias"] else \
        (None if oid in REG["binary_ids"] | set(REG["button_ids"]) else oid)
    if fkey in REG["binary_ids"]:
        v = _num(fields, 2)
        if v is not None:
            publish_state(client, binary_sensor_state_topic(fkey), "ON" if v > 0 else "OFF")
            note_change("alarm", fkey, v > 0, f"{BINARY_NAMES.get(fkey, fkey)}: {'ACTIVE' if v > 0 else 'normal'}")
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
    quiet = LINK is None or LINK.recently_subscribed(oid) or SCANNER.running or IDMAP.quiet()
    if fkey in REG["button_ids"]:
        code = _num(fields, 2)
        if code is not None and not quiet:
            kode = int(code)
            log.info("Button %s: %s (code %d)", REG["button_ids"][fkey], KNAPP_KODER.get(kode, "?"), kode)
            etype = KNAPP_KODER.get(kode, f"code_{min(kode, 9)}")
            LAST_BUTTON[fkey] = (etype, time.time())
            web.EVENTS.add(f"{REG['button_ids'][fkey]}: {etype.replace('_', ' ')}", "knapp")
            client.publish(event_topic(fkey), json.dumps({"event_type": etype, "code": kode}),
                           qos=0, retain=False)
    elif name.startswith("IN_") and oid not in REG["alias"] and not quiet:
        RECENT_UNKNOWN[oid] = (name, time.time())
        log.info("Unknown button/input %s (%d) sent %s -- it can be added under Buttons on the web page",
                 name, oid, " ".join(fields))
    if oid in REG["markise_ids"]:
        mlog.debug("Awning object %s: %s", REG["names"].get(oid, oid), " ".join(fields))


# Innganger som har sendt noe, men som ikke er lagt til ennå: {id: (navn, tid)}.
# Vises øverst i Verktøy så de kan legges til uten et fullt søk.
RECENT_UNKNOWN = {}


def on_hm_ack(kind):
    STATS.radio.append(time.time())
    web.RAW_RING.add(f"{web.ts()}  radiokvittering {kind}")


def on_hm_other(text):
    web.RAW_RING.add(f"{web.ts()}  {text}")


def id_watch():
    """Sjekker at innganger har samme nummer som før: ved oppstart, hver 6.
    time og etter hvert fulle søk fra nettsiden."""
    time.sleep(10)
    last_scan = SCANNER.finished
    next_check = 0.0
    while True:
        if time.time() >= next_check or SCANNER.finished != last_scan:
            last_scan = SCANNER.finished
            next_check = time.time() + 6 * 3600
            if not SCANNER.running:
                names_before = dict(IDMAP.expected)
                try:
                    changes = IDMAP.verify(SCANNER.objects)
                except Exception as e:  # noqa: BLE001
                    log.warning("Could not check object numbers: %s", e)
                    next_check = time.time() + 600
                    changes = []
                if changes or IDMAP.expected != names_before:
                    ids = rebuild_registry()
                    if LINK:
                        LINK.subscribe(ids)
                    if MQTT_STATE["client"] is not None:
                        publish_event_discovery(MQTT_STATE["client"])
        time.sleep(30)


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
                log.info("Light %s: the lamp did not confirm %d %% within %.0fs (slow radio, "
                         "or a lamp that does not report its status)", REG["names"].get(smux, smux),
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
            problems.append("not connected to MQTT")
        if now - HEARTBEAT["poll"] > max(OPTS["poll_interval"], FULL_AVLESNING_S) * 2 + 120:
            problems.append("polling is stuck")
        if now - HEARTBEAT["batch"] > 60:
            problems.append("the command thread is stuck")
        if now - HEARTBEAT["markise"] > 60:
            problems.append("the awning thread is stuck")
        return problems

    @staticmethod
    def hm_status():
        """«ok» naar vi har kontakt med Home Manager, «feil» naar siste forsoek
        feilet, «ukjent» rett etter start foer noe er forsoekt."""
        if (LINK and (getattr(LINK, "connected", False) or LINK.healthy())) or STATS.hm_ok:
            return "ok"
        if (LINK and getattr(LINK, "last_error", "")) or STATS.last_error != "none":
            return "feil"
        return "ukjent"

    @staticmethod
    def hm_error():
        if LINK and getattr(LINK, "last_error", ""):
            return LINK.last_error
        return "" if STATS.last_error == "none" else STATS.last_error

    def status(self):
        snap = STATS.snapshot(MARKISER_CTRL.queue_length())
        return {
            "versjon": BRIDGE_VERSION,
            "nytt_anlegg": not PROFILE["innebygd"],
            "oppetid_sek": round(time.time() - START_TIME),
            "problemer": self.health(),
            "antall": {"lys": len(xc.LIGHTS), "brytere": len(xc.SWITCHES), "markiser": len(xc.MARKISER),
                       "termostater": len(xc.THERMOSTATS), "knapper": len(BUTTONS)},
            "mqtt": MQTT_STATE["connected"],
            "mqtt_host": OPTS["mqtt_host"],
            "lytter_ok": bool(LINK and LINK.healthy()),
            "hm_status": self.hm_status(),
            "hm_adresse": f"{OPTS['xcomfort_ip']}:{OPTS['xcomfort_port']}",
            "hm_feil": self.hm_error(),
            "usb": self.usb(),
            "siste_melding_sek": round(time.time() - LINK.last_rx) if LINK and LINK.last_rx else None,
            "sendt": snap["sent"], "feilet": snap["failed"], "siste_feil": snap["last_error"],
            "bekreftet": snap["confirmed"], "ubekreftet": snap["unconfirmed"],
            "snitt_respons": snap["resp_avg"], "radio_5min": snap["radio_5min"],
            "poll_modus": STATS.poll_modus, "poll_sek": round(STATS.poll_seconds, 1),
            "responstider": list(STATS.responstider),
            "markiser": [{"id": sid, "navn": m.name,
                          "posisjon": round(m.current_position()), "tilstand": m.ha_state()}
                         for sid, m in MARKISER_CTRL.markiser.items()],
        }

    def devices(self):
        with _state_cache_lock:
            cache = dict(_state_cache)
        extra = self._read_extra()
        extra_items = {int(x["id"]): (k, x) for k in EXTRA_KINDS for x in extra.get(k, [])}
        extra_ids = set(extra_items)
        out = []
        for zone, name, smux in xc.LIGHTS:
            st = json.loads(cache.get(light_state_topic(smux), "{}") or "{}")
            on = st.get("state") == "ON"
            out.append({"type": "lys", "id": smux, "sone": zone, "navn": name, "på": on,
                        "tilstand": f"{st.get('brightness', 100)} %" if on else ("off" if st else "?")})
        for zone, name, smux in xc.SWITCHES:
            v = cache.get(switch_state_topic(smux))
            out.append({"type": "bryter", "id": smux, "sone": zone, "navn": name, "på": v == "ON",
                        "tilstand": {"ON": "on", "OFF": "off"}.get(v, "?")})
        for zone, name, smux in xc.MARKISER:
            m = MARKISER_CTRL.markiser[smux]
            out.append({"type": "markise", "id": smux, "sone": zone, "navn": name, "på": m.position > 1,
                        "posisjon": round(m.current_position()), "bevegelse": m.ha_state(),
                        "tilstand": f"{round(m.current_position())} % ({m.ha_state()})"})
        for zone, name, smux in xc.THERMOSTATS:
            cur = cache.get(climate_current_topic(smux))
            setp = cache.get(climate_temp_state_topic(smux))
            out.append({"type": "termostat", "id": smux, "sone": zone, "navn": name, "på": False,
                        "temp": cur, "mal": setp,
                        "tilstand": f"{cur} °C (target {setp} °C)" if cur else "?"})
        for zone, name, oid in BUTTONS:
            z2, n2 = button_label(zone, name, oid)
            last = LAST_BUTTON.get(oid)
            out.append({"type": "knapp", "id": oid, "sone": z2, "navn": n2, "på": False,
                        "ekstra": oid in extra_ids,
                        "siste": last[0] if last else None, "sek_siden": round(time.time() - last[1]) if last else None,
                        "tilstand": last[0].replace("_", " ") if last else "no presses yet"})
        for zone, name, smux, unit in CONTINUOUS_SENSORS:
            v = cache.get(sensor_state_topic(smux))
            out.append({"type": "sensor", "id": smux, "sone": zone, "navn": name, "på": False,
                        "enhet": unit, "tilstand": f"{v} {unit}".strip() if v is not None else "?"})
        for zone, name, key, _unit in BINARY_SENSORS:
            v = cache.get(binary_sensor_state_topic(key))
            out.append({"type": "alarm", "id": key, "sone": zone, "navn": name, "på": v == "ON",
                        "tilstand": {"ON": "active", "OFF": "normal"}.get(v, "?")})
        for d in out:
            d.setdefault("ekstra", d["id"] in extra_ids)
            if d["ekstra"]:
                kind, item = extra_items[d["id"]]
                d["art"] = kind
                if kind == "markiser":
                    d["runtime"] = item.get("runtime", DEFAULT_AWNING_RUNTIME)
                    d["reverse"] = bool(item.get("reverse"))
                if kind == "sensorer":
                    d["enhet"] = item.get("enhet", "")
        return out

    def usb(self):
        info = STICK.snapshot() if STICK else {"tilkoblet": False, "navn": "", "sendt": 0, "feilet": 0,
                                                "siste_feil": ""}
        info.update({"aktivert": light_mode() != "home_manager", "modus": light_mode(),
                     "ubekreftet": USB_STATS["ubekreftet"], "bekreftet_hm": USB_STATS["bekreftet_hm"],
                     "lys_med_kart": len(USB_MAP), "radio_dimmere": list(RADIO_LOG)[-25:],
                     "lys_totalt": len(xc.LIGHTS), "bekreftet": USB_STATS["bekreftet"],
                     "via_reserve": USB_STATS["reserve"], "store_bolker": USB_STATS["stor_bolk"],
                     "laering": {k: v for k, v in LEARN.items() if k not in ("hort", "resultat", "lys")},
                     "kart": [{"id": k, "navn": REG["names"].get(k, str(k)), "serienr": v}
                              for k, v in sorted(USB_MAP.items(), key=lambda kv: REG["names"].get(kv[0], ""))]})
        return info

    def usb_learn(self, body):
        if LEARN["kjorer"]:
            return {"ok": False, "feil": "learning is already running"}
        if STICK is None or not STICK.healthy():
            return {"ok": False, "feil": "the USB stick is not connected (and Light control must be hybrid or usb_stick)"}
        wanted = body.get("lys")
        lights = [s for _, _, s in xc.LIGHTS]
        if wanted == "mangler":
            lights = [s for s in lights if s not in USB_MAP]
        elif isinstance(wanted, list):
            lights = [s for s in lights if s in {int(x) for x in wanted}]
        if not lights:
            return {"ok": False, "feil": "no lights to learn"}
        threading.Thread(target=learn_usb_map, args=(lights,), daemon=True, name="usb-laering").start()
        return {"ok": True, "antall": len(lights)}

    def usb_map_upload(self, body):
        lys = body.get("lys") if isinstance(body, dict) else None
        if not isinstance(lys, dict):
            raise ValueError("the file must look like {\"lys\": {\"<id>\": [serial, ...]}}")
        clean = {}
        for k, v in lys.items():
            if isinstance(v, dict):
                v = v.get("serienr", [])
            clean[int(k)] = [int(x) for x in v]
        n = len(save_usb_map(clean))
        log.info("New USB map saved: %d lights have dimmer serial numbers", n)
        return {"ok": True, "lys_med_kart": n}

    def _read_extra(self):
        try:
            with open(EXTRA_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}

    def _write_extra(self, extra):
        tmp = EXTRA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(extra, f, indent=2, ensure_ascii=False)
        os.replace(tmp, EXTRA_FILE)

    @staticmethod
    def _drop_from_lists(oid, forget_awning=False):
        """Fjerner en enhet lagt til fra nettsiden fra listene i minnet."""
        xc.LIGHTS[:] = [x for x in xc.LIGHTS if x[2] != oid]
        xc.SWITCHES[:] = [x for x in xc.SWITCHES if x[2] != oid]
        xc.MARKISER[:] = [x for x in xc.MARKISER if x[2] != oid]
        xc.THERMOSTATS[:] = [x for x in xc.THERMOSTATS if x[2] != oid]
        BUTTONS[:] = [x for x in BUTTONS if x[2] != oid]
        CONTINUOUS_SENSORS[:] = [x for x in CONTINUOUS_SENSORS if x[2] != oid]
        BINARY_SENSORS[:] = [x for x in BINARY_SENSORS if x[2] != oid]
        BINARY_NAMES.pop(oid, None)
        if forget_awning:
            MARKISER_CTRL.markiser.pop(oid, None)
            STATS.markise.pop(oid, None)

    def _refresh(self):
        ids = rebuild_registry()
        if LINK:
            LINK.subscribe(ids)
        client = MQTT_STATE["client"]
        if client:
            publish_discovery(client)

    def _find_extra(self, extra, oid):
        for kind in EXTRA_KINDS:
            for item in extra.get(kind, []):
                if int(item["id"]) == oid:
                    return kind, item
        return None, None

    # Discovery-emner som må tømmes når en enhet fjernes, så den forsvinner fra HA.
    REMOVE_TOPICS = {
        "lys": ["light/xcomfort_{id}"],
        "brytere": ["switch/xcomfort_{id}", "light/xcomfort_onoff_{id}"],
        "knapper": ["event/xcomfort_in_{id}"],
        "markiser": ["cover/xcomfort_{id}", "sensor/xcomfort_diag_markise_{id}_sendt",
                     "sensor/xcomfort_diag_markise_{id}_feilet", "sensor/xcomfort_diag_markise_{id}_siste"],
        "termostater": ["climate/xcomfort_{id}", "sensor/xcomfort_valve_{id}"],
        "sensorer": ["sensor/xcomfort_{id}"],
        "alarmer": ["binary_sensor/xcomfort_{id}"],
    }

    def remove_extra(self, body):
        """Fjerner en enhet som er lagt til fra nettsiden (ikke de faste)."""
        oid = int(body.get("id"))
        extra = self._read_extra()
        kind, _item = self._find_extra(extra, oid)
        if kind is None:
            return {"ok": False, "feil": "device not found among those added from the web page"}
        extra[kind] = [x for x in extra[kind] if int(x["id"]) != oid]
        self._write_extra(extra)
        self._drop_from_lists(oid, forget_awning=True)
        EXTRA_IDS.discard(oid)
        if kind in ("knapper", "alarmer"):
            IDMAP.expected.pop(oid, None)
        rebuild_registry()
        client = MQTT_STATE["client"]
        if client:
            for t in self.REMOVE_TOPICS[kind]:
                client.publish(f"{DISCOVERY_PREFIX}/{t.format(id=oid)}/config", "", qos=1, retain=True)
        log.info("Removed %s from HA (from the web page)", oid)
        return {"ok": True}

    def update_extra(self, body):
        """Endrer en enhet lagt til fra nettsiden: navn, rom, og for markiser
        kjøretid og retning, for sensorer enhet."""
        oid = int(body.get("id"))
        extra = self._read_extra()
        kind, item = self._find_extra(extra, oid)
        if kind is None:
            return {"ok": False, "feil": "device not found among those added from the web page"}
        want = dict(body)
        if kind != "markiser":
            want.pop("runtime", None)
            want.pop("reverse", None)
        if kind != "sensorer":
            want.pop("enhet", None)
        self._clean(item, want)
        self._write_extra(extra)
        self._drop_from_lists(oid)
        load_extras()
        self._refresh()
        log.info("Changed %s: %s in %s", oid, item["navn"], item.get("sone", "-"))
        return {"ok": True}

    def command(self, body):
        kind, smux, value = body.get("type"), int(body.get("id")), str(body.get("value"))
        log.info("Command from the web page: %s %s -> %s", kind, smux, value)
        if kind == "lys":
            pct = max(0, min(100, int(float(value))))
            payload = {"state": "OFF"} if pct == 0 else {"state": "ON", "brightness": pct}
            _queue_command("light", smux, json.dumps(payload).encode())
        elif kind == "bryter":
            _queue_command("switch", smux, b"ON" if value.upper() == "ON" else b"OFF")
        elif kind == "markise":
            if value not in ("ut", "inn", "stopp"):
                return {"ok": False, "feil": "unknown awning action"}
            MARKISER_CTRL.enqueue(smux, value)
        else:
            return {"ok": False, "feil": "unknown type"}
        return {"ok": True}

    def scan_start(self):
        return {"ok": SCANNER.start()}

    def _known(self):
        """Alt broen allerede bruker: skal ikke foreslås som nytt."""
        return (REG["light_ids"] | REG["switch_ids"] | REG["binary_ids"] | REG["sensor_ids"]
                | REG["thermo_ids"] | set(REG["button_ids"]) | REG["markise_ids"] | set(REG["val_to_light"])
                | set(REG["alias"]) | EXTRA_IDS)

    def scan_info(self):
        cands = scanner.candidates(SCANNER.objects, self._known())
        now = time.time()
        nylig = [{"id": oid, "objekt": name, "navn": scanner.pretty(name.split("_", 1)[-1]),
                  "sek_siden": round(now - t)}
                 for oid, (name, t) in sorted(RECENT_UNKNOWN.items(), key=lambda x: -x[1][1])
                 if oid not in REG["alias"] and oid not in EXTRA_IDS and now - t < 24 * 3600]
        return {"status": SCANNER.status(), "kandidater": cands, "nylig": nylig, "enheter": list(SENSOR_UNITS)}

    def scan_all(self):
        known = self._known()
        return [{"id": oid, "navn": name, "type": typ, "kjent": oid in known}
                for oid, (name, typ) in sorted(SCANNER.objects.items())]

    @staticmethod
    def _clean(item, want):
        """Felles felt fra nettsiden: navn, rom, kjøretid, enhet, retning."""
        if str(want.get("navn") or "").strip():
            item["navn"] = str(want["navn"]).strip()[:60]
        if str(want.get("sone") or "").strip():
            item["sone"] = str(want["sone"]).strip()[:40]
        if "runtime" in want and want["runtime"] not in (None, ""):
            item["runtime"] = max(1.0, min(300.0, float(want["runtime"])))
        if "reverse" in want:
            item["reverse"] = bool(want["reverse"])
        if "enhet" in want and want["enhet"] in SENSOR_UNITS:
            item["enhet"] = want["enhet"]
        return item

    def add_extra(self, body):
        extra = self._read_extra()
        cands = scanner.candidates(SCANNER.objects, self._known())
        added = []
        for kind in ("lys", "brytere", "knapper", "markiser", "termostater", "sensorer"):
            for entry in body.get(kind, []):
                want = entry if isinstance(entry, dict) else {"id": entry}
                oid = int(want["id"])
                item = next((dict(c) for c in cands[kind] if c["id"] == oid), None)
                if item is None and kind == "knapper" and (oid in SCANNER.objects or oid in RECENT_UNKNOWN):
                    # Hvilket som helst objekt kan foelges som knapp/hendelse
                    name = (SCANNER.objects.get(oid) or RECENT_UNKNOWN[oid])[0]
                    item = {"id": oid, "navn": scanner.pretty(name.split("_", 1)[-1]), "objekt": name}
                if item is None:
                    continue
                item.pop("lagt_til", None)
                self._clean(item, want)
                target = kind
                if kind == "knapper" and want.get("som") == "sensor":
                    target = "alarmer"     # inngang som av/på-sensor i stedet for knapp
                if kind == "markiser":
                    item.setdefault("runtime", DEFAULT_AWNING_RUNTIME)
                    item.setdefault("reverse", False)
                if any(int(x["id"]) == oid for k in EXTRA_KINDS for x in extra.get(k, [])):
                    continue
                extra.setdefault(target, []).append(item)
                added.append(item["navn"])
        if added:
            self._write_extra(extra)
            load_extras()
            self._refresh()
            log.info("Added from the web page: %s", ", ".join(added))
        return {"ok": True, "lagt_til": added}

    def cache(self):
        return xc.load_cache()


# ------------------------------------------------------------------ main --

def main():
    setup_logging(OPTS["log_level"])
    if not str(OPTS["xcomfort_ip"]).strip():
        log.error("Home Manager IP is not set. Open the app's Configuration tab, "
                  "enter the IP address of the Home Manager (and the MQTT username/password), "
                  "save and restart the app.")
        sys.exit(1)
    log.info("Starting xComfort MQTT bridge v%s (Home Manager %s:%s, MQTT %s:%s, log level %s)",
             BRIDGE_VERSION, OPTS["xcomfort_ip"], OPTS["xcomfort_port"],
             OPTS["mqtt_host"], OPTS["mqtt_port"], OPTS["log_level"])
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
    apply_profile()
    start_usb_stick()
    load_extras()
    if MARKISER_CTRL.markiser:
        log.info("Awnings: position=%s, runtimes=%s, gap=%.1fs, double stop=%s, repeat move=%s",
                 OPTS["awning_position"],
                 {m.name: m.runtime for m in MARKISER_CTRL.markiser.values()},
                 OPTS["awning_command_gap"], OPTS["awning_double_stop"],
                 OPTS["awning_repeat_move"])
    ids = rebuild_registry()
    if OPTS["instant_status"]:
        LINK = hm_link.HMLink(on_hm_object, on_hm_ack, on_hm_other)
        LINK.subscribe(ids)
        threading.Thread(target=LINK.run, daemon=True, name="lytter").start()
        log.info("Instant status is ON: listening to %d objects in Home Manager", len(ids))
    else:
        log.info("Instant status is OFF: polling every %ss only", OPTS["poll_interval"])
    threading.Thread(target=housekeeping, args=(client,), daemon=True, name="husholdning").start()
    threading.Thread(target=id_watch, daemon=True, name="idsjekk").start()
    web.start(Api(), HEALTH_PORT)
    threading.Thread(target=poll_loop, args=(client,), daemon=True, name="poll").start()
    threading.Thread(target=command_batch_worker, args=(client,), daemon=True, name="bolk").start()
    threading.Thread(target=MARKISER_CTRL.run, args=(client,), daemon=True, name="markise").start()

    log.info("Connecting to the MQTT broker at %s:%s ...", OPTS["mqtt_host"], OPTS["mqtt_port"])
    client.connect_async(OPTS["mqtt_host"], OPTS["mqtt_port"], keepalive=60)
    try:
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
