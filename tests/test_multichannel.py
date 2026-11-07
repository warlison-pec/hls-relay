"""Multi-channel manager + /admin API (v0.5).

Run:  python3 -I tests/test_multichannel.py -v
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_relay_v04 import SEG, make_cfg, r, sim  # noqa: E402

PASSWORD = "segredo-teste"


class MultiChannelTests(unittest.TestCase):
    def setUp(self):
        self.a = sim.SimOrigin(sim.synthetic_timeline(80, SEG, epoch="A"), window=4).begin()
        self.b = sim.SimOrigin(sim.synthetic_timeline(80, SEG, epoch="B"), window=4).begin()
        self.state = tempfile.mkdtemp(prefix="relay-state-")
        self.base = make_cfg(self.a.source_url)
        self.manager = r.ChannelManager(self.base, self.state, PASSWORD)
        self.manager.start_enabled()
        self.server = r.RelayHTTPServer(("127.0.0.1", 0), self.manager)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.manager.stop_all()
        self.a.stop()
        self.b.stop()
        shutil.rmtree(self.state, ignore_errors=True)

    # helpers ---------------------------------------------------------------
    def req(self, path, method="GET", body=None, password=PASSWORD):
        headers = {"Content-Type": "application/json"}
        if password is not None:
            headers["X-Admin-Password"] = password
        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        rq = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                    headers=headers)
        try:
            with urllib.request.urlopen(rq, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def wait_playlist(self, path, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            status, body = self.req(path, password=None)
            if status == 200:
                return body.decode()
            time.sleep(0.2)
        self.fail(f"{path} never published")

    def add(self, **body):
        status, raw = self.req("/api/channels", "POST", body)
        self.assertEqual(status, 200, raw)
        return json.loads(raw)["slot"]

    def segment_bytes(self, playlist):
        uri = [ln for ln in playlist.splitlines() if ln.startswith("/segment/")][-1]
        status, body = self.req(uri, password=None)
        self.assertEqual(status, 200)
        return body

    # tests -----------------------------------------------------------------
    def test_first_start_seeds_channel_1_from_env_and_keeps_live_m3u8(self):
        spec = self.manager.specs[1]
        self.assertEqual((spec["url"], spec["enabled"]), (self.a.source_url, True))
        self.assertTrue(os.path.exists(os.path.join(self.state, "channels.json")))
        pl = self.wait_playlist("/live.m3u8")
        self.assertIn(self.segment_bytes(pl), [e.payload for e in self.a.timeline])
        self.assertEqual(self.wait_playlist("/ch/1/live.m3u8").splitlines()[0], "#EXTM3U")

    def test_admin_auth(self):
        self.assertEqual(self.req("/api/channels", password=None)[0], 401)
        self.assertEqual(self.req("/api/channels", password="errada")[0], 401)
        status, body = self.req("/api/channels")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["channels"]), 1)
        self.assertEqual(self.req("/api/channels", "POST", {}, password="errada")[0], 401)
        self.assertEqual(self.req("/api/channels/1/start", "POST", password="errada")[0], 401)
        no_pw = r.ChannelManager(self.base)
        srv = r.RelayHTTPServer(("127.0.0.1", 0), no_pw)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            rq = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/channels",
                                        headers={"X-Admin-Password": ""})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(rq, timeout=5)
            self.assertEqual(ctx.exception.code, 403)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_second_channel_configure_start_stop(self):
        self.assertEqual(self.req("/ch/2/live.m3u8", password=None)[0], 404)   # does not exist yet
        self.assertEqual(self.add(), 2)
        self.assertEqual(self.req("/ch/2/live.m3u8", password=None)[0], 503)   # exists, switched off
        status, body = self.req("/api/channels/2", "POST",
                                {"name": "Esportes", "url": self.b.source_url, "live_delay_seconds": 0,
                                 "max_parallel_downloads": 1})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.req("/api/channels/2/start", "POST")[0], 200)
        pl2 = self.wait_playlist("/ch/2/live.m3u8")
        pl1 = self.wait_playlist("/live.m3u8")
        self.assertIn(self.segment_bytes(pl2), [e.payload for e in self.b.timeline])
        self.assertIn(self.segment_bytes(pl1), [e.payload for e in self.a.timeline])
        relay2 = self.manager.relay(2)
        self.assertEqual(relay2.cfg.max_parallel_downloads, 1)
        self.assertEqual(relay2.channel, "2")
        # stop: playlist answers "desligado", threads asked to stop, state persisted
        self.assertEqual(self.req("/api/channels/2/stop", "POST")[0], 200)
        status, body = self.req("/ch/2/live.m3u8", password=None)
        self.assertEqual(status, 503)
        self.assertIn(b"desligado", body)
        self.assertTrue(relay2.stop_event.is_set())
        with open(os.path.join(self.state, "channels.json"), encoding="utf-8") as fh:
            saved = json.load(fh)
        ch2 = [c for c in saved["channels"] if c["slot"] == 2][0]
        self.assertEqual((ch2["name"], ch2["url"], ch2["enabled"]), ("Esportes", self.b.source_url, False))

    def test_changing_url_restarts_running_channel(self):
        self.wait_playlist("/live.m3u8")
        old = self.manager.relay(1)
        status, body = self.req("/api/channels/1", "POST", {"url": self.b.source_url})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["restarted"])
        new = self.manager.relay(1)
        self.assertIsNot(old, new)
        self.assertTrue(old.stop_event.is_set())
        pl = self.wait_playlist("/live.m3u8")
        self.assertIn(self.segment_bytes(pl), [e.payload for e in self.b.timeline])
        # renaming only: no restart
        status, body = self.req("/api/channels/1", "POST", {"name": "Novo nome"})
        self.assertFalse(json.loads(body)["restarted"])
        self.assertIs(self.manager.relay(1), new)

    def test_state_survives_restart(self):
        self.add()
        self.assertEqual(self.add(name="Filmes", url=self.b.source_url, live_delay_seconds=300), 3)
        self.req("/api/channels/3/start", "POST")
        self.manager.stop_all()
        again = r.ChannelManager(self.base, self.state, PASSWORD)
        self.assertEqual(again.specs[3]["name"], "Filmes")
        self.assertTrue(again.specs[3]["enabled"])
        self.assertEqual(again.next_id, 4)
        self.assertEqual(again.config_for(again.specs[3]).live_delay_seconds, 300)
        self.assertGreaterEqual(again.config_for(again.specs[3]).live_delay_max_seconds, 310)
        again.start_enabled()
        try:
            self.assertEqual(sorted(again.relays), [1, 3])
        finally:
            again.stop_all()

    def test_validation_and_unknown_routes(self):
        self.add()
        for body in ({"url": "ftp://x/y"}, {"url": "nada"}, {"max_parallel_downloads": 9},
                     {"live_delay_seconds": -5}):
            self.assertEqual(self.req("/api/channels/2", "POST", body)[0], 400, body)
        status, body = self.req("/api/channels/2/start", "POST")
        self.assertEqual(status, 400)
        self.assertIn("URL", json.loads(body)["error"])
        self.assertEqual(self.req("/ch/7/live.m3u8", password=None)[0], 404)
        self.assertEqual(self.req("/api/channels/9", "POST", {})[0], 404)

    def test_admin_page_and_health_redaction(self):
        status, body = self.req("/admin", password=None)
        self.assertEqual(status, 200)
        self.assertIn(b"Relay de canais", body)
        self.wait_playlist("/live.m3u8")
        status, body = self.req("/health", password=None)
        h = json.loads(body)
        self.assertEqual(len(h["channels"]), 1)
        self.assertNotIn("http://", body.decode())   # full URLs only in the authenticated API
        self.assertIn("127.0.0.1/~/master.m3u8", body.decode())
        self.add()
        status, body = self.req("/ch/2/health", password=None)
        self.assertEqual(json.loads(body)["status"], "off")

    def test_two_channels_do_not_mix_segments(self):
        self.add(url=self.b.source_url)
        self.req("/api/channels/2/start", "POST")
        a_payloads = {e.payload for e in self.a.timeline}
        b_payloads = {e.payload for e in self.b.timeline}
        for _ in range(3):
            pl1, pl2 = self.wait_playlist("/live.m3u8"), self.wait_playlist("/ch/2/live.m3u8")
            for line in pl1.splitlines():
                if line.startswith("/segment/"):
                    status, body = self.req(line, password=None)
                    if status == 200:
                        self.assertIn(body, a_payloads)
            for line in pl2.splitlines():
                if line.startswith("/segment/"):
                    status, body = self.req(line, password=None)
                    if status == 200:
                        self.assertIn(body, b_payloads)
            time.sleep(1)

    def test_add_remove_limit_and_no_reuse(self):
        self.manager.base.max_channels = 3
        self.assertEqual(self.add(name="B", url=self.b.source_url), 2)
        self.assertEqual(self.add(name="C"), 3)
        status, body = self.req("/api/channels", "POST", {})
        self.assertEqual(status, 400)
        self.assertIn("Limite", json.loads(body)["error"])
        # removing a running channel stops it and frees its address
        self.req("/api/channels/2/start", "POST")
        relay2 = self.manager.relay(2)
        self.wait_playlist("/ch/2/live.m3u8")
        self.assertEqual(self.req("/api/channels/2/delete", "POST")[0], 200)
        self.assertTrue(relay2.stop_event.is_set())
        self.assertEqual(self.req("/ch/2/live.m3u8", password=None)[0], 404)
        self.assertEqual(self.req("/api/channels/2/start", "POST")[0], 404)
        # numbers are never reused, also after a restart
        self.assertEqual(self.add(name="D"), 4)
        again = r.ChannelManager(self.base, self.state, PASSWORD)
        self.assertEqual(sorted(again.specs), [1, 3, 4])
        self.assertEqual(again.next_id, 5)
        # channel 1 can be removed too; /live.m3u8 then answers 404
        self.assertEqual(self.req("/api/channels/1/delete", "POST")[0], 200)
        self.assertEqual(self.req("/live.m3u8", password=None)[0], 404)

    def test_loads_v050_state_with_four_fixed_slots(self):
        legacy = {"version": "0.5.0", "channels": [
            {"slot": 1, "name": "Premiere", "url": self.a.source_url, "enabled": True},
            {"slot": 2, "name": "Canal 2", "url": "", "enabled": False},
            {"slot": 3, "name": "History", "url": self.b.source_url, "enabled": False},
            {"slot": 4, "name": "Canal 4", "url": "", "enabled": False}]}
        state = tempfile.mkdtemp(prefix="relay-legacy-")
        try:
            with open(os.path.join(state, "channels.json"), "w", encoding="utf-8") as fh:
                json.dump(legacy, fh)
            m = r.ChannelManager(self.base, state, PASSWORD)
            self.assertEqual(sorted(m.specs), [1, 2, 3, 4])
            self.assertEqual(m.next_id, 5)
            self.assertEqual(m.specs[3]["name"], "History")
            self.assertTrue(m.specs[1]["enabled"])
        finally:
            shutil.rmtree(state, ignore_errors=True)

    def _age(self, relay, seconds):
        """Pretend the last client request happened <seconds> ago."""
        with relay.lock:
            relay.started -= seconds
            for c in relay.clients.values():
                c["last_seen"] -= seconds

    def test_on_demand_starts_on_request_and_stops_when_idle(self):
        slot = self.add(name="Reserva", url=self.b.source_url, mode="on_demand", idle_stop_minutes=20)
        self.assertIsNone(self.manager.relay(slot))
        # the first request starts it; the playlist appears once the cushion is ready
        status, body = self.req(f"/ch/{slot}/live.m3u8", password=None)
        self.assertIn(status, (200, 503))
        self.assertIsNotNone(self.manager.relay(slot))
        self.assertEqual(self.manager.started_by[slot], "demand")
        pl = self.wait_playlist(f"/ch/{slot}/live.m3u8")
        self.assertIn(self.segment_bytes(pl), [e.payload for e in self.b.timeline])
        relay = self.manager.relay(slot)
        # watched recently: the reaper keeps it
        self.manager.reap()
        self.assertIs(self.manager.relay(slot), relay)
        # 19 min idle: still on; 21 min idle: stopped and freed
        self._age(relay, 19 * 60)
        self.manager.reap()
        self.assertIs(self.manager.relay(slot), relay)
        self._age(relay, 2 * 60)
        self.manager.reap()
        self.assertIsNone(self.manager.relay(slot))
        self.assertTrue(relay.stop_event.is_set())
        # the next request turns it on again
        self.wait_playlist(f"/ch/{slot}/live.m3u8")
        self.assertIsNotNone(self.manager.relay(slot))

    def test_always_on_channels_are_never_reaped(self):
        relay = self.manager.relay(1)
        self._age(relay, 10 * 3600)
        self.manager.reap()
        self.assertIs(self.manager.relay(1), relay)

    def test_on_demand_not_started_at_boot_and_mode_switch(self):
        slot = self.add(name="Reserva", url=self.b.source_url, mode="on_demand")
        self.req(f"/api/channels/{slot}/start", "POST")          # manual start of an on-demand channel
        self.assertIsNotNone(self.manager.relay(slot))
        self.assertFalse(self.manager.specs[slot]["enabled"])     # but not "start with the container"
        again = r.ChannelManager(self.base, self.state, PASSWORD)
        again.start_enabled()
        try:
            self.assertEqual(sorted(again.relays), [1])
        finally:
            again.stop_all()
        # switching a running on-demand channel to always-on keeps it on and starts it at boot
        status, body = self.req(f"/api/channels/{slot}", "POST", {"mode": "always"})
        self.assertFalse(json.loads(body)["restarted"])
        self.assertTrue(self.manager.specs[slot]["enabled"])
        self.assertEqual(self.req(f"/api/channels/{slot}", "POST", {"mode": "sometimes"})[0], 400)
        # stopped on-demand channel: request starts it; stopped always-on channel: stays off
        self.req(f"/api/channels/{slot}/stop", "POST")
        self.assertEqual(self.req(f"/ch/{slot}/live.m3u8", password=None)[0], 503)
        self.assertIsNone(self.manager.relay(slot))

    def test_summary_reports_on_demand_state(self):
        slot = self.add(name="Reserva", url=self.b.source_url, mode="on_demand", idle_stop_minutes=5)
        status, body = self.req("/api/channels")
        item = [c for c in json.loads(body)["channels"] if c["slot"] == slot][0]
        self.assertEqual((item["mode"], item["status"], item["idle_stop_minutes_effective"]),
                         ("on_demand", "off", 5.0))
        self.wait_playlist(f"/ch/{slot}/live.m3u8")
        item = [c for c in json.loads(self.req("/api/channels")[1])["channels"] if c["slot"] == slot][0]
        self.assertEqual(item["started_by"], "demand")
        self.assertIn("idle_seconds", item["metrics"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
