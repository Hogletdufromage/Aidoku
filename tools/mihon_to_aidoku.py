#!/usr/bin/env python3
"""Merge a Mihon backup (.tachibk) into an Aidoku backup (.aib).

Restoring a backup in Aidoku replaces everything, so the output starts from an
Aidoku backup and adds the Mihon library on top: series, chapters, read state,
history and categories. Nothing from the Aidoku backup is removed.

- Weeb Central uses the same series and chapter keys in both apps, so its
  series come over as they are.
- MangaFire series keep Mihon's old keys. Aidoku's MangaFire source asks to
  migrate them, and "Migrate with history" carries the read chapters over by
  chapter number.
- Series from sources Aidoku does not have are left out and listed.

Only the Python standard library is used. Usage:
    mihon_to_aidoku.py --mihon backup.tachibk --aidoku aidoku.aib --out merged.aib
"""
import argparse
import datetime as dt
import gzip
import plistlib
import re
import struct
import sys

# Mihon source name -> Aidoku source key and site, for sources both apps have.
SOURCES = {
    "Weeb Central": ("en.weebcentral", "https://weebcentral.com"),
    "MangaFire": ("multi.mangafire", "https://mangafire.to"),
}

# Mihon category name -> Aidoku category name. Others keep their Mihon name.
CATEGORY_NAMES = {
    "Ongoing": "En cours",
    "Finished": "Terminé",
    "A lire": "A démarrer",
}

# Mihon SManga status -> Aidoku PublishingStatus.
STATUS = {0: 0, 1: 1, 2: 2, 3: 0, 4: 2, 5: 3, 6: 4}


# --- Mihon backup: gzip + protobuf, field numbers from Mihon's backup models ---

def read_varint(buf, i):
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def fields(buf):
    i = 0
    while i < len(buf):
        key, i = read_varint(buf, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            val, i = read_varint(buf, i)
        elif wire == 1:
            val, i = buf[i:i + 8], i + 8
        elif wire == 2:
            n, i = read_varint(buf, i)
            val, i = buf[i:i + n], i + n
        elif wire == 5:
            val, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield num, wire, val


def decode(buf, spec):
    """Decode one message. Absent fields keep the defaults given in spec."""
    out = {name: ([] if repeated else default) for name, _, repeated, default in spec.values()}
    for num, wire, val in fields(buf):
        if num not in spec:
            continue
        name, kind, repeated, _ = spec[num]
        if kind == "str":
            v = val.decode("utf-8", "replace")
        elif kind == "int":
            v = val - (1 << 64) if val >= 1 << 63 else val
        elif kind == "bool":
            v = bool(val)
        elif kind == "float":
            v = struct.unpack("<f", val)[0]
        elif kind == "ints":
            if wire == 2:  # packed
                j = 0
                while j < len(val):
                    x, j = read_varint(val, j)
                    out[name].append(x)
                continue
            v = val
        else:
            v = decode(val, kind)
        if repeated:
            out[name].append(v)
        else:
            out[name] = v
    return out


CHAPTER = {
    1: ("url", "str", False, ""), 2: ("name", "str", False, ""), 3: ("scanlator", "str", False, None),
    4: ("read", "bool", False, False), 6: ("lastPageRead", "int", False, 0),
    7: ("dateFetch", "int", False, 0), 8: ("dateUpload", "int", False, 0),
    9: ("chapterNumber", "float", False, -1.0), 10: ("sourceOrder", "int", False, 0),
    11: ("lastModifiedAt", "int", False, 0),
}
HISTORY = {1: ("url", "str", False, ""), 2: ("lastRead", "int", False, 0)}
MANGA = {
    1: ("source", "int", False, 0), 2: ("url", "str", False, ""), 3: ("title", "str", False, ""),
    4: ("artist", "str", False, None), 5: ("author", "str", False, None),
    6: ("description", "str", False, None), 7: ("genre", "str", True, None),
    8: ("status", "int", False, 0), 9: ("thumbnailUrl", "str", False, None),
    13: ("dateAdded", "int", False, 0), 16: ("chapters", CHAPTER, True, None),
    17: ("categories", "ints", True, None), 100: ("favorite", "bool", False, True),
    104: ("history", HISTORY, True, None),
}
CATEGORY = {1: ("name", "str", False, ""), 2: ("order", "int", False, 0)}
SOURCE = {1: ("name", "str", False, ""), 2: ("sourceId", "int", False, 0)}
BACKUP = {1: ("manga", MANGA, True, None), 2: ("categories", CATEGORY, True, None),
          101: ("sources", SOURCE, True, None)}


def load_mihon(path):
    raw = open(path, "rb").read()
    return decode(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw, BACKUP)


# --- conversion ---

def utc(seconds):
    # plistlib reads and writes naive datetimes as UTC
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).replace(tzinfo=None)


def from_millis(ms):
    return utc(ms / 1000) if ms > 0 else None


def from_seconds(s):
    return utc(s) if s > 0 else None


def chapter_title(name):
    """Drop names that only repeat the number, as Aidoku shows the number itself."""
    name = name.strip()
    if not name or re.fullmatch(r"(?i)(chapter|chap|ch)\.?\s*[\d.]+", name):
        return None
    return name


def merge(mihon, aidoku, now):
    report = {"added": 0, "merged": 0, "chapters": 0, "history_added": 0,
              "history_completed": 0, "skipped": [], "mangafire": []}
    names = {s["sourceId"]: s["name"] for s in mihon["sources"]}
    mihon_categories = {c["order"]: c["name"].strip() for c in mihon["categories"]}

    out = dict(aidoku)
    for key in ("manga", "library", "chapters", "history", "categories", "sources"):
        out[key] = list(aidoku.get(key) or [])

    manga_ids = {(m["sourceId"], m["id"]) for m in out["manga"]}
    library = {(m["sourceId"], m["mangaId"]): m for m in out["library"]}
    chapter_ids = {(c["sourceId"], c["mangaId"], c["id"]) for c in out["chapters"]}
    history = {(h["sourceId"], h["mangaId"], h["chapterId"]): h for h in out["history"]}

    category_titles = {c.get("title") for c in out["categories"]}
    next_sort = max((c.get("sort") or 0 for c in out["categories"]), default=-1) + 1
    for order in sorted(mihon_categories):
        title = CATEGORY_NAMES.get(mihon_categories[order], mihon_categories[order])
        if title not in category_titles:
            out["categories"].append({"title": title, "sort": next_sort, "group": False})
            category_titles.add(title)
            next_sort += 1

    used_sources = set()
    for m in mihon["manga"]:
        if not m["favorite"]:
            continue  # history only, not in the Mihon library
        source_name = names.get(m["source"], "")
        if source_name not in SOURCES:
            report["skipped"].append((m["title"], source_name or str(m["source"])))
            continue
        source_key, site = SOURCES[source_name]
        used_sources.add(source_key)
        manga_key = m["url"]
        ident = (source_key, manga_key)
        if source_key == "multi.mangafire":
            report["mangafire"].append(m["title"])

        read_at = {h["url"]: from_millis(h["lastRead"]) for h in m["history"] if h["lastRead"] > 0}
        added_at = from_millis(m["dateAdded"]) or now

        if ident not in manga_ids:
            entry = {"id": manga_key, "sourceId": source_key, "title": m["title"] or manga_key,
                     "url": site + manga_key, "status": STATUS.get(m["status"], 0), "nsfw": 0, "viewer": 0}
            for key, value in (("author", m["author"]), ("artist", m["artist"]),
                               ("desc", m["description"]), ("cover", m["thumbnailUrl"])):
                if value:
                    entry[key] = value
            if m["genre"]:
                entry["tags"] = m["genre"]
            out["manga"].append(entry)
            manga_ids.add(ident)

        last_read = None
        for c in m["chapters"]:
            cid = (source_key, manga_key, c["url"])
            if cid not in chapter_ids:
                chapter = {"sourceId": source_key, "mangaId": manga_key, "id": c["url"],
                           "url": site + c["url"], "lang": "en", "sourceOrder": c["sourceOrder"]}
                if c["chapterNumber"] >= 0:
                    chapter["chapter"] = c["chapterNumber"]
                if title := chapter_title(c["name"]):
                    chapter["title"] = title
                if c["scanlator"]:
                    chapter["scanlator"] = c["scanlator"]
                if uploaded := from_millis(c["dateUpload"]):
                    chapter["dateUploaded"] = uploaded
                out["chapters"].append(chapter)
                chapter_ids.add(cid)
                report["chapters"] += 1

            started = c["lastPageRead"] > 0
            if not (c["read"] or started):
                continue
            when = (read_at.get(c["url"]) or from_seconds(c["lastModifiedAt"])
                    or from_millis(c["dateFetch"]) or added_at)
            last_read = max(last_read, when) if last_read else when
            existing = history.get(cid)
            if existing is None:
                record = {"sourceId": source_key, "mangaId": manga_key, "chapterId": c["url"],
                          "dateRead": when, "completed": c["read"]}
                if not c["read"]:
                    record["progress"] = c["lastPageRead"] + 1
                out["history"].append(record)
                history[cid] = record
                report["history_added"] += 1
            else:
                if c["read"] and not existing.get("completed"):
                    existing["completed"] = True
                    existing.pop("progress", None)
                    report["history_completed"] += 1
                existing["dateRead"] = max(existing["dateRead"], when)

        categories = [CATEGORY_NAMES.get(mihon_categories.get(o, ""), mihon_categories.get(o, ""))
                      for o in m["categories"] if o in mihon_categories]
        if ident in library:
            lib = library[ident]
            if last_read and (lib.get("lastRead") is None or lib["lastRead"] < last_read):
                lib["lastRead"] = last_read
            if not lib.get("categories") and categories:
                lib["categories"] = categories
            report["merged"] += 1
        else:
            lib = {"mangaId": manga_key, "sourceId": source_key, "dateAdded": added_at,
                   "lastOpened": last_read or added_at, "lastUpdated": now}
            if last_read:
                lib["lastRead"] = last_read
            if categories:
                lib["categories"] = categories
            out["library"].append(lib)
            library[ident] = lib
            report["added"] += 1

    # Sources missing from the Aidoku backup are offered for install on restore.
    source_keys = {s if isinstance(s, str) else s.get("id") for s in out["sources"]}
    out["sources"] += sorted(used_sources - source_keys)

    out["date"] = now
    out["name"] = "Mihon + Aidoku"
    out["automatic"] = False
    return out, report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mihon", required=True, help="Mihon backup (.tachibk)")
    parser.add_argument("--aidoku", required=True, help="Aidoku backup (.aib) to merge into")
    parser.add_argument("--out", required=True, help="merged Aidoku backup to write")
    args = parser.parse_args()

    mihon = load_mihon(args.mihon)
    with open(args.aidoku, "rb") as fh:
        aidoku = plistlib.load(fh)
    merged, report = merge(mihon, aidoku, utc(dt.datetime.now().timestamp()))
    with open(args.out, "wb") as fh:
        plistlib.dump(merged, fh, fmt=plistlib.FMT_BINARY)

    print(f"Aidoku before:  {len(aidoku.get('library') or [])} series in library")
    print(f"Added:          {report['added']} series, {report['chapters']} chapters, "
          f"{report['history_added']} read or started chapters")
    print(f"Already there:  {report['merged']} series ({report['history_completed']} chapters newly marked read)")
    print(f"Aidoku after:   {len(merged['library'])} series in library, {len(merged['history'])} history entries")
    if report["mangafire"]:
        print(f"MangaFire, to migrate in Aidoku: {len(report['mangafire'])} series")
    if report["skipped"]:
        print(f"Left out (source not in Aidoku): {len(report['skipped'])}")
        for title, source in sorted(report["skipped"], key=lambda t: t[1]):
            print(f"  - {title} ({source})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
