#!/usr/bin/env python3
"""Build a multi-provider podcast-download test matrix.

For a set of shows (resolved to feeds via the iTunes Search API), grab the
latest episode enclosure and measure what the streaming downloader actually
faces per chunk: redirect-chain depth, Range/206 support, and total size.
"""
import json
import re
import urllib.parse
import urllib.request

SHOWS = [
    "Dan Carlin Hardcore History",
    "A Way with Words",
    "Radiolab",
    "99% Invisible",
    "Conan OBrien Needs a Friend",
    "Darknet Diaries",
    "The Rest Is History",
    "Crime Junkie",
    "The Daily",
    "Lex Fridman Podcast",
]
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


def itunes_feed(term):
    u = "https://itunes.apple.com/search?media=podcast&limit=1&term=" + urllib.parse.quote(term)
    d = json.load(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=25))
    return (d["results"][0].get("feedUrl") if d.get("results") else None)


def first_enclosure(feedurl):
    xml = urllib.request.urlopen(urllib.request.Request(feedurl, headers={"User-Agent": UA}), timeout=30).read().decode("utf-8", "replace")
    m = re.search(r'<enclosure[^>]*url="([^"]+?\.mp3[^"]*)"', xml) or re.search(r'<enclosure[^>]*url="([^"]+)"', xml)
    return m.group(1).replace("&amp;", "&") if m else None


class Counter(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        self.n = 0
        self.hops = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.n += 1
        self.hops.append(urllib.parse.urlparse(newurl).netloc)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def measure(url):
    c = Counter()
    op = urllib.request.build_opener(c)
    req = urllib.request.Request(url, headers={"Range": "bytes=0-1", "User-Agent": UA})
    try:
        r = op.open(req, timeout=45)
        status = r.status
        cr = r.headers.get("Content-Range")
        cl = r.headers.get("Content-Length")
        final = urllib.parse.urlparse(r.geturl()).netloc
        r.close()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:60]}
    total = None
    if cr:
        mm = re.search(r"/(\d+)", cr)
        total = int(mm.group(1)) if mm else None
    elif cl and status == 200:
        total = int(cl)
    return {
        "redirects": c.n,
        "range206": status == 206,
        "startHost": urllib.parse.urlparse(url).netloc,
        "finalHost": final,
        "sizeMB": round(total / 1048576, 1) if total else None,
    }


def main():
    print(f"{'show':28} {'startHost':22} {'finalHost':26} {'redir':5} {'206':4} {'MB':>6}")
    print("-" * 100)
    rows = []
    for show in SHOWS:
        try:
            feed = itunes_feed(show)
            enc = first_enclosure(feed) if feed else None
            if not enc:
                print(f"{show[:27]:28} <no enclosure>")
                continue
            m = measure(enc)
            if "error" in m:
                print(f"{show[:27]:28} <err: {m['error']}>")
                continue
            print(f"{show[:27]:28} {m['startHost'][:21]:22} {m['finalHost'][:25]:26} "
                  f"{m['redirects']:^5} {('yes' if m['range206'] else 'NO'):4} {str(m['sizeMB']):>6}")
            rows.append({"show": show, "url": enc, **m})
        except Exception as e:  # noqa: BLE001
            print(f"{show[:27]:28} <fail: {str(e)[:50]}>")
    with open("/tmp/provider_matrix.json", "w") as f:
        json.dump(rows, f, indent=2)
    print(f"\nsaved {len(rows)} rows -> /tmp/provider_matrix.json")


if __name__ == "__main__":
    main()
