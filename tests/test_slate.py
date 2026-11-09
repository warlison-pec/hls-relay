""""Please wait" video for on-demand channels.

Run:  python3 -I tests/test_slate.py -v
"""
import re
import shutil
import subprocess
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

ROOT = Path(__file__).resolve().parent.parent
HAVE_FFMPEG = bool(shutil.which("ffmpeg"))


def fake_slate(directory, count=3, duration=6.0):
    lines = ["#EXTM3U"]
    for i in range(count):
        (Path(directory) / f"{i:03d}.ts").write_bytes(b"\x47" + bytes(187))
        lines += [f"#EXTINF:{duration:.6f},", f"{i:03d}.ts"]
    (Path(directory) / "index.m3u8").write_text("\n".join(lines) + "\n#EXT-X-ENDLIST\n")


def seq_of(playlist):
    return int(re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", playlist).group(1))


def dseq_of(playlist):
    return int(re.search(r"#EXT-X-DISCONTINUITY-SEQUENCE:(\d+)", playlist).group(1))


class Base(unittest.TestCase):
    def serve(self, slate_dir, timeline=None, **cfg):
        self.origin = sim.SimOrigin(timeline or sim.synthetic_timeline(80, SEG), window=4).begin()
        self.addCleanup(self.origin.stop)
        self.manager = r.ChannelManager(make_cfg(self.origin.source_url, slate_dir=slate_dir, **cfg))
        self.addCleanup(self.manager.stop_all)
        self.server = r.RelayHTTPServer(("127.0.0.1", 0), self.manager)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def url(self, path):
        return f"http://127.0.0.1:{self.server.server_address[1]}{path}"

    def get(self, path):
        try:
            with urllib.request.urlopen(self.url(path), timeout=5) as resp:
                return resp.status, resp.read(), resp.headers.get("Content-Type")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers.get("Content-Type")

    def channel(self, mode):
        return self.manager.add({"url": self.origin.source_url, "mode": mode, "enabled": False})


class SlateTests(Base):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="slate-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        fake_slate(self.dir)

    def test_off_by_default_keeps_the_503(self):
        self.serve("", live_delay_seconds=30.0, live_delay_max_seconds=40.0)
        self.assertIsNone(self.manager.slate)
        status, body, _ = self.get(f"/ch/{self.channel('on_demand')}/live.m3u8")
        self.assertEqual(status, 503)
        self.assertNotIn(b"/slate/", body)
        self.assertEqual(self.get("/slate/0.ts")[0], 404)

    def test_broken_slate_falls_back_to_503(self):
        (Path(self.dir) / "001.ts").write_bytes(b"not a transport stream")
        self.serve(self.dir, live_delay_seconds=30.0, live_delay_max_seconds=40.0)
        self.assertIsNone(self.manager.slate)
        self.assertEqual(self.get(f"/ch/{self.channel('on_demand')}/live.m3u8")[0], 503)

    def test_missing_directory_falls_back_to_503(self):
        self.serve(self.dir + "-nope", live_delay_seconds=30.0, live_delay_max_seconds=40.0)
        self.assertIsNone(self.manager.slate)

    def test_on_demand_channel_shows_slate_while_buffering(self):
        self.serve(self.dir, live_delay_seconds=30.0, live_delay_max_seconds=40.0)
        slot = self.channel("on_demand")
        status, body, ctype = self.get(f"/ch/{slot}/live.m3u8")
        self.assertEqual((status, ctype), (200, "application/vnd.apple.mpegurl"))
        self.assertIn(slot, self.manager.relays)  # the request still turned the channel on
        uri = next(ln for ln in body.decode().splitlines() if ln.startswith("/slate/"))
        status, data, ctype = self.get(uri)
        self.assertEqual((status, ctype), (200, "video/mp2t"))
        self.assertEqual(data[:1], b"\x47")

    def test_always_on_channel_is_not_affected(self):
        self.serve(self.dir, live_delay_seconds=30.0, live_delay_max_seconds=40.0)
        slot = self.channel("always")
        self.assertEqual(self.get(f"/ch/{slot}/live.m3u8")[0], 503)        # switched off
        self.manager.start(slot)
        self.assertEqual(self.get(f"/ch/{slot}/live.m3u8")[0], 503)        # buffering

    def test_gives_up_on_a_dead_origin(self):
        self.serve(self.dir, live_delay_seconds=30.0, live_delay_max_seconds=40.0)
        slot = self.channel("on_demand")
        self.assertEqual(self.get(f"/ch/{slot}/live.m3u8")[0], 200)
        relay = self.manager.relays[slot]
        relay.started -= 30.0 + r.SLATE_GRACE_SECONDS
        self.assertEqual(self.get(f"/ch/{slot}/live.m3u8")[0], 503)

    def test_playlist_is_one_continuous_stream_through_the_switch(self):
        fake_slate(self.dir, count=50)   # long enough not to loop during the test
        self.serve(self.dir, live_delay_seconds=2.0, live_delay_max_seconds=8.0, min_start_segments=1)
        path = f"/ch/{self.channel('on_demand')}/live.m3u8"
        seen, heads, end = {}, [], time.monotonic() + 40
        marked = False
        while time.monotonic() < end:
            text = self.get(path)[1].decode()
            lines = text.splitlines()
            head = seq_of(text)
            heads.append((head, dseq_of(text)))
            uris = [ln for ln in lines if ln.startswith("/")]
            for i, uri in enumerate(uris):
                self.assertEqual(seen.setdefault(head + i, uri), uri, "a sequence number changed meaning")
            real = lines.index(next(ln for ln in lines if ln.startswith("/segment/"))) if "/segment/" in text else None
            if real is not None and "/slate/" in text:
                self.assertEqual(lines[real - 2], "#EXT-X-DISCONTINUITY", text)
                marked = True
            if real is not None and "/slate/" not in text and marked:
                break
            time.sleep(0.1)
        self.assertTrue(marked, "the switch was never announced")
        self.assertNotIn("/slate/", text)
        self.assertEqual(sorted(seen), list(range(min(seen), max(seen) + 1)), "gap in the numbering")
        self.assertEqual(heads, sorted(heads), "head or discontinuity sequence went backwards")
        self.assertEqual(heads[-1][1], heads[0][1] + 1)   # one marked switch, here with no loop restart

    def test_no_bridge_when_nobody_saw_the_slate(self):
        self.serve(self.dir, live_delay_seconds=2.0, live_delay_max_seconds=8.0, min_start_segments=1)
        slot = self.channel("on_demand")
        self.manager.start(slot, persist=False, reason="demand")
        relay = self.manager.relays[slot]
        end = time.monotonic() + 20
        while time.monotonic() < end and not relay.release_started:
            time.sleep(0.05)
            with relay.lock:
                relay._release(relay.clock())
        text = self.get(f"/ch/{slot}/live.m3u8")[1].decode()
        self.assertNotIn("/slate/", text)
        self.assertNotIn("#EXT-X-DISCONTINUITY\n", text)
        self.assertEqual(seq_of(text), int(re.search(r"-(\d+)\.ts", text).group(1)))

    def test_loop_point(self):
        now = [12.0]
        relay = r.Relay(make_cfg("http://127.0.0.1:1/x.m3u8", live_delay_seconds=500.0, live_delay_max_seconds=600.0),
                        clock=lambda: now[0])
        relay.started = 0.0
        relay.slate = r.Slate(self.dir)              # 3 segments of 6 s
        text = relay.playlist().decode()
        self.assertEqual(seq_of(text), relay.slate_base + 2)
        self.assertEqual(dseq_of(text), 0)
        self.assertEqual(re.findall(r"/slate/(\d+)\.ts", text), ["2", "0", "1", "2"])
        self.assertEqual(text.count("#EXT-X-DISCONTINUITY\n"), 1)
        now[0] = 18.0
        self.assertEqual(dseq_of(relay.playlist().decode()), 1)

    def test_unknown_segment_is_404(self):
        self.serve(self.dir)
        self.assertEqual(self.get("/slate/99.ts")[0], 404)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class SlateEndToEnd(Base):
    def test_ffmpeg_client_goes_from_slate_to_real_video(self):
        work = tempfile.mkdtemp(prefix="slate-e2e-")
        self.addCleanup(shutil.rmtree, work, True)
        subprocess.run(["bash", str(ROOT / "scripts/make_slate.sh"), "-o", work], check=True, capture_output=True)
        self.serve(work, sim.ffmpeg_timeline(60, hls_time=1.0), live_delay_seconds=6.0,
                   live_delay_max_seconds=10.0, min_start_segments=3)
        slot = self.channel("on_demand")
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", self.url(f"/ch/{slot}/live.m3u8"),
               "-t", "40", "-map", "0:v:0", "-map", "0:a:0?", "-f", "null", "-"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        relay = self.manager.relays[slot]
        stats = next(iter(relay.clients.values()))
        print(f"\n[e2e] exit={proc.returncode} real_segments={stats['segment_requests']} "
              f"404={stats['segment_404']} stderr_lines={len(proc.stderr.splitlines())}")
        for line in proc.stderr.splitlines()[:12]:
            print("   ", line)
        self.assertEqual(proc.returncode, 0)
        self.assertGreater(stats["segment_requests"], 5, "client never reached the real video")
        self.assertEqual(stats["segment_404"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
