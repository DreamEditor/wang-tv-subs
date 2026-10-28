#!/usr/bin/env python3
"""One-shot bootstrap: turn WANG-TV local materials into the repo truth sources.

Inputs (read-only, never modified):
  argv[1] live m3u      (wang-live-standalone.m3u)
  argv[2] tvbox json    (tvbox-wang.json)
  argv[3] direct-verified.json  (candidate pool, optional)

Outputs:
  sources/live-sources.json
  sources/vod-sites.json
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_text(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def parse_m3u(text):
    """-> list of {name, group, urls[]} preserving file order."""
    channels = []
    cur = None
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue
        if line.startswith("#EXTINF"):
            name = line.rsplit(",", 1)[-1].strip()
            group = ""
            i = line.find('group-title="')
            if i >= 0:
                j = line.find('"', i + len('group-title="'))
                group = line[i + len('group-title="'):j].strip()
            cur = {"name": name, "group": group or "其他", "urls": []}
            channels.append(cur)
        elif line.startswith("#"):
            continue
        elif line.lower().startswith(("http://", "https://")):
            if cur is None:
                continue
            if line not in cur["urls"]:
                cur["urls"].append(line)
    return channels


def main():
    live_m3u, tvbox_json = sys.argv[1], sys.argv[2]
    verified_json = sys.argv[3] if len(sys.argv) > 3 else None

    channels = parse_m3u(read_text(live_m3u))
    pool = {}
    if verified_json and os.path.exists(verified_json):
        pool = json.loads(read_text(verified_json))

    out_channels = []
    for ch in channels:
        urls = list(ch["urls"])
        for extra in pool.get(ch["name"], []):
            if extra not in urls:
                urls.append(extra)
        if not urls:
            continue
        cands = [{
            "url": u,
            "prio": i + 1,
            "fails": 0,
            "last_ok": None,
        } for i, u in enumerate(urls)]
        out_channels.append({
            "name": ch["name"],
            "group": ch["group"],
            "candidates": cands,
        })

    tv = json.loads(read_text(tvbox_json))
    sites = []
    for i, s in enumerate(tv.get("sites") or []):
        sites.append({
            "key": s.get("key") or ("site%d" % i),
            "name": s.get("name") or s.get("key") or ("site%d" % i),
            "api": s.get("api") or "",
            "type": int(s.get("type") or 1),
            "prio": i + 1,
            "fails": 0,
            "last_ok": None,
        })

    live_path = os.path.join(ROOT, "sources", "live-sources.json")
    vod_path = os.path.join(ROOT, "sources", "vod-sites.json")
    with open(live_path, "w", encoding="utf-8") as fh:
        json.dump(out_channels, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    with open(vod_path, "w", encoding="utf-8") as fh:
        json.dump(sites, fh, ensure_ascii=False, indent=1)
        fh.write("\n")

    lines = sum(len(c["candidates"]) for c in out_channels)
    print("channels=%d lines=%d sites=%d" % (len(out_channels), lines, len(sites)))


if __name__ == "__main__":
    main()
