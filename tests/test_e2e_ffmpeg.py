"""End-to-end: real H.264 + AC-3 segments (ffmpeg) -> simulated origin -> relay
-> ffmpeg as an HLS client decoding /live.m3u8. Skipped without ffmpeg.

Run:  python3 -I tests/test_e2e_ffmpeg.py -v
"""
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_relay_v04 import Harness, check_playlist_sequence, r, sim

HAVE_FFMPEG = bool(shutil.which("ffmpeg"))


def play(url, seconds):
    """Decode the live playlist like a player would; return ffmpeg error lines."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
           "-i", url, "-t", str(seconds), "-map", "0:v:0", "-map", "0:a:0?", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 60)
    return [ln for ln in proc.stderr.splitlines() if ln.strip()]


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class FfmpegEndToEnd(unittest.TestCase):
    def serve(self, relay):
        server = r.RelayHTTPServer(("127.0.0.1", 0), relay)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/live.m3u8"

    def wait_ready(self, relay, timeout=20):
        end = time.monotonic() + timeout
        while relay.playlist() is None and time.monotonic() < end:
            time.sleep(0.2)
        self.assertIsNotNone(relay.playlist(), "relay never published")

    def test_clean_stream_decodes_without_errors(self):
        tl = sim.ffmpeg_timeline(40, hls_time=1.0)
        with Harness(tl, window=4, start_behind_segments=3, max_cushion_segments=6,
                     min_start_segments=3) as h:
            url = self.serve(h.relay)
            self.wait_ready(h.relay)
            errors = play(url, 20)
            h.run(extra=2)
            self.assertEqual(errors, [], "\n".join(errors[:10]))
            got = h.committed_indices()
            self.assertEqual(got, list(range(got[0], got[0] + len(got))))
            self.assertEqual(h.relay.m["keyframe_start"], h.relay.m["keyframe_checked"])
            self.assertEqual(h.relay.m["discontinuities"], 0)
            check_playlist_sequence(self, h.snapshots)

    def test_live_delay_mode_decodes_without_errors(self):
        tl = sim.ffmpeg_timeline(40, hls_time=1.0)
        with Harness(tl, window=4, live_delay_seconds=8.0, live_delay_max_seconds=12.0,
                     min_start_segments=3) as h:
            url = self.serve(h.relay)
            self.wait_ready(h.relay)
            self.assertGreaterEqual(h.relay.health()["buffer"]["cushion_duration_seconds"], 7.0)
            errors = play(url, 15)
            h.run(extra=2)
            self.assertEqual(errors, [], "\n".join(errors[:10]))
            check_playlist_sequence(self, h.snapshots)

    def test_faults_reset_missing_truncated(self):
        a = sim.ffmpeg_timeline(14, hls_time=1.0, tag="a")
        b = sim.ffmpeg_timeline(14, hls_time=1.0, tag="a", pts_offset=5000.0)  # restart: same names
        for i, e in enumerate(b):
            e.seq = i  # numbering restarts; names are reused with different content
        tl = a + b
        faults = sim.Faults(missing={a[9].name}, truncate={a[4].name: 2})
        with Harness(tl, faults, window=4, start_behind_segments=3, max_cushion_segments=6,
                     min_start_segments=3, publish_window_segments=40) as h:
            url = self.serve(h.relay)
            self.wait_ready(h.relay)
            errors = play(url, 18)
            h.run(extra=3)
            index = {e.payload: i for i, e in enumerate(tl)}
            got = [index.get(s.payload, -1) for s in h.commits]
            self.assertNotIn(-1, got, "partial or foreign bytes were committed")
            self.assertEqual(got, sorted(set(got)))
            # the origin 404s the *name* a-seg0009.ts, which exists in both epochs
            # (index 9 and 23); everything else must arrive exactly once, in order
            self.assertEqual(got, [i for i in range(got[0], 28) if i not in (9, 23)])
            self.assertGreaterEqual(h.relay.m["incomplete_downloads"], 2)
            self.assertEqual(h.relay.m["discontinuities"], 3)  # gap, reset, gap
            self.assertEqual(h.relay.m["keyframe_start"], h.relay.m["keyframe_checked"])
            check_playlist_sequence(self, h.snapshots)
            self.fault_errors = errors
            print(f"\n[e2e] ffmpeg client error lines with faults: {len(errors)}")
            for line in errors[:8]:
                print("   ", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
