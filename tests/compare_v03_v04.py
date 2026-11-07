"""Side-by-side: v0.3 vs v0.4 against identical simulated origins.

Usage:  python3 -I tests/compare_v03_v04.py /path/to/v0.3/relay.py

Time is scaled by 1/3 (1 s segments stand for the real ~3 s ones; POLL=2 s ->
0.67 s; HTTP_TIMEOUT=20 s -> 6.7 s). Origin window: 4 segments (~12 s real).
"""
import importlib.util
import logging
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hls_sim as sim  # noqa: E402
import repro_v03 as v03  # noqa: E402

spec = importlib.util.spec_from_file_location("relay_v04_cmp", os.path.join(HERE, "..", "relay.py"))
r = importlib.util.module_from_spec(spec)
sys.modules["relay_v04_cmp"] = r
spec.loader.exec_module(r)
logging.getLogger("hls-relay").setLevel(logging.CRITICAL)
S = v03.SCALE


def run_v04(timeline, faults, seconds):
    origin = sim.SimOrigin(timeline, window=4, faults=faults).begin()
    cfg = r.Config(source_url=origin.source_url, poll_interval=2 * S, min_poll_interval=0.1,
                   http_timeout=20 * S, playlist_timeout=10 * S, segment_read_timeout=6 * S,
                   segment_give_up_seconds=30 * S, log_segments=False,
                   live_delay_seconds=0).normalize()
    relay = r.Relay(cfg)
    saved = []
    relay.on_commit = lambda seg: saved.append(seg.payload)
    relay.start()
    end = time.monotonic() + seconds
    while time.monotonic() < end and not origin.finished():
        time.sleep(0.2)
    time.sleep(3)
    relay.stop()
    origin.stop()
    index = {e.payload: i for i, e in enumerate(timeline)}
    seq = [index.get(p, -1) for p in saved]
    miss = {i for i, e in enumerate(timeline) if e.name in origin.faults.missing}
    expected = set(range(seq[0] if seq else 0, origin.available())) - miss
    return {"saved": len(seq), "duplicates": len(seq) - len(set(seq)),
            "lost": len(expected - set(seq)), "discontinuities": relay.m["discontinuities"],
            "segment_requests": origin.requests.get("segment", 0),
            "master_requests": origin.requests.get("master", 0)}


def run_v03(path, timeline, faults, seconds):
    res, _ = v03.scenario("x", path, timeline, faults, seconds=seconds)
    return {"saved": res["saved"], "duplicates": res["duplicates"], "lost": res["lost"],
            "discontinuities": res["discontinuities"],
            "segment_requests": res["origin_requests"].get("segment", 0),
            "master_requests": res["origin_requests"].get("master", 0)}


def scenarios():
    def tl(n=30, **kw):
        return sim.synthetic_timeline(n, 1.0, **kw)

    yield "baseline", lambda: (tl(), None, 40)
    yield "rotating_tokens", lambda: (tl(), sim.Faults(rotate_tokens=True), 40)
    t = tl()
    yield "one_404_segment", lambda: (t, sim.Faults(missing={t[10].name}), 40)

    def reuse():
        a = tl(15, epoch="a")
        b = tl(15, epoch="b", pts_start=50 * 90000)
        b[0].discontinuity = True
        return a + b, None, 40
    yield "restart_reuses_names", reuse
    yield "master_redirect", lambda: (tl(20), sim.Faults(redirect_master=True), 25)
    t2 = tl()
    yield "one_stalled_connection", lambda: (t2, sim.Faults(stall={t2[8].name: 1}, stall_seconds=8), 40)
    t3 = tl(40)
    size = len(t3[0].payload)
    yield "link_1.18x_bitrate", lambda: (t3, sim.Faults(throttle_bps=size / 0.85), 50)
    t4 = tl(40)
    yield "per_conn_0.8x_bitrate", lambda: (t4, sim.Faults(throttle_bps=size / 1.25), 50)
    t5 = tl(30)
    yield "truncated_twice", lambda: (t5, sim.Faults(truncate={t5[6].name: 2, t5[12].name: 2}), 40)


def main():
    path = sys.argv[1]
    rows = []
    for name, make in scenarios():
        timeline, faults, secs = make()
        a = run_v03(path, timeline, faults, secs)
        timeline, faults, secs = make()
        b = run_v04(timeline, faults, secs)
        rows.append((name, a, b))
        print(f"{name:24s} v0.3 {a}\n{'':24s} v0.4 {b}", flush=True)
    print("\n| cenário | v0.3 perdidos / duplicados / desc. / downloads | v0.4 perdidos / duplicados / desc. / downloads |")
    print("|---|---|---|")
    for name, a, b in rows:
        print(f"| {name} | {a['lost']} / {a['duplicates']} / {a['discontinuities']} / {a['segment_requests']} "
              f"| {b['lost']} / {b['duplicates']} / {b['discontinuities']} / {b['segment_requests']} |")


if __name__ == "__main__":
    main()
