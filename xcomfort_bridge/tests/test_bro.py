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

    def __init__(self, handshake=True, values=None, ack_ids=(), idle_timeout=5):
        self.handshake = handshake
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
                        val = self.values.get(oid, 0)
                        c.sendall(f"\x01SSN {oid} OBJ_{oid} 23 1 0 {val}\x02\x00".encode())
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
    for mod in ("xcomfort_mqtt_bridge", "xcomfort_local_control", "hm_link", "scanner", "web"):
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
            bro, xc = load_bridge(fake, markise_pause_mellom_kommandoer=0.3, markise_dobbel_stopp=True)
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
            bro, xc = load_bridge(fake, markise_pause_mellom_kommandoer=0.0)
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
            bro, xc = load_bridge(fake, markise_pause_mellom_kommandoer=0.0, markise_dobbel_stopp=False)
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
            bro, xc = load_bridge(fake, markise_pause_mellom_kommandoer=0.3, markise_dobbel_stopp=True)
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
        self.assertEqual(ev, {"event_type": "kode_2", "kode": 2})
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
            bro.log.info("hei fra testen")
            server.shutdown()
            server.server_close()
        finally:
            fake.close()
            os.environ.pop("XCOMFORT_WEB_OPEN", None)


if __name__ == "__main__":
    unittest.main()
