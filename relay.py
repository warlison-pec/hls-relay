#!/usr/bin/env python3
"""BoxServer HLS Relay v0.4 - authorized single-source HLS (MPEG-TS) relay.

Pipeline (every stage runs in its own thread; none blocks the HTTP server):

  poller   refreshes the media playlist at the cadence of EXT-X-TARGETDURATION,
           detects new segments, sequence resets, stale playlists and gaps
  workers  download segments with bounded concurrency, keep-alive connections,
           Content-Length enforcement, MPEG-TS validation and retry/backoff
  commit   stores segments strictly in source order, assigns a monotonic local
           sequence and decides EXT-X-DISCONTINUITY from the real PTS timeline
           (falls back to sequence numbers when PTS is unavailable)
  release  paces publication through a cushion of START_BEHIND_SEGMENTS so
           short origin stalls are absorbed instead of passed to clients
  http     /live.m3u8, /segment/<id>.ts, /health

Never decrypts, transcodes, refreshes third-party credentials, fabricates video
or hides origin failures.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import math
import os
import random
import re
import secrets
import socket
import ssl
import sys
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlsplit

VERSION = "0.5.2"
MB = 1024 * 1024
PTS_WRAP = 1 << 33
log = logging.getLogger("hls-relay")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# =========================================================================
# Configuration
# =========================================================================
class ConfigError(ValueError):
    pass


def _num(env, name, default, cast, lo=None, hi=None):
    raw = (env.get(name) or "").strip()
    if raw == "":
        value = default
    else:
        try:
            value = cast(raw)
        except ValueError:
            raise ConfigError(f"{name} must be a {cast.__name__}") from None
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(f"{name} must be finite")
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def _bool(env, name, default):
    raw = (env.get(name) or "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on")


def _list(env, name, default):
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        raw = default
    return [item.strip() for item in raw.split(",") if item.strip()]


DEFAULT_CLIENT_CIDRS = ("127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,"
                        "100.64.0.0/10,fd7a:115c:a1e0::/48")


@dataclass
class Config:
    source_url: str
    listen_host: str = "0.0.0.0"
    port: int = 8099
    poll_interval: float = 2.0            # upper bound for playlist reload interval
    min_poll_interval: float = 0.5
    window_segments: int = 40             # retained segments (soft, see grace)
    publish_window_segments: int = 30
    start_behind_segments: int = 4        # target cushion (unpublished segments)
    max_cushion_segments: int = 8         # latency cap: cushion above this is released
    live_delay_seconds: float = 120.0     # >0: cushion measured in SECONDS (overrides the two above)
    live_delay_max_seconds: float = 150.0 # latency cap in delay mode
    live_delay_idle_seconds: float = 30.0 # no client for this long => rebuild the delay for free
    rebuild_pause_seconds: float = 0.0    # >0: while watched, hold publication this long ...
    rebuild_every_seconds: float = 300.0  # ... at most once per this interval (0 pause = off)
    min_start_segments: int = 3
    max_cache_mb: int = 512               # hard memory cap for segment payloads
    http_timeout: float = 20.0            # socket timeout for connect/read
    playlist_timeout: float = 10.0
    segment_read_timeout: float = 0.0     # 0 = auto: clamp(2 x target, 4, HTTP_TIMEOUT)
    segment_retries: int = 3              # attempts per segment
    segment_max_seconds: float = 60.0     # total deadline of one attempt
    segment_give_up_seconds: float = 30.0
    max_parallel_downloads: int = 2
    max_pending_segments: int = 60
    variant_policy: str = "highest"
    master_refresh_seconds: float = 0.0   # 0 = re-resolve master only after errors
    user_agent: str = "BoxServer-HLS-Relay/0.4"
    referer: str = ""
    allowed_client_cidrs: list = field(default_factory=lambda: DEFAULT_CLIENT_CIDRS.split(","))
    source_allowed_hosts: list = field(default_factory=list)
    pts_jump_threshold: float = 1.5
    expired_grace_seconds: float = 30.0
    stale_tolerance_segments: int = 3
    validate_ts: bool = True
    log_format: str = "text"
    log_level: str = "INFO"
    log_segments: bool = True
    max_channels: int = 0                 # channels that can be registered in /admin; 0 = unlimited
    demand_idle_minutes: float = 20.0     # on-demand channels stop after this long without clients
    slate_dir: str = ""                   # "please wait" video for on-demand channels; empty = off

    @classmethod
    def from_env(cls, env=None, require_source: bool = True) -> "Config":
        env = os.environ if env is None else env
        src = (env.get("HLS_SOURCE_URL") or "").strip()
        if src or require_source:
            parts = urlsplit(src) if src else None
            if not src or parts.scheme not in ("http", "https") or not parts.hostname:
                raise ConfigError("HLS_SOURCE_URL must be an authorized HTTP(S) HLS playlist URL")
        behind = _num(env, "START_BEHIND_SEGMENTS", 4, int, 0, 60)
        timeout = _num(env, "HTTP_TIMEOUT", 20.0, float, 3.0, 120.0)
        cfg = cls(
            source_url=src,
            listen_host=(env.get("LISTEN_HOST") or "0.0.0.0").strip(),
            port=_num(env, "PORT", 8099, int, 1, 65535),
            poll_interval=_num(env, "POLL_INTERVAL", 2.0, float, 0.5, 30.0),
            window_segments=_num(env, "WINDOW_SEGMENTS", 40, int, 3, 5000),
            publish_window_segments=_num(env, "PUBLISH_WINDOW_SEGMENTS", 30, int, 3, 2000),
            start_behind_segments=behind,
            max_cushion_segments=_num(env, "MAX_CUSHION_SEGMENTS", 2 * behind, int, 0, 240),
            live_delay_seconds=_num(env, "LIVE_DELAY_SECONDS", 120.0, float, 0.0, 1800.0),
            live_delay_max_seconds=_num(env, "LIVE_DELAY_MAX_SECONDS", 0.0, float, 0.0, 3600.0),
            live_delay_idle_seconds=_num(env, "LIVE_DELAY_IDLE_SECONDS", 30.0, float, 5.0, 3600.0),
            rebuild_pause_seconds=_num(env, "LIVE_DELAY_REBUILD_PAUSE_SECONDS", 0.0, float, 0.0, 10.0),
            rebuild_every_seconds=_num(env, "LIVE_DELAY_REBUILD_EVERY_SECONDS", 300.0, float, 30.0, 86400.0),
            min_start_segments=_num(env, "MIN_START_SEGMENTS", 3, int, 1, 20),
            max_cache_mb=_num(env, "MAX_CACHE_MB", 512, int, 16, 65536),
            http_timeout=timeout,
            playlist_timeout=_num(env, "PLAYLIST_TIMEOUT", min(10.0, timeout), float, 2.0, 120.0),
            segment_read_timeout=_num(env, "SEGMENT_READ_TIMEOUT", 0.0, float, 0.0, 120.0),
            segment_retries=_num(env, "SEGMENT_RETRIES", 3, int, 1, 10),
            segment_max_seconds=_num(env, "SEGMENT_MAX_SECONDS", 60.0, float, 5.0, 600.0),
            segment_give_up_seconds=_num(env, "SEGMENT_GIVE_UP_SECONDS", 30.0, float, 3.0, 600.0),
            max_parallel_downloads=_num(env, "MAX_PARALLEL_DOWNLOADS", 2, int, 1, 4),
            max_pending_segments=_num(env, "MAX_PENDING_SEGMENTS", 60, int, 10, 1000),
            variant_policy=(env.get("HLS_VARIANT_POLICY") or "highest").strip().lower(),
            master_refresh_seconds=_num(env, "MASTER_REFRESH_SECONDS", 0.0, float, 0.0, 86400.0),
            user_agent=(env.get("SOURCE_USER_AGENT") or "").strip() or "BoxServer-HLS-Relay/0.4",
            referer=(env.get("SOURCE_REFERER") or "").strip(),
            allowed_client_cidrs=_list(env, "ALLOWED_CLIENT_CIDRS", DEFAULT_CLIENT_CIDRS),
            source_allowed_hosts=_list(env, "SOURCE_ALLOWED_HOSTS", ""),
            pts_jump_threshold=_num(env, "PTS_JUMP_THRESHOLD", 1.5, float, 0.2, 60.0),
            expired_grace_seconds=_num(env, "EXPIRED_GRACE_SECONDS", 30.0, float, 0.0, 3600.0),
            stale_tolerance_segments=_num(env, "STALE_TOLERANCE_SEGMENTS", 3, int, 0, 100),
            validate_ts=_bool(env, "VALIDATE_TS", True),
            log_format=(env.get("LOG_FORMAT") or "text").strip().lower(),
            log_level=(env.get("LOG_LEVEL") or "INFO").strip().upper(),
            log_segments=_bool(env, "LOG_SEGMENTS", True),
            max_channels=_num(env, "MAX_CHANNELS", 0, int, 0, 9999),
            demand_idle_minutes=_num(env, "DEMAND_IDLE_MINUTES", 20.0, float, 0.5, 1440.0),
            slate_dir=(env.get("SLATE_DIR") or "").strip(),
        )
        return cfg.normalize()

    def normalize(self) -> "Config":
        self.notes = []
        if self.variant_policy not in ("highest", "lowest"):
            raise ConfigError("HLS_VARIANT_POLICY must be 'highest' or 'lowest'")
        if self.log_format not in ("text", "json"):
            raise ConfigError("LOG_FORMAT must be 'text' or 'json'")
        if self.max_cushion_segments < self.start_behind_segments:
            self.max_cushion_segments = self.start_behind_segments
        if self.live_delay_seconds > 0:
            floor = self.live_delay_seconds + 30.0
            if self.live_delay_max_seconds < self.live_delay_seconds + 10.0:
                if self.live_delay_max_seconds:
                    self.notes.append(f"LIVE_DELAY_MAX_SECONDS raised to {floor:g}")
                self.live_delay_max_seconds = floor
            self.notes.append(f"LIVE_DELAY_SECONDS={self.live_delay_seconds:g}: delay mode; "
                              "START_BEHIND_SEGMENTS/MAX_CUSHION_SEGMENTS are ignored")
        need = self.publish_window_segments + self.max_cushion_segments + 2
        if self.window_segments < need:
            self.notes.append(f"WINDOW_SEGMENTS raised from {self.window_segments} to {need} "
                              "(PUBLISH_WINDOW + MAX_CUSHION + 2)")
            self.window_segments = need
        try:
            self.client_networks = [ipaddress.ip_network(c, strict=False)
                                    for c in self.allowed_client_cidrs]
        except ValueError as exc:
            raise ConfigError(f"ALLOWED_CLIENT_CIDRS: {exc}") from None
        return self

    def public_view(self) -> dict:
        """Configuration without URLs, hosts or headers."""
        keys = ("port", "poll_interval", "window_segments", "publish_window_segments",
                "start_behind_segments", "max_cushion_segments", "live_delay_seconds",
                "live_delay_max_seconds", "live_delay_idle_seconds", "rebuild_pause_seconds",
                "rebuild_every_seconds", "max_cache_mb",
                "http_timeout", "playlist_timeout", "segment_read_timeout", "segment_retries",
                "segment_give_up_seconds", "max_parallel_downloads", "variant_policy",
                "master_refresh_seconds", "pts_jump_threshold", "expired_grace_seconds",
                "validate_ts", "log_format")
        out = {k: getattr(self, k) for k in keys}
        out["allowed_client_cidrs"] = len(self.allowed_client_cidrs)
        out["source_allowed_hosts"] = len(self.source_allowed_hosts)
        return out


# =========================================================================
# Structured logging and redaction
# =========================================================================
_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.I)


def redact_url(url: str) -> str:
    """host + last path component only: no query (tokens), no directories
    (Xtream-style user/password paths), no userinfo."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or "?"
    except ValueError:
        return "<url>"
    name = parts.path.rsplit("/", 1)[-1]
    if len(name) > 40:
        name = name[:16] + "~" + name[-12:]
    return f"{host}/~/{name}" if name else f"{host}/"


def safe_text(value) -> str:
    return _URL_RE.sub(lambda m: redact_url(m.group(0)), str(value))[:300]


class EventLog:
    def __init__(self, fmt: str = "text"):
        self.fmt = fmt

    @staticmethod
    def _value(v):
        if isinstance(v, float):
            v = round(v, 3)
        s = str(v)
        if s == "" or any(c in s for c in ' "='):
            s = json.dumps(s)
        return s

    def __call__(self, event: str, level: int = logging.INFO, **fields):
        if not log.isEnabledFor(level):
            return
        if self.fmt == "json":
            msg = json.dumps({"event": event, **fields}, default=str, separators=(",", ":"))
        else:
            msg = "event=" + event + "".join(f" {k}={self._value(v)}" for k, v in fields.items())
        log.log(level, msg)


ev = EventLog()


# =========================================================================
# HTTP client: keep-alive, validated redirects, deadlines, integrity checks
# =========================================================================
class FetchError(Exception):
    def __init__(self, kind: str, detail: str = "", status: int | None = None,
                 partial: int = 0, expected: int | None = None):
        super().__init__(f"{kind}{' ' + str(status) if status else ''}: {safe_text(detail)}")
        self.kind = kind
        self.status = status
        self.partial = partial
        self.expected = expected

    @property
    def category(self) -> str:
        return f"http_{self.status}" if self.kind == "http" else self.kind

    @property
    def retryable(self) -> bool:
        if self.kind in ("timeout", "incomplete", "connection", "invalid_ts", "empty"):
            return True
        if self.kind == "http":
            return self.status in (404, 408, 425, 429, 500, 502, 503, 504)
        return False  # blocked, too_large, redirect


class UrlPolicy:
    """SSRF guard. The configured source host is always trusted. Any other host
    must be listed in SOURCE_ALLOWED_HOSTS (exact or '.suffix') or, when the list
    is empty, resolve only to public (globally routable) addresses."""

    def __init__(self, source_url: str, allowed_hosts=(), resolver=socket.getaddrinfo):
        self.source_host = (urlsplit(source_url).hostname or "").lower()
        self.allowed = [h.lower() for h in allowed_hosts]
        self.resolver = resolver
        self._cache = {}
        self._lock = threading.Lock()

    def _listed(self, host: str) -> bool:
        for item in self.allowed:
            if item.startswith("."):
                if host.endswith(item) or host == item[1:]:
                    return True
            elif host == item:
                return True
        return False

    def check(self, url: str) -> None:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            raise FetchError("blocked", "malformed URL") from None
        if parts.scheme not in ("http", "https"):
            raise FetchError("blocked", f"scheme {parts.scheme!r}")
        host = (parts.hostname or "").lower()
        if not host:
            raise FetchError("blocked", "URL without host")
        if host == self.source_host:
            return
        if parts.username or parts.password:
            raise FetchError("blocked", "credentials in URL for non-source host")
        if self.allowed:
            if self._listed(host):
                return
            raise FetchError("blocked", f"host {host} not in SOURCE_ALLOWED_HOSTS")
        if not self._public(host, port or (443 if parts.scheme == "https" else 80)):
            raise FetchError("blocked", f"non-public destination {host}")

    def _public(self, host: str, port: int) -> bool:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(host)
            if hit and now - hit[1] < 300:
                return hit[0]
        try:
            addrs = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                infos = self.resolver(host, port, proto=socket.IPPROTO_TCP)
            except OSError as exc:
                raise FetchError("connection", f"DNS {type(exc).__name__}") from None
            addrs = [ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos]
        ok = bool(addrs)
        for addr in addrs:
            if addr.version == 6 and addr.ipv4_mapped:
                addr = addr.ipv4_mapped
            ok = ok and addr.is_global
        with self._lock:
            self._cache[host] = (ok, now)
        return ok


class HttpClient:
    MAX_REDIRECTS = 5

    def __init__(self, cfg: Config, policy: UrlPolicy):
        self.cfg = cfg
        self.policy = policy
        self._tls = ssl.create_default_context()
        self._local = threading.local()

    def _pool(self) -> dict:
        pool = getattr(self._local, "pool", None)
        if pool is None:
            pool = self._local.pool = {}
        return pool

    def _drop(self, key) -> None:
        conn = self._pool().pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def close_thread(self) -> None:
        for key in list(self._pool()):
            self._drop(key)

    def _connection(self, key, timeout: float, fresh: bool):
        pool = self._pool()
        if fresh:
            self._drop(key)
        conn = pool.get(key)
        reused = conn is not None
        if conn is None:
            scheme, host, port = key
            if scheme == "https":
                conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=self._tls)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=timeout)
            pool[key] = conn
        else:
            conn.timeout = timeout
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
        return conn, reused

    def get(self, url: str, *, timeout: float, deadline_s: float, max_bytes: int):
        """Returns (body, final_url, seconds). Raises FetchError."""
        start = time.monotonic()
        deadline = start + deadline_s
        current = url
        headers = {"User-Agent": self.cfg.user_agent, "Accept": "*/*",
                   "Accept-Encoding": "identity", "Connection": "keep-alive"}
        if self.cfg.referer:
            headers["Referer"] = self.cfg.referer
        for _hop in range(self.MAX_REDIRECTS + 1):
            self.policy.check(current)
            parts = urlsplit(current)
            key = (parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
            target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            resp = None
            for attempt in (0, 1):
                conn, reused = self._connection(key, timeout, fresh=attempt == 1)
                try:
                    conn.request("GET", target, headers=headers)
                    resp = conn.getresponse()
                    break
                except (http.client.RemoteDisconnected, http.client.BadStatusLine,
                        http.client.CannotSendRequest, http.client.ResponseNotReady,
                        ConnectionResetError, BrokenPipeError) as exc:
                    self._drop(key)
                    if reused and attempt == 0:
                        continue  # stale keep-alive socket: retry once on a new one
                    raise FetchError("connection", type(exc).__name__) from None
                except TimeoutError:
                    self._drop(key)
                    raise FetchError("timeout", "connect/headers") from None
                except ssl.SSLError as exc:
                    self._drop(key)
                    raise FetchError("connection", f"TLS {type(exc).__name__}") from None
                except OSError as exc:
                    self._drop(key)
                    raise FetchError("connection", type(exc).__name__) from None
            status = resp.status
            if status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                self._drop(key)
                if not location:
                    raise FetchError("http", "redirect without Location", status=status)
                current = urljoin(current, location.strip())
                continue
            if status != 200:
                self._drop(key)
                raise FetchError("http", resp.reason or "", status=status)
            body = self._read_body(resp, key, deadline, max_bytes)
            if resp.will_close:
                self._drop(key)
            return body, current, time.monotonic() - start
        raise FetchError("redirect", "too many redirects")

    def _read_body(self, resp, key, deadline: float, max_bytes: int) -> bytes:
        raw = resp.getheader("Content-Length")
        expected = int(raw) if raw and raw.strip().isdigit() else None
        if expected is not None and expected > max_bytes:
            self._drop(key)
            raise FetchError("too_large", f"{expected} bytes")
        chunks, total = [], 0
        try:
            while True:
                if time.monotonic() > deadline:
                    self._drop(key)
                    raise FetchError("timeout", "total deadline", partial=total, expected=expected)
                chunk = resp.read1(262144)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    self._drop(key)
                    raise FetchError("too_large", f">{max_bytes} bytes")
                chunks.append(chunk)
        except http.client.IncompleteRead as exc:
            self._drop(key)
            total += len(exc.partial)
            raise FetchError("incomplete", f"{total} of {expected}", partial=total,
                             expected=expected) from None
        except TimeoutError:
            self._drop(key)
            raise FetchError("timeout", "body read stalled", partial=total, expected=expected) from None
        except (OSError, http.client.HTTPException) as exc:
            self._drop(key)
            raise FetchError("incomplete" if total else "connection", type(exc).__name__,
                             partial=total, expected=expected) from None
        # http.client returns b"" (no exception) on early EOF when reading in chunks.
        if expected is not None and total != expected:
            self._drop(key)
            raise FetchError("incomplete", f"{total} of {expected}", partial=total, expected=expected)
        return b"".join(chunks)


# =========================================================================
# Playlist parsing
# =========================================================================
class PlaylistError(ValueError):
    pass


_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def parse_attrs(text: str) -> dict:
    return {k: v.strip('"') for k, v in _ATTR_RE.findall(text)}


def path_key(url: str) -> str:
    """Segment file name only: ignores query tokens, rotating directories and
    CDN edge host changes, which must not look like a new segment."""
    return urlsplit(url).path.rsplit("/", 1)[-1]


@dataclass
class SourceItem:
    seq: int
    uri: str
    key: str
    duration: float
    discontinuity: bool
    pdt: str | None


@dataclass
class MediaPlaylist:
    target_duration: float
    media_sequence: int
    discontinuity_sequence: int
    items: list
    endlist: bool


def _check_scheme(url: str) -> str:
    if urlsplit(url).scheme not in ("http", "https"):
        raise PlaylistError("unsupported URL scheme in playlist")
    return url


def parse_master(text: str, base: str):
    variants, alt_audio = [], 0
    lines = [ln.strip() for ln in text.splitlines()]
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = parse_attrs(line.split(":", 1)[1])
            if attrs.get("TYPE") == "AUDIO" and attrs.get("URI"):
                alt_audio += 1
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = parse_attrs(line.split(":", 1)[1])
        try:
            bandwidth = int(attrs.get("BANDWIDTH", "0") or 0)
        except ValueError:
            bandwidth = 0
        for candidate in lines[i + 1:]:
            if candidate and not candidate.startswith("#"):
                variants.append((bandwidth, _check_scheme(urljoin(base, candidate)), attrs))
                break
    if not variants:
        raise PlaylistError("master playlist has no variants")
    return variants, alt_audio


def parse_media_playlist(text: str, base: str) -> MediaPlaylist:
    if not text.lstrip().startswith("#EXTM3U"):
        raise PlaylistError("response is not an M3U8 playlist")
    target = None
    media_seq = 0
    disc_seq = 0
    endlist = False
    items = []
    duration = None
    disc = False
    pdt = None
    try:
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#EXT-X-TARGETDURATION:"):
                target = float(line.split(":", 1)[1])
            elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                media_seq = int(line.split(":", 1)[1])
            elif line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
                disc_seq = int(line.split(":", 1)[1])
            elif line == "#EXT-X-DISCONTINUITY":
                disc = True
            elif line.startswith("#EXTINF:"):
                duration = float(line[8:].split(",", 1)[0])
                if not math.isfinite(duration) or duration <= 0 or duration > 600:
                    raise PlaylistError("invalid EXTINF duration")
            elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                pdt = line.split(":", 1)[1]
            elif line.startswith("#EXT-X-KEY:"):
                method = parse_attrs(line.split(":", 1)[1]).get("METHOD", "")
                if method.upper() != "NONE":
                    raise PlaylistError("encrypted HLS (EXT-X-KEY) is not supported")
            elif line.startswith("#EXT-X-MAP:"):
                raise PlaylistError("fMP4 HLS (EXT-X-MAP) is not supported")
            elif line.startswith("#EXT-X-BYTERANGE"):
                raise PlaylistError("byte-range HLS is not supported")
            elif line == "#EXT-X-ENDLIST":
                endlist = True
            elif line.startswith("#"):
                continue
            else:
                if duration is None:
                    raise PlaylistError("segment URI without EXTINF")
                uri = _check_scheme(urljoin(base, line))
                items.append(SourceItem(media_seq + len(items), uri, path_key(uri),
                                        duration, disc, pdt))
                duration, disc, pdt = None, False, None
    except PlaylistError:
        raise
    except ValueError as exc:
        raise PlaylistError(f"malformed playlist: {exc}") from None
    if target is None:
        target = math.ceil(max((it.duration for it in items), default=6.0))
    return MediaPlaylist(target, media_seq, disc_seq, items, endlist)


# =========================================================================
# MPEG-TS inspection (structure, PTS range, keyframe at segment start)
# =========================================================================
@dataclass
class TsInfo:
    ok: bool
    packets: int = 0
    sync_errors: int = 0
    tail_bytes: int = 0
    video_pid: int | None = None
    video_codec: str | None = None
    min_pts: int | None = None
    max_pts: int | None = None
    starts_with_keyframe: bool | None = None
    cc_errors: int = 0
    audio_streams: int = 0
    error: str = ""


_VIDEO_TYPES = {0x1B: "h264", 0x24: "hevc", 0x02: "mpeg2", 0x01: "mpeg1"}
_AUDIO_TYPES = {0x03, 0x04, 0x0F, 0x11, 0x81, 0x87, 0x06}


def _read_pts(b) -> int:
    return (((b[0] >> 1) & 0x07) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | (b[3] << 7) | (b[4] >> 1)


def pts_delta(a: int, b: int) -> float:
    """Signed seconds from b to a, 33-bit wrap aware."""
    d = (a - b) % PTS_WRAP
    if d > PTS_WRAP // 2:
        d -= PTS_WRAP
    return d / 90000.0


def _write_pts(buf, i: int, value: int):
    buf[i] = (buf[i] & 0xF0) | (((value >> 30) & 0x07) << 1) | 1
    buf[i + 1] = (value >> 22) & 0xFF
    buf[i + 2] = (((value >> 15) & 0x7F) << 1) | 1
    buf[i + 3] = (value >> 7) & 0xFF
    buf[i + 4] = ((value & 0x7F) << 1) | 1


def shift_ts(data: bytes, shift: int) -> bytes:
    """Copy of an MPEG-TS buffer with every PCR, PTS and DTS moved by `shift` (90 kHz)."""
    out = bytearray(data)
    for off in range(0, len(out) - 187, 188):
        if out[off] != 0x47:
            continue
        b1 = out[off + 1]
        pid = ((b1 & 0x1F) << 8) | out[off + 2]
        afc = (out[off + 3] >> 4) & 0x03
        p = off + 4
        if afc & 0x02:
            alen = out[p]
            if alen >= 7 and out[p + 1] & 0x10:
                q = p + 2
                pcr = (out[q] << 25) | (out[q + 1] << 17) | (out[q + 2] << 9) | (out[q + 3] << 1) | (out[q + 4] >> 7)
                pcr = (pcr + shift) % PTS_WRAP
                out[q], out[q + 1] = (pcr >> 25) & 0xFF, (pcr >> 17) & 0xFF
                out[q + 2], out[q + 3] = (pcr >> 9) & 0xFF, (pcr >> 1) & 0xFF
                out[q + 4] = ((pcr & 1) << 7) | (out[q + 4] & 0x7F)
            p += 1 + alen
        if not afc & 0x01 or not b1 & 0x40 or pid in (0, 0x1FFF) or p + 14 > off + 188:
            continue
        if out[p:p + 3] != b"\x00\x00\x01":
            continue
        sid = out[p + 3]
        if not (0xC0 <= sid <= 0xEF or sid == 0xBD):
            continue
        flags = out[p + 7] >> 6
        if flags & 0x02:
            _write_pts(out, p + 9, (_read_pts(out[p + 9:p + 14]) + shift) % PTS_WRAP)
        if flags == 0x03 and p + 19 <= off + 188:
            _write_pts(out, p + 14, (_read_pts(out[p + 14:p + 19]) + shift) % PTS_WRAP)
    return bytes(out)


def _nal_types(es: bytes, codec: str | None) -> set:
    types = set()
    i = es.find(b"\x00\x00\x01")
    while i != -1 and i + 3 < len(es):
        b = es[i + 3]
        types.add(((b >> 1) & 0x3F) if codec == "hevc" else (b & 0x1F))
        i = es.find(b"\x00\x00\x01", i + 3)
    return types


def inspect_ts(data: bytes) -> TsInfo:
    n = len(data)
    if n < 188:
        return TsInfo(False, error="shorter than one TS packet")
    if data[0] != 0x47:
        return TsInfo(False, error="no MPEG-TS sync byte at offset 0")
    packets, tail = divmod(n, 188)
    mv = memoryview(data)
    pmt_pid = None
    video_pid = None
    codec = None
    audio = set()
    cc = {}
    cc_errors = 0
    sync_errors = 0
    first_pts = None
    lo = hi = None
    first_es = bytearray()
    collecting = False
    first_done = False
    for off in range(0, packets * 188, 188):
        if data[off] != 0x47:
            sync_errors += 1
            continue
        b1, b2, b3 = data[off + 1], data[off + 2], data[off + 3]
        pid = ((b1 & 0x1F) << 8) | b2
        if pid == 0x1FFF:
            continue
        pusi = b1 & 0x40
        afc = (b3 >> 4) & 0x03
        p = off + 4
        disc_indicator = False
        if afc & 0x02:
            alen = data[p]
            if alen and p + 1 < off + 188:
                disc_indicator = bool(data[p + 1] & 0x80)
            p += 1 + alen
        if not afc & 0x01:
            continue
        counter = b3 & 0x0F
        prev = cc.get(pid)
        if prev is not None and not disc_indicator and counter != prev and counter != ((prev + 1) & 0x0F):
            cc_errors += 1
        cc[pid] = counter
        if p >= off + 188:
            continue
        payload = mv[p:off + 188]
        if pid == 0 and pusi and pmt_pid is None:
            sec = payload[1 + payload[0]:]
            if len(sec) >= 12:
                length = ((sec[1] & 0x0F) << 8) | sec[2]
                for i in range(8, min(3 + length - 4, len(sec) - 3), 4):
                    if (sec[i] << 8) | sec[i + 1]:
                        pmt_pid = ((sec[i + 2] & 0x1F) << 8) | sec[i + 3]
                        break
        elif pid == pmt_pid and pusi and video_pid is None and not audio:
            sec = payload[1 + payload[0]:]
            if len(sec) >= 12:
                length = ((sec[1] & 0x0F) << 8) | sec[2]
                end = min(3 + length - 4, len(sec))
                i = 12 + (((sec[10] & 0x0F) << 8) | sec[11])
                while i + 5 <= end:
                    stype = sec[i]
                    epid = ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
                    if stype in _VIDEO_TYPES and video_pid is None:
                        video_pid, codec = epid, _VIDEO_TYPES[stype]
                    elif stype in _AUDIO_TYPES:
                        audio.add(epid)
                    i += 5 + (((sec[i + 3] & 0x0F) << 8) | sec[i + 4])
        elif pid == video_pid and video_pid is not None:
            if pusi:
                if collecting:
                    collecting, first_done = False, True
                if len(payload) >= 14 and payload[0] == 0 and payload[1] == 0 and payload[2] == 1:
                    if payload[7] & 0x80:
                        pts = _read_pts(payload[9:14])
                        if first_pts is None:
                            first_pts, lo, hi = pts, 0, 0
                        else:
                            rel = pts_delta(pts, first_pts) * 90000.0
                            lo, hi = min(lo, rel), max(hi, rel)
                    if not first_done:
                        collecting = True
                        first_es += payload[9 + payload[8]:]
            elif collecting and len(first_es) < 262144:
                first_es += payload
    info = TsInfo(ok=True, packets=packets, sync_errors=sync_errors, tail_bytes=tail,
                  video_pid=video_pid, video_codec=codec, cc_errors=cc_errors,
                  audio_streams=len(audio))
    if first_pts is not None:
        info.min_pts = int(first_pts + lo) % PTS_WRAP
        info.max_pts = int(first_pts + hi) % PTS_WRAP
    if first_es and codec in ("h264", "hevc"):
        types = _nal_types(bytes(first_es), codec)
        info.starts_with_keyframe = (5 in types) if codec == "h264" else bool(types & set(range(16, 22)))
    if sync_errors or tail:
        info.ok = False
        info.error = f"sync_errors={sync_errors} tail_bytes={tail} (truncated or corrupt)"
    elif pmt_pid is None:
        info.ok = False
        info.error = "no PAT/PMT in segment"
    return info


# =========================================================================
# Relay core
# =========================================================================
@dataclass
class Pending:
    epoch: int
    seq: int
    uri: str
    key: str
    duration: float
    src_disc: bool
    pdt: str | None
    first_seen: float
    listed: bool = True
    status: str = "queued"          # queued | downloading | done | failed
    attempts: int = 0
    next_try: float = 0.0
    payload: bytes | None = None
    ts: TsInfo | None = None
    dl_seconds: float = 0.0
    error: str = ""
    gap_before: int = 0


@dataclass
class Stored:
    local_seq: int
    name: str
    payload: bytes
    duration: float
    discontinuity: bool
    dseq: int
    epoch: int
    source_seq: int
    pdt: str | None
    committed_at: float
    released_at: float | None = None
    left_window_at: float | None = None
    max_pts: int | None = None
    min_pts: int | None = None


class Relay:
    def __init__(self, cfg: Config, http_client=None, clock=time.monotonic, channel: str = "1"):
        self.cfg = cfg
        self.channel = str(channel)
        self.clock = clock
        self.policy = UrlPolicy(cfg.source_url, cfg.source_allowed_hosts)
        self.http = http_client or HttpClient(cfg, self.policy)
        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        self.stop_event = threading.Event()
        self.instance = secrets.token_hex(3)
        self.started = clock()
        self.started_wall = utcnow()
        # source side
        self.media_url = None
        self.need_resolve = True
        self.resolved_at = None
        self.variant = {"master": None, "variants": 0, "selected_bandwidth": None,
                        "policy": cfg.variant_policy, "alt_audio_renditions": 0}
        self.source_target = None
        self.source_window = 0
        self.source_media_sequence = None
        self.epoch = 0
        self.high_seq = None
        self.path_by_seq = OrderedDict()
        self.prev_playlist_last = None
        self.pending = OrderedDict()
        self.endlist_logged = False
        self.path_check = True
        self.path_resets = deque(maxlen=8)
        # stored / published side
        self.store = OrderedDict()
        self.bytes = 0
        self.next_local = int(time.time())   # monotonic across container restarts
        self.dseq = 0
        self.slate = None                 # shown until the first real segment is published
        self.slate_base = self.next_local
        self.slate_head = None            # first sequence of the last slate playlist handed out
        self.bridge = None                # (first real local_seq, real segments in the first playlist)
        self.slate_anchor = None          # 90 kHz PTS given to slate_base; None = not yet, False = unaligned
        self.slate_cache = OrderedDict()  # (index, loop) -> slate payload with shifted timestamps
        self.last_committed = None
        self.recent_hashes = OrderedDict()
        self.release_started = False
        self.next_release_at = 0.0
        self.target_out = 1
        self.underrun_since = None
        self.rebuilding = None            # None | "idle" while holding publication
        self.last_rebuild_pause = None
        # download control
        self.parallel_limit = cfg.max_parallel_downloads
        self.inflight = 0
        # observability
        self.clients = OrderedDict()
        self.m = {
            "playlist_fetches": 0, "playlist_ok": 0, "playlist_errors": {},
            "consecutive_playlist_failures": 0, "master_resolves": 0,
            "segments_discovered": 0, "segments_committed": 0, "segments_published": 0,
            "download_attempts": 0, "downloads_ok": 0, "download_failures": 0,
            "incomplete_downloads": 0, "invalid_ts": 0, "segment_errors": {},
            "retries": 0, "segments_dropped": 0, "gaps": 0, "missing_segments": 0,
            "duplicates_skipped": 0, "resets": 0, "stale_playlists": 0,
            "discontinuities": 0, "pts_jumps": 0, "source_discontinuities": 0,
            "cc_errors": 0, "keyframe_start": 0, "keyframe_checked": 0,
            "forced_evictions": 0, "evictions": 0, "underruns": 0, "underrun_seconds": 0.0,
            "pending_overflow": 0, "throttle_events": 0, "blocked_urls": 0,
            "rebuilds": 0, "rebuild_pauses": 0, "rebuild_pause_seconds": 0.0,
            "clients_denied": 0, "client_segment_404": 0,
        }
        self.dl_samples = deque(maxlen=120)  # (dl_seconds, bytes, duration)
        self.ingest = deque()                # (t, duration) of committed segments
        self.last_commit_at = None
        self.last_new_item_at = None
        self.last_playlist_ok_at = None
        self.last_playlist_error = None
        self.last_segment_error = None
        self.state = "starting"
        self.state_since = clock()
        self.last_stall_log = 0.0
        self.last_summary = clock()
        self.on_commit = None  # optional callback(Stored), used by tests

    def _ev(self, event, level=logging.INFO, **fields):
        ev(event, level, ch=self.channel, **fields)

    # ------------------------------------------------------------ threads
    def start(self):
        threading.Thread(target=self._poll_loop, name="poller", daemon=True).start()
        for i in range(self.cfg.max_parallel_downloads):
            threading.Thread(target=self._worker_loop, name=f"download-{i}", daemon=True).start()
        return self

    def stop(self):
        self.stop_event.set()
        with self.cv:
            self.cv.notify_all()

    # ------------------------------------------------------------ poller
    def _resolve(self):
        cfg = self.cfg
        url = cfg.source_url
        for depth in range(4):
            body, final, _ = self.http.get(url, timeout=cfg.playlist_timeout,
                                           deadline_s=cfg.playlist_timeout * 2, max_bytes=4 * MB)
            text = body.decode("utf-8-sig", errors="replace")
            if not text.lstrip().startswith("#EXTM3U"):
                raise PlaylistError("response is not an M3U8 playlist")
            if "#EXT-X-STREAM-INF:" not in text:
                with self.lock:
                    self.media_url = final
                    self.need_resolve = False
                    self.resolved_at = self.clock()
                    self.variant["master"] = depth > 0
                    self.m["master_resolves"] += 1
                return text, final
            variants, alt_audio = parse_master(text, final)
            variants.sort(key=lambda v: v[0], reverse=cfg.variant_policy != "lowest")
            bandwidth, url, attrs = variants[0]
            with self.lock:
                self.variant.update(variants=len(variants), selected_bandwidth=bandwidth,
                                    alt_audio_renditions=alt_audio,
                                    resolution=attrs.get("RESOLUTION"), codecs=attrs.get("CODECS"))
            if alt_audio and attrs.get("AUDIO"):
                self._ev("alternate_audio_rendition", logging.WARNING,
                   detail="variant uses a separate audio playlist; only the variant TS is relayed")
        raise PlaylistError("too many nested master playlists")

    def _fetch_media_playlist(self) -> MediaPlaylist:
        cfg = self.cfg
        with self.lock:
            self.m["playlist_fetches"] += 1
            refresh = (cfg.master_refresh_seconds and self.resolved_at is not None and
                       self.clock() - self.resolved_at > cfg.master_refresh_seconds)
            need = self.need_resolve or self.media_url is None or refresh
            media_url = self.media_url
        if need:
            text, final = self._resolve()
        else:
            body, final, _ = self.http.get(media_url, timeout=cfg.playlist_timeout,
                                           deadline_s=cfg.playlist_timeout * 2, max_bytes=4 * MB)
            text = body.decode("utf-8-sig", errors="replace")
        if "#EXT-X-STREAM-INF:" in text:
            with self.lock:
                self.need_resolve = True
            raise PlaylistError("media URL returned a master playlist")
        return parse_media_playlist(text, final)

    def _poll_loop(self):
        failures = 0
        while not self.stop_event.is_set():
            t0 = self.clock()
            delay = self.cfg.poll_interval
            try:
                playlist = self._fetch_media_playlist()
            except (FetchError, PlaylistError) as exc:
                failures += 1
                self._on_playlist_error(exc, failures)
                delay = min(self.cfg.poll_interval * (2 ** min(failures - 1, 3)), 15.0)
            except Exception as exc:  # never let the poller die
                failures += 1
                self._on_playlist_error(exc, failures)
                delay = min(self.cfg.poll_interval * 2, 15.0)
            else:
                failures = 0
                with self.cv:
                    self._on_playlist(playlist, self.clock())
                    self.cv.notify_all()
                delay = max(self.cfg.min_poll_interval,
                            min(self.cfg.poll_interval, playlist.target_duration / 2.0))
            with self.cv:
                now = self.clock()
                self._commit_ready(now)
                self._release(now)
                self._update_state(now)
                self.cv.notify_all()
            self.stop_event.wait(max(0.05, delay - (self.clock() - t0)))
        getattr(self.http, "close_thread", lambda: None)()

    def _on_playlist_error(self, exc, failures):
        if isinstance(exc, FetchError):
            category = exc.category
            if exc.kind == "blocked":
                self.m["blocked_urls"] += 1
            if exc.status in (401, 403, 404, 410) or exc.kind in ("blocked", "redirect"):
                self.need_resolve = True
        elif isinstance(exc, PlaylistError):
            category = "invalid_playlist"
            self.need_resolve = True
        else:
            category = f"internal_{type(exc).__name__}"
        with self.lock:
            errs = self.m["playlist_errors"]
            errs[category] = errs.get(category, 0) + 1
            self.m["consecutive_playlist_failures"] = failures
            self.last_playlist_error = {"at": utcnow(), "category": category, "detail": safe_text(exc)}
        self._ev("playlist_error", logging.WARNING, category=category, consecutive=failures,
           detail=safe_text(exc))

    def _on_playlist(self, pl: MediaPlaylist, now: float):
        self.m["playlist_ok"] += 1
        self.m["consecutive_playlist_failures"] = 0
        self.last_playlist_ok_at = now
        self.source_target = pl.target_duration
        self.source_window = len(pl.items)
        self.source_media_sequence = pl.media_sequence
        if pl.endlist and not self.endlist_logged:
            self.endlist_logged = True
            self._ev("source_endlist", logging.WARNING, detail="origin playlist has EXT-X-ENDLIST")
        if pl.items:
            self._reconcile(pl, now)

    def _reconcile(self, pl: MediaPlaylist, now: float):
        cfg = self.cfg
        items = pl.items
        last = items[-1].seq
        if self.high_seq is not None:
            reason = None
            for it in items if self.path_check else ():
                known = self.path_by_seq.get(it.seq)
                if known is not None and known != it.key:
                    reason = "path_changed_for_known_sequence"
                    break
            if reason == "path_changed_for_known_sequence":
                self.path_resets.append(now)
                recent = [t for t in self.path_resets if now - t < 120]
                if len(recent) >= 3:
                    # file names rotate on every playlist (token in the name):
                    # rely on MEDIA-SEQUENCE rewinds and PTS instead
                    self.path_check = False
                    reason = None
                    self._ev("path_reset_detection_disabled", logging.WARNING,
                       detail="segment file names change for the same sequence on every refresh")
            if reason is None and last < self.high_seq - cfg.stale_tolerance_segments:
                reason = "media_sequence_rewind"
            if reason is None and last < self.high_seq:
                self.m["stale_playlists"] += 1
                self._ev("stale_playlist", logging.DEBUG, playlist_last=last, known_last=self.high_seq)
                return
            if reason:
                self.epoch += 1
                self.m["resets"] += 1
                self._ev("source_reset", logging.WARNING, reason=reason, previous_last_seq=self.high_seq,
                   new_first_seq=items[0].seq, epoch=self.epoch)
                self.high_seq = None
                self.path_by_seq.clear()
        listed = set()
        for it in items:
            listed.add(it.seq)
            if self.high_seq is not None and it.seq <= self.high_seq:
                job = self.pending.get((self.epoch, it.seq))
                if job is not None and job.status == "queued":
                    job.uri = it.uri  # follow rotating tokens
                continue
            gap = 0
            if self.high_seq is not None and it.seq > self.high_seq + 1:
                gap = it.seq - self.high_seq - 1
                self.m["gaps"] += 1
                self.m["missing_segments"] += gap
                self._ev("gap", logging.WARNING, reason="segments_left_origin_window_before_discovery",
                   missing=gap, after_seq=self.high_seq, epoch=self.epoch)
            job = Pending(self.epoch, it.seq, it.uri, it.key, it.duration, it.discontinuity,
                          it.pdt, first_seen=now, gap_before=gap)
            self.pending[(self.epoch, it.seq)] = job
            self.path_by_seq[it.seq] = it.key
            while len(self.path_by_seq) > 4096:
                self.path_by_seq.popitem(last=False)
            self.high_seq = it.seq
            self.last_new_item_at = now
            self.m["segments_discovered"] += 1
        for (ep, seq), job in self.pending.items():
            job.listed = ep == self.epoch and seq in listed
        while len(self.pending) > cfg.max_pending_segments:
            victim = next((j for j in self.pending.values() if j.status == "queued"), None)
            if victim is None:
                break
            victim.status, victim.error = "failed", "pending_overflow"
            self.m["pending_overflow"] += 1
            self._commit_ready(now)
        self.prev_playlist_last = last

    # ------------------------------------------------------------ workers
    def _read_timeout(self) -> float:
        if self.cfg.segment_read_timeout:
            return self.cfg.segment_read_timeout
        target = self.source_target or 6.0
        return max(4.0, min(self.cfg.http_timeout, 2.0 * target))

    def _next_job(self, now):
        if self.inflight >= self.parallel_limit:
            return None, 0.25
        wait = 0.5
        for job in self.pending.values():
            if job.status != "queued":
                continue
            if job.next_try <= now:
                return job, 0.0
            wait = min(wait, job.next_try - now)
        return None, max(0.02, wait)

    def _worker_loop(self):
        while not self.stop_event.is_set():
            with self.cv:
                job, wait = self._next_job(self.clock())
                if job is None:
                    self.cv.wait(wait)
                    continue
                job.status = "downloading"
                job.attempts += 1
                self.inflight += 1
                self.m["download_attempts"] += 1
                uri = job.uri
                timeout = self._read_timeout()
            try:
                body, _final, seconds = self.http.get(
                    uri, timeout=timeout, deadline_s=self.cfg.segment_max_seconds,
                    max_bytes=max(MB, self.cfg.max_cache_mb * MB // 4))
                if not body:
                    raise FetchError("empty", "empty segment body")
                info = inspect_ts(body) if self.cfg.validate_ts else None
                if info is not None and not info.ok:
                    raise FetchError("invalid_ts", info.error)
            except FetchError as exc:
                with self.cv:
                    self.inflight -= 1
                    self._on_download_error(job, exc, self.clock())
                    self.cv.notify_all()
            except Exception as exc:
                with self.cv:
                    self.inflight -= 1
                    self._on_download_error(job, FetchError("connection", type(exc).__name__), self.clock())
                    self.cv.notify_all()
            else:
                with self.cv:
                    self.inflight -= 1
                    self._on_download_ok(job, body, info, seconds, self.clock())
                    self.cv.notify_all()
        getattr(self.http, "close_thread", lambda: None)()

    def _on_download_ok(self, job, body, info, seconds, now):
        job.status = "done"
        job.payload = body
        job.ts = info
        job.dl_seconds = seconds
        self.m["downloads_ok"] += 1
        self.dl_samples.append((seconds, len(body), job.duration))
        self._commit_ready(now)
        self._release(now)

    def _on_download_error(self, job, exc: FetchError, now):
        cfg = self.cfg
        self.m["download_failures"] += 1
        cat = exc.category
        errs = self.m["segment_errors"]
        errs[cat] = errs.get(cat, 0) + 1
        if exc.kind == "incomplete" or (exc.kind == "timeout" and exc.partial):
            self.m["incomplete_downloads"] += 1
        if exc.kind == "invalid_ts":
            self.m["invalid_ts"] += 1
        if exc.kind == "blocked":
            self.m["blocked_urls"] += 1
        if exc.status in (429, 503) and self.parallel_limit > 1:
            self.parallel_limit = 1
            self.m["throttle_events"] += 1
            self._ev("parallel_downloads_reduced", logging.WARNING, status=exc.status, limit=1)
        if exc.status in (401, 403, 410):
            self.need_resolve = True
        age = now - job.first_seen
        retry = (exc.retryable and job.attempts < cfg.segment_retries and
                 age < cfg.segment_give_up_seconds and
                 not (exc.status == 404 and not job.listed))
        self.last_segment_error = {"at": utcnow(), "category": cat, "seq": job.seq,
                                   "attempt": job.attempts, "detail": safe_text(exc)}
        if retry:
            backoff = min(4.0, 0.5 * (2 ** (job.attempts - 1))) * random.uniform(0.8, 1.2)
            job.status = "queued"
            job.next_try = now + backoff
            self.m["retries"] += 1
            self._ev("segment_retry", logging.WARNING, seq=job.seq, attempt=job.attempts,
               category=cat, partial_bytes=exc.partial, expected_bytes=exc.expected,
               backoff_s=backoff, uri=redact_url(job.uri))
        else:
            job.status = "failed"
            job.error = cat
            self._ev("segment_failed", logging.WARNING, seq=job.seq, attempts=job.attempts,
               category=cat, listed=job.listed, age_s=age, uri=redact_url(job.uri))
            self._commit_ready(now)
            self._release(now)

    # ------------------------------------------------------------ commit
    def _commit_ready(self, now):
        give_up = self.cfg.segment_give_up_seconds
        while self.pending:
            key, job = next(iter(self.pending.items()))
            if job.status == "queued" and now - job.first_seen > give_up and job.attempts:
                job.status, job.error = "failed", job.error or "give_up_timeout"
            if job.status == "done":
                self.pending.popitem(last=False)
                self._commit(job, now)
            elif job.status == "failed":
                self.pending.popitem(last=False)
                self.m["segments_dropped"] += 1
                self.m["gaps"] += 1
                self.m["missing_segments"] += 1
                self._ev("gap", logging.WARNING, reason=f"segment_unavailable:{job.error}",
                   missing=1, seq=job.seq, epoch=job.epoch)
                # the commit step will mark the next segment (PTS jump or sequence gap)
            else:
                break

    def _commit(self, job: Pending, now: float):
        digest = hashlib.sha256(job.payload).digest()[:16]
        if digest in self.recent_hashes:
            self.m["duplicates_skipped"] += 1
            self._ev("duplicate_skipped", logging.WARNING, seq=job.seq, epoch=job.epoch)
            return
        self.recent_hashes[digest] = None
        while len(self.recent_hashes) > 512:
            self.recent_hashes.popitem(last=False)
        prev = self.last_committed
        info = job.ts
        reasons = []
        if job.src_disc:
            reasons.append("source_tag")
            self.m["source_discontinuities"] += 1
        if prev is not None:
            if info is not None and info.min_pts is not None and prev["max_pts"] is not None:
                jump = pts_delta(info.min_pts, prev["max_pts"])
                limit = min(self.cfg.pts_jump_threshold, max(0.3, 0.5 * prev["duration"]))
                if jump > limit or jump < -0.5:
                    reasons.append(f"pts_jump:{jump:+.2f}s")
                    self.m["pts_jumps"] += 1
            elif job.epoch != prev["epoch"] or job.seq != prev["seq"] + 1:
                reasons.append("sequence_gap_or_reset")
        discontinuity = bool(reasons) and prev is not None
        if discontinuity:
            self.dseq += 1
            self.m["discontinuities"] += 1
            self._ev("discontinuity", logging.WARNING, local_seq=self.next_local, source_seq=job.seq,
               epoch=job.epoch, reasons=",".join(reasons))
        local = self.next_local
        self.next_local += 1
        seg = Stored(local, f"{self.instance}-{local}", job.payload, job.duration, discontinuity,
                     self.dseq, job.epoch, job.seq, job.pdt, now,
                     max_pts=info.max_pts if info else None, min_pts=info.min_pts if info else None)
        self.store[local] = seg
        self.bytes += len(job.payload)
        self.last_committed = {"epoch": job.epoch, "seq": job.seq, "duration": job.duration,
                               "max_pts": info.max_pts if info else None}
        rounded = int(math.floor(job.duration + 0.5))
        self.target_out = max(self.target_out, rounded, int(self.source_target or 1), 1)
        self.m["segments_committed"] += 1
        if info is not None:
            self.m["cc_errors"] += info.cc_errors
            if info.starts_with_keyframe is not None:
                self.m["keyframe_checked"] += 1
                self.m["keyframe_start"] += int(info.starts_with_keyframe)
        self.ingest.append((now, job.duration))
        self.last_commit_at = now
        if self.cfg.log_segments:
            size = len(job.payload)
            self._ev("segment_ok", seq=job.seq, local_seq=local, dur=job.duration, bytes=size,
               dl_s=job.dl_seconds, mbps=(size * 8 / job.dl_seconds / 1e6) if job.dl_seconds else 0,
               rt_factor=(job.duration / job.dl_seconds) if job.dl_seconds else 0,
               attempts=job.attempts, keyframe=(info.starts_with_keyframe if info else None),
               wait_s=now - job.first_seen)
        if self.on_commit is not None:
            self.on_commit(seg)
        self._evict(now)

    # ------------------------------------------------------------ release (pacing)
    def _release(self, now: float):
        cfg = self.cfg
        unreleased = [s for s in self.store.values() if s.released_at is None]
        if not unreleased:
            if self.release_started and self.underrun_since is None and now >= self.next_release_at:
                self.underrun_since = self.next_release_at
                self.m["underruns"] += 1
                self._ev("cushion_empty", logging.WARNING,
                   detail="publication waiting for origin; clients will consume their own buffer")
            return
        delay = cfg.live_delay_seconds
        if not self.release_started:
            if delay > 0:
                # publish only what keeps >= LIVE_DELAY_SECONDS of video unannounced
                remaining = sum(seg.duration for seg in unreleased)
                count = 0
                for seg in unreleased:
                    if remaining - seg.duration < delay:
                        break
                    remaining -= seg.duration
                    count += 1
                if count < cfg.min_start_segments:
                    return
            else:
                if len(self.store) < cfg.start_behind_segments + cfg.min_start_segments:
                    return
                count = len(unreleased) - cfg.start_behind_segments
            for seg in unreleased[:count]:
                self._mark_released(seg, now)
            self.release_started = True
            self.next_release_at = now + unreleased[count - 1].duration
            self._ev("publish_start", published=count,
               cushion_s=sum(seg.duration for seg in unreleased[count:]))
            self._evict(now)
            return
        if self.underrun_since is not None:
            self.m["underrun_seconds"] += max(0.0, now - self.underrun_since)
            self.underrun_since = None
        queue = deque(unreleased)
        if delay > 0:
            held = sum(seg.duration for seg in queue)
            while queue and held > cfg.live_delay_max_seconds:
                seg = queue.popleft()
                held -= seg.duration
                self._mark_released(seg, now)
            immediate = False
            if self._delay_rebuild(now, held):
                self._evict(now)
                return
        else:
            while len(queue) > cfg.max_cushion_segments:
                self._mark_released(queue.popleft(), now)
            immediate = cfg.start_behind_segments == 0
        while queue and (now >= self.next_release_at or immediate):
            seg = queue.popleft()
            self._mark_released(seg, now)
            base = self.next_release_at if now - self.next_release_at < seg.duration else now
            self.next_release_at = base + seg.duration
        self._evict(now)

    def _watching(self, now: float) -> bool:
        idle = self.cfg.live_delay_idle_seconds
        return any(now - c["last_seen"] <= idle for c in self.clients.values())

    def _delay_rebuild(self, now: float, held: float) -> bool:
        """Recover delay lost to origin outages. Lost seconds can only come back
        as waiting time, so: wait for free while nobody watches; optionally, short
        spaced pauses while someone watches. Returns True to hold publication now."""
        cfg = self.cfg
        target = cfg.live_delay_seconds
        margin = 10.0  # hysteresis: normal 1:1 jitter (one segment) must not trigger a rebuild
        if not self._watching(now):
            if held < target - margin or (self.rebuilding == "idle" and held < target):
                if self.rebuilding != "idle":
                    self.rebuilding = "idle"
                    self.m["rebuilds"] += 1
                    self._ev("delay_rebuild", state="start", mode="no_clients", held_s=held, target_s=target)
                self.next_release_at = now  # resume immediately when a client arrives
                return True
        if self.rebuilding == "idle":
            self.rebuilding = None
            self._ev("delay_rebuild", state="stop", held_s=held,
               reason="target_reached" if held >= target else "client_connected")
        if (cfg.rebuild_pause_seconds > 0 and held < target - margin and
                (self.last_rebuild_pause is None or
                 now - self.last_rebuild_pause >= cfg.rebuild_every_seconds) and
                now >= self.next_release_at):
            self.last_rebuild_pause = now
            self.next_release_at = now + cfg.rebuild_pause_seconds
            self.m["rebuild_pauses"] += 1
            self.m["rebuild_pause_seconds"] += cfg.rebuild_pause_seconds
            self._ev("delay_rebuild_pause", pause_s=cfg.rebuild_pause_seconds, held_s=held, target_s=target)
            return True
        return False

    def _mark_released(self, seg: Stored, now: float):
        seg.released_at = now
        self.m["segments_published"] += 1

    def _evict(self, now: float):
        cfg = self.cfg
        cap = cfg.max_cache_mb * MB
        released = [s for s in self.store.values() if s.released_at is not None]
        window = released[-cfg.publish_window_segments:]
        window_start = window[0].local_seq if window else None
        for seg in released[:len(released) - len(window)]:
            if seg.left_window_at is None:
                seg.left_window_at = now
        while self.store:
            oldest = next(iter(self.store.values()))
            over_bytes = self.bytes > cap
            over_count = len(self.store) > cfg.window_segments
            if not (over_bytes or over_count):
                break
            evictable = (oldest.released_at is not None and window_start is not None and
                         oldest.local_seq < window_start and oldest.left_window_at is not None and
                         now - oldest.left_window_at >= cfg.expired_grace_seconds)
            if not evictable and not over_bytes:
                break
            if not evictable:
                self.m["forced_evictions"] += 1
                self._ev("cache_pressure", logging.WARNING, local_seq=oldest.local_seq,
                   detail="MAX_CACHE_MB reached; evicting a segment that may still be needed")
            self.store.popitem(last=False)
            self.bytes -= len(oldest.payload)
            self.m["evictions"] += 1

    # ------------------------------------------------------------ outputs
    def playlist(self):
        with self.lock:
            now = self.clock()
            self._release(now)
            released = [s for s in self.store.values() if s.released_at is not None]
            window = released[-self.cfg.publish_window_segments:]
            if not window:
                return self._slate_playlist(now)
            first = window[0]
            seq, dseq, prefix = first.local_seq, first.dseq, []
            if self.slate_head is not None:
                seq, dseq, prefix, window = self._bridge(window)
            out = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{self.target_out}",
                   f"#EXT-X-MEDIA-SEQUENCE:{seq}",
                   f"#EXT-X-DISCONTINUITY-SEQUENCE:{dseq}"] + prefix
            for i, seg in enumerate(window):
                if (i and seg.discontinuity) or (not i and prefix):
                    out.append("#EXT-X-DISCONTINUITY")
                if seg.pdt:
                    out.append("#EXT-X-PROGRAM-DATE-TIME:" + seg.pdt)
                out.append(f"#EXTINF:{seg.duration:.3f},")
                out.append(f"/segment/{seg.name}.ts")
            return ("\n".join(out) + "\n").encode("utf-8")

    def _slate_playlist(self, now: float):
        slate = self.slate
        if slate is None or now - self.started >= self.cfg.live_delay_seconds + SLATE_GRACE_SECONDS:
            return None   # includes a dead origin: end in 503 instead of an endless slate
        base = self.slate_base
        first = base + int((now - self.started) / slate.duration)
        self.slate_head = first
        out = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{math.ceil(slate.duration)}",
               f"#EXT-X-MEDIA-SEQUENCE:{first}",
               f"#EXT-X-DISCONTINUITY-SEQUENCE:{slate.discontinuity(base, first)}"]
        out += slate.lines(base, first, first + slate.WINDOW, self.instance)
        return ("\n".join(out) + "\n").encode("utf-8")

    def _bridge(self, window):
        """Sequence, discontinuity sequence and slate entries that lead into the real video.

        Real segments continue the slate's numbering, and the slate entries leave the playlist
        one per newly published segment, so a player already on the slate reaches the real video
        through a marked discontinuity. Segment names keep the relay's own local_seq.
        """
        slate, head, base = self.slate, self.slate_head, self.slate_base
        end = head + slate.WINDOW       # the real video's first sequence
        if self.bridge is None:
            start = self._bridge_start(window, end)
            self.bridge = (start, sum(1 for s in window if s.local_seq >= start))
        start, initial = self.bridge
        window = [s for s in window if s.local_seq >= start]
        dropped = window[-1].local_seq - start + 1 - initial
        if dropped < slate.WINDOW and window[0].local_seq == start:
            first = head + dropped
            return (first, slate.discontinuity(base, first),
                    slate.lines(base, first, end, self.instance), window)
        return (window[0].local_seq - start + end,
                window[0].dseq + slate.discontinuity(base, end - 1) + 1, [], window)

    def _bridge_start(self, window, end: int) -> int:
        """First real segment after the slate: the one whose timestamps continue the slate's.

        Players behind an ffmpeg remux (Dispatcharr) freeze the picture on a big forward
        timestamp jump, so the slate is timed to end inside the published window
        (see slate_payload) and the real video starts where the slate's clock is.
        """
        anchor, slate = self.slate_anchor, self.slate
        if type(anchor) is not int:
            return window[0].local_seq
        slate_end = (anchor + round((end - self.slate_base) * slate.duration * 90000)) % PTS_WRAP
        best = ahead = None
        for seg in window:
            if seg.min_pts is None:
                continue
            back = pts_delta(slate_end, seg.min_pts)   # seconds the clock goes back at the switch
            if 0.0 <= back <= SLATE_MAX_REWIND_SECONDS:
                best = (seg, back)                     # remuxers absorb a small step back
            elif -1.0 <= back < 0.0 and ahead is None:
                ahead = (seg, back)                    # a step forward only pauses briefly
        best = best or ahead
        if best is None:
            gap = pts_delta(window[0].min_pts, slate_end) if window[0].min_pts is not None else None
            self._ev("slate_switch", logging.WARNING, aligned=False,
               jump_s=round(gap, 1) if gap is not None else None)
            return window[0].local_seq
        seg, back = best
        self._ev("slate_switch", aligned=True, rewind_s=round(back, 2),
           skipped=sum(1 for s in window if s.local_seq < seg.local_seq))
        return seg.local_seq

    def _slate_anchor(self, now: float):
        """PTS for slate_base, chosen so that the slate ends where the real video starts.

        The first real segment to be published is the oldest one stored (P0). Publication
        starts once the origin has produced the live delay plus MIN_START_SEGMENTS after P0;
        the slate is timed to reach P0 (+ SLATE_LEAD_SECONDS) at that moment. If it starts
        later, the switch picks a later real segment (see _bridge_start)."""
        if self.slate_anchor is not None:
            return self.slate_anchor
        first = next((s for s in self.store.values() if s.epoch == self.epoch), None)
        if first is None or first.min_pts is None or self.high_seq is None:
            return None
        cfg, slate, dur = self.cfg, self.slate, first.duration
        listed = (self.high_seq - first.source_seq + 1.5) * dur   # origin video after P0 (+half a segment since listed)
        if cfg.live_delay_seconds > 0:
            needed = cfg.live_delay_seconds + cfg.min_start_segments * dur
        else:
            needed = (cfg.start_behind_segments + cfg.min_start_segments) * dur
        publish_at = (now - self.started) + max(0.0, needed - listed)
        target = first.min_pts / 90000.0 + SLATE_LEAD_SECONDS - publish_at - slate.WINDOW * slate.duration
        self.slate_anchor = round(target * 90000) % PTS_WRAP
        self._ev("slate_aligned", listed_s=round(listed, 1), publish_in_s=round(max(0.0, needed - listed), 1))
        return self.slate_anchor

    def slate_payload(self, seq: int, wait: float | None = None):
        """Slate segment for sequence `seq`, its timestamps moved onto the channel's clock.

        Called without the lock: it may wait a few seconds for the first real segment,
        which is what the slate's clock is derived from."""
        slate = self.slate
        offset = seq - self.slate_base
        if slate is None or not 0 <= offset < 1_000_000:
            return None
        count = len(slate.payloads)
        index, loop = offset % count, offset // count
        if slate.first_pts is None:
            return slate.payloads[index]
        deadline = time.monotonic() + (SLATE_ALIGN_WAIT_SECONDS if wait is None else wait)
        while True:
            with self.lock:
                anchor = self._slate_anchor(self.clock())
            if anchor is not None or time.monotonic() >= deadline or self.stop_event.is_set():
                break
            time.sleep(0.1)
        with self.lock:
            if self.slate_anchor is None:
                self.slate_anchor = False
                self._ev("slate_unaligned", logging.WARNING, detail="no real segment in time; plain slate")
            anchor = self.slate_anchor
            if anchor is False:
                return slate.payloads[index]
            data = self.slate_cache.get((index, loop))
            if data is None:
                shift = (anchor + round(loop * count * slate.duration * 90000) - slate.first_pts) % PTS_WRAP
                data = shift_ts(slate.payloads[index], shift)
                self.slate_cache[(index, loop)] = data
                while len(self.slate_cache) > 8:
                    self.slate_cache.popitem(last=False)
            return data

    _SEG_RE = re.compile(r"([0-9a-f]{6})-(\d{1,20})")

    def segment(self, name: str):
        match = self._SEG_RE.fullmatch(name)
        if not match or match.group(1) != self.instance:
            return None
        with self.lock:
            seg = self.store.get(int(match.group(2)))
            if seg is None or seg.released_at is None:
                return None
            return seg

    def note_client(self, ip: str, kind: str, local_seq=None, ok=True, nbytes=0):
        with self.lock:
            now = self.clock()
            c = self.clients.get(ip)
            if c is None:
                c = {"first_seen": now, "playlist_requests": 0, "segment_requests": 0,
                     "segment_404": 0, "bytes_sent": 0, "last_segment": None, "user_agent": ""}
                self.clients[ip] = c
                self._ev("client_new", ip=ip)
            self.clients.move_to_end(ip)
            c["last_seen"] = now
            c[f"{kind}_requests"] = c.get(f"{kind}_requests", 0) + 1
            c["bytes_sent"] += nbytes
            if local_seq is not None and ok:
                c["last_segment"] = local_seq
            if not ok and kind == "segment":
                c["segment_404"] += 1
                self.m["client_segment_404"] += 1
            while len(self.clients) > 64:
                self.clients.popitem(last=False)

    # ------------------------------------------------------------ state / health
    def _ingest_horizon(self) -> float:
        # >= 15 segments in the window: with 10 s segments a 60 s window holds
        # 5 or 6 of them and the ratio jumps between 0.83 and 1.16 (false alarms)
        return max(60.0, 15.0 * float(self.source_target or 0))

    def _ingest_ratio(self, now, horizon=None):
        horizon = horizon or self._ingest_horizon()
        while self.ingest and now - self.ingest[0][0] > max(300.0, 2 * horizon):
            self.ingest.popleft()
        if not self.ingest:
            return None
        span = min(horizon, now - self.started)
        if span < 10:
            return None
        content = sum(d for t, d in self.ingest if now - t <= horizon)
        return content / span

    def _update_state(self, now):
        target = self.source_target or 6.0
        stall_after = max(3 * target, 10.0)
        since_commit = now - (self.last_commit_at or self.started)
        ratio = self._ingest_ratio(now)
        if self.last_commit_at is None and now - self.started < 30:
            state = "starting"
        elif not self.release_started and since_commit <= stall_after:
            state = "buffering"
        elif since_commit > stall_after:
            if self.last_playlist_ok_at is None or now - self.last_playlist_ok_at > stall_after:
                state = "stalled:playlist_failing"
            elif self.last_new_item_at is None or now - self.last_new_item_at > stall_after:
                state = "stalled:origin_not_advancing"
            else:
                state = "stalled:segment_downloads_failing"
        elif ratio is not None and ratio < 0.9 and now - self.started > 60:
            state = "degraded:ingest_slower_than_realtime"
        elif self.m["consecutive_playlist_failures"]:
            state = "degraded:playlist_errors"
        else:
            state = "ok"
        if state != self.state:
            self._ev("state_change", logging.WARNING if state != "ok" else logging.INFO,
               previous=self.state, state=state, since_last_segment_s=since_commit,
               ingest_ratio_60s=ratio)
            self.state, self.state_since = state, now
        elif state.startswith("stalled") and now - self.last_stall_log > 30:
            self._ev("stall", logging.WARNING, state=state, since_last_segment_s=since_commit)
            self.last_stall_log = now
        if not getattr(self, "_memory_checked", False) and len(self.dl_samples) >= 8:
            self._memory_checked = True
            byte_rate = sum(x[1] for x in self.dl_samples) / max(1e-6, sum(x[2] for x in self.dl_samples))
            seg = sum(x[2] for x in self.dl_samples) / len(self.dl_samples)
            hold = ((self.cfg.live_delay_max_seconds if self.cfg.live_delay_seconds
                     else self.cfg.max_cushion_segments * seg) +
                    self.cfg.publish_window_segments * seg + self.cfg.expired_grace_seconds + 4 * seg)
            need_mb = byte_rate * hold / MB
            level = logging.WARNING if need_mb > self.cfg.max_cache_mb * 0.9 else logging.INFO
            self._ev("memory_estimate", level, stream_mbps=byte_rate * 8 / 1e6, buffered_seconds=hold,
               needed_mb=need_mb, max_cache_mb=self.cfg.max_cache_mb,
               detail="raise MAX_CACHE_MB (and compose mem_limit)" if level == logging.WARNING else "ok")
        if now - self.last_summary >= 60:
            self.last_summary = now
            h = self._health_locked(now)
            self._ev("summary", state=state, ingest_ratio_60s=h["ingest"]["ingest_ratio_60s"],
               avg_mbps=h["ingest"]["avg_download_mbps"], rt_factor=h["ingest"]["avg_realtime_factor"],
               cushion_s=h["buffer"]["cushion_duration_seconds"],
               published_s=h["buffer"]["published_duration_seconds"],
               gaps=self.m["gaps"], retries=self.m["retries"], clients=h["clients"]["active"])

    def health(self) -> dict:
        with self.lock:
            now = self.clock()
            self._release(now)
            return self._health_locked(now)

    def _health_locked(self, now) -> dict:
        m = self.m
        released = [s for s in self.store.values() if s.released_at is not None]
        window = released[-self.cfg.publish_window_segments:]
        cushion = [s for s in self.store.values() if s.released_at is None]
        samples = list(self.dl_samples)
        dl_times = sorted(s[0] for s in samples)
        total_dl = sum(s[0] for s in samples)
        total_bytes = sum(s[1] for s in samples)
        total_dur = sum(s[2] for s in samples)
        attempts = m["download_attempts"] or 0

        def r(x, n=2):
            return None if x is None else round(x, n)

        edge = window[-1].local_seq if window else None
        clients = []
        for ip, c in reversed(self.clients.items()):
            idle = now - c["last_seen"]
            if idle > 600:
                continue
            clients.append({"ip": ip, "idle_seconds": r(idle, 1),
                            "playlist_requests": c["playlist_requests"],
                            "segment_requests": c["segment_requests"],
                            "segment_404": c["segment_404"],
                            "mb_sent": r(c["bytes_sent"] / MB, 1),
                            "segments_behind_live_edge": (edge - c["last_segment"])
                            if edge is not None and c["last_segment"] is not None else None})
        ingest_ratio = self._ingest_ratio(now)
        return {
            "version": VERSION, "status": self.state, "started_at": self.started_wall,
            "uptime_seconds": r(now - self.started, 0),
            "source": {
                "state": self.state,
                "variant": dict(self.variant),
                "target_duration": self.source_target,
                "origin_window_segments": self.source_window,
                "origin_media_sequence": self.source_media_sequence,
                "epoch": self.epoch, "resets": m["resets"],
                "stale_playlists": m["stale_playlists"],
                "playlist_fetches": m["playlist_fetches"], "playlist_ok": m["playlist_ok"],
                "playlist_errors": dict(m["playlist_errors"]),
                "consecutive_playlist_failures": m["consecutive_playlist_failures"],
                "seconds_since_playlist_ok": r(now - self.last_playlist_ok_at, 1)
                if self.last_playlist_ok_at else None,
                "seconds_since_new_segment_listed": r(now - self.last_new_item_at, 1)
                if self.last_new_item_at else None,
                "last_playlist_error": self.last_playlist_error,
            },
            "ingest": {
                "segments_received": m["segments_committed"],
                "segments_discovered": m["segments_discovered"],
                "download_attempts": attempts, "downloads_ok": m["downloads_ok"],
                "download_failures": m["download_failures"],
                "success_rate": r(m["downloads_ok"] / (m["downloads_ok"] + m["download_failures"]), 4)
                if (m["downloads_ok"] + m["download_failures"]) else None,
                "retries": m["retries"], "incomplete_downloads": m["incomplete_downloads"],
                "invalid_ts": m["invalid_ts"], "errors_by_category": dict(m["segment_errors"]),
                "segments_dropped": m["segments_dropped"], "gaps": m["gaps"],
                "missing_segments": m["missing_segments"],
                "duplicates_skipped": m["duplicates_skipped"],
                "discontinuities": m["discontinuities"], "pts_jumps": m["pts_jumps"],
                "source_discontinuity_tags": m["source_discontinuities"],
                "ts_continuity_errors": m["cc_errors"],
                "keyframe_start_ratio": r(m["keyframe_start"] / m["keyframe_checked"], 3)
                if m["keyframe_checked"] else None,
                "avg_download_mbps": r(total_bytes * 8 / total_dl / 1e6) if total_dl else None,
                "source_bitrate_mbps": r(total_bytes * 8 / total_dur / 1e6) if total_dur else None,
                "avg_segment_download_seconds": r(total_dl / len(samples), 3) if samples else None,
                "p95_segment_download_seconds": r(dl_times[int(0.95 * (len(dl_times) - 1))], 3)
                if dl_times else None,
                "avg_realtime_factor": r(total_dur / total_dl) if total_dl else None,
                "ingest_ratio_60s": r(ingest_ratio, 3),
                "ingest_window_seconds": r(self._ingest_horizon(), 0),
                "seconds_since_last_segment": r(now - self.last_commit_at, 1)
                if self.last_commit_at else None,
                "pending": len(self.pending), "in_flight": self.inflight,
                "parallel_limit": self.parallel_limit,
                "throttle_events": m["throttle_events"], "blocked_urls": m["blocked_urls"],
                "last_segment_error": self.last_segment_error,
            },
            "buffer": {
                "retained_segments": len(self.store), "retained_mb": r(self.bytes / MB, 1),
                "cache_duration_seconds": r(sum(s.duration for s in self.store.values())),
                "published_segments": len(window),
                "published_duration_seconds": r(sum(s.duration for s in window)),
                "cushion_segments": len(cushion),
                "cushion_duration_seconds": r(sum(s.duration for s in cushion)),
                "target_cushion_segments": self.cfg.start_behind_segments,
                "live_delay_target_seconds": self.cfg.live_delay_seconds or None,
                "live_delay_max_seconds": self.cfg.live_delay_max_seconds or None,
                "delay_rebuilding": self.rebuilding,
                "delay_rebuilds": m["rebuilds"], "rebuild_pauses": m["rebuild_pauses"],
                "rebuild_pause_seconds": r(m["rebuild_pause_seconds"], 1),
                "estimated_delay_behind_source_seconds": r(
                    sum(s.duration for s in cushion) + (self.source_window or 0) * (self.source_target or 0)
                    if self.release_started else None, 1),
                "buffering": not self.release_started,
                "buffering_progress_seconds": r(sum(s.duration for s in cushion), 1)
                if not self.release_started else None,
                "underruns": m["underruns"], "underrun_seconds": r(m["underrun_seconds"], 1),
                "media_sequence": window[0].local_seq if window else None,
                "live_edge_sequence": edge,
                "discontinuity_sequence": window[0].dseq if window else None,
                "target_duration": self.target_out,
                "evictions": m["evictions"], "forced_evictions": m["forced_evictions"],
            },
            "clients": {"active": sum(1 for c in clients if c["idle_seconds"] <= 30),
                        "segment_404_total": m["client_segment_404"],
                        "denied": m["clients_denied"], "recent": clients[:16]},
            "config": self.cfg.public_view(),
        }


# =========================================================================
# Multi-channel manager (independent relays, persistent state)
# =========================================================================
_SPEC_FIELDS = ("name", "url", "enabled", "mode", "idle_stop_minutes", "live_delay_seconds",
                "max_parallel_downloads", "master_refresh_seconds")
MODES = ("always", "on_demand")


def cgroup_memory() -> dict:
    out = {}
    for key, path in (("limit", "/sys/fs/cgroup/memory.max"), ("used", "/sys/fs/cgroup/memory.current")):
        try:
            with open(path, encoding="ascii") as fh:
                raw = fh.read().strip()
            out[key] = None if raw == "max" else int(raw)
        except (OSError, ValueError):
            out[key] = None
    return out


SLATE_GRACE_SECONDS = 120.0   # beyond the live delay: a dead origin must end in 503, not an endless slate
SLATE_LEAD_SECONDS = 2.0      # the slate is timed to end this far into the first real segment
SLATE_MAX_REWIND_SECONDS = 45.0  # ffmpeg remuxers absorb backward jumps up to ~60 s, forward ones only < 10 s
SLATE_ALIGN_WAIT_SECONDS = 8.0   # a slate request waits this long for the first real segment


class Slate:
    """Pre-encoded "please wait" video, served while an on-demand channel fills its delay.

    Each relay numbers it from its own start, so the real playlist can continue the same
    sequence (see Relay._bridge) and players see one stream with a marked discontinuity.
    """
    WINDOW = 4
    MAX_BYTES = 64 * 1024 * 1024

    def __init__(self, directory: str):
        with open(os.path.join(directory, "index.m3u8"), encoding="utf-8") as fh:
            text = fh.read()
        durations = [float(d) for d in re.findall(r"#EXTINF:([0-9.]+)", text)]
        names = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        if not names or len(names) != len(durations):
            raise ValueError("index.m3u8 sem segmentos")
        self.payloads = []
        total = 0
        for name in names:
            with open(os.path.join(directory, os.path.basename(name)), "rb") as fh:
                data = fh.read(self.MAX_BYTES - total + 1)
            total += len(data)
            if total > self.MAX_BYTES or data[:1] != b"\x47":
                raise ValueError(f"segmento inválido ou grande demais: {name}")
            self.payloads.append(data)
        self.duration = sum(durations) / len(durations)
        if self.duration <= 0:
            raise ValueError("duração inválida")
        self.first_pts = inspect_ts(self.payloads[0]).min_pts   # None: timestamps are left alone

    @classmethod
    def load(cls, directory: str):
        """The slate is optional: any problem only logs and leaves the plain 503 behaviour."""
        if not directory:
            return None
        try:
            slate = cls(directory)
        except (OSError, ValueError) as exc:
            ev("slate_unavailable", logging.ERROR, path=directory, detail=safe_text(exc))
            return None
        ev("slate_loaded", path=directory, segments=len(slate.payloads), duration_s=slate.duration)
        return slate

    def discontinuity(self, base: int, seq: int) -> int:
        """Loop restarts before `seq`, for a slate numbered from `base`."""
        return (seq - base) // len(self.payloads)

    def lines(self, base: int, start: int, stop: int, instance: str) -> list:
        count = len(self.payloads)
        out = []
        for seq in range(start, stop):
            if seq > start and (seq - base) % count == 0:  # the loop restarts (unaligned: timestamps jump back)
                out.append("#EXT-X-DISCONTINUITY")
            out.append(f"#EXTINF:{self.duration:.3f},")
            out.append(f"/slate/{instance}-{seq}.ts")
        return out

    def segment(self, index: int):
        return self.payloads[index] if index < len(self.payloads) else None


class ChannelManager:
    """Owns the Relay instances. Channel settings (name, URL, on/off and a few
    per-channel overrides) persist in <state_dir>/channels.json; everything else comes
    from the environment (.env), exactly as in single-channel mode."""

    def __init__(self, base: Config, state_dir: str | None = None, admin_password: str = ""):
        self.base = base
        self.lock = threading.RLock()
        self.state_path = os.path.join(state_dir, "channels.json") if state_dir else None
        self.admin_password = admin_password or ""
        self.relays = {}
        self.denied = 0
        self.persist_error = None
        self.last_error = {}
        self.specs = {}
        self.next_id = 1   # channel numbers are never reused: an old link must not hit another channel
        self.started_by = {}
        self.stop_event = threading.Event()
        self.slate = Slate.load(base.slate_dir)
        self._load()

    @staticmethod
    def _blank(slot: int) -> dict:
        return {"slot": slot, "name": f"Canal {slot}", "url": "", "enabled": False, "mode": "always",
                "idle_stop_minutes": None, "live_delay_seconds": None, "max_parallel_downloads": None,
                "master_refresh_seconds": None}

    @classmethod
    def single(cls, relay: "Relay") -> "ChannelManager":
        """Wrap one already running relay (tests / embedding) as channel 1."""
        manager = cls(relay.cfg)
        manager.relays[1] = relay
        manager.specs.setdefault(1, manager._blank(1)).update(url=relay.cfg.source_url, enabled=True)
        return manager

    # ------------------------------------------------------------ persistence
    def _load(self):
        data = None
        if self.state_path and os.path.exists(self.state_path):
            try:
                with open(self.state_path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError) as exc:
                ev("channels_state_unreadable", logging.ERROR, detail=safe_text(exc))
        if isinstance(data, dict) and isinstance(data.get("channels"), list):
            for item in data["channels"]:
                try:
                    slot = int(item.get("slot", 0))
                    clean = self._validate(item, partial=True)
                except (TypeError, ValueError):
                    continue
                if slot < 1:
                    continue
                spec = self.specs.setdefault(slot, self._blank(slot))
                spec.update(clean)
                spec["enabled"] = bool(item.get("enabled")) and bool(spec["url"])
            highest = max(self.specs, default=0)
            try:
                self.next_id = max(int(data.get("next_id") or 1), highest + 1)
            except (TypeError, ValueError):
                self.next_id = highest + 1
        else:
            # first start: channel 1 = HLS_SOURCE_URL from .env (same behaviour as v0.4)
            self.specs[1] = self._blank(1)
            self.next_id = 2
            if self.base.source_url:
                self.specs[1].update(url=self.base.source_url, enabled=True)
            self._save()

    def _save(self):
        if not self.state_path:
            return
        payload = {"version": VERSION, "next_id": self.next_id,
                   "channels": [dict(self.specs[i]) for i in sorted(self.specs)]}
        tmp = self.state_path + ".tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.state_path)
            self.persist_error = None
        except OSError as exc:
            self.persist_error = f"{type(exc).__name__}: {exc.strerror or exc}"
            ev("channels_state_not_saved", logging.ERROR, detail=self.persist_error)

    # ------------------------------------------------------------ validation / config
    @staticmethod
    def _validate(data: dict, partial: bool = False) -> dict:
        out = {}
        if "name" in data:
            name = str(data["name"] or "").strip()[:40]
            out["name"] = name or "Canal"
        if "url" in data:
            url = str(data["url"] or "").strip()
            if url:
                parts = urlsplit(url)
                if len(url) > 2048 or parts.scheme not in ("http", "https") or not parts.hostname:
                    raise ValueError("URL inválida: use http(s)://... de uma playlist HLS autorizada")
            out["url"] = url

        def opt(key, cast, lo, hi):
            if key not in data:
                return
            value = data[key]
            if value in (None, "", "default"):
                out[key] = None
                return
            try:
                value = cast(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key}: valor inválido") from None
            if not lo <= value <= hi:
                raise ValueError(f"{key}: use entre {lo} e {hi}")
            out[key] = value

        opt("live_delay_seconds", float, 0.0, 1800.0)
        opt("max_parallel_downloads", int, 1, 4)
        opt("master_refresh_seconds", float, 0.0, 86400.0)
        opt("idle_stop_minutes", float, 0.5, 1440.0)
        if "mode" in data:
            mode = str(data["mode"] or "always").strip()
            if mode not in MODES:
                raise ValueError("mode: use 'always' (sempre ligado) ou 'on_demand' (sob demanda)")
            out["mode"] = mode
        return out

    def config_for(self, spec: dict) -> Config:
        base = self.base
        kw = {"source_url": spec["url"]}
        if spec.get("live_delay_seconds") is not None:
            delay = float(spec["live_delay_seconds"])
            extra = (base.live_delay_max_seconds - base.live_delay_seconds) if base.live_delay_seconds else 30.0
            kw["live_delay_seconds"] = delay
            kw["live_delay_max_seconds"] = delay + max(10.0, extra) if delay > 0 else 0.0
        if spec.get("max_parallel_downloads"):
            kw["max_parallel_downloads"] = int(spec["max_parallel_downloads"])
        if spec.get("master_refresh_seconds") is not None:
            kw["master_refresh_seconds"] = float(spec["master_refresh_seconds"])
        return dataclasses.replace(base, **kw).normalize()

    # ------------------------------------------------------------ lifecycle
    def _slot(self, slot) -> int:
        slot = int(slot)
        if slot not in self.specs:
            raise ValueError(f"Canal {slot} não existe")
        return slot

    def add(self, data: dict | None = None) -> int:
        clean = self._validate(data or {})
        with self.lock:
            if self.base.max_channels and len(self.specs) >= self.base.max_channels:
                raise ValueError(f"Limite de {self.base.max_channels} canais (MAX_CHANNELS no .env)")
            slot = self.next_id
            self.next_id += 1
            spec = self._blank(slot)
            spec.update(clean)
            spec["enabled"] = False
            self.specs[slot] = spec
            self._save()
        ev("channel_added", ch=slot, name=spec["name"])
        return slot

    def remove(self, slot):
        slot = self._slot(slot)
        self.stop(slot, persist=False)
        with self.lock:
            self.specs.pop(slot, None)
            self.last_error.pop(slot, None)
            self._save()
        ev("channel_removed", ch=slot)

    def start(self, slot, persist=True, reason="manual"):
        slot = self._slot(slot)
        with self.lock:
            spec = self.specs[slot]
            if not spec["url"]:
                raise ValueError("Defina a URL do canal antes de ligar")
            if slot in self.relays:
                return False
            relay = Relay(self.config_for(spec), channel=str(slot))
            if spec.get("mode") == "on_demand":
                relay.slate = self.slate
            relay.start()
            self.relays[slot] = relay
            self.started_by[slot] = reason
            self.last_error.pop(slot, None)
            # "enabled" = start with the container; only meaningful for always-on channels
            if persist and spec.get("mode", "always") == "always" and not spec["enabled"]:
                spec["enabled"] = True
                self._save()
        ev("channel_start", ch=slot, name=spec["name"], reason=reason, mode=spec.get("mode", "always"),
           source=redact_url(spec["url"]))
        return True

    def stop(self, slot, persist=True, reason="manual"):
        slot = self._slot(slot)
        with self.lock:
            relay = self.relays.pop(slot, None)
            self.started_by.pop(slot, None)
            if persist and self.specs[slot]["enabled"]:
                self.specs[slot]["enabled"] = False
                self._save()
        if relay is not None:
            relay.stop()  # threads finish their current request and exit; memory is released
            ev("channel_stop", ch=slot, reason=reason)

    # ------------------------------------------------------------ on-demand channels
    def idle_limit(self, slot) -> float:
        minutes = self.specs[slot].get("idle_stop_minutes") or self.base.demand_idle_minutes
        return float(minutes) * 60.0

    @staticmethod
    def idle_seconds(relay) -> float:
        """Seconds since the last playlist/segment request (or since the start)."""
        with relay.lock:
            last = max([c["last_seen"] for c in relay.clients.values()] + [relay.started])
            return relay.clock() - last

    def ensure_on_demand(self, slot) -> bool:
        """Start an on-demand channel because someone asked for it. True if started now."""
        with self.lock:
            spec = self.specs.get(slot)
            if not spec or spec.get("mode") != "on_demand" or not spec["url"] or slot in self.relays:
                return False
            return self.start(slot, persist=False, reason="demand")

    def reap(self):
        """Stop on-demand channels that nobody watched for idle_stop_minutes."""
        for slot, relay in list(self.relays.items()):
            spec = self.specs.get(slot)
            if not spec or spec.get("mode") != "on_demand":
                continue
            idle = self.idle_seconds(relay)
            if idle >= self.idle_limit(slot):
                ev("channel_idle_stop", ch=slot, idle_min=round(idle / 60, 1))
                self.stop(slot, persist=False, reason="idle")

    def start_reaper(self, interval=10.0):
        def loop():
            while not self.stop_event.wait(interval):
                try:
                    self.reap()
                except Exception as exc:
                    ev("reaper_error", logging.ERROR, detail=safe_text(exc))
        threading.Thread(target=loop, name="on-demand-reaper", daemon=True).start()

    def restart(self, slot):
        self.stop(slot, persist=False)
        self.start(slot)

    def update(self, slot, data: dict) -> bool:
        """Apply settings; restarts the channel if it is running and the source changed.
        Returns True when a restart happened."""
        slot = self._slot(slot)
        clean = self._validate(data)
        runtime = ("url", "live_delay_seconds", "max_parallel_downloads", "master_refresh_seconds")
        with self.lock:
            spec = self.specs[slot]
            changed = any(k in clean and clean[k] != spec.get(k) for k in runtime)
            spec.update(clean)
            running = slot in self.relays
            if spec.get("mode") == "on_demand":
                spec["enabled"] = False      # never auto-started with the container
            elif "mode" in clean:
                spec["enabled"] = running    # switched to always-on: keep the current state
            if not spec["url"]:
                spec["enabled"] = False
            self._save()
        if running and not spec["url"]:
            self.stop(slot)
            return False
        if running and changed:
            self.restart(slot)
            return True
        return False

    def start_enabled(self):
        for slot, spec in sorted(self.specs.items()):
            if spec["enabled"] and spec["url"] and spec.get("mode", "always") == "always":
                try:
                    self.start(slot, persist=False, reason="boot")
                except Exception as exc:  # one bad channel must not stop the others
                    self.last_error[slot] = safe_text(exc)
                    ev("channel_start_failed", logging.ERROR, ch=slot, detail=safe_text(exc))

    def stop_all(self):
        self.stop_event.set()
        for slot in list(self.relays):
            self.stop(slot, persist=False)

    def relay(self, slot):
        return self.relays.get(slot)

    def by_instance(self, instance: str):
        for relay in list(self.relays.values()):
            if relay.instance == instance:
                return relay
        return None

    # ------------------------------------------------------------ status
    def summary(self, include_urls=False) -> dict:
        channels = []
        with self.lock:
            specs = {k: dict(v) for k, v in self.specs.items()}
            relays = dict(self.relays)
        for slot, spec in sorted(specs.items()):
            relay = relays.get(slot)
            item = dict(spec)
            if not include_urls:
                item["url"] = redact_url(spec["url"]) if spec["url"] else ""
            item["running"] = relay is not None
            item["path"] = f"/ch/{slot}/live.m3u8"
            item["error"] = self.last_error.get(slot)
            item["mode"] = spec.get("mode", "always")
            item["idle_stop_minutes_effective"] = self.idle_limit(slot) / 60.0
            item["started_by"] = self.started_by.get(slot)
            cfg = relay.cfg if relay else (self.config_for(spec) if spec["url"] else None)
            item["effective"] = None if cfg is None else {
                "live_delay_seconds": cfg.live_delay_seconds,
                "live_delay_max_seconds": cfg.live_delay_max_seconds,
                "max_parallel_downloads": cfg.max_parallel_downloads,
                "master_refresh_seconds": cfg.master_refresh_seconds}
            if relay is None:
                item["status"] = "off"
                channels.append(item)
                continue
            h = relay.health()
            i, b, s = h["ingest"], h["buffer"], h["source"]
            byte_rate = (i["source_bitrate_mbps"] or 0) * 1e6 / 8
            hold = ((cfg.live_delay_max_seconds or 0) + (b["published_duration_seconds"] or 0) +
                    cfg.expired_grace_seconds)
            item["status"] = h["status"]
            item["metrics"] = {
                "stream_mbps": i["source_bitrate_mbps"], "download_mbps": i["avg_download_mbps"],
                "realtime_factor": i["avg_realtime_factor"], "ingest_ratio": i["ingest_ratio_60s"],
                "cushion_seconds": b["cushion_duration_seconds"],
                "published_seconds": b["published_duration_seconds"],
                "buffering": b["buffering"], "buffering_progress_seconds": b["buffering_progress_seconds"],
                "rebuilding": b["delay_rebuilding"], "underruns": b["underruns"],
                "gaps": i["gaps"], "discontinuities": i["discontinuities"],
                "retries": i["retries"], "segment_errors": sum(i["errors_by_category"].values()),
                "playlist_errors": sum(s["playlist_errors"].values()),
                "seconds_since_last_segment": i["seconds_since_last_segment"],
                "clients": h["clients"]["active"], "retained_mb": b["retained_mb"],
                "estimated_mb": round(byte_rate * hold / MB) if byte_rate else None,
                "uptime_seconds": h["uptime_seconds"],
                "idle_seconds": round(self.idle_seconds(relay), 1)}
            channels.append(item)
        defaults = {"live_delay_seconds": self.base.live_delay_seconds,
                    "idle_stop_minutes": self.base.demand_idle_minutes,
                    "max_parallel_downloads": self.base.max_parallel_downloads,
                    "master_refresh_seconds": self.base.master_refresh_seconds}
        return {"version": VERSION, "channels": channels, "memory": cgroup_memory(), "defaults": defaults,
                "max_channels": self.base.max_channels,
                "persist": bool(self.state_path), "persist_error": self.persist_error,
                "admin_enabled": bool(self.admin_password), "max_cache_mb": self.base.max_cache_mb}


# =========================================================================
# Admin page (/admin) - single self-contained file, no external resources
# =========================================================================
ADMIN_HTML = r'''<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BoxServer · Canais</title>
<style>
:root{--bg:#0b0b0c;--panel:#141416;--panel2:#1b1b1e;--line:#2a2a2f;--text:#ececef;--muted:#9a9aa3;
--acc:#ff7a1a;--acc2:#ff9a4d;--ok:#2fbf71;--warn:#e8b23a;--bad:#ef5350;--off:#5d5d66;--r:14px}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,Ubuntu,sans-serif}
header{display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding:18px 24px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:rgba(11,11,12,.92);backdrop-filter:blur(6px);z-index:5}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:17px;letter-spacing:.2px}
.logo{width:30px;height:30px;border-radius:8px;background:linear-gradient(135deg,var(--acc),#c2410c);display:grid;place-items:center;color:#111;font-weight:900}
.brand small{color:var(--muted);font-weight:500;font-size:12px}
.spacer{flex:1}
.mem{min-width:220px}.mem .lbl{display:flex;justify-content:space-between;color:var(--muted);font-size:12px}
.bar{height:6px;border-radius:9px;background:var(--panel2);overflow:hidden;margin-top:4px}
.bar>i{display:block;height:100%;width:0;background:var(--acc);transition:width .4s}
.bar.hi>i{background:var(--bad)}
main{padding:22px 24px;max-width:1400px;margin:0 auto}
.banner{background:#2a1a0c;border:1px solid #6b3a12;color:#ffcf9e;padding:10px 14px;border-radius:10px;margin-bottom:16px;display:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(520px,1fr));gap:18px}
@media(max-width:600px){.grid{grid-template-columns:1fr}main,header{padding:14px}}
.grid>*{min-width:0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:16px 16px 14px;display:flex;flex-direction:column;gap:12px}
.card.on{border-color:#3a2a1c}
.top{display:flex;align-items:center;gap:10px}
.num{flex:none;width:34px;height:34px;border-radius:10px;background:var(--panel2);border:1px solid var(--line);display:grid;place-items:center;font-weight:800;color:var(--acc2)}
.name{flex:1;width:0;min-width:0;background:transparent;border:1px solid transparent;color:var(--text);font-size:16px;font-weight:650;padding:6px 8px;border-radius:8px}
.name:hover,.name:focus{border-color:var(--line);background:var(--panel2);outline:none}
.pill{flex:none;font-size:12px;font-weight:650;padding:4px 10px;border-radius:99px;background:var(--panel2);color:var(--muted);display:flex;align-items:center;gap:6px}
.pill:before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.pill.wait{color:#7aa2ff}.pill.ok{color:var(--ok)}.pill.warn{color:var(--warn)}.pill.bad{color:var(--bad)}.pill.buf{color:var(--acc2)}.pill.off{color:var(--off)}
label.f{display:flex;flex-direction:column;gap:5px;color:var(--muted);font-size:12px}
input.url,select{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:9px;padding:9px 10px;font-size:13px}
input.url{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
input.url:focus,select:focus{outline:none;border-color:var(--acc)}
.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.idle-note{color:#9fb6ff;font-size:12px;display:none}
.pinbox{display:flex;flex-direction:column;gap:5px;color:var(--muted);font-size:12px}
button.pin{width:100%;padding:9px 10px}button.pin.on{background:#12351f;border-color:var(--ok);color:#7ee2a8}
.stats{display:grid;grid-template-columns:repeat(6,1fr);gap:8px}
.stat{background:var(--panel2);border-radius:9px;padding:7px 8px}
.stat b{display:block;font-size:15px;font-variant-numeric:tabular-nums}.stat span{color:var(--muted);font-size:11px}
.stat.alert b{color:var(--warn)}
@media(max-width:600px){.row3,.row2{grid-template-columns:1fr}.stats{grid-template-columns:repeat(3,1fr)}}
.prog{display:none}.prog .lbl{display:flex;justify-content:space-between;color:var(--muted);font-size:12px}
.link{display:flex;gap:8px;align-items:center}
.link input{flex:1;min-width:0;background:transparent;border:1px dashed var(--line);color:var(--muted);border-radius:9px;padding:7px 10px;font:12px ui-monospace,Menlo,Consolas,monospace}
.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button{border:1px solid var(--line);background:var(--panel2);color:var(--text);padding:8px 14px;border-radius:9px;font-weight:600;cursor:pointer;font-size:13px}
button:hover{border-color:#44444b}button:disabled{opacity:.45;cursor:default}
button.pri{background:var(--acc);border-color:var(--acc);color:#141414}button.pri:hover{background:var(--acc2)}
button.danger{color:#ffb4b2;border-color:#5a2524}
.dirty{color:var(--acc2);font-size:12px;display:none}
.actions .remove{margin-left:auto;background:transparent;color:#ffb4b2;border-color:#5a2524}
.toolbar{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.toolbar .count{color:var(--muted);font-size:13px}
.empty{border:1px dashed var(--line);border-radius:var(--r);padding:40px;text-align:center;color:var(--muted)}.err{color:#ffb4b2;font-size:12px}
.note{color:var(--muted);font-size:12px}
#login{position:fixed;inset:0;background:rgba(0,0,0,.75);display:none;place-items:center;z-index:20}
#login form{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:26px;width:min(360px,92vw);display:flex;flex-direction:column;gap:14px}
#login input{background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:9px;padding:10px 12px;font-size:15px}
#toast{position:fixed;right:20px;bottom:20px;background:#222226;border:1px solid var(--line);padding:11px 16px;border-radius:10px;opacity:0;transform:translateY(8px);transition:.25s;z-index:30;max-width:420px}
#toast.show{opacity:1;transform:none}#toast.bad{border-color:#5a2524;color:#ffb4b2}
</style></head><body>
<header>
  <div class="brand"><div class="logo">B</div><div>Relay de canais<br><small id="ver">BoxServer</small></div></div>
  <div class="spacer"></div>
  <div class="mem"><div class="lbl"><span>Memória do container</span><span id="memtxt">–</span></div><div class="bar" id="membar"><i></i></div></div>
  <button id="logout" title="Sair">Sair</button>
</header>
<main>
  <div class="banner" id="banner"></div>
  <div class="toolbar"><button class="pri" id="addBtn">+ Adicionar canal</button><span class="count" id="count"></span></div>
  <div class="empty" id="empty" style="display:none">Nenhum canal cadastrado. Use "Adicionar canal".</div>
  <div class="grid" id="grid"></div>
  <p class="note" style="margin-top:18px">O canal 1 também responde em <code>/live.m3u8</code> (endereço usado até a v0.4). Números de canais removidos não são reaproveitados. Mudar a URL, o atraso, as conexões ou o "Fixar servidor" de um canal ligado reinicia o canal e a reserva começa do zero.</p>
</main>
<div id="login"><form id="loginForm"><div class="brand"><div class="logo">B</div><div>Relay de canais<br><small>Entrar no painel</small></div></div>
<input type="password" id="pw" placeholder="Senha do painel" autocomplete="current-password" required>
<div class="err" id="loginErr"></div><button class="pri" type="submit">Entrar</button></form></div>
<div id="toast"></div>
<script>
(() => {
const KEY = 'relayAdminPw';
let pw = sessionStorage.getItem(KEY) || '';
let data = null, timer = null;
const dirty = {}, busy = {};
const $ = (s, el = document) => el.querySelector(s);
const grid = $('#grid');

function toast(msg, bad) {
  const t = $('#toast'); t.textContent = msg; t.className = 'show' + (bad ? ' bad' : '');
  clearTimeout(t._h); t._h = setTimeout(() => t.className = '', 3500);
}
function showLogin(msg) {
  $('#login').style.display = 'grid'; $('#loginErr').textContent = msg || ''; $('#pw').focus();
  clearInterval(timer); timer = null;
}
async function api(path, opts = {}) {
  const r = await fetch(path, {...opts, headers: {'X-Admin-Password': pw, 'Content-Type': 'application/json'}});
  let j = {}; try { j = await r.json(); } catch (e) {}
  if (r.status === 401) { sessionStorage.removeItem(KEY); showLogin('Senha incorreta'); throw new Error('auth'); }
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}
const fmtS = s => { if (s == null) return '–'; s = Math.max(0, Math.round(s)); const m = Math.floor(s / 60); return m ? m + ':' + String(s % 60).padStart(2, '0') : s + 's'; };
const fmtN = (v, d = 1) => v == null ? '–' : Number(v).toFixed(d);
function delayLabel(s) { if (s == null) return '–'; if (!s) return 'sem atraso'; return s % 60 ? s + ' s' : (s / 60) + ' min'; }

function statusOf(c) {
  if (!c.running && c.error) return ['bad', 'Erro ao ligar'];
  if (!c.running) return c.mode === 'on_demand' && c.url ? ['wait', 'Em espera'] : ['off', 'Desligado'];
  const m = c.metrics || {}, s = c.status || '';
  if (m.buffering) return ['buf', 'Enchendo reserva'];
  if (m.rebuilding) return ['buf', 'Recompondo reserva'];
  if (s === 'ok') return ['ok', 'No ar'];
  if (s === 'starting') return ['buf', 'Iniciando'];
  if (s.startsWith('stalled')) return ['bad', 'Fonte parada'];
  if (s.startsWith('degraded')) return ['warn', 'Instável'];
  return ['warn', s];
}

function card(c) {
  const el = document.createElement('section');
  el.className = 'card'; el.dataset.slot = c.slot;
  el.innerHTML = `
  <div class="top"><div class="num">${c.slot}</div><input class="name" maxlength="40" aria-label="Nome do canal"><span class="pill off">–</span></div>
  <label class="f">URL da fonte (HLS .m3u8)<input class="url" spellcheck="false" placeholder="http://servidor/caminho/index.m3u8"></label>
  <div class="row2">
    <label class="f">Modo<select class="mode"></select></label>
    <label class="f">Sob demanda: desligar sem clientes após<select class="idle"></select></label>
  </div>
  <div class="row3">
    <label class="f">Atraso mínimo<select class="delay"></select></label>
    <label class="f">Conexões com a fonte<select class="par"></select></label>
    <div class="pinbox">Fixar servidor da fonte<button type="button" class="pin" title="Ligado: fica no mesmo servidor da fonte e só troca se ele der erro (evita lacunas quando a fonte alterna entre servidores). Desligado: volta à URL original a cada recarga (útil se a fonte usa token que expira rápido).">–</button></div>
  </div>
  <div class="prog"><div class="lbl"><span class="plbl">Enchendo reserva</span><span class="ptxt"></span></div><div class="bar"><i></i></div></div>
  <div class="stats">
    <div class="stat s-cush"><b>–</b><span>reserva</span></div>
    <div class="stat s-rate"><b>–</b><span>Mbit/s canal</span></div>
    <div class="stat s-ing"><b>–</b><span>coleta</span></div>
    <div class="stat s-cli"><b>–</b><span>clientes</span></div>
    <div class="stat s-gap"><b>–</b><span>lacunas</span></div>
    <div class="stat s-mem"><b>–</b><span>MB memória</span></div>
  </div>
  <div class="idle-note"></div>
  <div class="link"><input readonly class="addr"><button class="copy">Copiar</button></div>
  <div class="actions"><button class="pri save">Salvar</button><button class="toggle">Ligar</button><button class="restart">Reiniciar</button>
    <span class="dirty">alterações não salvas</span><span class="err"></span><button class="remove">Remover</button></div>`;
  const d = data.defaults || {};
  const opt = (sel, items) => { for (const [v, t] of items) { const o = document.createElement('option'); o.value = v; o.textContent = t; sel.appendChild(o); } };
  opt($('.delay', el), [['', 'Padrão do .env (' + delayLabel(d.live_delay_seconds) + ')'], ['0', 'Sem atraso'], ['30', '30 segundos'], ['60', '1 minuto'], ['120', '2 minutos'], ['180', '3 minutos'], ['300', '5 minutos'], ['600', '10 minutos']]);
  opt($('.par', el), [['', 'Padrão do .env (' + (d.max_parallel_downloads ?? '–') + ')'], ['1', '1'], ['2', '2'], ['3', '3'], ['4', '4']]);
  opt($('.mode', el), [['always', 'Sempre ligado (liga/desliga no painel)'], ['on_demand', 'Sob demanda (liga quando alguém pedir)']]);
  opt($('.idle', el), [['', 'Padrão do .env (' + (d.idle_stop_minutes ?? 20) + ' min)'], ['0.5', '30 s'], ['1', '1 min'], ['2', '2 min'], ['3', '3 min'], ['5', '5 min'], ['10', '10 min'], ['20', '20 min'], ['30', '30 min'], ['60', '1 hora'], ['180', '3 horas']]);
  const syncIdle = () => { $('.idle', el).disabled = $('.mode', el).value !== 'on_demand'; };
  $('.mode', el).addEventListener('change', syncIdle); el._syncIdle = syncIdle;
  const mark = () => { dirty[c.slot] = true; $('.dirty', el).style.display = 'inline'; };
  el.querySelectorAll('input.name,input.url,select').forEach(i => { i.addEventListener('input', mark); i.addEventListener('change', mark); });
  $('.save', el).onclick = () => save(c.slot);
  $('.toggle', el).onclick = () => toggle(c.slot);
  $('.pin', el).onclick = () => togglePin(c.slot);
  $('.restart', el).onclick = () => act(c.slot, 'restart', 'Reiniciar o canal? A reserva começa do zero.');
  $('.remove', el).onclick = () => removeChannel(c.slot);
  $('.copy', el).onclick = () => { const i = $('.addr', el); i.select();
    (navigator.clipboard ? navigator.clipboard.writeText(i.value) : Promise.reject()).then(() => toast('Endereço copiado'))
      .catch(() => { document.execCommand('copy'); toast('Endereço copiado'); }); };
  return el;
}

function fillForm(el, c) {
  $('.name', el).value = c.name || '';
  $('.url', el).value = c.url || '';
  $('.delay', el).value = c.live_delay_seconds == null ? '' : String(Math.round(c.live_delay_seconds));
  if (!$('.delay', el).value && c.live_delay_seconds != null) {
    const o = document.createElement('option'); o.value = String(Math.round(c.live_delay_seconds));
    o.textContent = delayLabel(c.live_delay_seconds); $('.delay', el).appendChild(o); $('.delay', el).value = o.value; }
  $('.par', el).value = c.max_parallel_downloads == null ? '' : String(c.max_parallel_downloads);
  $('.mode', el).value = c.mode || 'always';
  $('.idle', el).value = c.idle_stop_minutes == null ? '' : String(c.idle_stop_minutes);
  if (!$('.idle', el).value && c.idle_stop_minutes != null) {
    const o = document.createElement('option'); o.value = String(c.idle_stop_minutes);
    o.textContent = o.value + ' min'; $('.idle', el).appendChild(o); $('.idle', el).value = o.value; }
  el._syncIdle && el._syncIdle();
  dirty[c.slot] = false; $('.dirty', el).style.display = 'none';
}

function update(el, c) {
  const [cls, txt] = statusOf(c), pill = $('.pill', el);
  pill.className = 'pill ' + cls; pill.textContent = txt;
  el.classList.toggle('on', c.running);
  const m = c.metrics || {}, e = c.effective || {};
  const set = (k, v, alert) => { const s = $(k, el); $('b', s).textContent = v; s.classList.toggle('alert', !!alert); };
  set('.s-cush', c.running ? fmtS(m.cushion_seconds) : '–', c.running && e.live_delay_seconds && m.cushion_seconds < e.live_delay_seconds - 15 && !m.buffering);
  set('.s-rate', c.running ? fmtN(m.stream_mbps) : '–');
  set('.s-ing', c.running && m.ingest_ratio != null ? fmtN(m.ingest_ratio, 2) + 'x' : '–', c.running && m.ingest_ratio != null && m.ingest_ratio < 0.9);
  set('.s-cli', c.running ? (m.clients ?? 0) : '–');
  set('.s-gap', c.running ? (m.gaps ?? 0) : '–', m.gaps > 0);
  set('.s-mem', c.running ? fmtN(m.retained_mb, 0) : '–');
  const prog = $('.prog', el), target = e.live_delay_seconds || 0;
  if (c.running && m.buffering && target) {
    prog.style.display = 'block'; const have = m.buffering_progress_seconds || 0;
    $('.plbl', prog).textContent = 'Enchendo reserva (o canal aparece quando completar)';
    $('.ptxt', prog).textContent = fmtS(have) + ' de ' + fmtS(target);
    $('.bar>i', prog).style.width = Math.min(100, 100 * have / target) + '%';
  } else prog.style.display = 'none';
  $('.addr', el).value = location.origin + (c.slot === 1 ? '/live.m3u8' : c.path);
  const t = $('.toggle', el); t.textContent = c.running ? 'Desligar' : (c.mode === 'on_demand' ? 'Ligar agora' : 'Ligar'); t.classList.toggle('danger', c.running);
  const note = $('.idle-note', el), lim = c.idle_stop_minutes_effective;
  if (c.mode === 'on_demand') {
    note.style.display = 'block';
    if (!c.running) note.textContent = `Sob demanda: liga sozinho quando alguém abrir o link (a imagem aparece depois de encher a reserva) e desliga após ${lim} min sem clientes.`;
    else { const idle = m.idle_seconds || 0;
      note.textContent = (m.clients ? `Sob demanda: ${m.clients} cliente(s) assistindo.` : `Sob demanda: sem clientes há ${fmtS(idle)}; desliga em ${fmtS(Math.max(0, lim * 60 - idle))}.`) + (c.started_by === 'demand' ? ' Ligado por pedido.' : ''); }
  } else note.style.display = 'none';
  const pin = $('.pin', el), pinned = isPinned(c);
  pin.textContent = pinned ? 'Ligado' : 'Desligado'; pin.classList.toggle('on', pinned); pin.disabled = !!busy[c.slot];
  $('.restart', el).disabled = !c.running || busy[c.slot];
  $('.save', el).disabled = t.disabled = $('.remove', el).disabled = !!busy[c.slot];
  $('.err', el).textContent = c.error ? 'Erro: ' + c.error : '';
  el.title = c.running ? `Atraso ${delayLabel(e.live_delay_seconds)} · ${e.max_parallel_downloads} conexões · download ${fmtN(m.download_mbps)} Mbit/s · ${m.discontinuities ?? 0} descontinuidades` : '';
}

function render() {
  $('#ver').textContent = 'BoxServer · v' + data.version;
  const ban = [];
  if (!data.persist) ban.push('Sem pasta de dados (RELAY_STATE_DIR): as alterações valem só até o container reiniciar.');
  if (data.persist_error) ban.push('Não consegui salvar a configuração: ' + data.persist_error);
  const b = $('#banner'); b.textContent = ban.join(' '); b.style.display = ban.length ? 'block' : 'none';
  const mem = data.memory || {}, est = data.channels.reduce((a, c) => a + ((c.metrics || {}).estimated_mb || 0), 0);
  if (mem.used && mem.limit) {
    const p = 100 * mem.used / mem.limit;
    $('#memtxt').textContent = `${Math.round(mem.used / 1048576)} / ${Math.round(mem.limit / 1048576)} MB` + (est ? ` · previsto ${est} MB` : '');
    $('#membar>i').style.width = Math.min(100, p) + '%';
    $('#membar').classList.toggle('hi', p > 85 || est * 1048576 > mem.limit * 0.85);
  } else $('#memtxt').textContent = mem.used ? Math.round(mem.used / 1048576) + ' MB' : '–';
  const slots = new Set(data.channels.map(c => c.slot));
  grid.querySelectorAll('.card').forEach(el => { if (!slots.has(Number(el.dataset.slot))) el.remove(); });
  for (const c of data.channels) {
    let el = grid.querySelector(`[data-slot="${c.slot}"]`);
    if (!el) { el = card(c); grid.appendChild(el); fillForm(el, c); }
    else if (!dirty[c.slot] && !busy[c.slot] && !el.contains(document.activeElement)) fillForm(el, c);
    update(el, c);
  }
  const n = data.channels.length, max = data.max_channels;
  const full = max > 0 && n >= max;
  $('#count').textContent = `${n}${max > 0 ? ' de ' + max : ''} canais · ${data.channels.filter(c => c.running).length} ligado(s)`;
  $('#addBtn').disabled = full;
  $('#addBtn').title = full ? 'Limite atingido (MAX_CHANNELS no .env)' : '';
  $('#empty').style.display = n ? 'none' : 'block';
}

async function refresh() {
  try { data = await api('/api/channels'); render(); }
  catch (e) { if (e.message !== 'auth') toast('Sem resposta do relay: ' + e.message, true); }
}
function chan(slot) { return data.channels.find(c => c.slot === slot); }
function formOf(slot) {
  const el = grid.querySelector(`[data-slot="${slot}"]`), v = s => $(s, el).value;
  return {name: v('.name'), url: v('.url').trim(),
    live_delay_seconds: v('.delay') === '' ? null : Number(v('.delay')),
    max_parallel_downloads: v('.par') === '' ? null : Number(v('.par')),
    mode: v('.mode'), idle_stop_minutes: v('.idle') === '' ? null : Number(v('.idle'))};
}
async function run(slot, fn) { busy[slot] = true; render(); try { await fn(); } catch (e) { if (e.message !== 'auth') toast(e.message, true); } finally { busy[slot] = false; await refresh(); } }
async function save(slot) {
  const f = formOf(slot), c = chan(slot);
  const runtime = ['url', 'live_delay_seconds', 'max_parallel_downloads'].some(k => (f[k] ?? null) !== (c[k] ?? null));
  if (c.running && runtime && !confirm('Salvar vai reiniciar o canal ' + slot + ' e a reserva começa do zero. Continuar?')) return;
  await run(slot, async () => {
    const r = await api('/api/channels/' + slot, {method: 'POST', body: JSON.stringify(f)});
    dirty[slot] = false; toast(r.restarted ? 'Salvo. Canal reiniciado.' : 'Salvo.');
  });
}
async function act(slot, action, ask) {
  if (ask && !confirm(ask)) return;
  await run(slot, async () => { await api('/api/channels/' + slot + '/' + action, {method: 'POST'}); toast({start: 'Canal ligado', stop: 'Canal desligado', restart: 'Canal reiniciado'}[action]); });
}
async function toggle(slot) {
  const c = chan(slot);
  if (c.running) return act(slot, 'stop', 'Desligar o canal ' + slot + '? Quem estiver assistindo perde o sinal.');
  if (dirty[slot]) { await run(slot, () => api('/api/channels/' + slot, {method: 'POST', body: JSON.stringify(formOf(slot))}).then(() => { dirty[slot] = false; })); }
  return act(slot, 'start');
}
const isPinned = c => (c.master_refresh_seconds ?? (data.defaults || {}).master_refresh_seconds) === 0;
async function togglePin(slot) {
  const c = chan(slot), on = !isPinned(c);
  if (c.running && !confirm('Isso vai reiniciar o canal ' + slot + ' e a reserva começa do zero. Continuar?')) return;
  await run(slot, async () => {
    const r = await api('/api/channels/' + slot, {method: 'POST', body: JSON.stringify({master_refresh_seconds: on ? 0 : 1})});
    toast('Fixar servidor ' + (on ? 'ligado' : 'desligado') + (r.restarted ? '. Canal reiniciado.' : '.'));
  });
}
async function addChannel() {
  $('#addBtn').disabled = true;
  try {
    const r = await api('/api/channels', {method: 'POST', body: '{}'});
    await refresh();
    const el = grid.querySelector(`[data-slot="${r.slot}"]`);
    if (el) { el.scrollIntoView({behavior: 'smooth', block: 'center'}); $('.name', el).select(); }
    toast('Canal ' + r.slot + ' criado. Preencha a URL, salve e ligue.');
  } catch (e) { if (e.message !== 'auth') toast(e.message, true); await refresh(); }
}
async function removeChannel(slot) {
  const c = chan(slot);
  let msg = `Remover o canal ${slot} (${c.name})? O endereço /ch/${slot}/live.m3u8 deixa de funcionar`;
  if (slot === 1) msg += ' e o /live.m3u8 também';
  if (!confirm(msg + '. O número ' + slot + ' não será reaproveitado.')) return;
  await run(slot, async () => { await api('/api/channels/' + slot + '/delete', {method: 'POST'}); delete dirty[slot]; toast('Canal ' + slot + ' removido'); });
}
$('#addBtn').onclick = addChannel;
function startPolling() { if (!timer) timer = setInterval(() => { if (!document.hidden) refresh(); }, 3000); }

$('#loginForm').onsubmit = async ev => {
  ev.preventDefault(); pw = $('#pw').value;
  try { data = await api('/api/channels'); sessionStorage.setItem(KEY, pw); $('#login').style.display = 'none'; render(); startPolling(); }
  catch (e) { if (e.message !== 'auth') $('#loginErr').textContent = e.message; }
};
$('#logout').onclick = () => { sessionStorage.removeItem(KEY); pw = ''; showLogin(); };
if (pw) { refresh().then(() => { if (data) startPolling(); }); } else showLogin();
})();
</script></body></html>'''


# =========================================================================
# HTTP server
# =========================================================================
class RelayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address, target):
        self.manager = target if isinstance(target, ChannelManager) else ChannelManager.single(target)
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            log.debug("client %s disconnected: %s", client_address[0], type(exc).__name__)
            return
        ev("http_handler_error", logging.ERROR, ip=client_address[0], error=type(exc).__name__,
           detail=safe_text(exc))


_CH_RE = re.compile(r"/ch/(\d{1,6})/(live\.m3u8|live\.m3u|health)")
_SLATE_RE = re.compile(r"/slate/(\d{1,4})\.ts")
_SLATE_SEQ_RE = re.compile(r"/slate/([0-9a-f]{6})-(\d{1,20})\.ts")
_API_RE = re.compile(r"/api/channels/(\d{1,6})(?:/(start|stop|restart|delete))?")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BoxServer-HLS-Relay/" + VERSION
    sys_version = ""
    timeout = 30

    def log_message(self, fmt, *args):
        log.debug("http %s %s", self.client_address[0], safe_text(fmt % args))

    def _client_ip(self) -> str:
        return self.client_address[0].split("%", 1)[0]

    def _allowed(self) -> bool:
        try:
            ip = ipaddress.ip_address(self._client_ip())
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in net for net in self.server.manager.base.client_networks)

    def _send(self, status, body, ctype, head=False, cache="no-store", extra=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if not head:
                self.wfile.write(body)
            return True
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
            return False

    def _json(self, status, obj, head=False):
        return self._send(status, json.dumps(obj, indent=2, default=str).encode(), "application/json", head)

    def _deny(self, head):
        manager = self.server.manager
        with manager.lock:
            manager.denied += 1
        ev("client_denied", logging.WARNING, ip=self._client_ip())
        return self._send(403, b"Forbidden\n", "text/plain", head)

    def do_GET(self):
        self._route(head=False)

    def do_HEAD(self):
        self._route(head=True)

    # ------------------------------------------------------------ media
    def _playlist(self, slot, head):
        manager = self.server.manager
        relay = manager.relay(slot)
        if relay is None:
            if slot not in manager.specs:
                return self._send(404, f"Canal {slot} não existe\n".encode(), "text/plain", head)
            try:
                if manager.ensure_on_demand(slot):
                    ev("channel_demand_start", ch=slot, ip=self._client_ip())
            except Exception as exc:
                manager.last_error[slot] = safe_text(exc)
                return self._send(503, f"Canal {slot}: falha ao ligar ({safe_text(exc)})\n".encode(),
                                  "text/plain", head, extra={"Retry-After": "30"})
            relay = manager.relay(slot)
            if relay is None:
                return self._send(503, f"Canal {slot} desligado\n".encode(), "text/plain", head,
                                  extra={"Retry-After": "10"})
        relay.note_client(self._client_ip(), "playlist")  # before playlist(): ends an idle rebuild
        body = relay.playlist()
        if body is None:
            with relay.lock:
                have = sum(s.duration for s in relay.store.values())
            msg = (f"Buffering: {have:.0f}s of {relay.cfg.live_delay_seconds:.0f}s live delay collected\n"
                   if relay.cfg.live_delay_seconds else "Buffering: no segment published yet\n")
            return self._send(503, msg.encode(), "text/plain", head, extra={"Retry-After": "5"})
        return self._send(200, body, "application/vnd.apple.mpegurl", head, cache="no-cache")

    def _health(self, slot, head, with_channels=False):
        manager = self.server.manager
        relay = manager.relay(slot)
        body = relay.health() if relay else {"version": VERSION, "status": "off", "channel": slot}
        if with_channels:
            body["channels"] = manager.summary()["channels"]
            body["clients_denied"] = manager.denied
        return self._json(200, body, head)

    def _segment(self, name, head):
        ip = self._client_ip()
        relay = self.server.manager.by_instance(name[:6])
        seg = relay.segment(name) if relay else None
        if seg is None:
            if relay is not None:
                relay.note_client(ip, "segment", ok=False)
            ev("client_segment_expired", logging.WARNING, ip=ip, name=name[:40],
               ch=relay.channel if relay else "?")
            return self._send(404, b"Segment expired or unknown\n", "text/plain", head)
        ok = self._send(200, seg.payload, "video/mp2t", head, cache="max-age=600")
        relay.note_client(ip, "segment", seg.local_seq, True, len(seg.payload) if ok and not head else 0)
        return None

    def _route(self, head):
        if not self._allowed():
            return self._deny(head)
        path = self.path.split("?", 1)[0]
        if path in ("/live.m3u8", "/live.m3u"):
            return self._playlist(1, head)
        match = _CH_RE.fullmatch(path)
        if match:
            slot = int(match.group(1))
            if slot not in self.server.manager.specs:
                return self._send(404, b"Unknown channel\n", "text/plain", head)
            if match.group(2) == "health":
                return self._health(slot, head)
            return self._playlist(slot, head)
        if path == "/health":
            return self._health(1, head, with_channels=True)
        match = _SLATE_RE.fullmatch(path)
        if match:
            slate = self.server.manager.slate
            data = slate.segment(int(match.group(1))) if slate else None
            if data is None:
                return self._send(404, b"Unknown slate segment\n", "text/plain", head)
            return self._send(200, data, "video/mp2t", head, cache="max-age=600")
        match = _SLATE_SEQ_RE.fullmatch(path)
        if match:
            relay = self.server.manager.by_instance(match.group(1))
            data = relay.slate_payload(int(match.group(2))) if relay else None
            if data is None:
                return self._send(404, b"Unknown slate segment\n", "text/plain", head)
            return self._send(200, data, "video/mp2t", head, cache="max-age=600")
        if path.startswith("/segment/") and path.endswith(".ts"):
            return self._segment(path[len("/segment/"):-3], head)
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon", head, cache="max-age=86400")
        if path in ("/admin", "/admin/"):
            return self._send(200, ADMIN_HTML.encode("utf-8"), "text/html; charset=utf-8", head)
        if path == "/api/channels":
            if not self._authorized():
                return None
            return self._json(200, self.server.manager.summary(include_urls=True), head)
        if path == "/":
            lines = [f"BoxServer HLS Relay {VERSION}", "/admin  (painel)", "/live.m3u8  (canal 1)"]
            lines += [f"/ch/{i}/live.m3u8" for i in sorted(self.server.manager.specs)] + ["/health"]
            return self._send(200, ("\n".join(lines) + "\n").encode(), "text/plain", head)
        return self._send(404, b"Use /live.m3u8, /ch/N/live.m3u8, /admin or /health\n", "text/plain", head)

    # ------------------------------------------------------------ admin API
    def _authorized(self) -> bool:
        manager = self.server.manager
        if not manager.admin_password:
            self._json(403, {"error": "Painel desativado: defina ADMIN_PASSWORD no .env "
                                      "ou crie data/admin_password"})
            return False
        given = self.headers.get("X-Admin-Password", "")
        if hmac.compare_digest(given.encode("utf-8"), manager.admin_password.encode("utf-8")):
            return True
        time.sleep(0.5)  # slows down guessing
        ev("admin_auth_failed", logging.WARNING, ip=self._client_ip())
        self._json(401, {"error": "Senha incorreta"})
        return False

    def do_POST(self):
        if not self._allowed():
            return self._deny(False)
        path = self.path.split("?", 1)[0]
        manager = self.server.manager
        match = _API_RE.fullmatch(path)
        if path != "/api/channels" and (not match or int(match.group(1)) not in manager.specs):
            return self._json(404, {"error": "Canal ou rota desconhecida"})
        if not self._authorized():
            return None
        slot, action = (int(match.group(1)), match.group(2)) if match else (None, "add")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 16384:
                return self._json(413, {"error": "Corpo grande demais"})
            raw = self.rfile.read(length) if length else b""
            if action == "add":
                data = json.loads(raw or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("JSON inválido")
                new_slot = manager.add(data)
                ev("admin_channel_added", ch=new_slot, ip=self._client_ip())
                return self._json(200, {"ok": True, "slot": new_slot})
            if action == "delete":
                manager.remove(slot)
                ev("admin_channel_removed", ch=slot, ip=self._client_ip())
                return self._json(200, {"ok": True})
            if action is None:
                data = json.loads(raw or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("JSON inválido")
                restarted = manager.update(slot, data)
                ev("admin_channel_updated", ch=slot, ip=self._client_ip(), restarted=restarted)
                return self._json(200, {"ok": True, "restarted": restarted})
            getattr(manager, action)(slot)
            ev("admin_channel_" + action, ch=slot, ip=self._client_ip())
            return self._json(200, {"ok": True})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:
            ev("admin_error", logging.ERROR, ch=slot, detail=safe_text(exc))
            return self._json(500, {"error": safe_text(exc)})


# =========================================================================
# One-shot throughput probe (python relay.py --probe [N])
# =========================================================================
def probe(cfg: Config, count: int = 4) -> dict:
    """Download the newest N segments once serially and once 2-at-a-time and
    compare with the stream bitrate. Prints no URLs. Opens at most 3 extra
    connections to the origin for a few seconds."""
    relay = Relay(cfg)
    text, final = relay._resolve()
    pl = parse_media_playlist(text, final)
    items = pl.items[-count:]
    timeout = max(4.0, min(cfg.http_timeout, 2.0 * pl.target_duration))
    report = {"variant": relay.variant, "target_duration": pl.target_duration,
              "origin_window_segments": len(pl.items), "serial": [], "parallel": {}}
    print(f"variant={relay.variant} target={pl.target_duration}s origin_window={len(pl.items)} segments")

    def one(item):
        try:
            body, _, secs = relay.http.get(item.uri, timeout=timeout, deadline_s=cfg.segment_max_seconds,
                                           max_bytes=cfg.max_cache_mb * MB)
        except FetchError as exc:
            return {"seq": item.seq, "error": exc.category}
        info = inspect_ts(body)
        return {"seq": item.seq, "duration": item.duration, "mb": round(len(body) / MB, 2),
                "seconds": round(secs, 2), "mbps": round(len(body) * 8 / secs / 1e6, 2),
                "bitrate_mbps": round(len(body) * 8 / item.duration / 1e6, 2),
                "realtime_factor": round(item.duration / secs, 2), "ts_ok": info.ok,
                "keyframe_start": info.starts_with_keyframe, "cc_errors": info.cc_errors,
                "codec": info.video_codec, "audio_streams": info.audio_streams}

    for item in items:
        res = one(item)
        report["serial"].append(res)
        print("serial  ", res)
    results = []
    start = time.monotonic()
    threads = [threading.Thread(target=lambda it=it: results.append(one(it))) for it in items[-2:]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.monotonic() - start
    ok = [x for x in results if "error" not in x]
    if ok:
        dur = sum(x["duration"] for x in ok)
        report["parallel"] = {"segments": len(ok), "wall_seconds": round(wall, 2),
                              "aggregate_realtime_factor": round(dur / wall, 2)}
    print("parallel", report["parallel"])
    good = [x for x in report["serial"] if "error" not in x]
    verdict = []
    if good:
        rt = sum(x["duration"] for x in good) / sum(x["seconds"] for x in good)
        report["serial_realtime_factor"] = round(rt, 2)
        if rt < 1.0:
            verdict.append("ONE connection is SLOWER than real time: no buffer can prevent freezes "
                           "unless parallel downloads help (see below) or a lower variant is used")
        elif rt < 1.3:
            verdict.append("one connection is only slightly faster than real time: little margin "
                           "for retries; keep MAX_PARALLEL_DOWNLOADS=2")
        else:
            verdict.append("single-connection throughput has margin over the stream bitrate")
        par = report["parallel"].get("aggregate_realtime_factor")
        if par and rt < 2.0 and par > rt * 1.3:
            verdict.append("parallel downloads increase throughput (per-connection limit)")
        if any(x.get("keyframe_start") is False for x in good):
            verdict.append("segments do NOT start with an IDR frame: every gap/discontinuity or "
                           "join will show 'non-existing PPS' until the next keyframe")
    else:
        verdict.append("no segment could be downloaded")
    report["verdict"] = verdict
    for line in verdict:
        print("=>", line)
    return report


def _admin_password(state_dir):
    password = (os.environ.get("ADMIN_PASSWORD") or "").strip()
    if not password and state_dir:
        try:
            with open(os.path.join(state_dir, "admin_password"), encoding="utf-8") as fh:
                password = fh.read().strip()
        except OSError:
            password = ""
    return password


def main():
    try:
        cfg = Config.from_env(require_source=False)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        sys.exit(2)
    state_dir = (os.environ.get("RELAY_STATE_DIR") or "").strip() or None
    if "--probe" in sys.argv:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
        args = sys.argv[sys.argv.index("--probe") + 1:]
        count = int(args[0]) if args and args[0].isdigit() else 4
        slot = int(args[args.index("--ch") + 1]) if "--ch" in args else 1
        manager = ChannelManager(cfg, state_dir)
        spec = manager.specs.get(slot) or {}
        if not spec.get("url"):
            print(f"probe failed: canal {slot} sem URL", file=sys.stderr)
            sys.exit(1)
        try:
            probe(manager.config_for(spec), max(1, min(count, 10)))
        except (FetchError, PlaylistError) as exc:
            print(f"probe failed: {safe_text(exc)}", file=sys.stderr)
            sys.exit(1)
        return
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")
    ev.fmt = cfg.log_format
    for note in cfg.notes:
        ev("config_adjusted", logging.WARNING, detail=note)
    if state_dir and not os.access(state_dir, os.W_OK):
        ev("state_dir_not_writable", logging.ERROR, path=state_dir,
           detail="channel changes made in /admin will not survive a restart")
    manager = ChannelManager(cfg, state_dir, _admin_password(state_dir))
    manager.start_enabled()
    manager.start_reaper()
    server = RelayHTTPServer((cfg.listen_host, cfg.port), manager)
    ev("listening", version=VERSION, host=cfg.listen_host, port=cfg.port,
       channels_on=len(manager.relays), channels=len(manager.specs), admin=bool(manager.admin_password),
       persistent=bool(state_dir), live_delay_s=cfg.live_delay_seconds,
       parallel=cfg.max_parallel_downloads, publish_window=cfg.publish_window_segments)
    if not manager.admin_password:
        ev("admin_disabled", logging.WARNING,
           detail="set ADMIN_PASSWORD in .env (or data/admin_password) to use /admin")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.stop_all()
        server.server_close()


if __name__ == "__main__":
    main()
