"""BoxServer HLS Relay v0.4 - offline tests (simulated origin on 127.0.0.1).

Run:  python3 -I -m unittest discover -s tests -v
"""
import importlib.util
import json
import logging
import os
import socket
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hls_sim as sim  # noqa: E402

spec = importlib.util.spec_from_file_location("relay_v04", HERE.parent / "relay.py")
r = importlib.util.module_from_spec(spec)
sys.modules["relay_v04"] = r
spec.loader.exec_module(r)

logging.basicConfig(level=logging.CRITICAL)
SEG = 0.5  # seconds per simulated segment


def make_cfg(source, **kw):
    cfg = r.Config(source_url=source, poll_interval=0.3, min_poll_interval=0.1,
                   http_timeout=3, playlist_timeout=2, segment_read_timeout=1.5,
                   segment_give_up_seconds=6, start_behind_segments=2, max_cushion_segments=4,
                   min_start_segments=2, expired_grace_seconds=1, log_segments=False,
                   live_delay_seconds=0)
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg.normalize()


class Harness:
    """Origin + relay + recorder of committed segments and playlist snapshots."""

    def __init__(self, timeline, faults=None, window=4, master=True, **cfg):
        self.origin = sim.SimOrigin(timeline, window=window, faults=faults, master=master)
        self.cfg_kw = cfg
        self.commits = []
        self.snapshots = []

    def __enter__(self):
        self.origin.begin()
        self.relay = r.Relay(make_cfg(self.origin.source_url, **self.cfg_kw))
        self.relay.on_commit = self.commits.append
        self.relay.start()
        return self

    def run(self, extra=3.0, timeout=90, snapshot_every=0.25):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self.origin.finished():
            self._snap()
            time.sleep(snapshot_every)
        stop = time.monotonic() + extra
        while time.monotonic() < stop:
            self._snap()
            time.sleep(snapshot_every)
        return self

    def _snap(self):
        body = self.relay.playlist()
        if body:
            self.snapshots.append(body.decode())

    def __exit__(self, *exc):
        self.relay.stop()
        self.origin.stop()

    # analysis -------------------------------------------------------------
    def committed_indices(self):
        index = {e.payload: i for i, e in enumerate(self.origin.timeline)}
        return [index.get(s.payload, -1) for s in self.commits]

    def expected_indices(self):
        miss = self.origin.faults.missing
        return [i for i, e in enumerate(self.origin.timeline) if e.name not in miss]


def check_playlist_sequence(test, snapshots):
    """HLS consistency across successive /live.m3u8 responses."""
    seen = {}            # uri -> (media seq, discontinuity seq, duration)
    last_media = -1
    last_edge = -1
    target = None
    for text in snapshots:
        lines = text.strip().splitlines()
        test.assertEqual(lines[0], "#EXTM3U")
        hdr = {ln.split(":", 1)[0]: ln.split(":", 1)[1] for ln in lines if ln.startswith("#EXT-X-") and ":" in ln}
        td = int(hdr["#EXT-X-TARGETDURATION"])
        if target is not None:
            test.assertGreaterEqual(td, target, "TARGETDURATION decreased")
        target = td
        mseq = int(hdr["#EXT-X-MEDIA-SEQUENCE"])
        dseq = int(hdr["#EXT-X-DISCONTINUITY-SEQUENCE"])
        test.assertGreaterEqual(mseq, last_media, "MEDIA-SEQUENCE went backwards")
        last_media = mseq
        seq, disc, dur = mseq, dseq, None
        for ln in lines[1:]:
            if ln == "#EXT-X-DISCONTINUITY":
                disc += 1
            elif ln.startswith("#EXTINF:"):
                dur = float(ln[8:].rstrip(","))
                test.assertLessEqual(int(dur + 0.5), td, "EXTINF rounds above TARGETDURATION")
            elif not ln.startswith("#"):
                test.assertIsNotNone(dur, "URI without EXTINF")
                if ln in seen:
                    test.assertEqual(seen[ln], (seq, disc, dur), "segment changed identity between reloads")
                seen[ln] = (seq, disc, dur)
                seq, dur = seq + 1, None
        test.assertGreaterEqual(seq - 1, last_edge, "live edge regressed")
        last_edge = seq - 1


# =========================================================================
# Unit tests
# =========================================================================
class ParsingTests(unittest.TestCase):
    def test_media_playlist_tags(self):
        text = ("#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXT-X-MEDIA-SEQUENCE:100\n"
                "#EXT-X-KEY:METHOD=NONE\n#EXTINF:2.936,\na.ts?t=1\n#EXT-X-DISCONTINUITY\n"
                "#EXT-X-PROGRAM-DATE-TIME:2026-11-06T12:00:00Z\n#EXTINF:3.003,\nhttp://cdn.example/x/b.ts\n")
        pl = r.parse_media_playlist(text, "http://o.example/live/index.m3u8")
        self.assertEqual([i.seq for i in pl.items], [100, 101])
        self.assertEqual(pl.items[0].uri, "http://o.example/live/a.ts?t=1")
        self.assertEqual(pl.items[0].key, "a.ts")
        self.assertTrue(pl.items[1].discontinuity)
        self.assertEqual(pl.items[1].pdt, "2026-11-06T12:00:00Z")

    def test_unsupported_variants_rejected(self):
        for tag in ("#EXT-X-KEY:METHOD=AES-128,URI=\"k\"", "#EXT-X-MAP:URI=\"i.mp4\""):
            with self.assertRaises(r.PlaylistError):
                r.parse_media_playlist(f"#EXTM3U\n{tag}\n#EXTINF:2,\na.ts\n", "http://o/x.m3u8")
        with self.assertRaises(r.PlaylistError):
            r.parse_media_playlist("#EXTM3U\na.ts\n", "http://o/x.m3u8")
        with self.assertRaises(r.PlaylistError):
            r.parse_media_playlist("#EXTM3U\n#EXTINF:2,\nfile:///etc/passwd\n", "http://o/x.m3u8")

    def test_master_attribute_parsing(self):
        text = ('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,CODECS="avc1.4d401f,ac-3",AVERAGE-BANDWIDTH=9\n'
                'low.m3u8\n#EXT-X-STREAM-INF:AVERAGE-BANDWIDTH=1,BANDWIDTH=11520000\nhigh.m3u8\n')
        variants, alt = r.parse_master(text, "http://o/m.m3u8")
        self.assertEqual(sorted(v[0] for v in variants), [800000, 11520000])
        self.assertEqual(alt, 0)


class TsTests(unittest.TestCase):
    def test_valid_segment(self):
        data = sim.make_segment(900000, 2.0)
        info = r.inspect_ts(data)
        self.assertTrue(info.ok, info.error)
        self.assertEqual(info.video_codec, "h264")
        self.assertTrue(info.starts_with_keyframe)
        self.assertEqual(info.min_pts, 900000)
        self.assertEqual(info.max_pts, 900000 + 49 * 3600)
        self.assertEqual(info.cc_errors, 0)
        self.assertEqual(info.audio_streams, 1)

    def test_truncated_and_corrupt(self):
        data = sim.make_segment(0, 1.0)
        self.assertFalse(r.inspect_ts(data[: len(data) * 2 // 3 + 5]).ok)
        bad = bytearray(data)
        bad[188 * 5] = 0x00
        self.assertFalse(r.inspect_ts(bytes(bad)).ok)
        self.assertFalse(r.inspect_ts(b"<html>404</html>" * 20).ok)

    def test_non_keyframe_start_and_pts_wrap(self):
        info = r.inspect_ts(sim.make_segment(r.PTS_WRAP - 3600, 1.0, keyframe_first=False))
        self.assertTrue(info.ok)
        self.assertFalse(info.starts_with_keyframe)
        self.assertAlmostEqual(r.pts_delta(info.max_pts, info.min_pts), 24 * 3600 / 90000, places=3)


class PolicyTests(unittest.TestCase):
    def test_ssrf_policy(self):
        def resolver(host, port, proto=0):
            table = {"cdn.example": "93.184.216.34", "evil.example": "192.168.0.1",
                     "meta.example": "169.254.169.254"}
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port))]
        p = r.UrlPolicy("http://10.0.0.5:8080/master.m3u8", resolver=resolver)
        p.check("http://10.0.0.5:8080/seg/1.ts")          # configured source host
        p.check("https://cdn.example/seg/1.ts")           # public CDN
        for bad in ("http://evil.example/x.ts", "http://meta.example/latest", "http://127.0.0.1/x",
                    "ftp://cdn.example/x", "http://[::ffff:192.168.0.1]/x"):
            with self.assertRaises(r.FetchError, msg=bad) as ctx:
                p.check(bad)
            self.assertEqual(ctx.exception.kind, "blocked")
        listed = r.UrlPolicy("http://origin.example/m.m3u8", [".cdn.example"], resolver=resolver)
        listed.check("http://edge1.cdn.example/a.ts")
        with self.assertRaises(r.FetchError):
            listed.check("http://cdn.other/a.ts")

    def test_redaction(self):
        url = "http://prov.example:8080/live/USER/PASS/12345.ts?token=SECRET"
        red = r.redact_url(url)
        for secret in ("USER", "PASS", "SECRET", "token"):
            self.assertNotIn(secret, red)
        self.assertIn("12345.ts", red)
        self.assertNotIn("SECRET", r.safe_text(f"error fetching {url}: boom"))


class ConfigTests(unittest.TestCase):
    def test_env_and_adjustments(self):
        env = {"HLS_SOURCE_URL": "https://o.example/a.m3u8", "WINDOW_SEGMENTS": "10",
               "PUBLISH_WINDOW_SEGMENTS": "30", "START_BEHIND_SEGMENTS": "4"}
        cfg = r.Config.from_env(env)
        self.assertEqual(cfg.window_segments, 30 + 8 + 2)
        self.assertTrue(cfg.notes)
        with self.assertRaises(r.ConfigError):
            r.Config.from_env({"HLS_SOURCE_URL": "file:///etc/passwd"})
        with self.assertRaises(r.ConfigError):
            r.Config.from_env({**env, "ALLOWED_CLIENT_CIDRS": "not-a-cidr"})

    def test_v03_env_is_accepted(self):
        env = {"HLS_SOURCE_URL": "https://o.example/a.m3u8", "POLL_INTERVAL": "2",
               "WINDOW_SEGMENTS": "40", "PUBLISH_WINDOW_SEGMENTS": "30", "START_BEHIND_SEGMENTS": "4",
               "MAX_CACHE_MB": "512", "HTTP_TIMEOUT": "20", "SEGMENT_RETRIES": "3",
               "HLS_VARIANT_POLICY": "highest"}
        cfg = r.Config.from_env(env)
        self.assertEqual((cfg.window_segments, cfg.publish_window_segments, cfg.start_behind_segments),
                         (40, 30, 4))


class PacingTests(unittest.TestCase):
    """Deterministic clock: cushion absorbs origin stalls; edge never regresses."""

    def setUp(self):
        self.now = 1000.0
        cfg = make_cfg("http://o.example/m.m3u8", start_behind_segments=3, max_cushion_segments=6,
                       min_start_segments=3, publish_window_segments=10, validate_ts=True)
        self.relay = r.Relay(cfg, http_client=object(), clock=lambda: self.now)
        self.seq = 0

    def arrive(self, n=1, dur=3.0):
        for _ in range(n):
            pts = 900000 + int(self.seq * dur * 90000)
            payload = sim.make_segment(pts, dur, tag=str(self.seq).encode())
            job = r.Pending(0, self.seq, f"http://o.example/{self.seq}.ts", f"{self.seq}.ts",
                            dur, False, None, first_seen=self.now, status="done", payload=payload,
                            ts=r.inspect_ts(payload))
            self.relay.pending[(0, self.seq)] = job
            self.seq += 1
        with self.relay.lock:
            self.relay._commit_ready(self.now)
            self.relay._release(self.now)

    def edge(self):
        body = self.relay.playlist()
        if body is None:
            return None
        return body.decode().count("#EXTINF")

    def test_startup_and_stall_absorption(self):
        self.arrive(5)
        self.assertIsNone(self.edge(), "must wait for cushion + MIN_START")
        self.arrive(1)
        self.assertEqual(self.edge(), 3)
        # origin stalls for 9 s: the 3-segment cushion keeps the playlist growing
        published = []
        for _ in range(4):
            self.now += 3.0
            published.append(self.edge())
        self.assertEqual(published, [4, 5, 6, 6])
        self.assertEqual(self.relay.m["underruns"], 1)
        # burst arrives: first one released immediately, the rest rebuild the cushion
        self.arrive(4)
        self.assertEqual(self.edge(), 7)
        self.now += 3.0
        self.assertEqual(self.edge(), 8)
        self.assertEqual(self.relay.m["discontinuities"], 0)

    def test_latency_cap(self):
        self.arrive(6)
        self.assertEqual(self.edge(), 3)
        self.arrive(8)  # cushion would be 11 > MAX_CUSHION 6
        self.assertEqual(len([s for s in self.relay.store.values() if s.released_at is None]), 6)

    def test_pts_jump_marks_discontinuity_and_duplicates_skipped(self):
        self.arrive(6)
        dup = self.relay.store[next(reversed(self.relay.store))].payload
        job = r.Pending(0, 99, "u", "k", 3.0, False, None, first_seen=self.now, status="done",
                        payload=dup, ts=r.inspect_ts(dup))
        self.relay.pending[(0, 99)] = job
        self.seq = 50  # timestamps jump forward 132 s
        self.arrive(1)
        self.assertEqual(self.relay.m["duplicates_skipped"], 1)
        self.assertEqual(self.relay.m["discontinuities"], 1)
        self.assertEqual(self.relay.m["pts_jumps"], 1)

    def test_eviction_protects_published_window(self):
        self.relay.cfg.window_segments = 12
        self.relay.cfg.expired_grace_seconds = 5
        for _ in range(30):
            self.arrive(1)
            self.now += 3.0
            self.edge()
        rel = [s for s in self.relay.store.values() if s.released_at is not None]
        self.assertGreaterEqual(len(rel), 10)
        body = self.relay.playlist().decode()
        for line in body.splitlines():
            if line.startswith("/segment/"):
                self.assertIsNotNone(self.relay.segment(line[9:-3]))
        self.assertLessEqual(len(self.relay.store), 12 + 2)


class LiveDelayTests(unittest.TestCase):
    """LIVE_DELAY_SECONDS: >= 2 minutes of video held back, released 1:1 with real time."""

    def setUp(self):
        self.now = 1000.0
        cfg = make_cfg("http://o.example/m.m3u8", live_delay_seconds=120, live_delay_max_seconds=150,
                       min_start_segments=3, publish_window_segments=30, window_segments=40,
                       max_cache_mb=512, expired_grace_seconds=30)
        self.relay = r.Relay(cfg, http_client=object(), clock=lambda: self.now)
        self.seq = 0
        self.watching = True
        original = self.relay.playlist

        def playlist():
            if self.watching:  # a player is polling the playlist
                self.relay.note_client("10.0.0.2", "playlist")
            return original()
        self.relay.playlist = playlist

    arrive = PacingTests.arrive

    def held(self):
        return sum(s.duration for s in self.relay.store.values() if s.released_at is None)

    def edge(self):
        body = self.relay.playlist()
        return None if body is None else int(body.decode().split("#EXT-X-MEDIA-SEQUENCE:")[1].split()[0]) \
            + body.decode().count("#EXTINF") - 1

    def test_env_defaults_to_two_minutes(self):
        cfg = r.Config.from_env({"HLS_SOURCE_URL": "https://o.example/a.m3u8", "START_BEHIND_SEGMENTS": "4"})
        self.assertEqual((cfg.live_delay_seconds, cfg.live_delay_max_seconds), (120.0, 150.0))
        off = r.Config.from_env({"HLS_SOURCE_URL": "https://o.example/a.m3u8", "LIVE_DELAY_SECONDS": "0"})
        self.assertEqual(off.live_delay_seconds, 0)

    def test_startup_waits_for_delay_then_keeps_it(self):
        self.arrive(42)                      # 126 s collected: not enough for 120 s + 3 segments
        self.assertIsNone(self.relay.playlist())
        self.arrive(1)                       # 129 s
        self.assertIsNotNone(self.relay.playlist())
        self.assertGreaterEqual(self.held(), 120)
        # steady live: one segment in, one segment out; the delay stays >= 120 s
        for _ in range(100):
            self.now += 3.0
            self.arrive(1)
            self.relay.playlist()
            self.assertGreaterEqual(self.held(), 120 - 3.0)
            self.assertLessEqual(self.held(), 150)

    def test_two_minute_outage_is_absorbed(self):
        self.arrive(43)
        start_edge = self.edge()
        edges = []
        for _ in range(40):                  # origin silent for 120 s
            self.now += 3.0
            edges.append(self.edge())
        self.assertEqual(edges, list(range(start_edge + 1, start_edge + 41)))  # never stopped growing
        self.assertEqual(self.relay.m["underruns"], 0)
        # backlog delivered in a burst afterwards: delay rebuilt, capped at 150 s
        self.arrive(60)
        self.assertLessEqual(self.held(), 150)
        self.assertGreaterEqual(self.held(), 120)

    def test_rebuild_while_nobody_watches(self):
        self.arrive(43)
        for _ in range(20):                  # 60 s outage while watched: delay consumed
            self.now += 3.0
            self.edge()
        self.assertLess(self.held(), 75)
        self.watching = False                # viewer leaves; origin keeps delivering 1:1
        self.now += 31.0
        self.arrive(10)
        frozen = self.edge()
        self.assertEqual(self.relay.rebuilding, "idle")
        for _ in range(9):                   # 90 s -> 117 s: publication on hold
            self.now += 3.0
            self.arrive(1)
            self.assertEqual(self.edge(), frozen, "publication must hold while rebuilding")
        self.assertAlmostEqual(self.held(), 117.0)
        for _ in range(20):                  # target reached: back to 1:1, delay kept
            self.now += 3.0
            self.arrive(1)
            self.edge()
            self.assertGreaterEqual(self.held(), 117)
        self.assertIsNone(self.relay.rebuilding)
        self.watching = True                 # viewer returns: normal 1:1 pacing
        before = self.edge()
        self.now += 3.0
        self.arrive(1)
        self.assertEqual(self.edge(), before + 1)
        self.assertEqual(self.relay.m["rebuilds"], 1)
        self.assertEqual(self.relay.m["underruns"], 0)

    def test_rebuild_pauses_while_watching(self):
        self.relay.cfg.rebuild_pause_seconds = 2.0
        self.relay.cfg.rebuild_every_seconds = 300.0
        self.arrive(43)
        for _ in range(20):                  # outage: held drops to ~66 s
            self.now += 3.0
            self.edge()
        for _ in range(400):                 # 20 min of normal origin, viewer watching
            self.now += 3.0
            self.arrive(1)
            self.edge()
        self.assertEqual(self.relay.m["rebuild_pauses"], 5)   # t=0 and then one per 5 min
        self.assertAlmostEqual(self.relay.m["rebuild_pause_seconds"], 10.0)
        self.assertGreater(self.held(), 66 + 6)

    def test_no_rebuild_in_steady_state(self):
        self.relay.cfg.rebuild_pause_seconds = 2.0
        self.arrive(43)
        for _ in range(200):
            self.now += 3.0
            self.arrive(1)
            self.edge()
        self.assertEqual(self.relay.m["rebuild_pauses"], 0)
        self.assertEqual(self.relay.m["rebuilds"], 0)

    def test_ingest_ratio_stable_with_10s_segments(self):
        self.relay.source_target = 10
        self.relay.started = self.now - 1000
        ratios = []
        for _ in range(60):                  # exactly real time, 10 s segments
            self.now += 10.0
            self.arrive(1, dur=10.0)
            ratios.append(self.relay._ingest_ratio(self.now))
            self.now += 3.7                  # sample at an awkward phase too
            ratios.append(self.relay._ingest_ratio(self.now))
            self.now -= 3.7
        steady = ratios[40:]
        self.assertGreaterEqual(min(steady), 0.9, steady)
        self.assertLessEqual(max(steady), 1.1, steady)

    def test_publish_window_and_memory_fit(self):
        self.arrive(43)
        for _ in range(80):
            self.now += 3.0
            self.arrive(1)
            body = self.relay.playlist().decode()
        for line in body.splitlines():
            if line.startswith("/segment/"):
                self.assertIsNotNone(self.relay.segment(line[9:-3]))
        # held (~120-150 s) + published window (30) + grace: bounded
        self.assertLessEqual(len(self.relay.store), 50 + 30 + 12)


# =========================================================================
# Integration tests against the simulated origin
# =========================================================================
class IntegrationTests(unittest.TestCase):
    def assert_complete(self, h, allow_missing=(), from_first=False):
        got = h.committed_indices()
        self.assertTrue(got, "nothing committed")
        self.assertNotIn(-1, got, "committed bytes that the origin never sent (partial/corrupt)")
        self.assertEqual(len(got), len(set(got)), "duplicate segment committed")
        self.assertEqual(got, sorted(got), "segments committed out of order")
        expected = [i for i in h.expected_indices() if i not in allow_missing]
        if from_first:  # relay started while the origin playlist was failing
            expected = [i for i in expected if i >= got[0]]
        self.assertEqual(got, expected)
        check_playlist_sequence(self, h.snapshots)

    def test_baseline(self):
        with Harness(sim.synthetic_timeline(16, SEG)) as h:
            h.run()
            self.assert_complete(h)
            self.assertEqual(h.relay.m["discontinuities"], 0)
            self.assertEqual(h.origin.requests["segment"], 16, "each segment downloaded once")
            self.assertEqual(h.origin.requests.get("master"), 1, "master resolved once, not every poll")

    def test_rotating_tokens_no_duplicates(self):
        with Harness(sim.synthetic_timeline(16, SEG), sim.Faults(rotate_tokens=True)) as h:
            h.run()
            self.assert_complete(h)
            self.assertEqual(h.relay.m["discontinuities"], 0)
            self.assertEqual(len(h.origin.segment_requests), 16)

    def test_interrupted_download_is_retried_and_never_published(self):
        t = sim.synthetic_timeline(14, SEG)
        with Harness(t, sim.Faults(truncate={t[6].name: 2, t[9].name: 1})) as h:
            h.run()
            self.assert_complete(h)
            self.assertGreaterEqual(h.relay.m["incomplete_downloads"], 3)
            self.assertEqual(h.relay.m["discontinuities"], 0)

    def test_missing_segment_gap_does_not_block_later_segments(self):
        t = sim.synthetic_timeline(16, SEG)
        with Harness(t, sim.Faults(missing={t[7].name})) as h:
            h.run(extra=4)
            self.assert_complete(h)
            self.assertEqual(h.relay.m["gaps"], 1)
            self.assertEqual(h.relay.m["discontinuities"], 1)
            after = [s for s in h.commits if s.source_seq == 8][0]
            self.assertTrue(after.discontinuity)
            joined = "".join(h.snapshots[-1:])
            self.assertIn("#EXT-X-DISCONTINUITY", joined)

    def test_sequence_reset_with_reused_names(self):
        a = sim.synthetic_timeline(10, SEG, epoch="a")
        b = sim.synthetic_timeline(10, SEG, epoch="b", pts_start=90000 * 500)
        b[0].discontinuity = True
        with Harness(a + b) as h:
            h.run()
            self.assert_complete(h)
            self.assertGreaterEqual(h.relay.m["resets"], 1)
            self.assertEqual(h.relay.m["discontinuities"], 1)

    def test_reset_without_tag_detected_by_pts(self):
        a = sim.synthetic_timeline(10, SEG, epoch="a")
        b = sim.synthetic_timeline(10, SEG, start_seq=0, epoch="b", pts_start=90000 * 7,
                                   name_fmt="r{seq}.ts")
        with Harness(a + b) as h:
            h.run()
            self.assert_complete(h)
            self.assertEqual(h.relay.m["discontinuities"], 1)
            self.assertEqual(h.relay.m["pts_jumps"], 1)

    def test_source_discontinuity_tag_propagated(self):
        a = sim.synthetic_timeline(8, SEG, epoch="a")
        b = sim.synthetic_timeline(8, SEG, start_seq=8, epoch="b", pts_start=90000 * 900)
        b[0].discontinuity = True
        with Harness(a + b, publish_window_segments=30) as h:
            h.run()
            self.assert_complete(h)
            self.assertEqual(h.snapshots[-1].count("#EXT-X-DISCONTINUITY\n"), 1)

    def test_master_redirect_and_relative_urls(self):
        with Harness(sim.synthetic_timeline(12, SEG), sim.Faults(redirect_master=True)) as h:
            h.run()
            self.assert_complete(h)

    def test_stalled_connection_does_not_block_pipeline(self):
        t = sim.synthetic_timeline(16, SEG)
        with Harness(t, sim.Faults(stall={t[5].name: 1}, stall_seconds=4),
                     segment_read_timeout=0.8) as h:
            h.run()
            self.assert_complete(h)
            self.assertGreaterEqual(h.relay.m["incomplete_downloads"], 1)
            # later segments were downloaded while seg 5 was stalled
            idx = h.origin.segment_requests.index(t[6].name)
            self.assertLess(idx, len(h.origin.segment_requests) - 1)

    def test_parallel_downloads_keep_up_with_slow_connections(self):
        t = sim.synthetic_timeline(24, SEG)
        size = len(t[0].payload)
        faults = sim.Faults(throttle_bps=size / (SEG * 1.3))  # one connection = 0.77x real time
        with Harness(t, faults, max_parallel_downloads=2) as h:
            h.run(extra=5)
            self.assert_complete(h)
            self.assertLess(h.relay.health()["ingest"]["avg_realtime_factor"], 1.0)

    def test_live_delay_end_to_end(self):
        t = sim.synthetic_timeline(30, SEG)
        with Harness(t, live_delay_seconds=4.0, live_delay_max_seconds=6.0, min_start_segments=2) as h:
            began = time.monotonic()
            while h.relay.playlist() is None and time.monotonic() - began < 15:
                time.sleep(0.05)
            waited = time.monotonic() - began
            self.assertGreaterEqual(waited, 4.0 - 4 * SEG)  # origin preloads its 4-segment window
            h.run()
            self.assert_complete(h)
            hb = h.relay.health()["buffer"]
            self.assertEqual(hb["live_delay_target_seconds"], 4.0)
            check_playlist_sequence(self, h.snapshots)

    def test_stale_playlists_ignored(self):
        with Harness(sim.synthetic_timeline(16, SEG), sim.Faults(stale_every=3)) as h:
            h.run()
            self.assert_complete(h)
            self.assertEqual(h.relay.m["resets"], 0)

    def test_playlist_errors_recover_and_are_classified(self):
        t = sim.synthetic_timeline(20, SEG)
        with Harness(t, sim.Faults(playlist_error_until=4.0), window=10) as h:
            h.run()
            self.assert_complete(h, from_first=True)
            self.assertGreaterEqual(len(h.commits), 10)
            health = h.relay.health()
            self.assertGreater(health["source"]["playlist_errors"].get("http_500", 0), 0)
            self.assertEqual(health["ingest"]["errors_by_category"], {})

    def test_segment_redirect_to_private_host_blocked(self):
        t = sim.synthetic_timeline(6, SEG)
        origin = sim.SimOrigin(t, window=4)
        # segment URIs point to "localhost" (resolves to loopback, not the source host)
        origin.faults.redirect_segments_to = f"http://localhost:{origin.port}/"
        origin.begin()
        relay = r.Relay(make_cfg(origin.source_url)).start()
        try:
            time.sleep(3)
            self.assertGreater(relay.m["blocked_urls"], 0)
            self.assertEqual(relay.m["segments_committed"], 0)
            self.assertEqual(origin.requests.get("segment", 0), 0)
        finally:
            relay.stop()
            origin.stop()

    def test_logs_do_not_leak_tokens(self):
        records = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Grab(level=logging.DEBUG)
        r.log.addHandler(handler)
        r.log.setLevel(logging.DEBUG)
        try:
            t = sim.synthetic_timeline(10, SEG)
            with Harness(t, sim.Faults(rotate_tokens=True, truncate={t[4].name: 1},
                                       missing={t[6].name}), log_segments=True) as h:
                h.run()
        finally:
            r.log.removeHandler(handler)
            r.log.setLevel(logging.NOTSET)
        self.assertTrue(records)
        for msg in records:
            self.assertNotIn("token=", msg)
            self.assertNotIn("/seg/", msg)


class HttpServerTests(unittest.TestCase):
    def setUp(self):
        t = sim.synthetic_timeline(30, SEG)
        self.origin = sim.SimOrigin(t, window=6).begin()
        self.relay = r.Relay(make_cfg(self.origin.source_url)).start()
        self.server = r.RelayHTTPServer(("127.0.0.1", 0), self.relay)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        deadline = time.monotonic() + 10
        while self.relay.playlist() is None and time.monotonic() < deadline:
            time.sleep(0.1)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.relay.stop()
        self.origin.stop()

    def get(self, path, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    def test_playlist_segments_health_head(self):
        status, body, headers = self.get("/live.m3u8")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/vnd.apple.mpegurl")
        uri = [ln for ln in body.decode().splitlines() if ln.startswith("/segment/")][-1]
        status, seg, headers = self.get(uri)
        self.assertEqual(status, 200)
        self.assertTrue(r.inspect_ts(seg).ok)
        status, empty, headers = self.get(uri, "HEAD")
        self.assertEqual((status, empty, headers["Content-Length"]), (200, b"", str(len(seg))))
        self.assertEqual(self.get("/segment/000000-1.ts")[0], 404)
        status, body, _ = self.get("/health")
        h = json.loads(body)
        for key in ("source", "ingest", "buffer", "clients"):
            self.assertIn(key, h)
        self.assertNotIn("127.0.0.1:%d" % self.origin.port, body.decode())
        self.assertGreaterEqual(h["clients"]["active"], 1)

    def test_slow_client_does_not_block_others(self):
        slow = socket.create_connection(("127.0.0.1", self.port))
        slow.sendall(b"GET /live.m3u8 HTTP/1.1\r\nHost: x\r\n")  # never finishes headers
        try:
            t0 = time.monotonic()
            self.assertEqual(self.get("/live.m3u8")[0], 200)
            self.assertLess(time.monotonic() - t0, 1.0)
        finally:
            slow.close()

    def test_client_allowlist(self):
        self.relay.cfg.client_networks = [r.ipaddress.ip_network("10.99.0.0/16")]
        self.assertEqual(self.get("/live.m3u8")[0], 403)
        self.assertEqual(self.server.manager.denied, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
