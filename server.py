"""gbif_month: what has been recorded near a place in this month of the year,
from the Global Biodiversity Information Facility (GBIF).

Runs on the Tesserae side, never the client. Returns a JSON-serialisable dict,
or {"error": "friendly message"}; never raises.

One call to GBIF's public occurrence API (no key):

  /v1/occurrence/search   human observations within the circle, in the current
                          calendar month of any year, a page of 150

The page is aggregated here: occurrences are grouped by species, counted and
sorted, most recorded first, which is what is historically about at this time
of year. Common names come with the record where the source dataset carries
them, else from GBIF's vernacular names for the species, else the scientific
name stands. The answer is kept per rounded place, radius, kinds and month, so
a month's page is asked for once and re-read until the month turns.

Pictures, in the order the "pictures" option asks for them:

  photo      the newest occurrence's own picture where the record carries
             media, else GBIF's pictures of the species, resized for the layout
             and credited to the rights holder
  gen        an AI painting in a chosen style, made once per species through
             fal.ai from an openly licensed picture and kept (experimental,
             needs a fal key in the widget settings). Painting takes longer
             than Tesserae gives a fetch, so it runs in a background thread
             (paint.py) and the species shows its photo until the painting is
             ready. The admin page (blueprint() below) lists the paintings,
             keeps earlier versions, repaints and deletes.

The cluster page (sheet.py) cuts each painting out of its paper and packs the
species onto one page; until a painting exists the cluster is a montage of
photos. Every picture is resized and inlined as a data URI, as are the caption
fonts, because the render sandbox has no network.

Files in ctx["data_dir"]:
  result_<slug>.json    GBIF's answer, aggregated (slug: place, radius, kinds, month)
  names_<key>.json      a species' common name from GBIF, kept half a year
  media_<key>.json      GBIF's pictures of a species, kept half a year
  img/                  fetched originals and resized copies
  gen/                  AI paintings, their credits, cut-outs, the daily budget,
                        the last failure per species, and the sheets
  gen/archive/          earlier paintings kept when a species is repainted
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import json
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent


def _sibling(name: str) -> Any:
    """A helper module beside this file, loaded by path once under a name of
    its own (the plugin folder is neither a package nor on sys.path)."""
    full = f"_tesserae_plugins.gbif_month.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, HERE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


P = _sibling("paint")
S = _sibling("sheet")

GBIF_OCCURRENCES = "https://api.gbif.org/v1/occurrence/search"
GBIF_SPECIES = "https://api.gbif.org/v1/species"
# Citizen-science records only, a page of 150: enough for a ranking of what is
# about, within what GBIF hands back in one answer.
GBIF_BASIS = "HUMAN_OBSERVATION"
GBIF_LIMIT = 150
# A month's answer is kept this long (the month is part of the key, so a new
# month asks afresh); a species' names and pictures half a year.
CACHE_DAYS = 30
LOOKUP_DAYS = 180
ERR_UNREACHABLE = "Couldn't reach GBIF."
ERR_BUSY = "GBIF is busy (it answered {err}); back on the next refresh."
ERR_NOWHERE = "Set a place in this cell (or the app's location)."
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

LAYOUTS = ("cluster", "gallery", "latest", "list", "bleed", "tally")
PICTURES = ("photo", "gen", "none")
LEVELS = ("colour", "gray16", "gray4", "mono")
CLUSTER_STYLES = ("sheet", "numbered", "wreath", "notebook", "drawer")
CLUSTER_TEXTS = ("key", "under", "none")
FOOTERS = ("show", "credits", "hide")
TEXT_SCALES = ("100", "60", "80", "125", "150")
ARRANGEMENTS = ("auto", "oval", "circle", "ring", "rows", "scatter", "heap")
SIZINGS = ("kind", "busy", "rare", "same")
LABEL_LINES = ("name", "sci", "kind", "count", "when", "who")
# The longest text each label line can take, for reserving room when packing.
_LABEL_ROOM = {
    "sci": None,
    "kind": "Grasshopper",
    "count": "999 observations",
    "when": "seen 30 Sep",
    "who": "by somebody-long",
}
RADII = ("2", "5", "10", "25", "50")
FONTS = {
    "classic": (),
    "playfair": ("playfair",),
    "typewriter": ("special_elite",),
    "hand": ("caveat",),
    "modern": ("archivo_black", "archivo_narrow"),
    "theme": (),
}
# The engraved faces every page is set in.
FONT_BASE: tuple[str, ...] = ("im_fell", "im_fell_sc")
# The longest edge of a picture for each layout, in pixels.
SIZE = {
    "cluster": 1400,
    "gallery": 480,
    "latest": 900,
    "list": 900,
    "bleed": 1200,
    "tally": 0,
}
# Most species the lists take, and the sheet.
LIST_MAX = 12
SHEET_MAX = S.SHEET_MAX

_slug = P._slug


def _tz(options: dict[str, Any]) -> tzinfo | None:
    """IANA zone carried on the location option, or None for the server's."""
    for value in (options.get("timezone"), options.get("location")):
        if isinstance(value, dict):
            value = value.get("timezone")
        if isinstance(value, str) and value.strip():
            with contextlib.suppress(ValueError, KeyError, OSError):
                return ZoneInfo(value.strip())
    return None


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _choice(value: Any, allowed: tuple[str, ...]) -> str:
    return value if isinstance(value, str) and value in allowed else allowed[0]


def _picks(value: Any, allowed: tuple[str, ...] | None = None) -> list[str]:
    """A multiselect's picks as a list of strings (a saved cell may carry a
    comma string or a single value)."""
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    if not isinstance(value, list):
        return []
    out = [str(v) for v in value if str(v)]
    return [v for v in out if v in allowed] if allowed else out


def _place(options: dict[str, Any]) -> tuple[float, float, str] | None:
    """(lat, lon, name) from the cell's location (or the app's, promoted)."""
    try:
        lat, lon = float(options.get("latitude")), float(options.get("longitude"))
    except (TypeError, ValueError):
        return None
    name = ""
    loc = options.get("location")
    if isinstance(loc, dict):
        name = str(loc.get("label") or loc.get("name") or "")
    elif isinstance(loc, str):
        name = loc
    name = name or str(options.get("label") or "")
    return lat, lon, name.split(",")[0].strip()


# ----------------------------------------------------------------------
# iNaturalist
# ----------------------------------------------------------------------


# What a record is, from the names GBIF gives it: the iconic group the painter
# and the labels work with (paint.KINDS), and the finer kind by order, family
# or class where a plate would say so (paint.FINER keys, by name here).
_ICONIC_BY_CLASS = {
    "Aves": "Aves",
    "Mammalia": "Mammalia",
    "Amphibia": "Amphibia",
    "Insecta": "Insecta",
    "Arachnida": "Arachnida",
    "Squamata": "Reptilia",
    "Testudines": "Reptilia",
    "Crocodylia": "Reptilia",
    "Reptilia": "Reptilia",
}
_ICONIC_BY_KINGDOM = {
    "Plantae": "Plantae",
    "Fungi": "Fungi",
    "Chromista": "Chromista",
    "Protozoa": "Protozoa",
}
_FINER_BY_NAME = {
    "Lepidoptera": 47157,
    "Odonata": 47792,
    "Coleoptera": 47208,
    "Hymenoptera": 47201,
    "Formicidae": 47336,
    "Apidae": 47221,
    "Diptera": 47822,
    "Hemiptera": 47744,
    "Orthoptera": 47651,
    "Mantodea": 48112,
    "Araneae": 47118,
    "Chiroptera": 40268,
    "Rodentia": 43698,
    "Carnivora": 41573,
    "Cetacea": 152871,
    "Squamata": 26172,
    "Testudines": 39532,
    "Anura": 20979,
    "Elasmobranchii": 47273,
    "Gastropoda": 47114,
    "Decapoda": 47186,
    "Cnidaria": 47534,
    "Echinodermata": 47549,
    "Orchidaceae": 47217,
    "Asteraceae": 47604,
    "Rosaceae": 47148,
    "Pinopsida": 136329,
    "Polypodiopsida": 121943,
    "Bryophyta": 311249,
    "Agaricomycetes": 50814,
    "Lecanoromycetes": 54743,
    "Diprotodontia": 848319,
    "Dasyuromorphia": 848319,
    "Peramelemorphia": 848319,
    # the butterfly families, since the backbone has no superfamily to name them by
    "Nymphalidae": 47224,
    "Pieridae": 47224,
    "Lycaenidae": 47224,
    "Papilionidae": 47224,
    "Hesperiidae": 47224,
    "Riodinidae": 47224,
}
_NOT_FISH = set(_ICONIC_BY_CLASS)


def _taxon_of(r: dict[str, Any]) -> dict[str, Any]:
    """A record's names as the painter's kind_of reads them."""
    kingdom, phylum, cls = (
        str(r.get("kingdom") or ""),
        str(r.get("phylum") or ""),
        str(r.get("class") or ""),
    )
    iconic = _ICONIC_BY_CLASS.get(cls) or _ICONIC_BY_KINGDOM.get(kingdom) or ""
    if not iconic:
        if phylum == "Mollusca":
            iconic = "Mollusca"
        elif phylum == "Chordata" and cls not in _NOT_FISH:
            iconic = "Actinopterygii"
        elif kingdom == "Animalia":
            iconic = "Animalia"
    # family before order: a butterfly's family says butterfly, its order only moth
    names = [str(r.get(k) or "") for k in ("family", "order", "class", "phylum")]
    ancestors = [_FINER_BY_NAME[n] for n in names if n in _FINER_BY_NAME]
    return {"iconic_taxon_name": iconic, "ancestor_ids": ancestors}


def _query(
    place: tuple[float, float, str], radius_km: str, taxa: list[str], month: int
) -> list[tuple[str, str]]:
    """The occurrence search as GBIF takes it: a circle, the month of the year,
    citizen-science records, a page of 150, and the kinds asked for as
    repeated taxonKey values (a choice may carry several, joined with +)."""
    q = [
        ("geoDistance", f"{place[0]:.4f},{place[1]:.4f},{radius_km}km"),
        ("month", str(month)),
        ("basisOfRecord", GBIF_BASIS),
        ("limit", str(GBIF_LIMIT)),
    ]
    for t in taxa:
        for key in t.split("+"):
            if key.isdigit():
                q.append(("taxonKey", key))
    return q


def _recorder(value: Any) -> str:
    """Who recorded it, or '' when the dataset hands over a hash in place of a
    name (eBird does) or a list of several."""
    who = str(value or "").strip().split("|")[0].split(";")[0].strip()
    if re.fullmatch(r"[0-9a-f]{32,}", who):
        return ""
    return who[:40]


def _licence_words(value: str) -> str:
    """'CC BY NC 4.0' from a licence URL or short name GBIF carries."""
    v = str(value or "").strip()
    m = re.search(
        r"creativecommons\.org/(licenses|publicdomain)/([a-z-]+)/([0-9.]+)", v
    )
    if m:
        kind = (
            "CC0"
            if m.group(1) == "publicdomain"
            else "CC " + m.group(2).upper().replace("-", " ")
        )
        return f"{kind} {m.group(3)}"
    return v.replace("http://", "").replace("https://", "")[:24]


def _media_credit(m: dict[str, Any]) -> str:
    who = str(
        m.get("rightsHolder") or m.get("creator") or m.get("publisher") or ""
    ).strip()
    lic = _licence_words(str(m.get("license") or ""))
    return f"© {who}{f' ({lic})' if lic else ''} · GBIF" if who else "GBIF"


def _still(media: Any) -> dict[str, Any] | None:
    """The first picture among a record's media, with its address resized
    where the host offers sizes (iNaturalist's originals are large)."""
    for m in media if isinstance(media, list) else []:
        if (
            not isinstance(m, dict)
            or str(m.get("type") or "StillImage") != "StillImage"
        ):
            continue
        url = str(m.get("identifier") or "")
        if not url.startswith("http"):
            continue
        return {
            **m,
            "identifier": re.sub(r"/original\.(jpe?g|png)$", r"/medium.\1", url),
        }
    return None


def _from_gbif(q: list[tuple[str, str]]) -> tuple[dict[str, Any] | None, str | None]:
    """GBIF's page of occurrences, aggregated by species: count, the names GBIF
    gives the species, and its newest record (when, who, where, a picture);
    most recorded first. Plus the newest records as finds."""
    answer, err = P._json(f"{GBIF_OCCURRENCES}?{urllib.parse.urlencode(q)}")
    if err == "unreachable":
        return None, ERR_UNREACHABLE
    if err:
        return None, ERR_BUSY.format(err=err)
    if not isinstance(answer, dict) or not isinstance(answer.get("results"), list):
        return None, "GBIF's answer wasn't in the expected shape."
    by: dict[str, dict[str, Any]] = {}
    records = []
    for r in answer["results"]:
        if not isinstance(r, dict):
            continue
        sci = str(r.get("species") or "").strip()
        if not sci:
            continue  # identified no further than a genus or family: not a species to show
        when = str(r.get("eventDate") or "")[:10]
        rec = {
            "species": sci,
            "speciesKey": r.get("speciesKey"),
            "vernacularName": str(r.get("vernacularName") or "").strip() or None,
            "eventDate": when,
            "recordedBy": _recorder(r.get("recordedBy")),
            "place": str(r.get("locality") or r.get("stateProvince") or "")
            .split(",")[0]
            .strip(),
            "media": _still(r.get("media")),
            "taxon": _taxon_of(r),
        }
        records.append(rec)
        e = by.setdefault(sci, {**rec, "count": 0})
        e["count"] += 1
        if not e.get("vernacularName") and rec["vernacularName"]:
            e["vernacularName"] = rec["vernacularName"]
        if not e.get("media") and rec["media"]:
            e["media"] = rec["media"]
        # the newest record stands for when it was last seen and by whom
        if when > str(e.get("eventDate") or ""):
            e.update(eventDate=when, recordedBy=rec["recordedBy"], place=rec["place"])
            if rec["media"]:
                e["media"] = rec["media"]
    species = sorted(by.values(), key=lambda e: (-e["count"], e["species"]))
    for e in species:
        e["iconic"] = e["taxon"]["iconic_taxon_name"]
    # the finds: the newest record of each species, newest first
    records.sort(key=lambda r: r["eventDate"], reverse=True)
    finds: list[dict[str, Any]] = []
    seen_sci: set[str] = set()
    for rec in records:
        if rec["species"] not in seen_sci:
            seen_sci.add(rec["species"])
            finds.append(rec)
    return {
        "count": int(answer.get("count") or 0),
        "sample": len(answer["results"]),
        "species": species,
        "finds": finds[:LIST_MAX],
    }, None


def _lookup(path: Path, url: str) -> Any:
    """A GBIF species lookup kept on disk for half a year; None when it
    could not be had (and not asked again for a day)."""
    with contextlib.suppress(OSError, json.JSONDecodeError):
        age = time.time() - path.stat().st_mtime
        hit = json.loads(path.read_text(encoding="utf-8"))
        if age < LOOKUP_DAYS * 86400 and hit is not None or age < 86400:
            return hit
    answer, err = P._json(url)
    if err:
        P._write(path, b"null")
        return None
    P._write(path, json.dumps(answer).encode())
    return answer


def _vernacular(key: Any, data_dir: Path) -> str:
    """The species' English common name from GBIF, or ''."""
    if not key:
        return ""
    _lookup(
        data_dir / f"names_{int(key)}.json",
        f"{GBIF_SPECIES}/{int(key)}/vernacularNames?limit=50",
    )
    return _kept_name(key, data_dir)


def _kept_name(key: Any, data_dir: Path) -> str:
    """The species' English name from a lookup already on disk, or ''; nothing is fetched."""
    if not key:
        return ""
    got = P._read_json(data_dir / f"names_{int(key)}.json", None)
    rows = got.get("results") if isinstance(got, dict) else None
    for r in rows or []:
        if (
            isinstance(r, dict)
            and r.get("language") in ("eng", "")
            and r.get("vernacularName")
        ):
            return str(r["vernacularName"]).strip()
    return ""


def _species_media(key: Any, data_dir: Path) -> dict[str, Any] | None:
    """GBIF's first picture of the species, or None."""
    if not key:
        return None
    got = _lookup(
        data_dir / f"media_{int(key)}.json", f"{GBIF_SPECIES}/{int(key)}/media?limit=20"
    )
    rows = got.get("results") if isinstance(got, dict) else None
    return _still(rows)


def _day_words(iso: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso or "")
    if not m:
        return ""
    return f"{int(m.group(3))} {date(1, int(m.group(2)), 1).strftime('%b')}{'' if int(m.group(1)) >= date.today().year else ' ' + m.group(1)}"


def _days_ago(iso: str, today: date) -> int | None:
    with contextlib.suppress(ValueError, TypeError):
        return max(0, (today - date.fromisoformat(str(iso)[:10])).days)
    return None


def _rows(
    raw: dict[str, Any], today: date, data_dir: Path, lookups: int = SHEET_MAX
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(species rows, newest finds) in the shape the layouts read, from the
    aggregated answer. A species the record names no common name or picture
    for is looked up on GBIF, the first ``lookups`` of them side by side, so
    a page costs a bounded number of calls; the rest fall back to the
    scientific name and no picture."""
    entries = [
        e for e in raw.get("species", []) if isinstance(e, dict) and e.get("species")
    ]
    finds_raw = [
        r for r in raw.get("finds", []) if isinstance(r, dict) and r.get("species")
    ]
    # the lookups, side by side: names and pictures for the species shown
    need = [
        e
        for e in entries[:lookups]
        if not e.get("vernacularName") or not e.get("media")
    ]
    names: dict[Any, str] = {}
    media: dict[Any, dict[str, Any] | None] = {}
    if need:
        with ThreadPoolExecutor(max_workers=6) as pool:
            got_names = list(
                pool.map(
                    lambda e: (
                        _vernacular(e.get("speciesKey"), data_dir)
                        if not e.get("vernacularName")
                        else ""
                    ),
                    need,
                )
            )
            got_media = list(
                pool.map(
                    lambda e: (
                        _species_media(e.get("speciesKey"), data_dir)
                        if not e.get("media")
                        else None
                    ),
                    need,
                )
            )
        for e, n, m in zip(need, got_names, got_media, strict=True):
            names[e.get("speciesKey")] = n
            media[e.get("speciesKey")] = m

    def row(e: dict[str, Any], count: int) -> dict[str, Any]:
        group, kind, factor = P.kind_of(e.get("taxon"))
        key = e.get("speciesKey")
        name = str(e.get("vernacularName") or names.get(key) or "")
        pic = e.get("media") if isinstance(e.get("media"), dict) else media.get(key)
        seen = str(e.get("eventDate") or "")
        out = {
            "species": e["species"],
            "vernacularName": name or e["species"],
            "name": name or e["species"],
            "sci": e["species"],
            "iconic": str((e.get("taxon") or {}).get("iconic_taxon_name") or ""),
            "group": group,
            "kind": kind,
            "factor": factor,
            "count": count,
            "last": _day_words(seen),
            "ago_days": _days_ago(seen, today) if seen else None,
            "who": str(e.get("recordedBy") or "")[:40],
            "place": str(e.get("place") or ""),
            "photo": str(pic.get("identifier") or "") if pic else "",
            "photo_credit": _media_credit(pic) if pic else "",
        }
        # the reference a painting is made from: an openly licensed picture only
        if pic and re.search(
            r"creativecommons|CC[ -]?(0|BY)", str(pic.get("license") or ""), re.I
        ):
            out["ref"], out["ref_credit"] = out["photo"], out["photo_credit"]
        return out

    species = [row(e, int(e.get("count") or 0)) for e in entries]
    by_sci = {s["sci"]: s for s in species}
    finds = []
    for r in finds_raw:
        sp = by_sci.get(str(r["species"]))
        f = row(
            {**r, "vernacularName": r.get("vernacularName") or (sp or {}).get("name")},
            sp["count"] if sp else 1,
        )
        if not f["photo"] and sp:
            f["photo"], f["photo_credit"] = sp["photo"], sp["photo_credit"]
        f["species_row"] = sp
        finds.append(f)
    return species, finds


def _photo(
    row: dict[str, Any], size: int, data_dir: Path, levels: str
) -> dict[str, Any] | None:
    url = str(row.get("photo") or "")
    if not url:
        return None
    raw = P._cached_get(data_dir, url)
    if not raw:
        return None
    key = hashlib.sha1(f"{url}|{size}".encode()).hexdigest()[:20]
    src = P._render(raw, size, None, data_dir / "img" / f"f_{key}.jpg", levels)
    if not src:
        return None
    return {
        "src": src,
        "kind": "photo",
        "credit": row.get("photo_credit") or "iNaturalist",
        "focus": [0.5, 0.42],
    }


def _attach_pictures(
    rows: list[dict[str, Any]],
    mode: str,
    size: int,
    fal_key: str,
    data_dir: Path,
    levels: str = "colour",
    style: str = "gould",
    allow_new: bool = True,
) -> list[str]:
    """Give each row a ``pic`` per the pictures option. Returns notices. A
    painting is at most one new one per fetch, in the background; the
    photos are fetched side by side."""
    notices: list[str] = []
    if mode == "none" or not rows:
        return notices
    if mode == "gen" and not fal_key:
        notices.append(P.NOTE_NO_KEY)
    started = 0 if allow_new else P.PER_FETCH
    for row in rows:
        row["pic"] = None
        if mode != "gen":
            continue
        pic, did, note = _generated_for(
            row, size, data_dir, fal_key, started < P.PER_FETCH, levels, style
        )
        started += 1 if did else 0
        if note and note not in notices:
            notices.append(note)
        row["pic"] = pic
    missing = [r for r in rows if not r.get("pic")]
    with ThreadPoolExecutor(max_workers=6) as pool:
        photos = list(pool.map(lambda r: _photo(r, size, data_dir, levels), missing))
    for row, pic in zip(missing, photos, strict=True):
        row["pic"] = pic
    return notices


def _generated_for(
    row: dict[str, Any],
    size: int,
    data_dir: Path,
    fal_key: str,
    allow_new: bool,
    levels: str,
    style: str,
) -> tuple[dict[str, Any] | None, bool, str | None]:
    """A find row paints its species; a species row paints itself."""
    sp = row.get("species_row") if isinstance(row.get("species_row"), dict) else row
    return P._generated(
        sp, size, data_dir, fal_key, allow_new=allow_new, levels=levels, style=style
    )


def _specimen_sheet(
    species: list[dict[str, Any]],
    n: int,
    style: str,
    ratio: float,
    fal_key: str,
    data_dir: Path,
    levels: str,
    label: float = S.SHEET_LABEL,
    mode: str = "sheet",
    lines: tuple[str, ...] = ("name",),
    arrange: str = "auto",
    sizing: str = "kind",
) -> tuple[dict[str, Any] | None, list[str]]:
    """The cluster as a printed page: the most observed species cut out of
    their AI paintings and laid out together (a few new paintings per
    refresh). None until a species has a painting."""
    items = [dict(s) for s in sorted(species, key=lambda s: -s["count"])[:n]]
    notices: list[str] = []
    if not fal_key:
        notices.append(P.NOTE_NO_KEY)
    started = 0
    for b in items:
        b["key"] = None
        pic, did, note = P._generated(
            b,
            SIZE["gallery"],
            data_dir,
            fal_key,
            allow_new=started < P.PER_FETCH,
            levels=levels,
            style=style,
        )
        started += 1 if did else 0
        if note and note not in notices:
            notices.append(note)
        if pic is not None:
            b["key"] = P._pkey(_slug(b["sci"] or b["name"]), style)
    ready = [b for b in items if b["key"]]
    if not ready:
        return None, notices
    for b in ready:
        room = []
        for line in lines:
            if line == "name":
                room.append(b["name"])
            elif line == "sci":
                room.append(b["sci"] or "")
            elif line in _LABEL_ROOM:
                room.append(_LABEL_ROOM[line])
        b["label_room"] = [t for t in room if t]
        if sizing == "same":
            b["factor"] = 1.0
        elif sizing == "busy":
            top = max(x["count"] for x in ready) or 1
            b["factor"] = round(0.65 + (b["count"] / top) ** 0.325, 2)
        elif sizing == "rare":
            top = max(x["count"] for x in ready) or 1
            b["factor"] = round(1.75 - b["count"] / top, 2)
    made = S._sheet(
        ready,
        [b["key"] for b in ready],
        ratio,
        SIZE["cluster"],
        data_dir,
        levels,
        label,
        mode,
        arrange,
    )
    if not made:
        return None, notices
    by_name = {b["name"]: b for b in items}
    shown = [
        {
            **m,
            "count": by_name.get(m["name"], {}).get("count", 0),
            "last": by_name.get(m["name"], {}).get("last", ""),
            "kind": by_name.get(m["name"], {}).get("kind", ""),
            "who": by_name.get(m["name"], {}).get("who", ""),
        }
        for m in made["birds"]
    ]
    credit = (
        "AI paintings, "
        + P._style(style)[0].split(" (")[0].lower()
        + " (fal.ai nano-banana), after the observers' iNaturalist photos"
    )
    return {
        "sheet": True,
        "pic": {
            "src": made["src"],
            "kind": "sheet",
            "paper": made["paper"],
            "credit": credit,
        },
        "birds": shown,
        "lines": list(lines),
        "ratio": made["ratio"],
        "label": made["label"],
        "mode": made["mode"],
        "waiting": [b["name"] for b in items if not b["key"]],
        "waiting_verb": "paint",
        "montage": False,
    }, notices


def _montage(
    species: list[dict[str, Any]], n: int, size: int, data_dir: Path, levels: str
) -> tuple[dict[str, Any], list[str]]:
    rows = [dict(s) for s in sorted(species, key=lambda s: -s["count"])[:n]]
    notices = _attach_pictures(rows, "photo", size, "", data_dir, levels)
    shown = [
        {"name": r["name"], "sci": r["sci"], "count": r["count"], "pic": r.get("pic")}
        for r in rows
    ]
    return {"pic": None, "birds": shown, "montage": True, "waiting": []}, notices


def _font_css(groups: tuple[str, ...]) -> str:
    """@font-face rules for the bundled faces in ``groups``, as data URIs."""
    if not groups:
        return ""
    try:
        manifest = json.loads(
            (HERE / "fonts" / "fonts.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return ""
    rules = []
    for file, face in manifest.items():
        if face.get("group") not in groups:
            continue
        try:
            data = base64.b64encode((HERE / "fonts" / file).read_bytes()).decode()
        except OSError:
            continue
        rules.append(
            f"@font-face{{font-family:'{face['family']}';font-style:{face['style']};"
            f"font-weight:{face['weight']};font-display:block;"
            f"src:url(data:font/woff2;base64,{data}) format('woff2');unicode-range:{face['range']};}}"
        )
    return "".join(rules)


# ----------------------------------------------------------------------
# fetch
# ----------------------------------------------------------------------


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    """The widget's data. The daily limit and a custom prompt ride on context
    variables for the call (and the painting threads it starts)."""
    cap_token = P._CAP.set(P._daily_cap(settings))
    custom_token = P._CUSTOM.set(None)
    try:
        return _fetch(options, settings, ctx=ctx)
    finally:
        P._CUSTOM.reset(custom_token)
        P._CAP.reset(cap_token)


def _fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    layout = _choice(options.get("layout"), LAYOUTS)
    pictures = _choice(options.get("pictures"), PICTURES)
    if layout == "tally":
        pictures = "none"
    levels = _choice(options.get("panel"), LEVELS)
    art_style = _choice(options.get("art_style"), P.GEN_STYLE_IDS)
    if art_style == "custom":
        how = (
            " ".join(str(options.get("art_prompt") or "").split())[:800]
            or P.CUSTOM_EXAMPLE
        )
        art_style = P._custom_tag(how)
        P._CUSTOM.set({art_style: ("Custom style", how)})
    cluster_style = _choice(options.get("cluster_style"), CLUSTER_STYLES)
    cluster_lines = tuple(
        v for v in LABEL_LINES if v in _picks(options.get("cluster_labels"))
    ) or ("name",)
    cluster_arrange = _choice(options.get("cluster_arrange"), ARRANGEMENTS)
    cluster_text = _choice(options.get("cluster_text"), CLUSTER_TEXTS)
    cluster_sizing = _choice(options.get("cluster_sizing"), SIZINGS)
    text_scale = int(_choice(options.get("cluster_text_scale"), TEXT_SCALES)) / 100
    footer = _choice(options.get("footer"), FOOTERS)
    font = _choice(options.get("font"), tuple(FONTS))
    # one number for the cluster, the gallery and the paintings (a cell saved
    # before 0.1.3 carries it as cluster_size)
    want = _int(options.get("count") or options.get("cluster_size"), 8, 1, SHEET_MAX)
    cluster_n = max(2, want)
    radius_km = _choice(options.get("radius_km"), ("10", *RADII))
    taxa = [t for t in _picks(options.get("groups")) if re.fullmatch(r"\d+(\+\d+)*", t)]
    label = str(options.get("label") or "").strip()
    fal_key = str((settings or {}).get("fal_api_key") or "").strip()
    now = datetime.now(_tz(options))
    today = now.date()
    month = now.month
    cw, ch = int(ctx.get("cell_w") or 0), int(ctx.get("cell_h") or 0)
    data_dir = Path(ctx.get("data_dir") or ".")
    with contextlib.suppress(OSError):
        data_dir.mkdir(parents=True, exist_ok=True)

    place = _place(options)
    if not place:
        return {"error": ERR_NOWHERE, "label": label or "Nature nearby"}
    q = _query(place, radius_km, taxa, month)
    # seasonal data is still: the key is the place to a couple of decimals, the
    # circle, the kinds and the month, not the clock
    slug = hashlib.sha1(
        f"{round(place[0], 2)},{round(place[1], 2)}|{radius_km}|{','.join(sorted(taxa))}|{month}".encode()
    ).hexdigest()[:12]
    cache = data_dir / f"result_{slug}.json"
    raw = None
    if cache.exists() and time.time() - cache.stat().st_mtime < CACHE_DAYS * 86400:
        with contextlib.suppress(OSError, json.JSONDecodeError):
            raw = json.loads(cache.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raw, err = _from_gbif(q)
        if err or raw is None:
            # a stale answer beats an empty page while GBIF is busy
            with contextlib.suppress(OSError, json.JSONDecodeError):
                raw = (
                    json.loads(cache.read_text(encoding="utf-8"))
                    if cache.exists()
                    else None
                )
            if not isinstance(raw, dict):
                return {
                    "error": err or ERR_UNREACHABLE,
                    "label": label or "Nature nearby",
                }
        else:
            with contextlib.suppress(OSError):
                cache.write_text(json.dumps(raw), encoding="utf-8")

    species, finds = _rows(raw, today, data_dir, lookups=max(want, LIST_MAX))
    place_name = place[2]
    if not label:
        label = place_name or "Nature nearby"
    area = f"within {radius_km} km" + (
        ""
        if place_name.lower() == label.lower() or not place_name
        else f" of {place_name}"
    )
    period = f"in {MONTHS[month - 1]}"

    groups: dict[str, dict[str, Any]] = {}
    for s in species:
        g = groups.setdefault(s["group"], {"label": s["group"], "species": 0, "obs": 0})
        g["species"] += 1
        g["obs"] += s["count"]
    notices: list[str] = []
    cluster = None
    shown = species[: cluster_n if layout == "cluster" else min(want, LIST_MAX)]
    latest = finds[0] if finds else (species[0] if species else None)
    if layout == "cluster" and species:
        label_size = max(S.SHEET_LABEL, 13 / ch) if ch else S.SHEET_LABEL
        if pictures == "gen":
            ratio = cw / ch if cw and ch else 4 / 3
            if cluster_style == "numbered" and cluster_text == "key":
                # the numbered plate shares the cell with its key
                ratio = ratio * 0.64 / 0.92 if ratio >= 1 else ratio * 0.92 / 0.62
            cluster, notices = _specimen_sheet(
                species,
                cluster_n,
                art_style,
                ratio,
                fal_key,
                data_dir,
                levels,
                label_size,
                cluster_style,
                cluster_lines,
                cluster_arrange,
                cluster_sizing,
            )
        if cluster is None and pictures != "none":
            cluster, more = _montage(
                species, cluster_n, SIZE["gallery"], data_dir, levels
            )
            notices += [m for m in more if m not in notices]
        elif cluster is None:
            cluster, _m = _montage(
                species, cluster_n, SIZE["gallery"], data_dir, "colour"
            )
            for b in cluster["birds"]:
                b["pic"] = None
        cluster.setdefault("label", label_size)
        cluster.setdefault("lines", list(cluster_lines))
    elif layout == "gallery":
        notices = _attach_pictures(
            shown, pictures, SIZE["gallery"], fal_key, data_dir, levels, art_style
        )
    elif layout in ("latest", "list", "bleed") and latest:
        notices = _attach_pictures(
            [latest], pictures, SIZE[layout], fal_key, data_dir, levels, art_style
        )

    for f in finds:
        f.pop("species_row", None)
    for s in [*species, *finds]:
        s.pop("photo", None)
        s.pop("photo_credit", None)
        s.pop("ref", None)
        s.pop("ref_credit", None)
    credits: list[str] = []
    pictured: list[dict[str, Any]] = [*shown, *([latest] if latest else [])]
    if cluster:
        pictured = [{"pic": cluster["pic"]}] if cluster.get("pic") else cluster["birds"]
    for s in pictured:
        c = (s.get("pic") or {}).get("credit")
        if c and c not in credits:
            credits.append(c)
    if len(credits) > 3:
        credits = [
            "Photos "
            + ", ".join(c.removesuffix(" · GBIF") for c in credits[:2])
            + f" and {len(credits) - 2} more · GBIF"
        ]
    groups_fonts: tuple[str, ...] = FONT_BASE
    if layout == "cluster" and cluster_style == "notebook":
        groups_fonts = (*groups_fonts, "caveat")
    groups_fonts = tuple(dict.fromkeys((*groups_fonts, *FONTS[font])))

    return {
        "label": label,
        "area": area,
        "period": period,
        "layout": layout,
        "pictures": pictures,
        "art_style": art_style,
        "font": font,
        "panel": levels,
        "show_sci": options.get("show_sci", True) is not False,
        "footer": footer,
        "count": want,
        "cluster": cluster,
        "cluster_text": cluster_text,
        "text_scale": text_scale,
        "cluster_lines": list(cluster_lines),
        "species": shown,
        "finds": finds[: min(want, LIST_MAX)],
        "latest": latest,
        "species_total": len(species),
        "obs_total": int(raw.get("count") or 0) or sum(s["count"] for s in species),
        "sample": int(raw.get("sample") or 0),
        "groups": sorted(groups.values(), key=lambda g: -g["obs"]),
        "date": now.strftime("%a %d %b"),
        "date_iso": now.strftime("%Y-%m-%d"),
        "credits": credits,
        # what needs the owner (no key, a failure) is printed on the panel;
        # progress (a painting under way, the day's limit) shows on the paintings page
        "notices": [n for n in notices if not P._is_progress(n)],
        "status": [n for n in notices if P._is_progress(n)],
        "font_css": _font_css(groups_fonts),
        "empty": not species,
    }


# ----------------------------------------------------------------------
# Admin page: the AI paintings
# ----------------------------------------------------------------------

_SLUG_RE = re.compile(r"[a-z0-9_]{1,60}(--[a-z]{1,20}){0,2}")
_ARCHIVED_RE = re.compile(r"([a-z0-9_]{1,60}(?:--[a-z]{1,20}){0,2})__(\d{8}-\d{6})")


def _known_species(data_dir: Path) -> list[tuple[str, str, str]]:
    """(common name, scientific name, iconic taxon) for the admin page's
    picker and the batch painter: every species this widget has shown."""
    seen: dict[str, tuple[str, str]] = {}
    with contextlib.suppress(OSError):
        for f in sorted(data_dir.glob("result_*.json")):
            raw = P._read_json(f, {})
            for e in raw.get("species", []) if isinstance(raw, dict) else []:
                if isinstance(e, dict) and e.get("species"):
                    # a name looked up after the answer was kept sits in its own file
                    name = str(e.get("vernacularName") or "") or _kept_name(
                        e.get("speciesKey"), data_dir
                    )
                    seen.setdefault(
                        str(e["species"]),
                        (name or str(e["species"]), str(e.get("iconic") or "")),
                    )
    return sorted(
        ((n, sci, k) for sci, (n, k) in seen.items()), key=lambda b: b[0].lower()
    )


def _gallery(data_dir: Path) -> dict[str, Any]:
    """Everything the admin page shows, read from gen/: one entry per
    painting key (a species in a style)."""
    folder = P._gen_dir(data_dir)
    entries: dict[str, dict[str, Any]] = {}

    def entry(key: str) -> dict[str, Any]:
        base, _, style = key.partition("--")
        style = (
            style if style in P.GEN_STYLES or P._CUSTOM_RE.fullmatch(style) else "gould"
        )
        return entries.setdefault(
            key,
            {
                "slug": key,
                "name": base.replace("_", " ").capitalize(),
                "sci": "",
                "kind": "",
                "style": style,
                "style_label": P._style(style)[0],
                "active": None,
                "archive": [],
                "error": None,
            },
        )

    def take(e: dict[str, Any], meta: Any) -> None:
        if not isinstance(meta, dict):
            return
        if meta.get("name"):
            e["name"] = str(meta["name"])
        e["sci"] = e["sci"] or str(meta.get("sci") or "")
        e["kind"] = e["kind"] or str(meta.get("kind") or "")
        if meta.get("how") and not e.get("how"):
            e["how"] = str(meta["how"])
            words = e["how"]
            e["style_label"] = "Custom: " + (
                words[:44].rstrip() + "…" if len(words) > 45 else words
            )

    with contextlib.suppress(OSError):
        for f in folder.glob("*.jpg"):
            if (
                f.stem.startswith("sheet_")
                or not _SLUG_RE.fullmatch(f.stem)
                or not (folder / f"{f.stem}.json").exists()
            ):
                continue  # a resized copy or a built page, not a painting
            meta = P._read_json(folder / f"{f.stem}.json", {})
            e = entry(f.stem)
            take(e, meta)
            e["active"] = {
                "file": f.name,
                "credit": str(meta.get("credit") or ""),
                "made": time.strftime(
                    "%d %b %Y",
                    time.localtime(int(meta.get("made") or f.stat().st_mtime)),
                ),
            }
        for f in sorted((folder / "archive").glob("*.jpg"), reverse=True):
            m = _ARCHIVED_RE.fullmatch(f.stem)
            if not m:
                continue
            e = entry(m.group(1))
            take(e, P._read_json(f.with_suffix(".json"), {}))
            e["archive"].append(
                {
                    "file": f.name,
                    "made": datetime.strptime(m.group(2), "%Y%m%d-%H%M%S").strftime(
                        "%d %b"
                    ),
                }
            )
        for f in folder.glob("*.err"):
            if _SLUG_RE.fullmatch(f.stem) and not f.stem.startswith("sheet_"):
                err = P._read_json(f, {})
                e = entry(f.stem)
                take(e, err)
                e["error"] = str(err.get("why") or "failed")
    with P._PAINTING_LOCK:
        running = set(P._PAINTING)
    known = {_slug(sci): (n, sci, k) for n, sci, k in _known_species(data_dir)}
    for key, e in entries.items():
        e["painting"] = key in running
        base = key.partition("--")[0]
        if base in known:
            name, sci, kind = known[base]
            e["sci"] = e["sci"] or sci
            e["kind"] = e["kind"] or kind
            if e["name"].lower() == base.replace("_", " "):
                e["name"] = name
    ordered = sorted(
        entries.values(),
        key=lambda e: (e["active"] is None, e["name"].lower(), e["style"]),
    )
    return {
        "paintings": ordered,
        "used": P._budget_used(folder),
        "cap": P._CAP.get(),
        "running": sorted(running),
        "styles": [(k, v[0]) for k, v in P.GEN_STYLES.items()],
        "custom_example": P.CUSTOM_EXAMPLE,
        "style_groups": [
            (g, [(k, P.GEN_STYLES[k][0]) for k in ids]) for g, ids in P.GEN_STYLE_GROUPS
        ],
        "kinds": [(k, v[0]) for k, v in P.KINDS.items()],
    }


def _job_for(e: dict[str, Any]) -> dict[str, Any] | None:
    if not e["sci"]:
        return None
    job = {
        "key": e["slug"],
        "style": e["style"],
        "name": e["name"],
        "sci": e["sci"],
        "kind": e.get("kind") or "",
    }
    if e.get("how"):
        job["how"] = e["how"]
    return job


def blueprint() -> Any:
    """The admin page at /plugins/<id>/: every AI painting this widget has
    made, with earlier versions kept, and buttons to paint, repaint, delete
    or bring back an earlier version."""
    from flask import (
        Blueprint,
        abort,
        current_app,
        flash,
        redirect,
        render_template,
        request,
        send_from_directory,
        url_for,
    )

    bp = Blueprint("gbif_month_admin", __name__, template_folder="templates")

    def plugin() -> Any:
        registry = current_app.config["PLUGIN_REGISTRY"]
        for p in registry.plugins.values():
            with contextlib.suppress(OSError, AttributeError):
                if Path(p.path).resolve() == HERE:
                    return p
        abort(404)

    def data_dir() -> Path:
        path = Path(plugin().data_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def settings() -> dict[str, Any]:
        store = current_app.config.get("SETTINGS_STORE")
        if store is None:
            return {}
        p = plugin()
        got = store.get_for_runtime("plugins", p.id, p.manifest.get("settings", []))
        return got if isinstance(got, dict) else {}

    def fal_key() -> str:
        return str(settings().get("fal_api_key") or "").strip()

    @bp.before_request
    def daily_cap() -> None:
        P._CAP.set(P._daily_cap(settings()))

    def slug_or_404(slug: str) -> str:
        if not _SLUG_RE.fullmatch(slug):
            abort(404)
        return slug

    def back() -> Any:
        return redirect(url_for("gbif_month_admin.index"))

    def start(job: dict[str, Any], label: str) -> None:
        key = fal_key()
        folder = P._gen_dir(data_dir())
        if not key:
            flash("Add a fal.ai key in this widget's settings first.", "warn")
        elif P._budget_used(folder) >= P._CAP.get():
            flash(
                f"Today's limit of {P._CAP.get()} paintings is used up. Raise it in this widget's settings.",
                "warn",
            )
        elif not P._start_painting(job, data_dir(), key):
            flash(f"{label} is already being painted.", "info")
        else:
            with contextlib.suppress(OSError):
                (folder / f"{job['key']}.err").unlink()
            flash(
                f"Painting {label}. It takes up to a minute; this page redraws when it lands.",
                "success",
            )

    @bp.get("/")
    def index() -> str:
        dd = data_dir()
        return render_template(
            "gbif_month/admin.html",
            g=_gallery(dd),
            known=_known_species(dd),
            has_key=bool(fal_key()),
        )

    @bp.get("/progress")
    def progress() -> Any:
        """What is painting right now, for the page's live ticker."""
        from flask import jsonify

        folder = P._gen_dir(data_dir())
        now = time.time()
        with P._PAINTING_LOCK:
            running = [
                {
                    "key": k,
                    "name": name,
                    "style": P._style(style)[0],
                    "seconds": int(now - since),
                }
                for k, (since, name, style) in P._PAINTING_SINCE.items()
            ]
            batches = [P._style(t)[0] for t in sorted(P._BATCH)]
            finished = [{"key": k, "at": int(at)} for k, at in P._FINISHED]
        return jsonify(
            {
                "running": running,
                "batches": batches,
                "finished": finished,
                "used": P._budget_used(folder),
                "cap": P._CAP.get(),
                "has_key": bool(fal_key()),
            }
        )

    @bp.get("/painting/<path:name>")
    def image(name: str) -> Any:
        folder = P._gen_dir(data_dir())
        stem, _, ext = name.rpartition(".")
        if ext != "jpg":
            abort(404)
        if _SLUG_RE.fullmatch(stem):
            return send_from_directory(folder, name)
        if stem.startswith("archive/") and _ARCHIVED_RE.fullmatch(stem[8:]):
            return send_from_directory(folder / "archive", name[8:])
        abort(404)

    @bp.post("/paint")
    def paint() -> Any:
        sci = request.form.get("sci", "").strip()
        name = request.form.get("name", "").strip()
        known = {s: (n, k) for n, s, k in _known_species(data_dir())}
        kind = request.form.get("kind", "").strip()
        if not name:
            name = known.get(sci, (sci, ""))[0]
        if not kind:
            kind = known.get(sci, ("", ""))[1]
        if not sci or not re.fullmatch(r"[A-Z][a-z-]+ [a-z-]+( [a-z-]+)?", sci):
            flash("Give the scientific name, for example Amanita muscaria.", "warn")
            return back()
        if kind not in P.KINDS:
            kind = ""
        style = _choice(request.form.get("style"), P.GEN_STYLE_IDS)
        job = {
            "key": P._pkey(_slug(sci), style),
            "style": style,
            "name": name[:80],
            "sci": sci,
            "kind": kind,
        }
        if style == "custom":
            how = (
                " ".join(request.form.get("how", "").split())[:800] or P.CUSTOM_EXAMPLE
            )
            job["style"] = P._custom_tag(how)
            job["key"] = P._pkey(_slug(sci), job["style"])
            job["how"] = how
        start(job, f"the {name[:80]}")
        return back()

    @bp.post("/<slug>/repaint")
    def repaint(slug: str) -> Any:
        slug = slug_or_404(slug)
        e = next(
            (e for e in _gallery(data_dir())["paintings"] if e["slug"] == slug), None
        )
        job = _job_for(e) if e else None
        if job is None:
            abort(404)
        start(job, f"the {e['name']}")
        return back()

    @bp.post("/<slug>/delete")
    def delete(slug: str) -> Any:
        slug = slug_or_404(slug)
        folder = P._gen_dir(data_dir())
        for f in (
            folder / f"{slug}.jpg",
            folder / f"{slug}.json",
            folder / f"{slug}.err",
            folder / f"{slug}.cut.png",
        ):
            with contextlib.suppress(OSError):
                f.unlink()
        P._drop_derived(folder, slug)
        flash(
            "Painting deleted. With AI paintings on, the species is painted again the next time it is shown.",
            "success",
        )
        return back()

    def retire(keys: list[str]) -> int:
        folder = P._gen_dir(data_dir())
        done = 0
        for key in keys:
            if not _SLUG_RE.fullmatch(key) or not (folder / f"{key}.jpg").exists():
                continue
            P._archive(folder, key)
            with contextlib.suppress(OSError):
                (folder / f"{key}.err").unlink()
            done += 1
        return done

    def retired(n: int) -> None:
        if n:
            flash(
                f"{n} painting{'' if n == 1 else 's'} moved to earlier versions. The widget paints them again as the species are shown, within the daily limit.",
                "success",
            )
        else:
            flash("No paintings were selected.", "info")

    @bp.post("/budget-reset")
    def budget_reset() -> Any:
        P._write(
            P._gen_dir(data_dir()) / "budget.json",
            json.dumps({time.strftime("%Y-%m-%d"): 0}).encode(),
        )
        flash("Today's count is back to 0.", "success")
        return back()

    @bp.post("/bulk-delete")
    def bulk_delete() -> Any:
        retired(retire(request.form.getlist("keys")))
        return back()

    @bp.post("/style-paint")
    def style_paint() -> Any:
        """Paint every species this widget has shown in one style, skipping
        the ones already painted, one after another within the daily limit."""
        style = request.form.get("art", "")
        if style not in P.GEN_STYLES:
            abort(400)
        key = fal_key()
        if not key:
            flash("Add a fal.ai key in this widget's settings first.", "warn")
            return back()
        folder = P._gen_dir(data_dir())
        jobs = [
            {
                "key": P._pkey(_slug(sci), style),
                "style": style,
                "name": name,
                "sci": sci,
                "kind": kind,
            }
            for name, sci, kind in _known_species(data_dir())
            if not (folder / f"{P._pkey(_slug(sci), style)}.jpg").exists()
        ]
        label = P.GEN_STYLES[style][0]
        room = max(0, P._CAP.get() - P._budget_used(folder))
        if not jobs:
            flash(
                f"Every species shown so far already has a painting in {label}.", "info"
            )
        elif not room:
            flash(
                f"Today's limit of {P._CAP.get()} paintings is used up. Raise it in this widget's settings.",
                "warn",
            )
        elif not P._start_batch(jobs, data_dir(), key):
            flash(f"{label} is already being painted for every species.", "info")
        else:
            n = min(len(jobs), room)
            flash(
                f"Painting {n} species in {label}, one after another"
                + (
                    f"; {len(jobs) - n} more wait for tomorrow's limit"
                    if len(jobs) > n
                    else ""
                )
                + ". This page redraws as they arrive.",
                "success",
            )
        return back()

    @bp.post("/style-delete")
    def style_delete() -> Any:
        style = request.form.get("art", "")
        if style not in P.GEN_STYLES and not P._CUSTOM_RE.fullmatch(style):
            abort(400)
        keys = [
            e["slug"]
            for e in _gallery(data_dir())["paintings"]
            if e["style"] == style and e["active"]
        ]
        retired(retire(keys))
        return back()

    @bp.post("/<slug>/clear-error")
    def clear_error(slug: str) -> Any:
        slug = slug_or_404(slug)
        with contextlib.suppress(OSError):
            (P._gen_dir(data_dir()) / f"{slug}.err").unlink()
        return back()

    @bp.post("/archive/<stem>/use")
    def use_archived(stem: str) -> Any:
        import os

        m = _ARCHIVED_RE.fullmatch(stem)
        if not m:
            abort(404)
        folder = P._gen_dir(data_dir())
        slug, src = m.group(1), folder / "archive" / f"{stem}.jpg"
        if not src.exists():
            abort(404)
        meta = P._read_json(src.with_suffix(".json"), {})
        if (folder / f"{slug}.jpg").exists():
            P._archive(folder, slug)
        os.replace(src, folder / f"{slug}.jpg")
        if not isinstance(meta, dict) or not meta:
            meta = {"credit": "AI painting", "made": int(time.time())}
        P._write(folder / f"{slug}.json", json.dumps(meta).encode())
        with contextlib.suppress(OSError):
            src.with_suffix(".json").unlink()
        P._drop_derived(folder, slug)
        flash("That version is the species' painting again.", "success")
        return back()

    @bp.post("/archive/<stem>/delete")
    def delete_archived(stem: str) -> Any:
        if not _ARCHIVED_RE.fullmatch(stem):
            abort(404)
        arch = P._gen_dir(data_dir()) / "archive"
        for f in (arch / f"{stem}.jpg", arch / f"{stem}.json"):
            with contextlib.suppress(OSError):
                f.unlink()
        return back()

    return bp
