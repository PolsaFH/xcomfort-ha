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

    def __init__(self, handshake=True, values=None, ack_ids=()):
        self.handshake = handshake
        self.ack_ids = set(ack_ids)   # markiser som "kvitterer" (som ekte aktuatorer)
        self.values = values or {}
        self.received = []          # [(tid, kommando)]
        self.connections = 0
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
        if self.handshake:
            c.sendall(b'\x01<connect sn="1" ip="127.0.0.1" mac="x"/>\x02')
        buf = b""
        c.settimeout(5)
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
    for mod in ("xcomfort_mqtt_bridge", "xcomfort_local_control"):
        sys.modules.pop(mod, None)
    import xcomfort_local_control as xc
    xc.CACHE_FILE = os.path.join(tmp, "cache.json")
    import xcomfort_mqtt_bridge as bro
    bro.OPTS.update(options)
    xc.CONTROLLER_IP = "127.0.0.1"
    xc.CONTROLLER_PORT = fake.port
    bro.POSITION_FILE = os.path.join(tmp, "pos.json")
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


class TestKvittering(unittest.TestCase):
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

    def test_kvittering_telles_og_manglende_varsles(self):
        fake = FakeHM(ack_ids=[50980])
        try:
            bro, xc = load_bridge(fake, markise_pause_mellom_kommandoer=0.0, markise_dobbel_stopp=False)
            bro.MARKISE_KVITTERING_S = 1.0
            bro.MARKISER_CTRL.enqueue(50980, "ut")
            bro.MARKISER_CTRL.enqueue(65125, "ut")
            self.run_ctrl(bro, FakeClient(), 2.5)
            self.assertEqual(bro.STATS.markise[50980]["kvittert"], 1)
            self.assertEqual(bro.STATS.markise[50980]["uten_kvittering"], 0)
            self.assertEqual(bro.STATS.markise[65125]["kvittert"], 0)
            self.assertEqual(bro.STATS.markise[65125]["uten_kvittering"], 1)
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


if __name__ == "__main__":
    unittest.main()
