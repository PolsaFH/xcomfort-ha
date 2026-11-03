"""
Tester for xComfort MQTT-broen mot en SIMULERT Home Manager.
Kjoeres paa PC-en (ikke i HA):   python -m unittest discover -s tests -v
(krever paho-mqtt:  pip install paho-mqtt)

Ingen ekte markiser eller lys beveger seg -- "Home Manager" her er en liten
TCP-server som oppfoerer seg som den ekte (handshake, d_obj-svar) og logger
alt den mottar, saa vi kan sjekke koe, pauser, dobbel stopp, bolk-sending
og posisjonsberegning.
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# Ikke la testene lese/skrive /data paa maskinen.
os.environ.pop("XCOMFORT_IP", None)


class FakeHM:
    """Liten falsk Home Manager. Svarer med handshake, svarer paa d_obj med
    en SSN-linje, og husker alle kommandoer med tidsstempel."""

    def __init__(self, handshake=True, values=None, ack_ids=(), idle_timeout=5, exists=None, names=None):
        self.handshake = handshake
        self.names = names or {}
        self.exists = set(exists) if exists is not None else None  # None = alle ID-er finnes
        self.idle_timeout = idle_timeout
        self.ack_ids = set(ack_ids)   # markiser som "kvitterer" (som ekte aktuatorer)
        self.values = values or {}
        self.received = []          # [(tid, kommando)]
        self.connections = 0
        self.clients = []
        self.lock = threading.Lock()
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(20)
        self.port = self.srv.getsockname()[1]
        self.running = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while self.running:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            with self.lock:
                self.connections += 1
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        with self.lock:
            self.clients.append(c)
        if self.handshake:
            c.sendall(b'\x01<connect sn="1" ip="127.0.0.1" mac="x"/>\x02')
        buf = b""
        c.settimeout(self.idle_timeout)
        try:
            while True:
                data = c.recv(4096)
                if not data:
                    break
                buf += data
                while b"\x02" in buf:
                    frame, buf = buf.split(b"\x02", 1)
                    cmd = frame.replace(b"\x01", b"").decode()
                    with self.lock:
                        self.received.append((time.time(), cmd))
                    if cmd.startswith("new_state "):
                        oid = int(cmd.split()[1])
                        for base in self.ack_ids:
                            if oid - base in (61, 126, 191):
                                c.sendall(f"\x01\x07oid x RSN {base} D_Test 21 1 2 2\x02"
                                          f"\x01x_bm_st ACK_DIRECT seq=1\x02".encode())
                    if cmd.startswith("d_obj "):
                        oid = int(cmd.split()[1])
                        if self.exists is not None and oid not in self.exists:
                            continue
                        val = self.values.get(oid, 0)
                        c.sendall(f"\x01SSN {oid} {self.names.get(oid, f'OBJ_{oid}')} 23 1 0 {val}\x02\x00".encode())
        except OSError:
            pass
        finally:
            c.close()

    def push(self, text):
        """Sender en melding til ALLE aapne forbindelser (som ekte HM)."""
        with self.lock:
            clients = list(self.clients)
        for c in clients:
            try:
                c.sendall(b"\x01" + text.encode() + b"\x02")
            except OSError:
                pass

    def commands(self, prefix=""):
        with self.lock:
            return [(t, c) for t, c in self.received if c.startswith(prefix)]

    def close(self):
        self.running = False
        self.srv.close()


def load_bridge(fake, **options):
    """Laster broen paa nytt med gitte innstillinger og peker den mot den
    falske Home Manager-en."""
    tmp = tempfile.mkdtemp()
    for mod in ("xcomfort_mqtt_bridge", "xcomfort_local_control", "hm_link", "scanner", "web", "idmap", "usbstick"):
        sys.modules.pop(mod, None)
    import xcomfort_local_control as xc
    xc.CACHE_FILE = os.path.join(tmp, "cache.json")
    import xcomfort_mqtt_bridge as bro
    bro.OPTS.update(options)
    xc.CONTROLLER_IP = "127.0.0.1"
    xc.CONTROLLER_PORT = fake.port
    bro.POSITION_FILE = os.path.join(tmp, "pos.json")
    bro.EXTRA_FILE = os.path.join(tmp, "ekstra.json")
    bro.SCANNER = bro.scanner.Scanner(os.path.join(tmp, "objekter.json"))
    bro.IDMAP.path = os.path.join(tmp, "id_map.json")
    bro.IDMAP.current = {}
    bro.USB_MAP_FILE = os.path.join(tmp, "usb_kart.json")
    bro.USB_MAP = {}
    bro.STICK = None
    bro.MARKISER_CTRL = bro.MarkiseController()
    for m in bro.MARKISER_CTRL.markiser.values():
        m.position = 0.0
    return bro, xc


class FakeClient:
    def __init__(self):
        self.published = []
        self.lock = threading.Lock()

    def publish(self, topic, payload, qos=0, retain=False):
        with self.lock:
            self.published.append((topic, payload))

    def last(self, topic):
        with self.lock:
            vals = [p for t, p in self.published if t == topic]
        return vals[-1] if vals else None


class TestProtokoll(unittest.TestCase):
    def test_handshake_mangler_gir_feil(self):
        fake = FakeHM(handshake=False)
        try:
            bro, xc = load_bridge(fake)
            t0 = time.time()
            with self.assertRaises(ConnectionError):
                xc.XComfortConn(timeout=0.5)
            self.assertLess(time.time() - t0, 2)
        finally:
            fake.close()

    def test_open_hm_proever_flere_ganger(self):
        fake = FakeHM(handshake=False)
        try:
            bro, xc = load_bridge(fake)
            orig = xc.XComfortConn.__init__

            def fast_init(self, ip=None, port=None, timeout=0.3):
                orig(self, ip, port, timeout)
            xc.XComfortConn.__init__ = fast_init
            with self.assertRaises(ConnectionError):
                bro.open_hm("test", attempts=2)
            self.assertEqual(fake.connections, 2)
            self.assertFalse(bro.STATS.hm_ok)
        finally:
            fake.close()


class TestBolk(unittest.TestCase):
    def test_siste_kommando_per_enhet_vinner(self):
        fake = FakeHM()
        try:
            bro, xc = load_bridge(fake)
            batch = [("light", 1, b'{"state":"ON","brightness":10}'),
                     ("light", 2, b'{"state":"ON"}'),
                     ("light", 1, b'{"state":"ON","brightness":80}')]
            out = bro.dedupe_last_wins(batch)
            self.assertEqual([(k, s) for k, s, _ in out], [("light", 1), ("light", 2)])
            self.assertIn(b"80", out[0][2])
        finally:
            fake.close()

    def test_bolk_sender_set_value_foer_new_state_paa_en_forbindelse(self):
        fake = FakeHM()
        try:
            bro, xc = load_bridge(fake)
            xc.save_cache({"10": {"name": "D_A", "val_id": 110, "set_id": 210},
                           "20": {"name": "D_B", "val_id": 120, "set_id": 220}})
            client = FakeClient()
            bro._process_command_batch(client, [("light", 10, b'{"state":"ON","brightness":40}'),
                                                ("light", 20, b'{"state":"OFF"}')])
            cmds = [c for _, c in fake.commands()]
            self.assertEqual(cmds, ["set_value 110 40", "set_value 120 0", "new_state 210 0", "new_state 220 0"])
            self.assertEqual(fake.connections, 1)
            self.assertEqual(json.loads(client.last("xcomfort/light/10/state")), {"state": "ON", "brightness": 40})
        finally:
            fake.close()

    def test_bolk_uten_home_manager_teller_feil(self):
        fake = FakeHM()
        fake.close()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # ingen lytter paa denne porten -> "connection refused"
        bro, xc = load_bridge(fake)
        xc.CONTROLLER_PORT = port
        before = bro.STATS.failed
        bro._process_command_batch(FakeClient(), [("climate", 5, b"21")])
        self.assertEqual(bro.STATS.failed, before + 1)


class TestMarkiseKoe(unittest.TestCase):
    def run_ctrl(self, bro, client, seconds):
        t = threading.Thread(target=bro.MARKISER_CTRL.run, args=(client,), daemon=True)
        t.start()
        time.sleep(seconds)

    def test_pause_mellom_telegrammer_og_dobbel_stopp(self):
        fake = FakeHM()
        try:
            bro, xc = load_bridge(fake, awning_command_gap=0.3, awning_double_stop=True)
            client = FakeClient()
            for sid in (124354, 65125, 50980):
                bro.MARKISER_CTRL.enqueue(sid, "inn")
            for sid in (124354, 65125, 50980):
                bro.MARKISER_CTRL.enqueue(sid, "stopp")
            self.run_ctrl(bro, client, 4.5)
            cmds = fake.commands("new_state")
            ids = [int(c.split()[1]) for _, c in cmds]
            # inn = +61, stopp = +126; stopp sendes to ganger per markise
            self.assertEqual(ids[:3], [124354 + 61, 65125 + 61, 50980 + 61])
            self.assertEqual(sorted(ids[3:]), sorted([124354 + 126, 65125 + 126, 50980 + 126] * 2))
            gaps = [b[0] - a[0] for a, b in zip(cmds, cmds[1:])]
            self.assertGreaterEqual(min(gaps), 0.28, gaps)
            self.assertEqual(bro.STATS.markise[65125]["sendt"], 3)
            self.assertEqual(fake.connections, 1)  # en forbindelse for hele sekvensen
        finally:
            fake.close()

    def test_markise_venter_ikke_paa_mqtt_traaden(self):
        fake = FakeHM()
        try:
            bro, xc = load_bridge(fake)
            client = FakeClient()
            with bro.conn_lock:   # som om pollingen holder laasen
                t0 = time.time()
                bro.handle_cover_command(client, 65125, b"OPEN")
                self.assertLess(time.time() - t0, 0.05)
        finally:
            fake.close()

    def test_markise_sender_selv_om_pollingen_holder_laasen(self):
        fake = FakeHM()
        try:
            bro, xc = load_bridge(fake, awning_command_gap=0.0)
            client = FakeClient()
            bro.MARKISER_CTRL.enqueue(65125, "stopp")
            with bro.conn_lock:   # pollingen/lys-bolken holder laasen
                self.run_ctrl(bro, client, 0.8)
                self.assertIn("new_state 65251 0", [c for _, c in fake.commands()])
        finally:
            fake.close()

    def test_set_position_kjoerer_og_stopper_selv(self):
        fake = FakeHM()
        try:
            bro, xc = load_bridge(fake, awning_command_gap=0.0, awning_double_stop=False)
            m = bro.MARKISER_CTRL.markiser[50980]
            m.runtime = 2.0
            client = FakeClient()
            bro.MARKISER_CTRL.enqueue(50980, "set_position", 50)
            self.run_ctrl(bro, client, 2.0)
            ids = [int(c.split()[1]) for _, c in fake.commands("new_state")]
            self.assertEqual(ids, [50980 + 191, 50980 + 126])
            self.assertAlmostEqual(m.position, 50, delta=8)
            self.assertEqual(client.last("xcomfort/cover/50980/state"), "stopped")
        finally:
            fake.close()


class TestMarkiseRekkefoelge(unittest.TestCase):
    def run_ctrl(self, bro, client, seconds):
        threading.Thread(target=bro.MARKISER_CTRL.run, args=(client,), daemon=True).start()
        time.sleep(seconds)

    def test_ny_kommando_avbryter_ekstra_stopp(self):
        """Gjenskaper feilen 17.10 kl 19:36: 'stopp' og rett etter 'inn' til
        boblebad. Den ekstra stopp-en maa IKKE komme etter 'inn'."""
        fake = FakeHM()
        try:
            bro, xc = load_bridge(fake, awning_command_gap=0.3, awning_double_stop=True)
            bro.MARKISER_CTRL.markiser[65125].position = 98.0
            bro.MARKISER_CTRL.enqueue(65125, "stopp")
            bro.MARKISER_CTRL.enqueue(65125, "inn")
            self.run_ctrl(bro, FakeClient(), 2.0)
            ids = [int(c.split()[1]) for _, c in fake.commands("new_state")]
            self.assertEqual(ids, [65125 + 126, 65125 + 61])
            self.assertEqual(bro.MARKISER_CTRL.markiser[65125].moving, "inn")
        finally:
            fake.close()


class TestPosisjon(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHM()
        self.bro, _ = load_bridge(self.fake)

    def tearDown(self):
        self.fake.close()

    def test_anslag_underveis_og_endestopp(self):
        m = self.bro.Markise(1, "Test", 20.0)
        m.start("ut", now=100.0)
        self.assertAlmostEqual(m.current_position(110.0), 50.0)
        self.assertEqual(m.current_position(150.0), 100.0)
        m.stop(now=105.0)
        self.assertAlmostEqual(m.position, 25.0)
        m.start("inn", now=200.0)
        self.assertEqual(m.current_position(260.0), 0.0)

    def test_plan_set_position(self):
        m = self.bro.Markise(1, "Test", 20.0)
        m.position = 20.0
        self.assertEqual(self.bro.plan_set_position(m, 70, now=0), ("ut", 10.0))
        self.assertEqual(self.bro.plan_set_position(m, 21, now=0), None)
        d, secs = self.bro.plan_set_position(m, 0, now=0)
        self.assertEqual(d, "inn")
        self.assertAlmostEqual(secs, 4.0 + self.bro.ENDESTOPP_MARGIN_S)


# ---------------------------------------------------------------- 2.1 --

class TestLytter(unittest.TestCase):
    def test_parse_frame(self):
        import hm_link
        self.assertEqual(hm_link.parse_frame("\x07oid abc RSN 65125 D_Test 21 1 2 2"),
                         ("obj", 65125, "D_Test", ["21", "1", "2", "2"]))
        self.assertEqual(hm_link.parse_frame("SSN 12 M_D_X_Val 23 1 0 55.5"),
                         ("obj", 12, "M_D_X_Val", ["23", "1", "0", "55.5"]))
        self.assertEqual(hm_link.parse_frame("x_bm_st ACK_DIRECT seq=3")[:2], ("ack", "ACK_DIRECT"))
        self.assertEqual(hm_link.parse_frame("PANEL_BRIGHTNESS 70")[0], "annet")
        self.assertIsNone(hm_link.parse_frame("\x00"))

    def test_feed_deler_rammer_ogsaa_over_flere_pakker(self):
        import hm_link
        got, acks = [], []
        link = hm_link.HMLink(lambda o, n, f: got.append((o, n, f)), lambda k: acks.append(k))
        link.feed(b"\x01SSN 1 A 23 1 0 5\x02\x00\x01SSN 2 B")
        self.assertEqual([g[0] for g in got], [1])
        link.feed(b" 23 1 0 7\x02\x01x_bm_st ACK_DIRECT seq=9\x02")
        self.assertEqual([g[0] for g in got], [1, 2])
        self.assertEqual(acks, ["ACK_DIRECT"])
        self.assertEqual(link.frames, 3)

    def test_lytter_mot_falsk_home_manager(self):
        """Lytte-forbindelsen kobler til, abonnerer, og gir svarene videre."""
        fake = FakeHM(values={4242: 37})
        try:
            bro, xc = load_bridge(fake)
            import hm_link
            got = []
            link = hm_link.HMLink(lambda o, n, f: got.append((o, f)))
            link.subscribe([4242])
            threading.Thread(target=link.run, daemon=True).start()
            deadline = time.time() + 3
            while time.time() < deadline and not got:
                time.sleep(0.05)
            link.stopping = True
            self.assertTrue(link.connected or got)
            self.assertEqual(got[0], (4242, ["23", "1", "0", "37"]))
            self.assertTrue(link.recently_subscribed(4242))
        finally:
            fake.close()


class TestRegister(unittest.TestCase):
    def setUp(self):
        self.fake = FakeHM()
        self.bro, self.xc = load_bridge(self.fake)
        self.client = FakeClient()
        self.bro.MQTT_STATE["client"] = self.client
        zone, name, smux = self.xc.LIGHTS[0]
        self.smux = smux
        self.xc.save_cache({str(smux): {"name": "D_Test", "val_id": 900001, "set_id": 900002}})
        self.bro.rebuild_registry()

    def tearDown(self):
        self.fake.close()

    def test_val_gir_lys_tilstand(self):
        self.bro.on_hm_object(900001, "M_D_Test_Val", ["23", "1", "0", "42.4"])
        st = json.loads(self.client.last(self.bro.light_state_topic(self.smux)))
        self.assertEqual(st["state"], "ON")
        self.assertEqual(st["brightness"], 42)
        self.bro.on_hm_object(900001, "M_D_Test_Val", ["23", "1", "0", "0"])
        st = json.loads(self.client.last(self.bro.light_state_topic(self.smux)))
        self.assertEqual(st["state"], "OFF")

    def test_responstid_maales(self):
        self.bro.PENDING_CONFIRM[self.smux] = (60, time.time() - 1.5)
        self.bro.on_hm_object(self.smux, "D_Test", ["21", "1", "60", "0"])
        self.assertNotIn(self.smux, self.bro.PENDING_CONFIRM)
        self.assertEqual(self.bro.STATS.bekreftet, 1)
        self.assertGreater(self.bro.STATS.responstider[-1]["sek"], 1.4)

    def test_knapp_gir_hendelse_men_ikke_rett_etter_abonnement(self):
        import hm_link
        oid = self.bro.BUTTONS[0][2]
        self.bro.LINK = hm_link.HMLink(lambda *a: None)
        self.bro.LINK.subscribed_at[oid] = time.time()
        self.bro.on_hm_object(oid, "IN_Test", ["11", "1", "2"])
        self.assertIsNone(self.client.last(self.bro.event_topic(oid)))
        self.bro.LINK.subscribed_at[oid] = 0
        self.bro.on_hm_object(oid, "IN_Test", ["11", "1", "2"])
        ev = json.loads(self.client.last(self.bro.event_topic(oid)))
        self.assertEqual(ev, {"event_type": "long_up", "code": 2})
        self.bro.LINK = None

    def test_unique_id_er_uendret(self):
        """unique_id-ene til markisene maa aldri endres."""
        self.bro.publish_discovery(self.client)
        cfg = json.loads(self.client.last("homeassistant/cover/xcomfort_65125/config"))
        self.assertEqual(cfg["unique_id"], "xcomfort_cover_65125")


class TestSkanner(unittest.TestCase):
    def test_kandidater(self):
        import scanner
        objs = {
            10: ("D_Kjellerlys", 21), 11: ("M_D_Kjellerlys_Val", 23), 12: ("B_D_Kjellerlys_SET", 23),
            20: ("D_Ukomplett", 21),
            30: ("R_Pumpe", 8), 31: ("b_Pumpe_on", 9), 32: ("b_Pumpe_off", 9),
            40: ("IN_BRStue", 11),
            50: ("D_Kjent", 21), 51: ("M_D_Kjent_Val", 23), 52: ("B_D_Kjent_SET", 23),
        }
        c = scanner.candidates(objs, known_ids={50})
        self.assertEqual([x["id"] for x in c["lys"]], [10])
        self.assertEqual(c["lys"][0]["val_id"], 11)
        self.assertEqual(c["brytere"][0]["on_id"], 31)
        self.assertEqual(c["brytere"][0]["off_id"], 32)
        self.assertEqual(c["knapper"][0]["navn"], "BRStue")
        self.assertEqual(scanner.pretty("DownlightKjellerstue"), "Downlight Kjellerstue")


# Anonymisert utdrag av et ekte anlegg: samme typenumre, avstander og kuttede
# navn som Home Manager faktisk sender, men med nøytrale navn.
EKTE_OBJEKTER = {
    # Dimmer, avstand +171/+219
    1000: ("D_TaklampeKjokken", 21), 1171: ("M_D_TaklampeKjokken_Val", 23), 1219: ("B_D_TaklampeKjokken_SET", 6),
    # Dimmer, avstand +118/+166
    2000: ("D_DownlightHall", 21), 2118: ("M_D_DownlightHall_Val", 23), 2166: ("B_D_DownlightHall_SET", 6),
    # Dimmer med langt navn: Home Manager kutter styre-objektene (29 tegn)
    3000: ("D_TaklampeLysthusLysthusXY", 21), 3171: ("M_D_TaklampeLysthusLysthusX", 23),
    3219: ("B_D_TaklampeLysthusLysthusX", 6), 3220: ("B_D_TaklampeLysthusLysthusX", 6),
    # Markise: samme type som dimmer, men egne Open/Stop/Close/Up-objekter
    4000: ("D_MarkiseTerrasseUte", 21), 4061: ("B_MarkiseTerrasse_Open", 6),
    4126: ("B_MarkiseTerrasse_Stop", 6), 4191: ("B_MarkiseTerrasse_Close", 6), 4256: ("B_MarkiseTerrasse_Up", 6),
    # Av/på: R_ med B_R_<navn>_ON/OFF på +209/+285
    5000: ("R_SpeilBad", 8), 5209: ("B_R_SpeilBad_ON", 6), 5285: ("B_R_SpeilBad_OFF", 6),
    # Av/på: R_ med kuttede navn
    6000: ("R_GardsplasslyskasterUtelys", 8), 6209: ("B_R_GardsplasslyskasterUtely", 6),
    6285: ("B_R_GardsplasslyskasterUtely", 6),
    # Av/på: TS_<navn><sone> med B_<sone>_<navn>_On/Off på +71/+168
    7000: ("TS_TerassevarmerUte", 8), 7071: ("B_Ute_Terassevarmer_On", 6), 7168: ("B_Ute_Terassevarmer_Off", 6),
    # Internt relé uten av/på-objekter: skal IKKE foreslås
    8000: ("R_DummyStue", 8),
    # Termostat, inngang, sensorer
    9000: ("TH_Stue037", 7), 9100: ("IN_BRStue", 11),
    9200: ("TS_Solfler", 10), 9300: ("TS_Ute", 10), 9400: ("S_TaklampeStueTemp", 10),
    # Støy: interne objekter som ikke er enheter
    9500: ("TS_LightStue", 2), 9600: ("T_SafeCheck1", 4), 9700: ("D_SafeOffStue037", 13),
}


class TestSkannerEkteAnlegg(unittest.TestCase):
    def test_finner_alle_typer_riktig(self):
        import scanner
        c = scanner.candidates(EKTE_OBJEKTER, known_ids=set())
        lys = {x["id"]: (x["val_id"], x["set_id"]) for x in c["lys"]}
        self.assertEqual(lys, {1000: (1171, 1219), 2000: (2118, 2166), 3000: (3171, 3219)})
        self.assertEqual([(x["id"], x["open_id"], x["stop_id"], x["close_id"]) for x in c["markiser"]],
                         [(4000, 4061, 4126, 4191)])
        brytere = {x["id"]: (x["on_id"], x["off_id"]) for x in c["brytere"]}
        self.assertEqual(brytere, {5000: (5209, 5285), 6000: (6209, 6285), 7000: (7071, 7168)})
        self.assertEqual([(x["id"], x["navn"], x["sone"]) for x in c["termostater"]], [(9000, "Stue", "Stue")])
        self.assertEqual({x["id"]: x["enhet"] for x in c["sensorer"]}, {9200: "lux", 9300: "°C", 9400: "°C"})
        self.assertEqual([x["id"] for x in c["knapper"]], [9100])

    def test_kjente_enheter_foreslas_ikke(self):
        import scanner
        c = scanner.candidates(EKTE_OBJEKTER, known_ids={1000, 4000, 9000})
        self.assertNotIn(1000, [x["id"] for x in c["lys"]])
        self.assertEqual(c["markiser"], [])
        self.assertEqual(c["termostater"], [])

    def test_markise_med_kuttede_navn_finnes_paa_avstand(self):
        import scanner
        objs = {10: ("D_MarkiseVeldigLangtNavnUte", 21), 71: ("B_MarkiseVeldigLangtNavnU", 6),
                136: ("B_MarkiseVeldigLangtNavnU", 6), 201: ("B_MarkiseVeldigLangtNavnU", 6)}
        c = scanner.candidates(objs, known_ids=set())
        self.assertEqual([(x["open_id"], x["stop_id"], x["close_id"]) for x in c["markiser"]], [(71, 136, 201)])


class TestNyeEnhetstyper(unittest.TestCase):
    """Markiser, termostater, sensorer og av/på-sensorer lagt til fra nettsiden."""

    def _bridge(self):
        fake = FakeHM(ack_ids=())
        bro, xc = load_bridge(fake)
        bro.clear_builtin_devices()
        bro.SCANNER.objects = dict(EKTE_OBJEKTER)
        client = FakeClient()
        bro.MQTT_STATE["client"] = client
        return fake, bro, xc, client

    def test_markise_legges_til_og_styres_med_riktige_objekter(self):
        fake, bro, xc, client = self._bridge()
        try:
            api = bro.Api()
            r = api.add_extra({"markiser": [{"id": 4000, "navn": "Terrasse", "sone": "Ute", "runtime": 22}]})
            self.assertEqual(r["lagt_til"], ["Terrasse"])
            self.assertEqual(xc.MARKISER, [("Ute", "Terrasse", 4000)])
            m = bro.MARKISER_CTRL.markiser[4000]
            self.assertEqual((m.runtime, m.button_id("inn"), m.button_id("stopp"), m.button_id("ut")),
                             (22.0, 4061, 4126, 4191))
            cfg = json.loads(client.last("homeassistant/cover/xcomfort_4000/config"))
            self.assertEqual(cfg["device_class"], "awning")
            # Motsatt retning og ny kjøretid fra blyanten på nettsiden
            self.assertTrue(api.update_extra({"id": 4000, "runtime": 40, "reverse": True})["ok"])
            m = bro.MARKISER_CTRL.markiser[4000]
            self.assertEqual((m.runtime, m.button_id("ut"), m.button_id("inn")), (40.0, 4061, 4191))
            dev = next(d for d in api.devices() if d["id"] == 4000)
            self.assertEqual((dev["art"], dev["runtime"], dev["reverse"]), ("markiser", 40.0, True))
            # Kommandoen går til riktig objekt
            bro.MARKISER_CTRL._send(m, "stopp", "test")
            self.assertIn("new_state 4126 0", [c for _, c in fake.commands("new_state")])
            self.assertTrue(api.remove_extra({"id": 4000})["ok"])
            self.assertEqual(client.last("homeassistant/cover/xcomfort_4000/config"), "")
            self.assertNotIn(4000, bro.MARKISER_CTRL.markiser)
        finally:
            bro.MQTT_STATE["client"] = None
            fake.close()

    def test_termostat_sensor_og_av_paa_sensor(self):
        fake, bro, xc, client = self._bridge()
        try:
            api = bro.Api()
            r = api.add_extra({"termostater": [{"id": 9000, "navn": "Stue", "sone": "Stue"}],
                               "sensorer": [{"id": 9200, "navn": "Sol", "sone": "Ute", "enhet": "lux"}],
                               "knapper": [{"id": 9100, "navn": "Alarm", "sone": "Stue", "som": "sensor"}]})
            self.assertEqual(sorted(r["lagt_til"]), ["Alarm", "Sol", "Stue"])
            self.assertEqual(xc.THERMOSTATS, [("Stue", "Stue", 9000)])
            self.assertIn(("Ute", "Sol", 9200, "lux"), bro.CONTINUOUS_SENSORS)
            self.assertIn(("Stue", "Alarm", 9100, ""), bro.BINARY_SENSORS)
            self.assertEqual(bro.BUTTONS, [])          # inngangen ble sensor, ikke knapp
            self.assertTrue(client.last("homeassistant/climate/xcomfort_9000/config"))
            sensor = json.loads(client.last("homeassistant/sensor/xcomfort_9200/config"))
            self.assertEqual((sensor["device_class"], sensor["unit_of_measurement"]), ("illuminance", "lx"))
            alarm = json.loads(client.last("homeassistant/binary_sensor/xcomfort_9100/config"))
            self.assertNotIn("entity_category", alarm)
            ids = bro.rebuild_registry()
            self.assertTrue({9000, 9200} <= ids)
            # Endre enhet, så fjerne
            self.assertTrue(api.update_extra({"id": 9200, "enhet": "°C"})["ok"])
            self.assertIn(("Ute", "Sol", 9200, "°C"), bro.CONTINUOUS_SENSORS)
            api.update_extra({"id": 9200, "enhet": "parsec"})          # ugyldig enhet ignoreres
            self.assertIn(("Ute", "Sol", 9200, "°C"), bro.CONTINUOUS_SENSORS)
            for oid, topic in ((9000, "climate/xcomfort_9000"), (9200, "sensor/xcomfort_9200"),
                               (9100, "binary_sensor/xcomfort_9100")):
                self.assertTrue(api.remove_extra({"id": oid})["ok"])
                self.assertEqual(client.last(f"homeassistant/{topic}/config"), "")
            self.assertEqual((xc.THERMOSTATS, bro.BINARY_SENSORS), ([], []))
        finally:
            bro.MQTT_STATE["client"] = None
            fake.close()

    def test_lagt_til_foreslas_ikke_igjen_og_overlever_omstart(self):
        fake, bro, xc, client = self._bridge()
        try:
            api = bro.Api()
            api.add_extra({"markiser": [4000], "lys": [3000]})
            cands = api.scan_info()["kandidater"]
            self.assertEqual(cands["markiser"], [])
            self.assertNotIn(3000, [x["id"] for x in cands["lys"]])
            self.assertEqual(api.add_extra({"markiser": [4000]})["lagt_til"], [])   # ikke dobbelt
            # «Omstart»: tøm listene og les fila på nytt
            bro.clear_builtin_devices()
            bro.load_extras()
            self.assertEqual([x[2] for x in xc.MARKISER], [4000])
            self.assertEqual(xc.load_cache()["3000"], {"name": "D_TaklampeLysthusLysthusXY",
                                                       "val_id": 3171, "set_id": 3219})
        finally:
            bro.MQTT_STATE["client"] = None
            fake.close()

    def test_reset_knappen_bare_for_det_opprinnelige_anlegget(self):
        fake, bro, xc, client = self._bridge()
        try:
            bro.Api().add_extra({"markiser": [4000]})
            bro.publish_button_discovery(client)
            self.assertIsNone(client.last("homeassistant/button/xcomfort_balkong_reset/config"))
        finally:
            bro.MQTT_STATE["client"] = None
            fake.close()


class TestSkannerFortsetter(unittest.TestCase):
    def test_finner_objekter_forbi_max_id(self):
        """Nye objekter fra MRF ligger over max_id -- skal likevel bli funnet."""
        fake = FakeHM(idle_timeout=30, exists={5, 1500, 2600, 4100})
        try:
            bro, xc = load_bridge(fake)
            import scanner
            old = (scanner.WAIT_S, scanner.EMPTY_GAP)
            scanner.WAIT_S, scanner.EMPTY_GAP = 0.02, 1500
            try:
                sc = scanner.Scanner(os.path.join(tempfile.mkdtemp(), "o.json"))
                sc._run(2000)
            finally:
                scanner.WAIT_S, scanner.EMPTY_GAP = old
            self.assertEqual(sorted(sc.objects), [5, 1500, 2600, 4100])
            self.assertIsNone(sc.error)
        finally:
            fake.close()


class TestNyeNumre(unittest.TestCase):
    """xSetup kan nummerere om innganger (18.10.2026: Tett filter 231939 ->
    232053). Broen skal finne dem igjen på navn, uten å endre noe i HA."""

    def test_finner_nytt_nummer_og_publiserer_paa_samme_topic(self):
        names = {232053: "IN_Ventilasjon_Tettfilter", 234899: "IN_Ventilasjon_Drift",
                 235013: "IN_Ventilasjon_Drift_x", 232677: "IN_Ventilasjon_Overoppheting",
                 232304: "IN_Ute_BRMarkise1", 234213: "IN_Ute_BRMarkise2", 233840: "IN_Ute_BRMarkise3",
                 231939: "IN_Noe_Annet"}
        fake = FakeHM(idle_timeout=30, exists=set(names), names=names)
        try:
            bro, xc = load_bridge(fake)
            import idmap
            old = idmap.WINDOW
            idmap.WINDOW, idmap.WAIT_S = 500, 0.02
            try:
                changes = bro.IDMAP.verify({})
            finally:
                idmap.WINDOW, idmap.WAIT_S = old, 0.25
            self.assertEqual([(c[0], c[3]) for c in changes], [(231939, 232053)])
            self.assertEqual(bro.IDMAP.cur(231939), 232053)
            self.assertEqual(bro.IDMAP.cur(234899), 234899)
            ids = bro.rebuild_registry()
            self.assertIn(232053, ids)
            self.assertNotIn(231939, ids)
            client = FakeClient()
            bro.MQTT_STATE["client"] = client
            bro.on_hm_object(232053, "IN_Ventilasjon_Tettfilter", ["11", "1", "1", "0"])
            self.assertEqual(client.last(bro.binary_sensor_state_topic(231939)), "ON")
            # Det GAMLE nummeret er nå et annet objekt og skal ikke påvirke sensoren
            bro.on_hm_object(231939, "IN_Noe_Annet", ["11", "1", "0", "0"])
            self.assertEqual(client.last(bro.binary_sensor_state_topic(231939)), "ON")
            # Lagret til neste oppstart
            again = __import__("idmap").IdMap(bro.IDMAP.path)
            self.assertEqual(again.cur(231939), 232053)
        finally:
            fake.close()

    def test_bruker_siste_fulle_sok_foerst(self):
        names = {232053: "IN_Ventilasjon_Tettfilter", 234899: "IN_Ventilasjon_Drift",
                 232677: "IN_Ventilasjon_Overoppheting", 232304: "IN_Ute_BRMarkise1",
                 234213: "IN_Ute_BRMarkise2", 233840: "IN_Ute_BRMarkise3"}
        fake = FakeHM(idle_timeout=30, exists=set(names), names=names)
        try:
            bro, xc = load_bridge(fake)
            t0 = time.time()
            changes = bro.IDMAP.verify({232053: ("IN_Ventilasjon_Tettfilter", 11)})
            self.assertLess(time.time() - t0, 5)
            self.assertEqual(bro.IDMAP.cur(231939), 232053)
            self.assertEqual(len(changes), 1)
        finally:
            fake.close()


class TestSonebytte(unittest.TestCase):
    def test_finner_knapp_etter_sonebytte_i_xsetup(self):
        """18.10.2026: sone Hus -> Stue ga IN_Hus_BRstue1 (231174) nytt navn
        IN_Stue_BRstue1 og nytt nummer 230636."""
        names = {230636: "IN_Stue_BRstue1", 230841: "M_InSt_Stue_BRstue1", 231174: "IN_Stue_BRstue4",
                 234899: "IN_Ventilasjon_Drift", 231939: "IN_Ventilasjon_Tettfilter",
                 232677: "IN_Ventilasjon_Overoppheting", 232304: "IN_Ute_BRMarkise1",
                 234213: "IN_Ute_BRMarkise2", 233840: "IN_Ute_BRMarkise3"}
        fake = FakeHM(idle_timeout=30, exists=set(names), names=names)
        try:
            bro, xc = load_bridge(fake)
            bro.IDMAP.expect(231174, "IN_Hus_BRstue1")
            scanned = {i: (n, 11) for i, n in names.items()}
            changes = bro.IDMAP.verify(scanned)
            self.assertEqual(bro.IDMAP.cur(231174), 230636)
            self.assertEqual([c[3] for c in changes], [230636])
            self.assertEqual(bro.IDMAP.expected[231174], "IN_Stue_BRstue1")
        finally:
            fake.close()

    def test_navnenokkel(self):
        import idmap
        self.assertEqual(idmap.name_key("IN_Hus_BRstue1"), idmap.name_key("IN_Stue_BRstue1"))
        self.assertNotEqual(idmap.name_key("IN_Stue_BRstue1"), idmap.name_key("M_InSt_Stue_BRstue1"))
        self.assertEqual(idmap.name_key("IN_BRhalluvenstre"), ("IN", "BRhalluvenstre"))


class TestKnappNavn(unittest.TestCase):
    def test_knapp_far_rom_og_kort_navn(self):
        fake = FakeHM()
        try:
            bro, xc = load_bridge(fake)
            bro.BUTTONS.append(("Knapper", "Hus BRstue1", 231174))
            bro.IDMAP.expect(231174, "IN_Stue_BRstue1")
            client = FakeClient()
            bro.publish_event_discovery(client)
            cfg = json.loads(client.last("homeassistant/event/xcomfort_in_231174/config"))
            self.assertEqual(cfg["name"], "BRstue1")
            self.assertEqual(cfg["device"]["name"], "Stue")
            self.assertEqual(cfg["unique_id"], "xcomfort_event_231174")
            cfg = json.loads(client.last("homeassistant/event/xcomfort_in_232304/config"))
            self.assertEqual(cfg["name"], "Markiseknapp 1")
        finally:
            fake.close()


class TestNettside(unittest.TestCase):
    def test_api_svarer(self):
        import urllib.request
        os.environ["XCOMFORT_WEB_OPEN"] = "1"
        fake = FakeHM()
        try:
            bro, xc = load_bridge(fake)
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            server = bro.web.start(bro.Api(), port)
            base = f"http://127.0.0.1:{port}"
            st = json.loads(urllib.request.urlopen(base + "/api/status", timeout=3).read())
            self.assertEqual(st["versjon"], bro.BRIDGE_VERSION)
            devs = json.loads(urllib.request.urlopen(base + "/api/devices", timeout=3).read())
            self.assertTrue(any(d["type"] == "markise" for d in devs))
            html = urllib.request.urlopen(base + "/", timeout=3).read()
            self.assertIn(b"xComfort", html)
            bro.rebuild_registry()
            bro.SCANNER.objects = {777001: ("IN_BRstuekanal1Stue", 11), 777002: ("DummyFlagg", 23),
                                   65125: ("D_MarkiseBoblebad", 21)}
            alle = json.loads(urllib.request.urlopen(base + "/api/scan/all", timeout=3).read())
            self.assertEqual(len(alle), 3)
            self.assertTrue(next(o for o in alle if o["id"] == 65125)["kjent"])
            req = urllib.request.Request(base + "/api/extra", data=json.dumps({"knapper": [777002]}).encode(),
                                         method="POST")
            svar = json.loads(urllib.request.urlopen(req, timeout=3).read())
            self.assertEqual(len(svar["lagt_til"]), 1)
            self.assertIn(777002, bro.REG["button_ids"])
            bro.RECENT_UNKNOWN[888001] = ("IN_Stue_BRstue2", time.time())
            info = json.loads(urllib.request.urlopen(base + "/api/scan", timeout=3).read())
            self.assertEqual([x["id"] for x in info["nylig"]], [888001])
            req = urllib.request.Request(base + "/api/extra", data=json.dumps({"knapper": [888001]}).encode(),
                                         method="POST")
            self.assertEqual(len(json.loads(urllib.request.urlopen(req, timeout=3).read())["lagt_til"]), 1)
            self.assertIn(888001, bro.REG["button_ids"])
            info = json.loads(urllib.request.urlopen(base + "/api/scan", timeout=3).read())
            self.assertEqual(info["nylig"], [])
            bro.web.EVENTS.add("Stue BRstue1: kort opp", "knapp")
            ev = json.loads(urllib.request.urlopen(base + "/api/events", timeout=3).read())
            self.assertEqual(ev[-1]["l"], "knapp")
            self.assertIn("ts", ev[-1])
            devs = json.loads(urllib.request.urlopen(base + "/api/devices", timeout=3).read())
            self.assertTrue(any(d["type"] == "termostat" for d in devs))
            self.assertTrue(any(d["type"] == "knapp" for d in devs))
            req = urllib.request.Request(base + "/api/extra/remove", data=json.dumps({"id": 888001}).encode(),
                                         method="POST")
            self.assertTrue(json.loads(urllib.request.urlopen(req, timeout=3).read())["ok"])
            self.assertNotIn(888001, bro.REG["button_ids"])
            bro.log.info("hei fra testen")
            server.shutdown()
            server.server_close()
        finally:
            fake.close()
            os.environ.pop("XCOMFORT_WEB_OPEN", None)


class TestInnstillinger(unittest.TestCase):
    def test_gamle_norske_innstillinger_leses(self):
        fake = FakeHM()
        try:
            bro, _xc = load_bridge(fake)
            path = os.path.join(os.path.dirname(bro.EXTRA_FILE), "options.json")
            with open(path, "w") as f:
                json.dump({"xcomfort_ip": "1.2.3.4", "lysstyring": "pinne", "hurtig_status": False,
                           "markise_kjoretid_balkong": 18}, f)
            bro.OPTIONS_FILE = path
            o = bro.load_options()
            self.assertEqual((o["light_control"], o["instant_status"], o["awning_runtime_balcony"]),
                             ("usb_stick", False, 18.0))
        finally:
            fake.close()

    def test_tom_ip_stopper_med_tydelig_feil(self):
        fake = FakeHM()
        try:
            bro, _xc = load_bridge(fake, xcomfort_ip="")
            with self.assertLogs("xcomfort.bridge", level="ERROR") as cm, self.assertRaises(SystemExit):
                bro.main()
            self.assertIn("IP is not set", "\n".join(cm.output))
        finally:
            fake.close()


class TestHurtigvei(unittest.TestCase):
    def _start_link(self, bro):
        import hm_link
        link = hm_link.HMLink(lambda o, n, f: None)
        threading.Thread(target=link.run, daemon=True).start()
        deadline = time.time() + 3
        while time.time() < deadline and not link.connected:
            time.sleep(0.02)
        self.assertTrue(link.connected)
        bro.LINK = link
        return link

    def test_kjente_lys_sendes_paa_lytte_forbindelsen(self):
        fake = FakeHM(idle_timeout=30)
        try:
            bro, xc = load_bridge(fake)
            xc.save_cache({"10": {"name": "D_A", "val_id": 110, "set_id": 210},
                           "20": {"name": "D_B", "on_id": 301, "off_id": 302}})
            link = self._start_link(bro)
            before = fake.connections
            client = FakeClient()
            bro._process_command_batch(client, [("light", 10, b'{"state":"ON","brightness":40}'),
                                                ("switch", 20, b"OFF")])
            time.sleep(0.2)
            cmds = [c for _, c in fake.commands() if not c.startswith("d_obj")]
            self.assertEqual(cmds, ["set_value 110 40", "new_state 210 0", "new_state 302 0"])
            self.assertEqual(fake.connections, before)  # ingen ny tilkobling
            self.assertEqual(json.loads(client.last("xcomfort/light/10/state")), {"state": "ON", "brightness": 40})
            link.stopping = True
        finally:
            fake.close()

    def test_ukjent_lys_bruker_vanlig_vei(self):
        fake = FakeHM(idle_timeout=30)
        try:
            bro, xc = load_bridge(fake)
            xc.save_cache({"10": {"name": "D_A", "val_id": 110, "set_id": 210}})
            link = self._start_link(bro)
            self.assertIsNone(bro._fast_batch_lines([("light", 99, b'{"state":"ON"}')], xc.load_cache()))
            link.stopping = True
        finally:
            fake.close()


class FakeTransport:
    """Falsk USB-pinne: husker det som skrives og kan kvittere som en lampe."""

    def __init__(self, ack=True):
        self.written = []
        self.ack = ack
        self.inbox = []
        self.fail = False

    def write(self, data):
        if self.fail:
            raise IOError("pinnen ble trukket ut")
        assert len(data) == 62
        self.written.append(bytes(data[:data[1]]))
        if data[2] == 0x1B and self.ack:
            serial = data[15:19]
            self.inbox.append(bytes.fromhex("5a1a0301001221a03f00") + serial + b"\x00\x80\x00\x00\x00"
                              + bytes(6) + b"\xa5")
        if data[2] == 0x44:
            self.inbox.append(b"\x5a\x18\x44CKOZ-00/13ET00000\x00\x30\x0b\xa5")

    def read(self, timeout_ms):
        if self.inbox:
            return self.inbox.pop(0)
        time.sleep(timeout_ms / 1000 / 10)
        return None

    def close(self):
        pass


class TestUsbPinne(unittest.TestCase):
    def test_rammer_er_like_mrf(self):
        import usbstick
        # CRC-16/Kermit, standard testvektor. Rammene under er bygd likt med
        # det MRF sender (serienumrene er oppdiktede, ikke fra et ekte anlegg).
        self.assertEqual(usbstick.crc16_kermit(b"123456789"), 0x2189)
        self.assertEqual(usbstick.rf_frame(0x50, 1234567, b"", 0x12).hex(" "),
                         "5a 17 1b 50 00 13 12 82 07 00 80 00 00 00 00 87 d6 12 00 00 2c ae a5")
        self.assertEqual(usbstick.level_frame(2000001, 10.2, 0x10).hex(" "),
                         "5a 19 1b 5a 00 15 10 82 07 00 80 00 00 00 00 81 84 1e 00 01 00 1a b6 a4 a5")
        self.assertEqual(usbstick.level_frame(1234567, 0, 0x12)[3], 0x51)

    def test_kvittering_tolkes(self):
        import usbstick
        st = usbstick.UsbStick(transport_factory=lambda: None)
        st.feed(bytes.fromhex("5a1a0301001224a03f0087d6120000800000001717059a02fea5"))
        self.assertTrue(st.acked_since(1234567, 0))
        self.assertFalse(st.acked_since(7654321, 0))

    def test_uten_pyusb_eller_pinne_brukes_home_manager(self):
        import usbstick
        st = usbstick.UsbStick(transport_factory=lambda: None)
        self.assertFalse(st.connect())
        self.assertIsNone(st.set_levels([(1, 50)]))

    def _bridge_with_stick(self, fake, transport):
        bro, xc = load_bridge(fake)
        import usbstick
        stick = usbstick.UsbStick(transport_factory=lambda: transport)
        self.assertTrue(stick.connect())
        threading.Thread(target=stick.run, daemon=True).start()
        bro.STICK = stick
        smux = xc.LIGHTS[0][2]
        bro.USB_MAP = {smux: [2000001, 2000002]}
        xc.save_cache({str(smux): {"name": "D_A", "val_id": 110, "set_id": 210}})
        return bro, xc, stick, smux

    def test_lys_sendes_via_pinnen_og_ikke_via_home_manager(self):
        fake = FakeHM()
        tr = FakeTransport()
        try:
            bro, xc, stick, smux = self._bridge_with_stick(fake, tr)
            client = FakeClient()
            bro._process_command_batch(client, [("light", smux, b'{"state":"ON","brightness":40}')])
            rf = [w for w in tr.written if w[2] == 0x1B]
            self.assertEqual(len(rf), 2)
            self.assertEqual({int.from_bytes(w[15:19], "little") for w in rf}, {2000001, 2000002})
            self.assertTrue(all(w[3] == 0x5A and w[21] == 102 for w in rf))  # 40 % = 102 av 255
            self.assertEqual(fake.commands("set_value"), [])
            self.assertEqual(json.loads(client.last(f"xcomfort/light/{smux}/state")),
                             {"state": "ON", "brightness": 40})
            # Kvittert av pinnen = bekreftet, skal ikke telle som ubekreftet etter 15 s
            self.assertNotIn(smux, bro.PENDING_CONFIRM)
            self.assertEqual(bro.USB_STATS["bekreftet"], 1)
            time.sleep(0.3)
            bro._check_usb_acks([(smux, 40, [2000001, 2000002])], time.time() - 1)
            self.assertEqual(bro._pending_commands, [])
            stick.stopping = True
        finally:
            fake.close()

    def test_uten_kvittering_sendes_via_home_manager(self):
        fake = FakeHM()
        tr = FakeTransport(ack=False)
        try:
            bro, xc, stick, smux = self._bridge_with_stick(fake, tr)
            client = FakeClient()
            t0 = time.time()
            bro._process_command_batch(client, [("light", smux, b'{"state":"ON","brightness":40}')])
            bro._check_usb_acks([(smux, 40, [2000001, 2000002], bro.LAST_CMD_AT[smux])], t0)
            self.assertEqual(len(bro._pending_commands), 1)
            batch = bro._pending_commands[:]
            bro._pending_commands.clear()
            bro._process_command_batch(client, batch)
            self.assertEqual([c for _, c in fake.commands("set_value")], ["set_value 110 40"])
            stick.stopping = True
        finally:
            fake.close()

    def test_pinne_trukket_ut_gir_home_manager(self):
        fake = FakeHM()
        tr = FakeTransport()
        try:
            bro, xc, stick, smux = self._bridge_with_stick(fake, tr)
            tr.fail = True
            bro._process_command_batch(FakeClient(), [("light", smux, b'{"state":"OFF"}')])
            self.assertFalse(stick.healthy())
            self.assertEqual([c for _, c in fake.commands("set_value")], ["set_value 110 0"])
            stick.stopping = True
        finally:
            fake.close()

    def test_gamle_verdier_fra_home_manager_ignoreres_en_stund(self):
        fake = FakeHM()
        try:
            bro, xc = load_bridge(fake)
            bro.LAST_USB[5] = (60, time.time())
            self.assertTrue(bro.usb_grace(5, 20))
            self.assertFalse(bro.usb_grace(5, 60))
            bro.LAST_USB[5] = (60, time.time() - 60)
            self.assertFalse(bro.usb_grace(5, 20))
        finally:
            fake.close()

    def test_kart_lastes_opp_og_lagres(self):
        fake = FakeHM()
        try:
            bro, xc = load_bridge(fake)
            smux = xc.LIGHTS[0][2]
            r = bro.Api().usb_map_upload({"lys": {str(smux): {"navn": "x", "serienr": [1, 2]}, "999999999": [3]}})
            self.assertEqual(r["lys_med_kart"], 1)
            self.assertEqual(bro.load_usb_map(), {smux: [1, 2]})
            self.assertEqual(bro.Api().usb()["lys_med_kart"], 1)
        finally:
            fake.close()


class TestLysstyring(TestUsbPinne):
    def test_bare_home_manager_bruker_ikke_pinnen(self):
        fake = FakeHM()
        tr = FakeTransport()
        try:
            bro, xc, stick, smux = self._bridge_with_stick(fake, tr)
            bro.OPTS["light_control"] = "home_manager"
            bro._process_command_batch(FakeClient(), [("light", smux, b'{"state":"ON","brightness":30}')])
            self.assertEqual([w for w in tr.written if w[2] == 0x1B], [])
            self.assertEqual([c for _, c in fake.commands("set_value")], ["set_value 110 30"])
            stick.stopping = True
        finally:
            fake.close()

    def test_bare_pinne_sender_ikke_via_home_manager_uten_kvittering(self):
        fake = FakeHM()
        tr = FakeTransport(ack=False)
        try:
            bro, xc, stick, smux = self._bridge_with_stick(fake, tr)
            bro.OPTS["light_control"] = "usb_stick"
            t0 = time.time()
            bro._process_command_batch(FakeClient(), [("light", smux, b'{"state":"ON","brightness":30}')])
            bro._check_usb_acks([(smux, 30, [2000001, 2000002], bro.LAST_CMD_AT[smux])], t0, fallback=False)
            self.assertEqual(bro._pending_commands, [])
            self.assertEqual(fake.commands("set_value"), [])
            self.assertEqual(bro.USB_STATS["ubekreftet"], 1)
            stick.stopping = True
        finally:
            fake.close()

    def test_bare_pinne_uten_pinne_styrer_ikke_lyset(self):
        fake = FakeHM()
        tr = FakeTransport()
        try:
            bro, xc, stick, smux = self._bridge_with_stick(fake, tr)
            bro.OPTS["light_control"] = "usb_stick"
            tr.fail = True
            before = bro.STATS.failed
            bro._process_command_batch(FakeClient(), [("light", smux, b'{"state":"OFF"}')])
            self.assertEqual(fake.commands("set_value"), [])
            self.assertEqual(bro.STATS.failed, before + 1)
            stick.stopping = True
        finally:
            fake.close()

    def test_stor_bolk_gaar_via_home_manager_i_hybrid(self):
        fake = FakeHM()
        tr = FakeTransport()
        try:
            bro, xc, stick, smux = self._bridge_with_stick(fake, tr)
            bro.USB_MAP = {smux: list(range(1, bro.USB_MAX_DIMMERE_HYBRID + 2))}
            bro._process_command_batch(FakeClient(), [("light", smux, b'{"state":"ON","brightness":30}')])
            self.assertEqual([w for w in tr.written if w[2] == 0x1B], [])
            self.assertEqual([c for _, c in fake.commands("set_value")], ["set_value 110 30"])
            self.assertEqual(bro.USB_STATS["stor_bolk"], 1)
            stick.stopping = True
        finally:
            fake.close()

    def test_gammel_innstilling_respekteres(self):
        fake = FakeHM()
        try:
            bro, xc = load_bridge(fake)
            bro.OPTS.pop("light_control", None)
            bro.OPTS["usb_pinne"] = False
            self.assertEqual(bro.light_mode(), "home_manager")
            bro.OPTS["light_control"] = "usb_stick"
            self.assertEqual(bro.light_mode(), "usb_stick")
        finally:
            fake.close()


class TestUsbSkala(unittest.TestCase):
    def test_prosent_til_radio(self):
        import usbstick
        self.assertEqual(usbstick.percent_to_raw(100), 255)
        self.assertEqual(usbstick.percent_to_raw(50), 128)   # som MRF sendte (0x80)
        self.assertEqual(usbstick.percent_to_raw(1), 3)
        self.assertEqual(usbstick.percent_to_raw(0), 0)
        self.assertEqual(usbstick.raw_to_percent(255), 100)


class TestUsbBekreftHM(TestUsbPinne):
    def test_status_fra_home_manager_godtas_som_bekreftelse(self):
        fake = FakeHM()
        tr = FakeTransport(ack=False)
        try:
            bro, xc, stick, smux = self._bridge_with_stick(fake, tr)
            t0 = time.time()
            bro._process_command_batch(FakeClient(), [("light", smux, b'{"state":"ON","brightness":40}')])
            bro.PENDING_CONFIRM.pop(smux, None)   # lampa har meldt 40 % til Home Manager
            bro._check_usb_acks([(smux, 40, [2000001, 2000002], bro.LAST_CMD_AT[smux])], t0)
            self.assertEqual(bro._pending_commands, [])
            self.assertEqual(bro.USB_STATS["bekreftet_hm"], 1)
            stick.stopping = True
        finally:
            fake.close()

    def test_utdatert_kommando_sendes_ikke_paa_nytt(self):
        fake = FakeHM()
        tr = FakeTransport(ack=False)
        try:
            bro, xc, stick, smux = self._bridge_with_stick(fake, tr)
            t0 = time.time()
            bro._process_command_batch(FakeClient(), [("light", smux, b'{"state":"ON","brightness":40}')])
            bro.LAST_CMD_AT[smux] = t0 + 5   # en nyere kommando er gitt
            bro._check_usb_acks([(smux, 40, [2000001, 2000002])], t0)
            self.assertEqual(bro._pending_commands, [])
            stick.stopping = True
        finally:
            fake.close()

    def test_diagnose_teller_mottatte_rammer(self):
        import usbstick
        st = usbstick.UsbStick(transport_factory=lambda: None)
        st.feed(bytes.fromhex("5a1a0301001224a03f0087d6120000800000001717059a02fea5"))
        snap = st.snapshot()
        self.assertEqual((snap["mottatt"], snap["radio_mottatt"], snap["kvitteringer"]), (1, 1, 1))


class TestUsbLesing(unittest.TestCase):
    def test_leser_noyaktig_en_pakke(self):
        import usbstick
        asked = []

        class Ep:
            wMaxPacketSize = 62

            def read(self, n, timeout=0):
                asked.append(n)
                return bytearray(b"\x5a\x06\x02\x07\x31\xa5" + bytes(56))

        t = usbstick.PyUsbTransport(None, Ep(), None)
        import importlib.util
        if importlib.util.find_spec("usb") is None:
            self.skipTest("pyusb ikke installert")
        self.assertEqual(t.read(100)[:2], b"\x5a\x06")
        self.assertEqual(asked, [62])

    def test_enkelt_lesefeil_kobler_ikke_fra(self):
        import usbstick

        class T(FakeTransport):
            def __init__(self):
                super().__init__()
                self.errors = 3

            def read(self, timeout_ms):
                if self.errors:
                    self.errors -= 1
                    raise usbstick.TransientUsbError("[Errno 75] Overflow")
                return super().read(timeout_ms)

        tr = T()
        st = usbstick.UsbStick(transport_factory=lambda: tr)
        self.assertTrue(st.connect())
        threading.Thread(target=st.run, daemon=True).start()
        time.sleep(0.3)
        self.assertTrue(st.healthy())
        self.assertIsNotNone(st.set_levels([(1234567, 50)]))
        time.sleep(0.3)
        self.assertTrue(st.acked_since(1234567, 0))
        st.stopping = True

    def test_pinne_som_faller_ut_kobles_til_igjen(self):
        import usbstick
        tr = FakeTransport()
        st = usbstick.UsbStick(transport_factory=lambda: tr)
        self.assertTrue(st.connect())
        st._broken("test")
        self.assertIsNone(st.transport)
        self.assertFalse(st.healthy())
        self.assertTrue(st.connect())
        self.assertTrue(st.healthy())


class TestUsbSattInnIgjen(unittest.TestCase):
    def _sysfs(self, tmp, name, vid, pid, bus, dev, majmin):
        d = os.path.join(tmp, "sys", name)
        os.makedirs(d)
        for f, v in (("idVendor", vid), ("idProduct", pid), ("busnum", bus), ("devnum", dev), ("dev", majmin)):
            with open(os.path.join(d, f), "w") as fh:
                fh.write(v + "\n")

    def test_lager_enhetsfil_for_pinne_satt_inn_etterpaa(self):
        import usbstick
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            self._sysfs(tmp, "1-2", "188a", "1102", "1", "7", "189:6")
            self._sysfs(tmp, "1-3", "046d", "c52b", "1", "8", "189:7")   # en mus, skal ignoreres
            made = []
            with mock.patch.object(usbstick.os, "mknod", lambda p, m, d: made.append((p, os.major(d), os.minor(d)))):
                n = usbstick.ensure_dev_node(os.path.join(tmp, "sys"), os.path.join(tmp, "dev"))
            self.assertEqual(n, 1)
            self.assertEqual(made, [(os.path.join(tmp, "dev", "001", "007"), 189, 6)])

    def test_finnes_filen_lages_ingenting(self):
        import usbstick
        with tempfile.TemporaryDirectory() as tmp:
            self._sysfs(tmp, "1-2", "188a", "1102", "1", "7", "189:6")
            os.makedirs(os.path.join(tmp, "dev", "001"))
            open(os.path.join(tmp, "dev", "001", "007"), "w").close()
            self.assertEqual(usbstick.ensure_dev_node(os.path.join(tmp, "sys"), os.path.join(tmp, "dev")), 0)

    def test_ny_libusb_sesjon_hver_gang(self):
        import importlib.util
        if importlib.util.find_spec("usb") is None:
            self.skipTest("pyusb ikke installert")
        import usbstick
        a, b = usbstick._fresh_backend(), usbstick._fresh_backend()
        if a is None:
            self.skipTest("libusb ikke tilgjengelig")
        self.assertIsNot(a, b)
        usbstick._free_backend(a)
        usbstick._free_backend(b)

    def test_finner_pinnen_igjen_uten_omstart(self):
        import usbstick
        state = {"plugged": False}
        tr = FakeTransport()
        st = usbstick.UsbStick(transport_factory=lambda: tr if state["plugged"] else None)
        old = usbstick.RETRY_OPEN_S
        usbstick.RETRY_OPEN_S = 0.05
        try:
            threading.Thread(target=st.run, daemon=True).start()
            time.sleep(0.2)
            self.assertFalse(st.healthy())
            state["plugged"] = True
            time.sleep(1.5)
            self.assertTrue(st.healthy())
        finally:
            st.stopping = True
            usbstick.RETRY_OPEN_S = old


class TestAnlegg(unittest.TestCase):
    """Andre enn forfatteren skal ikke få hans enhetsliste."""

    def _bridge(self, names):
        fake = FakeHM(names=names)
        bro, xc = load_bridge(fake)
        bro.PROFILE_FILE = os.path.join(os.path.dirname(bro.EXTRA_FILE), "anlegg.json")
        return fake, bro, xc

    def test_eget_anlegg_gjenkjennes_paa_navn(self):
        import json as _j
        with open(os.path.join(os.path.dirname(__file__), "..", "seed_cache.json"), encoding="utf-8") as f:
            seed = _j.load(f)
        names = {int(k): v["name"] for k, v in seed.items()}
        fake, bro, xc = self._bridge(names)
        try:
            n = len(xc.LIGHTS)
            bro.apply_profile()
            self.assertTrue(bro.PROFILE["innebygd"])
            self.assertEqual(len(xc.LIGHTS), n)
            with open(bro.PROFILE_FILE) as f:
                self.assertEqual(_j.load(f), {"innebygd": True})
        finally:
            fake.close()

    def test_noen_omdopte_objekter_gjor_ingenting(self):
        import json as _j
        with open(os.path.join(os.path.dirname(__file__), "..", "seed_cache.json"), encoding="utf-8") as f:
            seed = _j.load(f)
        first = sorted(seed)[:2]
        fake, bro, xc = self._bridge({int(k): seed[k]["name"] for k in first})   # bare 2 av 12 stemmer
        try:
            self.assertTrue(bro._names_match_seed(seed))
        finally:
            fake.close()
        fake, bro, xc = self._bridge({int(first[0]): seed[first[0]]["name"]})    # 1 treff er ikke nok
        try:
            self.assertFalse(bro._names_match_seed(seed))
        finally:
            fake.close()

    def test_nytt_anlegg_starter_tomt(self):
        fake, bro, xc = self._bridge({})
        try:
            bro.apply_profile()
            self.assertFalse(bro.PROFILE["innebygd"])
            self.assertEqual((xc.LIGHTS, xc.SWITCHES, xc.MARKISER, xc.THERMOSTATS, bro.BUTTONS),
                             ([], [], [], [], []))
            self.assertEqual(bro.MARKISER_CTRL.markiser, {})
            self.assertEqual(bro.rebuild_registry(), set())
            client = FakeClient()
            bro.publish_discovery(client)
            self.assertFalse([t for t, p in client.published if p and "/light/" in t])
            self.assertFalse([t for t, p in client.published if "/button/" in t])
            self.assertTrue(bro.Api().status()["nytt_anlegg"])
        finally:
            fake.close()

    def test_lagret_valg_brukes(self):
        fake, bro, xc = self._bridge({})
        try:
            with open(bro.PROFILE_FILE, "w") as f:
                f.write('{"innebygd": true}')
            self.assertTrue(bro.decide_profile(check=lambda seed: self.fail("skal ikke spørre")))
        finally:
            fake.close()

    def test_home_manager_svarer_ikke(self):
        fake, bro, xc = self._bridge({})
        try:
            self.assertFalse(bro.decide_profile(check=lambda seed: None))
            self.assertFalse(os.path.exists(bro.PROFILE_FILE))   # sjekkes igjen neste gang
        finally:
            fake.close()

    def test_legg_til_med_navn_og_rom_og_endre_og_fjern(self):
        fake, bro, xc = self._bridge({})
        try:
            bro.clear_builtin_devices()
            bro.SCANNER.objects = {500: ("D_TaklampeStue", 21), 501: ("M_D_TaklampeStue_Val", 23),
                                   502: ("B_D_TaklampeStue_SET", 23), 600: ("IN_Stue_BR1", 11)}
            api = bro.Api()
            r = api.add_extra({"lys": [{"id": 500, "navn": "Taklampe", "sone": "Stue"}], "knapper": [600]})
            self.assertEqual(r["lagt_til"], ["Taklampe", "Stue BR1"])
            self.assertEqual(xc.LIGHTS, [("Stue", "Taklampe", 500)])
            self.assertEqual(xc.load_cache()["500"]["val_id"], 501)
            devs = {d["id"]: d for d in api.devices()}
            self.assertTrue(devs[500]["ekstra"] and devs[600]["ekstra"])
            self.assertEqual(devs[600]["sone"], "Stue")
            self.assertTrue(api.update_extra({"id": 500, "navn": "Tak", "sone": "Kjøkken"})["ok"])
            self.assertEqual(xc.LIGHTS, [("Kjøkken", "Tak", 500)])
            client = FakeClient()
            bro.MQTT_STATE["client"] = client
            self.assertTrue(api.remove_extra({"id": 500})["ok"])
            self.assertEqual(xc.LIGHTS, [])
            self.assertEqual(client.last("homeassistant/light/xcomfort_500/config"), "")
            self.assertFalse(api.remove_extra({"id": 999})["ok"])
            bro.MQTT_STATE["client"] = None
        finally:
            fake.close()


class TestUsbLaering(unittest.TestCase):
    def test_laerer_hvilke_dimmere_et_lys_styrer(self):
        fake = FakeHM()
        try:
            bro, xc = load_bridge(fake)
            a, b = xc.LIGHTS[0][2], xc.LIGHTS[1][2]
            dimmere = {a: [111, 112], b: []}      # lys b svarer ikke

            class Stick:
                def healthy(self):
                    return True
            bro.STICK = Stick()
            bro.LEARN_WINDOW_S = 0.05
            sent = []

            def fake_queue(kind, smux, payload):
                sent.append((smux, json.loads(payload)))
                self.assertIn(smux, bro._learning)          # skal gå via Home Manager
                bro._on_usb_radio(b"", (0x5A, 7654321, 4242))  # tilfeldig trafikk, bare første gang
                for s in dimmere[smux]:
                    bro._on_usb_radio(b"", (0x5A, 7654321, s))
                bro._on_usb_radio(b"", (0x01, 111, 128))   # kvittering til pinnen, ikke en kommando
            bro._queue_command = fake_queue
            # 4242 høres bare i første runde:
            orig = bro._on_usb_radio
            seen = {"n": 0}

            def radio(frame, info):
                if info[2] == 4242:
                    seen["n"] += 1
                    if seen["n"] > 1:
                        return
                orig(frame, info)
            bro._on_usb_radio = radio
            bro.publish_state(FakeClient(), bro.light_state_topic(a), json.dumps({"state": "ON", "brightness": 40}))
            bro.learn_usb_map([a, b])
            self.assertEqual(bro.LEARN["resultat"], {a: [111, 112]})
            self.assertEqual(len(bro.LEARN["uten_svar"]), 1)
            self.assertEqual(bro.load_usb_map()[a], [111, 112])
            self.assertEqual(sent[0], (a, {"state": "ON", "brightness": 40}))
            self.assertFalse(bro.LEARN["kjorer"])
            self.assertEqual(bro._learning, set())
        finally:
            fake.close()


class TestUsbOmsending(unittest.TestCase):
    def test_proever_igjen_naar_kvittering_mangler(self):
        import usbstick

        class T(FakeTransport):
            def __init__(self):
                super().__init__(ack=True)
                self.skip = 1   # første melding blir ikke kvittert

            def write(self, data):
                if data[2] == 0x1B and self.skip:
                    self.skip -= 1
                    self.written.append(bytes(data[:data[1]]))
                    return
                super().write(data)

        tr = T()
        st = usbstick.UsbStick(transport_factory=lambda: tr)
        self.assertTrue(st.connect())
        threading.Thread(target=st.run, daemon=True).start()
        t0, acked = st.set_levels([(1234567, 50)])
        self.assertEqual(acked, {1234567})
        self.assertEqual(st.retries, 1)
        self.assertEqual(len([w for w in tr.written if w[2] == 0x1B]), 2)
        st.stopping = True

    def test_radiologg_viser_home_manager_til_dimmer(self):
        fake = FakeHM()
        try:
            bro, xc = load_bridge(fake)
            smux = xc.LIGHTS[0][2]
            bro.save_usb_map({smux: [1234567]})
            frame = bytes.fromhex("5a2603530000001e3402071fb1cb74000087d61200b1cb74000000000000000000007e3e97e2fcfea5")
            bro._on_usb_radio(frame, (0x53, 7654321, 1234567))
            self.assertEqual(bro.RADIO_LOG[-1]["retning"], "to dimmer")
        finally:
            fake.close()


if __name__ == "__main__":
    unittest.main()
