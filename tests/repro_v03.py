"""Reproduce v0.3 defects against the simulated origin (time scaled: 1 s segments).

Usage:  python3 -I tests/repro_v03.py /path/to/v0.3/relay.py [scenario ...]

The v0.3 poll loop is replicated exactly (update(); sleep(POLL)) so it can be
stopped between scenarios. Real v0.3 timings are 3 s segments, POLL=2 s,
HTTP_TIMEOUT=20 s; here everything is scaled by 1/3.
"""
import importlib.util
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hls_sim as sim  # noqa: E402

SCALE = 1 / 3


def load_v03(path, source, **env):
    os.environ.update({"HLS_SOURCE_URL": source, "POLL_INTERVAL": "2", "WINDOW_SEGMENTS": "40",
                       "PUBLISH_WINDOW_SEGMENTS": "30", "START_BEHIND_SEGMENTS": "4",
                       "HTTP_TIMEOUT": "20", "SEGMENT_RETRIES": "3"})
    os.environ.update({k: str(v) for k, v in env.items()})
    spec = importlib.util.spec_from_file_location(f"relay_v03_{time.monotonic_ns()}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.POLL = 2 * SCALE
    mod.TIMEOUT = 20 * SCALE
    logging.getLogger("hls-relay").setLevel(logging.CRITICAL)
    saved = []
    original = mod.save_segment

    def recording(key, payload, *a):
        ok = original(key, payload, *a)
        if ok:
            saved.append(payload)
        return ok

    mod.save_segment = recording
    return mod, saved


def run(mod, origin, seconds):
    stop = time.monotonic() + seconds
    while time.monotonic() < stop and not origin.finished():
        try:
            mod.update()
        except Exception:
            pass
        time.sleep(mod.POLL)
    # drain
    for _ in range(3):
        try:
            mod.update()
        except Exception:
            pass


def analyse(origin, saved, mod):
    index = {e.payload: i for i, e in enumerate(origin.timeline)}
    seq = [index.get(p, -1) for p in saved]
    unique = set(seq)
    avail = origin.available()
    first = seq[0] if seq else 0
    expected = set(range(first, avail)) - {i for i, e in enumerate(origin.timeline)
                                           if e.name in origin.faults.missing}
    lost = sorted(expected - unique)
    dups = len(seq) - len(unique)
    order_violations = sum(1 for a, b in zip(seq, seq[1:]) if b <= a)
    return {"saved": len(seq), "unique": len(unique), "duplicates": dups, "lost": len(lost),
            "order_violations": order_violations,
            "discontinuities": mod.state["discontinuities"],
            "origin_requests": dict(origin.requests)}


def scenario(name, path, timeline, faults=None, seconds=30, master=True, **env):
    origin = sim.SimOrigin(timeline, window=4, faults=faults, master=master).begin()
    mod, saved = load_v03(path, origin.source_url, **env)
    try:
        run(mod, origin, seconds)
        res = analyse(origin, saved, mod)
    finally:
        origin.stop()
    res["scenario"] = name
    return res, mod


def tl(n=40, d=1.0, **kw):
    return sim.synthetic_timeline(n, d, **kw)


def main():
    path = sys.argv[1]
    wanted = set(sys.argv[2:])
    results = []

    def want(n):
        return not wanted or n in wanted

    if want("baseline"):
        results.append(scenario("baseline", path, tl(30))[0])
    if want("tokens"):
        results.append(scenario("rotating_tokens", path, tl(30), sim.Faults(rotate_tokens=True))[0])
    if want("missing"):
        t = tl(30)
        results.append(scenario("one_404_segment", path, t, sim.Faults(missing={t[10].name}))[0])
    if want("reuse"):
        a = tl(15, epoch="a")
        b = tl(15, epoch="b", pts_start=50 * 90000)  # restart: seq 0.. same names, new content
        b[0].discontinuity = True
        results.append(scenario("restart_reuses_names", path, a + b)[0])
    if want("redirect"):
        results.append(scenario("master_redirect", path, tl(20), sim.Faults(redirect_master=True), seconds=12)[0])
    if want("throughput"):
        t = tl(40)
        size = len(t[0].payload)
        # one connection downloads a segment in 0.85x its duration (link ~ 1.18x bitrate)
        results.append(scenario("link_1.18x_bitrate", path, t,
                                sim.Faults(throttle_bps=size / 0.85), seconds=45)[0])
    if want("stall"):
        t = tl(30)
        results.append(scenario("one_stalled_connection", path, t,
                                sim.Faults(stall={t[8].name: 1}, stall_seconds=8))[0])
    if want("srcdisc"):
        a = tl(12, epoch="a")
        b = tl(12, start_seq=12, epoch="b", pts_start=10_000_000)
        b[0].discontinuity = True
        res, mod = scenario("source_discontinuity_tag", path, a + b, seconds=20)
        mod.START_BEHIND = 0
        body, _ = mod.publish()
        res["published_DISCONTINUITY_tags"] = body.decode().count("#EXT-X-DISCONTINUITY\n")
        results.append(res)
    if want("edge"):
        origin = sim.SimOrigin(tl(10), window=4).begin()
        mod, _ = load_v03(path, origin.source_url)
        origin.stop()
        tails = []
        for i in range(10):
            mod.save_segment(mod.identity("m", i, f"u{i}"), b"x" * 188, 3.0, i, f"u{i}", "m", None)
            body, _ = mod.publish()
            tails.append(body.decode().strip().splitlines()[3].split(":")[1])
            tails[-1] = int(tails[-1]) + body.decode().count("#EXTINF") - 1
        results.append({"scenario": "startup_live_edge", "last_published_seq_per_step": tails,
                        "edge_regressed": any(b < a for a, b in zip(tails, tails[1:]))})
    for r in results:
        print(r)


if __name__ == "__main__":
    main()
