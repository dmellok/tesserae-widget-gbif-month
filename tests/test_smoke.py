"""gbif_month: GBIF parsing, aggregation and caching, the picture chain, AI
paintings, the cluster page, fonts, and a render smoke test of client.js.
No network: every outbound call is patched."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("gbif_server", ROOT / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


server = _load()
P = server.P

MELBOURNE = {
    "latitude": -37.81,
    "longitude": 144.96,
    "location": {"label": "Melbourne", "timezone": "Australia/Melbourne"},
}
GROUPS = ["212", "11592253+11418114+11493978", "216", "6", "5"]


def _settle(timeout: float = 10.0) -> None:
    end = time.time() + timeout
    while P._PAINTING and time.time() < end:
        time.sleep(0.02)
    assert not P._PAINTING, "a painting never finished"


def _jpeg(w: int = 300, h: int = 400, colour=(200, 120, 40)) -> bytes:
    """A blot on cream paper, like a painting."""
    im = Image.new("RGB", (w, h), (243, 234, 214))
    ImageDraw.Draw(im).ellipse((w * 0.3, h * 0.25, w * 0.7, h * 0.7), fill=colour)
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


def _png_alpha() -> bytes:
    w, h = 300, 400
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((80, 100, 220, 300), fill=(90, 60, 30, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _occ(
    n,
    sci,
    common,
    cls,
    order,
    family,
    kingdom="Animalia",
    phylum="Arthropoda",
    when="2026-10-05",
    who="somebody",
    media=True,
    key=None,
    licence="http://creativecommons.org/licenses/by-nc/4.0/",
):
    """A GBIF occurrence as the search hands it back, trimmed to what the backend reads."""
    r = {
        "key": n,
        "species": sci,
        "speciesKey": key or abs(hash(sci)) % 100000,
        "kingdom": kingdom,
        "phylum": phylum,
        "class": cls,
        "order": order,
        "family": family,
        "eventDate": f"{when}T08:00:00",
        "recordedBy": who,
        "locality": "Hawthorn East, Melbourne",
        "basisOfRecord": "HUMAN_OBSERVATION",
    }
    if common:
        r["vernacularName"] = common
    if media:
        r["media"] = [
            {
                "type": "StillImage",
                "identifier": f"https://inaturalist-open-data.s3.amazonaws.com/photos/{n}/original.jpg",
                "license": licence,
                "rightsHolder": who,
            }
        ]
    return r


# Eleven records, five species: the moth four times (the newest with a picture), the swan three
# times with no media and no common name (eBird's are like that), the fungus twice, a butterfly
# and an orchid once each, plus one record identified only to a genus.
OCCURRENCES = [
    _occ(
        1,
        "Agrotis infusa",
        None,
        "Insecta",
        "Lepidoptera",
        "Noctuidae",
        when="2026-10-11",
        who="dpcumming",
        key=1,
    ),
    _occ(
        2,
        "Agrotis infusa",
        None,
        "Insecta",
        "Lepidoptera",
        "Noctuidae",
        when="2026-10-02",
        who="a",
        media=False,
        key=1,
    ),
    _occ(
        3,
        "Agrotis infusa",
        None,
        "Insecta",
        "Lepidoptera",
        "Noctuidae",
        when="2025-10-20",
        who="b",
        media=False,
        key=1,
    ),
    _occ(
        4,
        "Agrotis infusa",
        None,
        "Insecta",
        "Lepidoptera",
        "Noctuidae",
        when="2024-10-01",
        who="c",
        media=False,
        key=1,
    ),
    _occ(
        5,
        "Cygnus atratus",
        "Black Swan",
        "Aves",
        "Anseriformes",
        "Anatidae",
        phylum="Chordata",
        when="2026-10-13",
        who="paperdaisy16",
        media=False,
        key=2,
    ),
    _occ(
        6,
        "Cygnus atratus",
        "Black Swan",
        "Aves",
        "Anseriformes",
        "Anatidae",
        phylum="Chordata",
        when="2025-10-03",
        who="d",
        media=False,
        key=2,
    ),
    _occ(
        7,
        "Cygnus atratus",
        "Black Swan",
        "Aves",
        "Anseriformes",
        "Anatidae",
        phylum="Chordata",
        when="2023-10-03",
        who="e",
        media=False,
        key=2,
    ),
    _occ(
        8,
        "Amanita muscaria",
        None,
        "Agaricomycetes",
        "Agaricales",
        "Amanitaceae",
        kingdom="Fungi",
        phylum="Basidiomycota",
        when="2026-10-09",
        who="fungal",
        key=3,
    ),
    _occ(
        9,
        "Amanita muscaria",
        None,
        "Agaricomycetes",
        "Agaricales",
        "Amanitaceae",
        kingdom="Fungi",
        phylum="Basidiomycota",
        when="2024-10-09",
        who="f",
        media=False,
        key=3,
    ),
    _occ(
        10,
        "Vanessa kershawi",
        None,
        "Insecta",
        "Lepidoptera",
        "Nymphalidae",
        when="2026-10-04",
        who="g",
        key=4,
        licence="All rights reserved",
    ),
    _occ(
        11,
        "Glossodia major",
        "Waxlip Orchid",
        "Liliopsida",
        "Asparagales",
        "Orchidaceae",
        kingdom="Plantae",
        phylum="Tracheophyta",
        when="2026-10-01",
        who="h",
        key=5,
    ),
    {
        "key": 12,
        "genus": "Acacia",
        "kingdom": "Plantae",
        "class": "Magnoliopsida",
        "eventDate": "2026-10-12T08:00:00",
        "basisOfRecord": "HUMAN_OBSERVATION",
    },
]
GBIF_ANSWER = {
    "offset": 0,
    "limit": 150,
    "endOfRecords": False,
    "count": 9866,
    "results": OCCURRENCES,
}


@pytest.fixture
def offline():
    """No network: iNaturalist, the photos and fal all answer from here."""

    class Calls(list):
        bodies: list[bytes]

    calls = Calls()
    calls.bodies = []

    def fake_http(url, token="", accept="application/json", **kw):  # noqa: ARG001
        calls.append(url)
        if "nano-banana" in url and kw.get("body"):
            calls.bodies.append(kw["body"])
        if "api.gbif.org/v1/occurrence/search" in url:
            return json.dumps(GBIF_ANSWER).encode(), None
        if re.search(r"api.gbif.org/v1/species/1/vernacularNames", url):
            return json.dumps(
                {
                    "results": [
                        {"vernacularName": "Bogong-Falter", "language": "deu"},
                        {"vernacularName": "Bogong Moth", "language": "eng"},
                    ]
                }
            ).encode(), None
        if re.search(r"api.gbif.org/v1/species/3/vernacularNames", url):
            return json.dumps(
                {"results": [{"vernacularName": "Fly Agaric", "language": "eng"}]}
            ).encode(), None
        if "vernacularNames" in url:
            return json.dumps({"results": []}).encode(), None
        if re.search(r"api.gbif.org/v1/species/2/media", url):
            return json.dumps(
                {
                    "results": [
                        {
                            "type": "StillImage",
                            "identifier": "https://images.example/swan.jpg",
                            "license": "CC BY-NC",
                            "rightsHolder": "Hans van der Meulen",
                        }
                    ]
                }
            ).encode(), None
        if "/media" in url and "gbif" in url:
            return json.dumps({"results": []}).encode(), None
        if "inaturalist.org/v1/taxa?" in url:
            return json.dumps({"results": [{"id": 7}]}).encode(), None
        if "inaturalist.org/v1/taxa/7" in url:
            photo = {
                "url": "https://inat.example/photos/1/square.jpg",
                "license_code": "cc-by",
                "attribution_name": "B. Botanist",
            }
            return json.dumps(
                {"results": [{"taxon_photos": [{"photo": photo}]}]}
            ).encode(), None
        if "birefnet" in url:
            return json.dumps(
                {"image": {"url": "https://fal.example/cut.png"}}
            ).encode(), None
        if "fal.run" in url:
            return json.dumps(
                {"images": [{"url": "https://fal.example/out.jpg"}]}
            ).encode(), None
        if accept == "image/*" and url.endswith(".png"):
            return _png_alpha(), None
        if accept == "image/*":
            return _jpeg(), None
        return None, "unreachable"

    with patch.object(P, "_http", side_effect=fake_http):
        yield calls


def _fetch(tmp_path, offline, **opts):  # noqa: ARG001
    settings = {"fal_api_key": opts.pop("fal_key", "")}
    options = {**MELBOURNE, "groups": GROUPS, "pictures": "none", **opts}
    return server.fetch(
        options, settings, ctx={"data_dir": str(tmp_path), "cell_w": 800, "cell_h": 480}
    )


def test_query_aggregation_and_cache(tmp_path, offline):
    with patch.object(server, "datetime") as dt:
        dt.now.return_value = datetime(
            2026, 10, 14, 9, 0, tzinfo=ZoneInfo("Australia/Melbourne")
        )
        r = _fetch(tmp_path, offline, layout="gallery", radius_km="25")
    assert not r.get("error"), r
    first = offline[0]
    assert first.startswith("https://api.gbif.org/v1/occurrence/search?")
    assert "geoDistance=-37.8100%2C144.9600%2C25km" in first
    assert (
        "month=10" in first
        and "basisOfRecord=HUMAN_OBSERVATION" in first
        and "limit=150" in first
    )
    # the kinds go out as repeated taxonKey values, a joined choice as several
    assert first.count("taxonKey=") == 7 and "taxonKey=11418114" in first
    assert (
        r["label"] == "Melbourne"
        and r["area"] == "within 25 km"
        and r["period"] == "in October"
    )
    # grouped by species, counted and sorted, most recorded first; the genus-only record left out
    assert [(s["sci"], s["count"]) for s in r["species"]] == [
        ("Agrotis infusa", 4),
        ("Cygnus atratus", 3),
        ("Amanita muscaria", 2),
        ("Glossodia major", 1),
        ("Vanessa kershawi", 1),
    ]
    moth = r["species"][0]
    assert (
        moth["species"] == "Agrotis infusa"
        and moth["vernacularName"] == "Bogong Moth"
        and moth["name"] == "Bogong Moth"
    )
    assert (moth["kind"], moth["group"], moth["last"], moth["who"], moth["place"]) == (
        "Moth",
        "Insects",
        "11 Oct",
        "dpcumming",
        "Hawthorn East",
    )
    assert r["species"][1]["name"] == "Black Swan", (
        "the record's own common name is kept"
    )
    assert (
        r["species"][2]["name"] == "Fly Agaric"
        and r["species"][2]["kind"] == "Mushroom"
    )
    assert (
        r["species"][3]["kind"] == "Orchid" and r["species"][4]["kind"] == "Butterfly"
    )
    assert r["species_total"] == 5 and r["obs_total"] == 9866 and r["sample"] == 12
    assert (
        r["latest"]["name"] == "Black Swan" and r["finds"][0]["who"] == "paperdaisy16"
    )
    assert [f["sci"] for f in r["finds"]] == [
        "Cygnus atratus",
        "Agrotis infusa",
        "Amanita muscaria",
        "Vanessa kershawi",
        "Glossodia major",
    ], "one find per species, newest first"
    assert (
        server._recorder(
            "db9e899e83ec1103de11aaf698ccb94ca893082dad341c98b23aee5be193dd51"
        )
        == ""
    )
    assert server._recorder("A. Person | B. Person") == "A. Person"
    assert [g["label"] for g in r["groups"]] == ["Insects", "Birds", "Fungi", "Plants"]
    assert "ref" not in moth and "photo" not in moth, (
        "fetch-time fields do not reach the client"
    )
    # the cache is keyed on the rounded place, the circle, the kinds and the month, not the clock
    n = len(offline)
    with patch.object(server, "datetime") as dt:
        dt.now.return_value = datetime(
            2026, 10, 30, 23, 0, tzinfo=ZoneInfo("Australia/Melbourne")
        )
        _fetch(
            tmp_path,
            offline,
            layout="gallery",
            radius_km="25",
            latitude=-37.812,
            longitude=144.957,
        )
    assert len(offline) == n, (
        "a nearby place in the same month is served from the cache"
    )
    with patch.object(server, "datetime") as dt:
        dt.now.return_value = datetime(
            2026, 11, 1, 9, 0, tzinfo=ZoneInfo("Australia/Melbourne")
        )
        _fetch(tmp_path, offline, layout="gallery", radius_km="25")
    assert any("month=11" in u for u in offline[n:]), "a new month asks afresh"


def test_empty_answer_is_an_empty_page(tmp_path, offline):
    with patch.object(
        P,
        "_http",
        return_value=(json.dumps({"count": 0, "results": []}).encode(), None),
    ):
        r = server.fetch(
            {**MELBOURNE, "groups": GROUPS, "layout": "cluster"},
            {},
            ctx={"data_dir": str(tmp_path)},
        )
    assert (
        not r.get("error") and r["empty"] and r["species"] == [] and r["latest"] is None
    )
    assert r["cluster"] is None and r["species_total"] == 0


def test_nowhere_is_a_friendly_message(tmp_path, offline):
    r = _fetch(tmp_path, offline, latitude=None, longitude=None, location="")
    assert r["error"] == server.ERR_NOWHERE


def test_busy_gbif_serves_the_last_answer(tmp_path, offline):
    r = _fetch(tmp_path, offline, layout="tally")
    assert not r.get("error")
    cache = next(tmp_path.glob("result_*.json"))
    import os

    os.utime(cache, (time.time() - 40 * 86400, time.time() - 40 * 86400))
    with patch.object(P, "_http", return_value=(None, "HTTP 503")):
        r = server.fetch(
            {**MELBOURNE, "groups": GROUPS, "layout": "tally"},
            {},
            ctx={"data_dir": str(tmp_path)},
        )
    assert not r.get("error") and r["species_total"] == 5
    with patch.object(P, "_http", return_value=(None, "HTTP 503")):
        r = server.fetch(
            {**MELBOURNE, "groups": GROUPS, "layout": "tally", "radius_km": "2"},
            {},
            ctx={"data_dir": str(tmp_path)},
        )
    assert "busy" in r["error"]
    with patch.object(P, "_http", return_value=(b'{"foo": 1}', None)):
        r = server.fetch(
            {**MELBOURNE, "groups": GROUPS, "layout": "tally", "radius_km": "5"},
            {},
            ctx={"data_dir": str(tmp_path)},
        )
    assert "expected shape" in r["error"]


def test_photos_come_with_the_record_or_from_gbif_and_are_credited(tmp_path, offline):
    r = _fetch(tmp_path, offline, layout="gallery", pictures="photo")
    pics = [s["pic"] for s in r["species"]]
    assert all(
        p and p["kind"] == "photo" and p["src"].startswith("data:image/jpeg")
        for p in pics
    )
    # the moth's newest record carries a picture, resized from the original; the swan's records
    # carry none, so GBIF's picture of the species stands in
    assert any("/photos/1/medium.jpg" in u for u in offline) and not any(
        "/photos/1/original.jpg" in u for u in offline
    )
    assert r["species"][0]["pic"]["credit"] == "© dpcumming (CC BY NC 4.0) · GBIF"
    assert r["species"][1]["pic"]["credit"] == "© Hans van der Meulen (CC BY-NC) · GBIF"
    assert r["credits"] == [
        "Photos © dpcumming (CC BY NC 4.0), © Hans van der Meulen (CC BY-NC) and 3 more · GBIF"
    ]


def test_painting_once_per_species_from_a_licensed_picture(tmp_path, offline):
    r = _fetch(tmp_path, offline, layout="gallery", pictures="gen", fal_key="k")
    _settle()
    assert "AI paintings need" not in " ".join(r["notices"])
    assert any(n.startswith("Painting the Bogong Moth") for n in r["status"])
    prompt = json.loads(offline.bodies[0])
    assert prompt["prompt"].startswith(
        "Paint only the insect in the reference photo, the Bogong Moth (Agrotis infusa), as a plate from"
    )
    assert "no ground, soil, leaf litter" in prompt["prompt"]
    # the reference is the record's own licensed picture
    assert prompt["image_urls"] == [
        "https://inaturalist-open-data.s3.amazonaws.com/photos/1/medium.jpg"
    ]
    gen = tmp_path / "gen"
    assert (gen / "agrotis_infusa--gould.jpg").exists() and (
        gen / "agrotis_infusa--gould.cut.png"
    ).exists()
    meta = json.loads((gen / "agrotis_infusa--gould.json").read_text())
    assert meta["kind"] == "Insecta" and meta["credit"].endswith(
        "after © dpcumming (CC BY NC 4.0) · GBIF"
    )
    r = _fetch(tmp_path, offline, layout="gallery", pictures="gen", fal_key="k")
    _settle()
    assert r["species"][0]["pic"]["kind"] == "generated"
    assert any(n.startswith("Painting the Black Swan") for n in r["status"]), (
        "one new painting per fetch"
    )
    assert len(offline.bodies) == 2
    swan = json.loads(offline.bodies[1])
    assert (
        "Paint only the bird in the reference photo, the Black Swan" in swan["prompt"]
    )
    assert "perched on a simple branch" in swan["prompt"]
    assert swan["image_urls"] == ["https://images.example/swan.jpg"], (
        "GBIF's licensed picture of the species"
    )
    # an all-rights-reserved picture is shown but never painted from: iNaturalist's licensed one stands in
    for _ in range(3):
        _fetch(tmp_path, offline, layout="gallery", pictures="gen", fal_key="k")
        _settle()
    lady = next(
        json.loads(b)
        for b in offline.bodies
        if "Vanessa kershawi" in json.loads(b)["prompt"]
    )
    assert lady["image_urls"] == ["https://inat.example/photos/1/large.jpg"]


def test_painting_needs_a_key_and_a_cap(tmp_path, offline):
    r = _fetch(tmp_path, offline, layout="gallery", pictures="gen")
    assert P.NOTE_NO_KEY in r["notices"]
    assert not offline.bodies
    (tmp_path / "gen").mkdir(exist_ok=True)
    (tmp_path / "gen" / "budget.json").write_text(
        json.dumps({time.strftime("%Y-%m-%d"): 6})
    )
    r = server.fetch(
        {**MELBOURNE, "groups": GROUPS, "layout": "gallery", "pictures": "gen"},
        {"fal_api_key": "k", "gen_daily_cap": "6"},
        ctx={"data_dir": str(tmp_path)},
    )
    assert any("limit of 6" in n for n in r["status"]) and not offline.bodies


def test_custom_prompt_and_kind_in_every_style(tmp_path, offline):
    r = _fetch(
        tmp_path,
        offline,
        layout="gallery",
        pictures="gen",
        fal_key="k",
        art_style="custom",
        art_prompt="a tiny gouache on grey card",
    )
    _settle()
    assert r["art_style"].startswith("custom")
    prompt = json.loads(offline.bodies[0])["prompt"]
    assert "as a tiny gouache on grey card" in prompt
    for sid, (_label, how) in P.GEN_STYLES.items():
        assert "bird" not in how.replace("{kind}", ""), (sid, how)
        assert "plumage" not in how, sid
    assert "grumpy" not in P.GEN_STYLES
    assert set(P.GEN_STYLES) == {i for _g, ids in P.GEN_STYLE_GROUPS for i in ids}


def test_cluster_is_a_montage_until_painted_then_a_sheet(tmp_path, offline):
    r = _fetch(tmp_path, offline, layout="cluster", pictures="photo", count="4")
    c = r["cluster"]
    assert (
        c["montage"]
        and len(c["birds"]) == 4
        and c["birds"][0]["pic"]["kind"] == "photo"
    )
    for _ in range(4):
        r = _fetch(
            tmp_path,
            offline,
            layout="cluster",
            pictures="gen",
            fal_key="k",
            count="4",
            refresh_min="5",
        )
        _settle()
    # every painting is in; the sheet is built in the background on the next fetch
    assert len(offline.bodies) == 4
    for _ in range(20):
        r = _fetch(
            tmp_path,
            offline,
            layout="cluster",
            pictures="gen",
            fal_key="k",
            count="4",
            cluster_labels=["name", "kind", "count"],
        )
        _settle()
        # an earlier sheet of the same shape stands in while the fuller one builds
        if (
            r["cluster"]
            and r["cluster"].get("sheet")
            and len(r["cluster"]["birds"]) == 4
        ):
            break
        time.sleep(0.2)
    c = r["cluster"]
    assert (
        c["sheet"]
        and c["pic"]["kind"] == "sheet"
        and c["pic"]["src"].startswith("data:image/jpeg")
    )
    assert len(c["birds"]) == 4 and c["lines"] == ["name", "kind", "count"]
    assert c["birds"][0]["kind"] and c["waiting"] == []
    for b in c["birds"]:
        assert 0 <= b["at"][0] <= 1 and 0 <= b["at"][1] <= 1.05
    assert r["credits"][0].startswith("AI paintings, hand-coloured lithograph")


def test_sizes_by_kind_and_sizing_option(tmp_path, offline):
    rows = [
        {"name": "A", "sci": "a a", "count": 10, "factor": 1.5, "kind": "Mammal"},
        {"name": "B", "sci": "b b", "count": 2, "factor": 0.6, "kind": "Insect"},
    ]
    gen = tmp_path / "gen"
    gen.mkdir()
    for r in rows:
        (gen / f"{P._slug(r['sci'])}--gould.jpg").write_bytes(_jpeg())
        (gen / f"{P._slug(r['sci'])}--gould.json").write_text(
            json.dumps({"credit": "x", "made": 1})
        )
    with patch.object(server.S, "_sheet", return_value=None) as sheet:
        server._specimen_sheet(
            rows, 2, "gould", 1.5, "k", tmp_path, "colour", sizing="kind"
        )
        assert [b["factor"] for b in sheet.call_args[0][0]] == [1.5, 0.6]
        server._specimen_sheet(
            rows, 2, "gould", 1.5, "k", tmp_path, "colour", sizing="busy"
        )
        assert sheet.call_args[0][0][0]["factor"] > sheet.call_args[0][0][1]["factor"]
        server._specimen_sheet(
            rows, 2, "gould", 1.5, "k", tmp_path, "colour", sizing="rare"
        )
        assert sheet.call_args[0][0][0]["factor"] < sheet.call_args[0][0][1]["factor"]
        server._specimen_sheet(
            rows, 2, "gould", 1.5, "k", tmp_path, "colour", sizing="same"
        )
        assert [b["factor"] for b in sheet.call_args[0][0]] == [1.0, 1.0]


def test_fonts_follow_the_look(tmp_path, offline):
    r = _fetch(tmp_path, offline, layout="gallery")
    assert "IM Fell English" in r["font_css"] and "Caveat" not in r["font_css"]
    r = _fetch(
        tmp_path, offline, layout="cluster", cluster_style="notebook", font="playfair"
    )
    assert "Caveat" in r["font_css"] and "Playfair" in r["font_css"]


def test_panel_levels_convert_pictures(tmp_path, offline):
    r = _fetch(tmp_path, offline, layout="gallery", pictures="photo", panel="mono")
    src = r["species"][0]["pic"]["src"]
    import base64

    im = Image.open(io.BytesIO(base64.b64decode(src.split(",", 1)[1])))
    assert im.mode == "L" and r["panel"] == "mono"


def test_progress_stays_off_the_panel(tmp_path, offline):
    r = _fetch(tmp_path, offline, layout="gallery", pictures="gen", fal_key="k")
    _settle()
    assert r["status"] and not any(n.startswith("Painting") for n in r["notices"])


def test_admin_gallery_lists_paintings(tmp_path, offline):
    _fetch(tmp_path, offline, layout="gallery", pictures="gen", fal_key="k")
    _settle()
    g = server._gallery(tmp_path)
    assert (
        g["paintings"][0]["name"] == "Bogong Moth"
        and g["paintings"][0]["sci"] == "Agrotis infusa"
    )
    assert g["paintings"][0]["kind"] == "Insecta" and g["paintings"][0]["active"]
    assert ("Fungi", "Fungi") in g["kinds"]
    known = server._known_species(tmp_path)
    assert ("Fly Agaric", "Amanita muscaria", "Fungi") in known
    job = server._job_for(g["paintings"][0])
    assert job == {
        "key": "agrotis_infusa--gould",
        "style": "gould",
        "name": "Bogong Moth",
        "sci": "Agrotis infusa",
        "kind": "Insecta",
    }


def test_manifest_is_well_formed():
    m = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    names = [o["name"] for o in m["cell_options"]]
    assert len(names) == len(set(names))
    for o in m["cell_options"]:
        if o["type"] in ("select", "multiselect"):
            values = [c["value"] for c in o["choices"]]
            assert len(values) == len(set(values)), o["name"]
            if "default" in o and o["type"] == "select":
                assert o["default"] in values, o["name"]
            if o["type"] == "multiselect":
                assert all(d in values for d in o["default"]), o["name"]
        for dep in o.get("show_if") or {}:
            assert dep in names, (o["name"], dep)
    art = next(o for o in m["cell_options"] if o["name"] == "art_style")
    assert {c["value"] for c in art["choices"]} == set(P.GEN_STYLE_IDS)
    groups = next(o for o in m["cell_options"] if o["name"] == "groups")
    assert all(re.fullmatch(r"\d+(\+\d+)*", c["value"]) for c in groups["choices"])
    assert len(groups["choices"]) >= 40
    assert not any(
        o["name"] in ("days", "quality", "user_login", "refresh_min")
        for o in m["cell_options"]
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("layout", server.LAYOUTS)
def test_client_renders_every_layout(tmp_path, offline, layout):
    data = _fetch(
        tmp_path,
        offline,
        layout=layout,
        pictures="photo" if layout != "tally" else "none",
    )
    script = (
        f"import('file://{ROOT}/client.js').then(m => {{ const s = {{innerHTML: ''}};"
        f" m.default(s, {{data: {json.dumps(data)}, cell: {{size: 'md'}}}});"
        " if (!s.innerHTML.includes('class=\"bb lay-')) throw new Error('no render');"
        " console.log('ok'); })"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout


def test_admin_template_renders(tmp_path, offline):
    """The admin page renders against the gallery payload: a stub of the host's
    base template and component macros stands in for Tesserae itself."""
    import jinja2
    from flask import Flask

    _fetch(tmp_path, offline, layout="gallery", pictures="gen", fal_key="k")
    _settle()
    app = Flask("t", template_folder=str(ROOT / "templates"))
    stubs = {
        "_base.html": "{% block title %}{% endblock %}{% block main %}{% endblock %}",
        "_components.html": (
            "{% macro card_head(t, icon_name='', level=2, blurb='') %}<h2>{{ t }}</h2>{{ blurb }}{% endmacro %}"
            "{% macro button(t, icon='', variant='', disabled=False) %}<button>{{ t }}</button>{% endmacro %}"
            "{% macro empty_state(i, t, b) %}<p>{{ t }} {{ b }}</p>{% endmacro %}"
        ),
    }
    app.jinja_loader = jinja2.ChoiceLoader(
        [jinja2.DictLoader(stubs), jinja2.FileSystemLoader(str(ROOT / "templates"))]
    )
    app.register_blueprint(server.blueprint(), url_prefix="/plugins/gbif_month")
    with app.test_request_context("/plugins/gbif_month/"):
        from flask import render_template

        html = render_template(
            "gbif_month/admin.html",
            g=server._gallery(tmp_path),
            known=server._known_species(tmp_path),
            has_key=True,
        )
    assert "Bogong Moth" in html and "Agrotis infusa" in html
    assert 'name="sci"' in html and html.count("<optgroup") >= 8
    assert "No paintings yet" not in html
