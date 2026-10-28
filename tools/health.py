#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WANG-TV subscription health checker / fixer / generator.

Pure standard library. Reads truth sources, probes every live channel and every
VOD site at segment level (not just HTTP 200), decides failover, regenerates
live.m3u + tvbox.json, self-validates the output and rolls back if the result
would be broken.

Exit code 0 always (workflow continues), rollback/warnings land in state/health.json.
"""
import argparse
import concurrent.futures
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

UA = "Mozilla/5.0 (Linux; Android 11; TV)"
TZ_CN = timezone(timedelta(hours=8))
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface every 3xx hop so we can see ISP-internal redirect targets."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)
# ISP-internal hosts must never go through a local/VPN proxy, so keep a direct one.
_DIRECT_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
DIRECT_ONLY = False
_DNS_CACHE = {}

SEG_MIN_BYTES = 20 * 1024          # a segment must be > 20KB to count as real media
SEG_CHECK_COUNT = 2                # download the last 2 segments, need 1 good
MAX_FAILS_BEFORE_SWITCH = 2        # consecutive failed runs before failover
MIN_SITES = 5                      # never publish fewer than this many VOD sites
MIN_CHANNELS = 30                  # format self-check floor for live.m3u
HISTORY_KEEP = 30
VOD_QUERY = "庆余年"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- http
def _read_capped(resp, max_bytes):
    chunks, total = [], 0
    while total < max_bytes:
        try:
            buf = resp.read(min(65536, max_bytes - total))
        except Exception:
            break
        if not buf:
            break
        chunks.append(buf)
        total += len(buf)
    return b"".join(chunks)


def classify(err, status=None):
    """Map a failure to a short stable tag."""
    if status:
        if status in (403, 401):
            return "http-403"
        if status == 404:
            return "http-404"
        if status == 429:
            return "http-429"
        if 500 <= status < 600:
            return "http-5xx"
        return "http-%d" % status
    e = (err or "").lower()
    if "timed out" in e or "timeout" in e:
        return "timeout"
    if "name or service not known" in e or "nodename" in e or "getaddrinfo" in e or "name resolution" in e:
        return "dns"
    if "refused" in e:
        return "conn-refused"
    if "ssl" in e or "certificate" in e:
        return "tls"
    if "reset" in e or "aborted" in e:
        return "conn-reset"
    return "net"


def is_private_host(url):
    """True when the URL points at a non-public address (ISP-internal hop)."""
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return False
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host in ("localhost",)
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def resolves_private(url):
    """True when a hostname resolves only into private address space (ISP DNS)."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return False
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return is_private_host(url)
    except ValueError:
        pass
    key = (host, port)
    if key in _DNS_CACHE:
        return _DNS_CACHE[key]
    verdict = False
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        ips = [i[4][0] for i in infos]
        parsed = [ipaddress.ip_address(i) for i in ips]
        verdict = bool(parsed) and all(p.is_private or p.is_loopback for p in parsed)
    except Exception:
        verdict = False
    _DNS_CACHE[key] = verdict
    return verdict


def _opener_for(url):
    if DIRECT_ONLY or is_private_host(url) or resolves_private(url):
        return _DIRECT_OPENER
    return _NO_REDIRECT_OPENER


def http_get(url, timeout=15, max_bytes=2 * 1024 * 1024):
    """GET with redirect following. Never raises."""
    started = time.time()
    out = {"status": None, "body": b"", "err": None, "error": None,
           "url": url, "ms": 0, "ctype": "", "private_hop": False, "hops": 0}
    current = url
    hops = 0
    for hop in range(6):
        hops = hop + 1
        req = urllib.request.Request(current, headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "close",
        })
        if resolves_private(current):
            out["private_hop"] = True
        try:
            resp = _opener_for(current).open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            loc = exc.headers.get("Location") if exc.headers else None
            if exc.code in (301, 302, 303, 307, 308) and loc and hop < 5:
                current = urllib.parse.urljoin(current, loc)
                if is_private_host(current):
                    out["private_hop"] = True
                continue
            out["status"] = exc.code
            out["error"] = classify(None, exc.code)
            try:
                out["body"] = _read_capped(exc, 4096)
            except Exception:
                pass
            break
        except (socket.timeout, TimeoutError):
            out["err"] = "timeout"
            out["error"] = "timeout"
            break
        except urllib.error.URLError as exc:
            out["err"] = str(getattr(exc, "reason", exc))
            out["error"] = classify(out["err"])
            break
        except Exception as exc:                               # noqa: BLE001
            out["err"] = "%s: %s" % (type(exc).__name__, exc)
            out["error"] = classify(out["err"])
            break
        try:
            out["status"] = getattr(resp, "status", 200)
            out["url"] = resp.geturl() or current
            out["ctype"] = resp.headers.get("Content-Type", "") or ""
            out["body"] = _read_capped(resp, max_bytes)
        finally:
            try:
                resp.close()
            except Exception:
                pass
        if is_private_host(out["url"]):
            out["private_hop"] = True
        if out["status"] != 200 and not out["error"]:
            out["error"] = classify(None, out["status"])
        break
    out["hops"] = hops
    out["ms"] = int((time.time() - started) * 1000)
    return out


def text_of(body):
    return body.decode("utf-8", "replace").lstrip("\ufeff")


# --------------------------------------------------------------------------- live
def _pick_variant(master_text, base_url):
    best, best_bw = None, -1
    lines = [l.strip() for l in master_text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", line)
            bw = int(m.group(1)) if m else 0
            for cand in lines[i + 1:]:
                if not cand.startswith("#"):
                    if bw > best_bw:
                        best, best_bw = cand, bw
                    break
    if not best:
        return None
    return urllib.parse.urljoin(base_url, best)


def probe_live(url, timeout=15):
    """Segment-level HLS probe. Returns dict(ok, error, segments[...], ms)."""
    started = time.time()
    res = {"ok": False, "error": None, "segments": [], "ms": 0,
           "final_url": url, "bytes": 0, "unverified": False}

    pl = http_get(url, timeout=timeout, max_bytes=512 * 1024)
    if pl["error"]:
        res["observed"] = pl["error"]
        if pl.get("private_hop"):
            # gateway answered 302 into a private ISP address: only reachable from
            # inside that ISP, so a cloud runner cannot judge this line at all.
            res.update(error="vantage:private-redirect", unverified=True,
                       ms=int((time.time() - started) * 1000))
            return res
        if pl["error"] == "dns":
            # ISP-bound hostname that the runner's resolver cannot even resolve:
            # a vantage artefact, not evidence that the line is dead.
            res.update(error="vantage:dns-unresolvable", unverified=True,
                       ms=int((time.time() - started) * 1000))
            return res
        res.update(error="playlist:" + pl["error"], ms=int((time.time() - started) * 1000))
        return res
    text = text_of(pl["body"])
    if "#EXTM3U" not in text:
        res.update(error="playlist:no-extm3u", ms=int((time.time() - started) * 1000))
        return res

    if "#EXT-X-STREAM-INF" in text:                      # master playlist -> one child
        child = _pick_variant(text, pl["url"])
        if not child:
            res.update(error="playlist:no-variant", ms=int((time.time() - started) * 1000))
            return res
        pl2 = http_get(child, timeout=timeout, max_bytes=512 * 1024)
        if pl2["error"]:
            res.update(error="variant:" + pl2["error"], ms=int((time.time() - started) * 1000))
            return res
        text = text_of(pl2["body"])
        if "#EXTM3U" not in text:
            res.update(error="variant:no-extm3u", ms=int((time.time() - started) * 1000))
            return res
        pl = pl2

    base = pl["url"]
    segs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        segs.append(urllib.parse.urljoin(base, line))
    if not segs:
        res.update(error="playlist:no-segments", ms=int((time.time() - started) * 1000))
        return res

    tail = segs[-SEG_CHECK_COUNT:]
    good = 0
    detail = []
    for seg in tail:
        r = http_get(seg, timeout=timeout, max_bytes=64 * 1024)
        size = len(r["body"])
        ok = (r["status"] == 200 and size > SEG_MIN_BYTES)
        detail.append({"url": seg[-120:], "status": r["status"],
                       "bytes": size, "error": r["error"], "ok": ok})
        if ok:
            good += 1
    res["segments"] = detail
    res["bytes"] = sum(d["bytes"] for d in detail)
    res["ms"] = int((time.time() - started) * 1000)
    if good == 0:
        tag = None
        for d in detail:
            if d["error"]:
                tag = d["error"]
                break
        if not tag:
            tag = "segment:too-small"
        res["error"] = "segment:" + tag if not tag.startswith(("timeout", "http-", "dns",
                                                               "conn-", "tls", "net")) else tag
        return res
    res["ok"] = True
    return res


# --------------------------------------------------------------------------- vod
PLAY_RE = re.compile(r"https?://[^\s#$'\"<>]+")


def probe_vod_site(site, timeout=15):
    started = time.time()
    res = {"ok": False, "error": None, "ms": 0, "note": ""}
    api = (site.get("api") or "").strip()
    if not api:
        res.update(error="no-api", ms=1)
        return res

    if int(site.get("type") or 1) == 3:                   # spider js
        r = http_get(api, timeout=timeout, max_bytes=512 * 1024)
        if r["error"]:
            res.update(error="spider:" + r["error"], ms=int((time.time() - started) * 1000))
            return res
        if len(r["body"]) < 1024:
            res.update(error="spider:too-small", ms=int((time.time() - started) * 1000))
            return res
        res["note"] = "spider.js %dB" % len(r["body"])
        res["ok"] = True
        res["ms"] = int((time.time() - started) * 1000)
        return res

    sep = "&" if "?" in api else "?"
    attempts = [
        "%s%sac=videolist&wd=%s" % (api, sep, urllib.parse.quote(VOD_QUERY)),
        "%s%sac=list" % (api, sep),
    ]
    last_err = None
    for attempt in attempts:
        r = http_get(attempt, timeout=timeout, max_bytes=2 * 1024 * 1024)
        if r["error"]:
            last_err = "api:" + r["error"]
            continue
        try:
            data = json.loads(text_of(r["body"]))
        except Exception:
            last_err = "api:bad-json"
            continue
        items = data.get("list") if isinstance(data, dict) else None
        if not items:
            last_err = "api:empty-list"
            continue
        first = items[0] if isinstance(items[0], dict) else {}
        play = first.get("vod_play_url") or ""
        m = PLAY_RE.search(str(play))
        if not m:
            last_err = "api:no-play-url"
            continue
        media = m.group(0)
        if media.lower().endswith(".m3u8") or ".m3u8" in media.lower():
            mr = http_get(media, timeout=timeout, max_bytes=512 * 1024)
            if mr["error"]:
                last_err = "media:" + mr["error"]      # API is fine, media host is not
                break
            if "#EXTM3U" not in text_of(mr["body"]):
                last_err = "media:no-extm3u"
                break
            res["note"] = "list=%d media=m3u8" % len(items)
        else:
            mr = http_get(media, timeout=timeout, max_bytes=64 * 1024)
            if mr["error"] or len(mr["body"]) < 1024:
                last_err = "media:" + (mr["error"] or "too-small")
                break
            res["note"] = "list=%d media=other" % len(items)
        res["ok"] = True
        res["ms"] = int((time.time() - started) * 1000)
        return res

    res.update(error=last_err or "unknown", ms=int((time.time() - started) * 1000))
    return res


def probe_with_retry(fn, arg, timeout):
    """One retry, as required by the failover policy."""
    first = fn(arg, timeout=timeout)
    if first.get("ok"):
        return first, 1
    second = fn(arg, timeout=timeout)
    return second, 2


# --------------------------------------------------------------------------- model
def now_cn():
    return datetime.now(TZ_CN)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json(path, obj, indent=1):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=indent)
        fh.write("\n")


def write_text(path, text):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)


def live_urls_for(channel):
    """Publish the best line first, then untried / previously good fallbacks."""
    # A channel marked unavailable is kept in sources as documentation but published nowhere:
    # marking it beats shipping a line that is known to fail on every player.
    if channel.get("status") == "unavailable":
        return []
    cands = channel.get("candidates") or []
    active = cands[0]
    out = [active["url"]]
    for c in cands[1:]:
        if c.get("fails", 0) < MAX_FAILS_BEFORE_SWITCH and c["url"] not in out:
            out.append(c["url"])
    return out[:2]          # keep 1-2 URLs per channel (player-agnostic fallback)


def build_live_m3u(channels):
    lines = ["#EXTM3U"]
    for ch in channels:
        urls = live_urls_for(ch)
        if not urls:
            continue
        head = '#EXTINF:-1 group-title="%s" tvg-name="%s",%s' % (
            ch.get("group") or "其他", ch.get("name"), ch.get("name"))
        lines.append(head)
        lines.extend(urls)
    return "\n".join(lines) + "\n"


def build_tvbox(base, sites):
    out = dict(base or {})
    keep = []
    for s in sites:
        keep.append({
            "key": s["key"],
            "name": s["name"],
            "type": int(s.get("type") or 1),
            "api": s["api"],
            "searchable": 1,
            "quickSearch": 1,
            "filterable": 1 if int(s.get("type") or 1) != 3 else 0,
        })
    out["sites"] = keep
    return out


def validate_outputs(m3u_text, tv_obj):
    problems = []
    if not m3u_text.startswith("#EXTM3U"):
        problems.append("live.m3u:no-header")
    ch = 0
    cur = False
    for line in m3u_text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            ch += 1
            cur = True                      # an entry may carry several fallback URLs
        elif line and not line.startswith("#"):
            if not cur:
                problems.append("live.m3u:url-without-extinf")
            if not line.lower().startswith(("http://", "https://")):
                problems.append("live.m3u:bad-url")
    if ch < MIN_CHANNELS:
        problems.append("live.m3u:channels=%d<%d" % (ch, MIN_CHANNELS))
    sites = (tv_obj or {}).get("sites") or []
    if not sites:
        problems.append("tvbox.json:no-sites")
    for s in sites:
        if not s.get("api") or not s.get("key"):
            problems.append("tvbox.json:site-missing-field")
            break
    return problems, ch, len(sites), sum(
        1 for l in m3u_text.splitlines() if l.strip().lower().startswith(("http://", "https://")))


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--generate-only", action="store_true", help="skip all network probes")
    ap.add_argument("--limit-channels", type=int, default=0, help="debug: probe only first N")
    ap.add_argument("--no-proxy", action="store_true", help="ignore system/env proxies")
    args = ap.parse_args()
    if args.no_proxy:
        globals()["DIRECT_ONLY"] = True

    ts = now_cn()
    stamp = ts.strftime("%Y%m%d-%H%M")
    version = "v" + stamp

    src_live = os.path.join(ROOT, "sources", "live-sources.json")
    src_vod = os.path.join(ROOT, "sources", "vod-sites.json")
    base_vod = os.path.join(ROOT, "sources", "tvbox-base.json")
    live_out = os.path.join(ROOT, "live.m3u")
    tv_out = os.path.join(ROOT, "tvbox.json")
    state_path = os.path.join(ROOT, "state", "health.json")
    lastgood = os.path.join(ROOT, "state", "last-good")

    channels = load_json(src_live, [])
    sites = load_json(src_vod, [])
    if not channels or not sites:
        print("FATAL: truth sources missing/empty (channels=%d sites=%d)" % (len(channels), len(sites)))
        return 1

    warnings = []
    live_report = []
    site_report = []

    probe_channels = channels[:args.limit_channels] if args.limit_channels else channels

    if args.generate_only:
        for ch in probe_channels:
            live_report.append({"name": ch["name"], "group": ch.get("group"),
                                "url": ch["candidates"][0]["url"], "status": "skipped",
                                "fails": ch["candidates"][0].get("fails", 0),
                                "error": None, "segments": []})
        for s in sites:
            site_report.append({"key": s["key"], "name": s["name"], "status": "skipped",
                                "fails": s.get("fails", 0), "error": None, "ms": 0, "note": ""})
        warnings.append("probes skipped (--generate-only)")
    else:
        # ---------------- live channels
        def do_channel(ch):
            cands = ch["candidates"]
            active = cands[0]
            r, tries = probe_with_retry(probe_live, active["url"], args.timeout)
            rec = {"name": ch["name"], "group": ch.get("group"), "url": active["url"],
                   "switched_to": None, "fails": active.get("fails", 0),
                   "error": None, "segments": r.get("segments") or [],
                   "bytes": r.get("bytes", 0), "ms": r.get("ms", 0), "tries": tries}
            if r.get("ok"):
                active["fails"] = 0
                active["last_ok"] = ts.isoformat(timespec="seconds")
                rec["status"] = "ok"
                return rec
            if r.get("unverified"):
                rec.update(status="unverified", error=r.get("error"),
                           fails=int(active.get("fails", 0)))
                return rec
            active["fails"] = int(active.get("fails", 0)) + 1
            rec["error"] = r.get("error")
            rec["fails"] = active["fails"]
            if active["fails"] < MAX_FAILS_BEFORE_SWITCH:
                rec["status"] = "degraded"
                return rec
            others = sorted(cands[1:],
                            key=lambda c: (0 if c.get("last_ok") else 1,
                                           int(c.get("fails", 0)), int(c.get("prio", 99))))
            for cand in others:
                if cand.get("last_ok"):
                    continue
                cr, _ = probe_with_retry(probe_live, cand["url"], args.timeout)
                if cr.get("ok"):
                    cand["fails"] = 0
                    cand["last_ok"] = ts.isoformat(timespec="seconds")
                    active["fails"] = 0
                    active["last_ok"] = None
                    ch["candidates"] = [cand] + [c for c in cands if c is not cand]
                    for i, c in enumerate(ch["candidates"]):
                        c["prio"] = i + 1
                    rec.update(status="switched", switched_to=cand["url"],
                               url=cand["url"], error=None,
                               segments=cr.get("segments") or [])
                    return rec
            rec["status"] = "down"
            return rec

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            for rec in ex.map(do_channel, probe_channels):
                live_report.append(rec)

        # ---------------- vod sites
        def do_site(site):
            r, tries = probe_with_retry(probe_vod_site, site, args.timeout)
            rec = {"key": site["key"], "name": site["name"], "fails": site.get("fails", 0),
                   "error": None, "ms": r.get("ms", 0), "note": r.get("note", ""),
                   "tries": tries}
            if r.get("ok"):
                site["fails"] = 0
                site["last_ok"] = ts.isoformat(timespec="seconds")
                rec.update(status="ok", ms=r.get("ms", 0), note=r.get("note", ""))
                return rec
            site["fails"] = int(site.get("fails", 0)) + 1
            rec.update(status="fail" if site["fails"] < MAX_FAILS_BEFORE_SWITCH else "demoted",
                       fails=site["fails"], error=r.get("error"))
            return rec

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            for rec in ex.map(do_site, sites):
                site_report.append(rec)

    # ---------------- decide published site list
    healthy = [s for s in sites if int(s.get("fails", 0)) < MAX_FAILS_BEFORE_SWITCH]
    published_sites = healthy if len(healthy) >= MIN_SITES else None
    dropped = [s["key"] for s in sites if s not in healthy]
    if published_sites:
        if dropped:
            warnings.append("demoted sites: " + ", ".join(dropped))
    else:
        prev = load_json(os.path.join(lastgood, "tvbox.json"), {}) or {}
        prev_sites = prev.get("sites") or []
        if len(prev_sites) >= MIN_SITES:
            published_sites = [s for s in sites if s["key"] in {p.get("key") for p in prev_sites}]
            warnings.append("only %d healthy site(s) < %d -> keeping previous published list (%d sites)"
                            % (len(healthy), MIN_SITES, len(published_sites)))
        else:
            published_sites = sites
            warnings.append("only %d healthy site(s) < %d -> publishing original list unchanged"
                            % (len(healthy), MIN_SITES))

    # ---------------- generate + self-check + rollback
    m3u_text = build_live_m3u(channels)
    base = load_json(base_vod) or load_json(tv_out, {}) or {}
    tv_obj = build_tvbox(base, published_sites)
    problems, ch_count, site_count, line_count = validate_outputs(m3u_text, tv_obj)

    rollback = False
    if problems:
        warnings.append("self-check failed: " + "; ".join(problems[:4]))
        gl = os.path.join(lastgood, "live.m3u")
        gt = os.path.join(lastgood, "tvbox.json")
        if os.path.exists(gl) and os.path.exists(gt):
            m3u_text = open(gl, "r", encoding="utf-8").read()
            tv_obj = load_json(gt, {})
            rollback = True
            p2, ch_count, site_count, line_count = validate_outputs(m3u_text, tv_obj)
            if p2:
                warnings.append("rollback copy also invalid: " + "; ".join(p2[:3]))
        else:
            warnings.append("no last-good snapshot; keeping previous published files")
            print("FAILED self-check: " + "; ".join(problems[:6]))
            return 2

    write_text(live_out, m3u_text)
    write_json(tv_out, tv_obj, indent=1)
    write_text(os.path.join(lastgood, "live.m3u"), m3u_text)
    write_json(os.path.join(lastgood, "tvbox.json"), tv_obj, indent=1)

    live_ok = sum(1 for r in live_report if r["status"] == "ok")
    live_sw = sum(1 for r in live_report if r["status"] == "switched")
    live_bad = sum(1 for r in live_report if r["status"] in ("down", "degraded"))
    live_unv = sum(1 for r in live_report if r["status"] == "unverified")
    sites_ok = sum(1 for r in site_report if r["status"] == "ok")
    if live_unv:
        warnings.append("%d channel(s) only reachable from a specific ISP network; "
                        "left untouched (not judged from this vantage point)" % live_unv)

    health = {        "version": version,
        "checked_at": ts.isoformat(timespec="seconds"),
        "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo": "DreamEditor/wang-tv-subs",
        "counts": {
            "channels": ch_count,
            "lines": line_count,
            "sites": site_count,
            "channels_ok": live_ok,
            "channels_switched": live_sw,
            "channels_degraded_or_down": live_bad,
            "channels_unverified_vantage": live_unv,
            "sites_ok": sites_ok,
            "sites_checked": len(site_report),
        },
        "policy": {
            "segment_min_bytes": SEG_MIN_BYTES,
            "segments_checked": SEG_CHECK_COUNT,
            "max_fails_before_switch": MAX_FAILS_BEFORE_SWITCH,
            "min_sites": MIN_SITES,
            "min_channels": MIN_CHANNELS,
        },
        "rollback": rollback,
        "warnings": warnings,
        "live": live_report,
        "sites": site_report,
        "pages": load_json(state_path, {}).get("pages") if isinstance(load_json(state_path, {}), dict) else None,
    }
    write_json(state_path, health, indent=1)

    hist_dir = os.path.join(ROOT, "state", "history")
    ensure_dir(hist_dir)
    write_json(os.path.join(hist_dir, stamp + ".json"), health, indent=1)
    snaps = sorted(f for f in os.listdir(hist_dir) if f.endswith(".json"))
    for old in snaps[:-HISTORY_KEEP]:
        try:
            os.remove(os.path.join(hist_dir, old))
        except OSError:
            pass

    # persist updated truth sources (fails counters are the cross-run memory)
    write_json(src_live, channels, indent=1)
    write_json(src_vod, sites, indent=1)

    summary = [
        "## WANG-TV subs %s" % version,
        "",
        "| item | value |",
        "|---|---|",
        "| live.m3u | %d channels / %d url lines |" % (ch_count, line_count),
        "| tvbox.json | %d sites |" % site_count,
        "| channels ok/switched/bad/unverified | %d / %d / %d / %d |" % (live_ok, live_sw, live_bad, live_unv),
        "| sites ok | %d / %d |" % (sites_ok, len(site_report)),
        "| rollback | %s |" % ("YES" if rollback else "no"),
    ]
    if warnings:
        summary += ["", "**warnings**"] + ["- " + w for w in warnings[:8]]
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        try:
            with open(step_summary, "a", encoding="utf-8") as fh:
                fh.write("\n".join(summary) + "\n")
        except OSError:
            pass

    print("version=%s channels=%d lines=%d sites=%d ok=%d switched=%d bad=%d unverified=%d sites_ok=%d rollback=%s"
          % (version, ch_count, line_count, site_count, live_ok, live_sw, live_bad, live_unv,
             sites_ok, rollback))
    for w in warnings:
        print("WARN: " + w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
