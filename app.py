#!/usr/bin/env python3
import json
import logging
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
CONFIG = json.loads(Path(os.environ.get("FLIGHTWALL_CONFIG", ROOT / "config.json")).read_text())
LOGO_CACHE = Path(os.environ.get("FLIGHTWALL_LOGO_CACHE", CONFIG.get("logo_cache_dir", ROOT / "logo-cache")))
LOG = logging.getLogger("flightwall")
LOCK = threading.Lock()
LOGO_LOCK = threading.Lock()
ADSB_FI_LOCK = threading.Lock()
STATE = {"aircraft": [], "receiver_ok": False, "updated": None, "error": None}
CACHE = {}
SKYAWARE_DB_CACHE = {}
HEXDB_CACHE = {}
FAA_INDEX_CACHE = {"mtime_ns": None, "data": {}}
PENDING = set()
LOGO_ATTEMPTS = {}
MOTION_HISTORY = {}
ADSB_FI_LAST_REQUEST = 0.0
POOL = ThreadPoolExecutor(max_workers=2)
FAA_INDEX = Path(os.environ.get("FLIGHTWALL_FAA_INDEX", ROOT / "faa-aircraft.json"))
HEX = re.compile(r"^[0-9a-f]{6}$", re.I)
CALLSIGN = re.compile(r"^[A-Z0-9]{2,8}$")
LOGO = re.compile(r"^[A-Z0-9]{2,4}$")
LOGO_FILE = re.compile(r"^([A-Z0-9]{2,4})\.(svg|png)$")
LOGO_STOP_WORDS = {"AIR", "AIRLINE", "AIRLINES", "AIRWAYS", "THE", "GROUP", "HOLDINGS", "INC", "LLC", "LTD"}
BUNDLED_AIRLINE_LOGOS = {
    "AAL", "AAY", "ABX", "ASA", "ASH", "DAL", "DLH", "EDV", "FDX", "FFT",
    "GJS", "IBE", "JBU", "JIA", "PDT", "RPA", "SWA", "TAI", "UAL", "UPS",
}
BUNDLED_LOGO_ALIASES = {
    "ENY": "AAL",  # Envoy Air operates American Eagle flights.
    "UCA": "UAL",  # CommuteAir operates United Express flights.
}
SYMBOL_TITLE_WORDS = {"SYMBOL", "ICON", "EMBLEM", "MARK", "CRANE", "HEART", "SHIELD", "TAIL"}
WORDMARK_TITLE_WORDS = {"WORDMARK", "LOGOTYPE", "TEXT"}
AIRCRAFT_MODEL_NAMES = {
    "A20N": "A320neo", "A21N": "A321neo", "A306": "A300-600", "A319": "A319", "A320": "A320", "A321": "A321",
    "A332": "A330-200", "A333": "A330-300", "A359": "A350-900",
    "B712": "717-200", "B721": "727-100", "B722": "727-200",
    "B731": "737-100", "B732": "737-200", "B733": "737-300", "B734": "737-400",
    "B735": "737-500", "B736": "737-600", "B737": "737-700", "B738": "737-800",
    "B739": "737-900", "B37M": "737 MAX 7", "B38M": "737 MAX 8", "B39M": "737 MAX 9",
    "B3XM": "737 MAX 10", "B741": "747-100", "B742": "747-200", "B743": "747-300",
    "B744": "747-400", "B748": "747-8", "B74S": "747SP",
    "B752": "757-200", "B753": "757-300", "B762": "767-200", "B763": "767-300",
    "B764": "767-400", "B772": "777-200", "B773": "777-300", "B77L": "777-200LR",
    "B77W": "777-300ER", "B788": "787-8", "B789": "787-9", "B78X": "787-10",
    "BCS1": "A220-100", "BCS3": "A220-300",
    "C172": "172 Skyhawk", "C68A": "Citation Latitude", "CL35": "Challenger 350",
    "CRJ2": "CRJ-200", "CRJ7": "CRJ-700", "CRJ9": "CRJ-900",
    "E145": "ERJ-145", "E45X": "ERJ-145XR", "E170": "E170", "E190": "E190",
    "E75L": "E175", "E75S": "E175",
}
MANUFACTURER_NAMES = {
    "AIRBUS": "Airbus", "AIRBUS SAS": "Airbus", "AIRBUS S.A.S.": "Airbus",
    "AIRBUS CANADA LP": "Airbus", "BOEING": "Boeing", "BOMBARDIER": "Bombardier",
    "BOEING COMPANY": "Boeing", "THE BOEING COMPANY": "Boeing",
    "CESSNA": "Cessna", "CIRRUS": "Cirrus", "CIRRUS DESIGN CORP": "Cirrus",
    "DASSAULT": "Dassault", "EMBRAER": "Embraer",
    "GULFSTREAM": "Gulfstream", "GULFSTREAM AEROSPACE": "Gulfstream",
    "PILATUS": "Pilatus", "PILATUS AIRCRAFT LTD": "Pilatus", "PIPER": "Piper",
}
PIAWARE_CATEGORY_ICONS = {
    "A1": "light", "A2": "small-jet", "A3": "airliner", "A4": "heavy-twin",
    "A5": "heavy-four", "A6": "high-performance", "A7": "helicopter",
    "B1": "light", "B2": "balloon", "B4": "light", "B7": "high-performance",
    "C0": "ground", "C1": "ground", "C2": "ground", "C3": "ground",
    "C4": "ground", "C5": "ground", "C6": "ground", "C7": "ground",
}


def validate_config(config):
    latitude = config.get("receiver_lat")
    longitude = config.get("receiver_lon")
    if (latitude is None) != (longitude is None):
        raise ValueError("receiver_lat and receiver_lon must both be set or both be null")
    for name, value, minimum, maximum in (
        ("receiver_lat", latitude, -90, 90),
        ("receiver_lon", longitude, -180, 180),
    ):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not minimum <= value <= maximum
        ):
            raise ValueError(f"{name} must be a number from {minimum} to {maximum}")


def fetch_json(url, timeout=5):
    request = Request(url, headers={"User-Agent": "FlightWall/0.1 (personal display)", "Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def post_json(url, payload, timeout=5):
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "User-Agent": "FlightWall/0.1 (personal display)",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def fetch_png(url, timeout=8, max_bytes=1_000_000):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"upload.wikimedia.org", "thumb.wikimedia.org"}:
        raise ValueError("Unexpected logo download host")
    request = Request(url, headers={"User-Agent": "FlightWallClone/0.2 (personal local display)", "Accept": "image/png"})
    with urlopen(request, timeout=timeout) as response:
        if response.headers.get_content_type() != "image/png":
            raise ValueError("Logo thumbnail was not PNG")
        payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError("Logo thumbnail exceeded size limit")
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Logo thumbnail had an invalid PNG signature")
    width = int.from_bytes(payload[16:20], "big")
    height = int.from_bytes(payload[20:24], "big")
    if not 1 <= width <= 2048 or not 1 <= height <= 2048:
        raise ValueError("Logo thumbnail dimensions were invalid")
    return payload


def logo_name_tokens(name):
    return [token for token in re.findall(r"[A-Z0-9]+", name.upper()) if token not in LOGO_STOP_WORDS and len(token) > 1]


def logo_asset_kind_from_title(title):
    title_tokens = set(re.findall(r"[A-Z0-9]+", title.upper()))
    return "symbol" if SYMBOL_TITLE_WORDS & title_tokens else "wordmark"


def choose_logo_candidate(pages, airline_name, allow_wordmark=True):
    wanted = logo_name_tokens(airline_name)
    if not wanted:
        return None
    candidates = []
    for page in pages if isinstance(pages, list) else []:
        title = str(page.get("title", ""))
        title_tokens = set(re.findall(r"[A-Z0-9]+", title.upper()))
        is_symbol = bool(SYMBOL_TITLE_WORDS & title_tokens)
        is_wordmark = bool(WORDMARK_TITLE_WORDS & title_tokens or "LOGO" in title_tokens)
        if not is_symbol and (not allow_wordmark or not is_wordmark):
            continue
        matches = sum(token in title_tokens for token in wanted)
        if not matches or wanted[0] not in title_tokens:
            continue
        image_info = page.get("imageinfo")
        if not isinstance(image_info, list) or not image_info:
            continue
        info = image_info[0]
        if info.get("thumbmime") != "image/png" or not info.get("thumburl"):
            continue
        candidates.append((is_symbol, matches, -len(title), title, info))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1], item[2]))[3:]


def record_logo_source(code, airline_name, title, description_url):
    source_file = LOGO_CACHE / "sources.json"
    with LOGO_LOCK:
        sources = {}
        if source_file.is_file():
            try:
                sources = json.loads(source_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                sources = {}
        sources[code] = {
            "airline": airline_name,
            "asset_kind": logo_asset_kind_from_title(title),
            "source_title": title,
            "source_url": description_url,
            "downloaded_at": int(time.time()),
        }
        temporary = source_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(sources, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(source_file)


def cache_airline_logo(code, airline_name):
    if not CONFIG.get("auto_download_logos", True) or not LOGO.fullmatch(code or "") or not airline_name:
        return False
    cached = LOGO_CACHE / f"{code}.png"
    if logo_extension(code):
        return True
    now = time.time()
    with LOGO_LOCK:
        if LOGO_ATTEMPTS.get(code, 0) > now:
            return False
        LOGO_ATTEMPTS[code] = now + 6 * 3600
    try:
        params = urlencode({
            "action": "query", "format": "json", "formatversion": 2,
            "generator": "search", "gsrnamespace": 6,
            "gsrsearch": f"{airline_name} airline symbol icon logo", "gsrlimit": 10,
            "prop": "imageinfo", "iiprop": "url|mime|thumbmime",
            "iiurlwidth": 320, "iiurlheight": 160,
        })
        result = fetch_json("https://commons.wikimedia.org/w/api.php?" + params, timeout=8)
        candidate = choose_logo_candidate(result.get("query", {}).get("pages", []), airline_name)
        if not candidate:
            return False
        title, info = candidate
        payload = fetch_png(info["thumburl"])
        LOGO_CACHE.mkdir(parents=True, exist_ok=True)
        temporary = cached.with_suffix(".tmp")
        temporary.write_bytes(payload)
        temporary.replace(cached)
        record_logo_source(code, airline_name, title, info.get("descriptionurl"))
        LOG.info("Cached logo for %s (%s) from %s", code, airline_name, title)
        return True
    except (HTTPError, URLError, TimeoutError, ValueError, OSError, KeyError) as exc:
        LOG.warning("Logo lookup failed for %s (%s): %s", code, airline_name, exc)
        return False


def logo_extension(code):
    if not LOGO.fullmatch(code or ""):
        return None
    asset_code = BUNDLED_LOGO_ALIASES.get(code, code)
    if asset_code in BUNDLED_AIRLINE_LOGOS:
        for extension in ("svg", "png"):
            if (ROOT / "static" / "logos" / f"{asset_code}.{extension}").is_file():
                return extension
    cached = LOGO_CACHE / f"{code}.png"
    source_file = LOGO_CACHE / "sources.json"
    if cached.is_file() and source_file.is_file():
        try:
            source = json.loads(source_file.read_text(encoding="utf-8")).get(code, {})
            if source.get("asset_kind") in {"symbol", "wordmark"}:
                return "png"
        except (OSError, ValueError):
            pass
    return None


def callsign_logo_code(callsign):
    match = re.match(r"^([A-Z]{3})[0-9]", callsign or "")
    return match.group(1) if match else None


def aircraft_display_name(manufacturer, model, type_code):
    manufacturer = str(manufacturer or "").strip()
    model = str(model or "").strip()
    type_code = str(type_code or "").strip().upper()
    if manufacturer:
        manufacturer = MANUFACTURER_NAMES.get(manufacturer.upper(), manufacturer)
    if model:
        inferred = None
        if type_code.startswith("A") and len(type_code) == 4:
            inferred = "Airbus"
        elif re.match(r"^B(?:3[789]|7)", type_code):
            inferred = "Boeing"
        elif type_code.startswith(("E1", "E4", "E5", "E7", "E9")):
            inferred = "Embraer"
        elif type_code.startswith(("CRJ", "CL")):
            inferred = "Bombardier"
        elif type_code.startswith(("C1", "C2", "C5", "C6", "C7")):
            inferred = "Cessna"
        maker = manufacturer or inferred
        raw_model = model.upper()
        if raw_model.startswith("GULFSTREAM "):
            maker = "Gulfstream"
            model = model[len("GULFSTREAM "):]
        model = AIRCRAFT_MODEL_NAMES.get(type_code, {
            "BD-500-1A10": "A220-100",
            "BD-500-1A11": "A220-300",
        }.get(raw_model, model))
        if type_code in {"BCS1", "BCS3"} or raw_model in {"BD-500-1A10", "BD-500-1A11"}:
            maker = "Airbus"
        if maker == "Airbus":
            family = re.match(r"^(A(?:318|319|320|321|330|340|350|380))(?:[-\s].*)?$", model.upper())
            if family:
                model = family.group(1)
        if maker == "Pilatus" and re.match(r"^PC-?12(?:[/\s-].*)?$", model.upper()):
            model = "PC-12"
        if maker == "Boeing" and type_code not in AIRCRAFT_MODEL_NAMES:
            # Enrichment databases often expose Boeing customer codes (for example
            # 717 2BD or 737NG 7H4/W). Keep the family/series and discard the suffix.
            max_model = re.fullmatch(r"(?:BOEING\s+)?737-(7|8|9|10)", model.upper())
            if max_model:
                model = f"737 MAX {max_model.group(1)}"
            else:
                match = re.search(r"\b(717|727|737|747|757|767|777)\s*-?\s*([1-9])[A-Z0-9]{2}(?:\([^)]*\)|/W)?\b", model.upper())
            if not max_model and match:
                family, series = match.groups()
                model = f"{family}-{series}00"
            elif not max_model:
                match = re.search(r"\b787\s*-?\s*(8|9|10)\b", model.upper())
                if match:
                    model = f"787-{match.group(1)}"
        return model if maker and maker.lower() in model.lower() else f"{maker} {model}" if maker else model
    return type_code or "-"


def aircraft_icon_kind(category, aircraft_type=None, manufacturer=None, model=None):
    type_code = str(aircraft_type or "").strip().upper()
    description = f"{manufacturer or ''} {model or ''}".upper()
    propeller_type = re.match(
        r"^(?:C1(?:50|52|62|70|72|75|77|80|82|85)|C20[68]|C210|C337|C208|"
        r"SR2[02]|P28[ART]|PA(?:18|24|28|30|31|32|34|44|46)|DA(?:20|40|42|62)|"
        r"M20[PT]|PC12|TBM[789]|BE(?:20|33|35|36|55|58|9L))$",
        type_code,
    )
    if propeller_type or any(word in description for word in ("SKYHAWK", "SINGLE ENGINE PISTON", "TURBOPROP")):
        return "single-prop"
    return PIAWARE_CATEGORY_ICONS.get(str(category or "").strip().upper(), "unknown")


def cache_get(key):
    with LOCK:
        item = CACHE.get(key)
        return item[0] if item and item[1] > time.time() else None


def cache_set(key, value, ttl):
    with LOCK:
        CACHE[key] = (value, time.time() + ttl)


def skyaware_database_url(hex_code):
    if not HEX.fullmatch(str(hex_code or "")):
        return None
    receiver = urlsplit(CONFIG.get("receiver_url", ""))
    suffix = "/data/aircraft.json"
    if receiver.scheme not in ("http", "https") or not receiver.netloc or not receiver.path.endswith(suffix):
        return None
    base_path = receiver.path[:-len(suffix)]
    shard = str(hex_code)[:2].upper()
    return urlunsplit((receiver.scheme, receiver.netloc, f"{base_path}/db/{shard}.json", "", ""))


def skyaware_aircraft_info(hex_code):
    url = skyaware_database_url(hex_code)
    if not url:
        return {}
    with LOCK:
        cached = SKYAWARE_DB_CACHE.get(url)
        shard_data = cached[0] if cached and cached[1] > time.time() else None
    if shard_data is None:
        try:
            shard_data = fetch_json(url, timeout=3)
        except HTTPError as exc:
            if exc.code != 404:
                raise
            shard_data = {}
        if not isinstance(shard_data, dict):
            raise ValueError("Invalid SkyAware aircraft database shard")
        with LOCK:
            SKYAWARE_DB_CACHE[url] = (shard_data, time.time() + 24 * 3600)
    suffix = str(hex_code)[2:].upper()
    aircraft = shard_data.get(suffix)
    if not isinstance(aircraft, dict):
        return {}
    return {
        "registration": aircraft.get("r"),
        "aircraft_type": aircraft.get("t"),
    }


def hexdb_aircraft_info(hex_code):
    hex_code = str(hex_code or "").upper()
    if not HEX.fullmatch(hex_code):
        return {}
    with LOCK:
        cached = HEXDB_CACHE.get(hex_code)
        if cached and cached[1] > time.time():
            return cached[0]
    url = f"https://hexdb.io/api/v1/aircraft/{hex_code}"
    try:
        aircraft = fetch_json(url, timeout=5)
    except HTTPError as exc:
        if exc.code != 404:
            raise
        aircraft = {}
    if not isinstance(aircraft, dict):
        raise ValueError("Invalid HexDB aircraft response")
    if str(aircraft.get("status")) == "404":
        aircraft = {}
    operator_code = aircraft.get("OperatorFlagCode")
    info = {
        "registration": aircraft.get("Registration"),
        "aircraft_type": aircraft.get("ICAOTypeCode"),
        "model": aircraft.get("Type"),
        "manufacturer": aircraft.get("Manufacturer"),
        "airline": aircraft.get("RegisteredOwners") if operator_code else None,
        "airline_icao": operator_code,
    }
    info = {field: value for field, value in info.items() if value}
    with LOCK:
        HEXDB_CACHE[hex_code] = (info, time.time() + 24 * 3600)
    return info


def adsb_fi_aircraft_info(hex_code):
    global ADSB_FI_LAST_REQUEST
    with ADSB_FI_LOCK:
        delay = 1.05 - (time.monotonic() - ADSB_FI_LAST_REQUEST)
        if delay > 0:
            time.sleep(delay)
        try:
            response = fetch_json(f"https://opendata.adsb.fi/api/v2/hex/{hex_code}", timeout=5)
        finally:
            ADSB_FI_LAST_REQUEST = time.monotonic()
    aircraft = response.get("ac") if isinstance(response, dict) else None
    if not isinstance(aircraft, list) or not aircraft or not isinstance(aircraft[0], dict):
        return {}
    record = aircraft[0]
    return {
        "registration": record.get("r"),
        "aircraft_type": record.get("t"),
        "model": record.get("desc"),
    }


def faa_aircraft_info(hex_code):
    hex_code = str(hex_code or "").lower()
    if not HEX.fullmatch(hex_code):
        return {}
    try:
        mtime_ns = FAA_INDEX.stat().st_mtime_ns
        with LOCK:
            if FAA_INDEX_CACHE["mtime_ns"] != mtime_ns:
                data = json.loads(FAA_INDEX.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("FAA aircraft index is not an object")
                FAA_INDEX_CACHE.update(mtime_ns=mtime_ns, data=data)
            record = FAA_INDEX_CACHE["data"].get(hex_code, {})
    except FileNotFoundError:
        return {}
    if not isinstance(record, dict):
        return {}
    return {
        field: record[field]
        for field in ("registration", "manufacturer", "model")
        if record.get(field)
    }


def adsbdb_aircraft_info(hex_code, callsign):
    aircraft_endpoint = f"https://api.adsbdb.com/v0/aircraft/{hex_code}"
    endpoint = aircraft_endpoint
    if callsign:
        endpoint += "?callsign=" + quote(callsign)
    try:
        response = fetch_json(endpoint)
        data = response.get("response", {})
    except HTTPError as exc:
        if exc.code != 404:
            raise
        # ADSBDB can reject a registration-style callsign (for example N32069)
        # even though its aircraft record is available. Retry without the
        # callsign so a missing route cannot discard valid aircraft identity.
        if callsign:
            try:
                response = fetch_json(aircraft_endpoint)
                data = response.get("response", {})
            except HTTPError as aircraft_exc:
                if aircraft_exc.code != 404:
                    raise
                data = {}
        else:
            data = {}
    if not isinstance(data, dict):
        data = {}
    if callsign and not isinstance(data.get("flightroute"), dict):
        try:
            route_response = fetch_json("https://api.adsbdb.com/v0/callsign/" + quote(callsign)).get("response", {})
            if isinstance(route_response, dict) and isinstance(route_response.get("flightroute"), dict):
                data["flightroute"] = route_response["flightroute"]
        except HTTPError as exc:
            if exc.code != 404:
                raise
    aircraft = data.get("aircraft") if isinstance(data.get("aircraft"), dict) else {}
    route = data.get("flightroute") if isinstance(data.get("flightroute"), dict) else {}
    airline = route.get("airline") if isinstance(route.get("airline"), dict) else {}

    def airport(which):
        obj = route.get(which)
        if not isinstance(obj, dict):
            return None
        return obj.get("iata_code") or obj.get("icao_code")

    def airport_coordinate(which, coordinate):
        obj = route.get(which)
        if not isinstance(obj, dict):
            return None
        value = obj.get(coordinate)
        return value if isinstance(value, (int, float)) else None

    return {
        "registration": aircraft.get("registration"),
        "aircraft_type": aircraft.get("icao_type"),
        "model": aircraft.get("type"),
        "manufacturer": aircraft.get("manufacturer"),
        "airline": airline.get("name"),
        "airline_icao": airline.get("icao"),
        "airline_iata": airline.get("iata"),
        "display_callsign": route.get("callsign_iata") or callsign,
        "origin": airport("origin"),
        "destination": airport("destination"),
        "_origin_lat": airport_coordinate("origin", "latitude"),
        "_origin_lon": airport_coordinate("origin", "longitude"),
        "_destination_lat": airport_coordinate("destination", "latitude"),
        "_destination_lon": airport_coordinate("destination", "longitude"),
    }, bool(route)


def adsb_im_route_info(callsign, latitude, longitude, track=None):
    if not callsign or not all(isinstance(value, (int, float)) for value in (latitude, longitude)):
        return {}
    routes = post_json(
        "https://adsb.im/api/0/routeset",
        {"planes": [{"callsign": callsign, "lat": latitude, "lng": longitude}]},
        timeout=5,
    )
    if not isinstance(routes, list):
        return {}
    route = next(
        (
            candidate for candidate in routes
            if isinstance(candidate, dict)
            and str(candidate.get("callsign", "")).strip().upper() == callsign.upper()
            and candidate.get("plausible") is not False
        ),
        None,
    )
    if not route:
        return {}
    airports = [
        airport for airport in route.get("_airports", [])
        if isinstance(airport, dict)
        and isinstance(airport.get("lat"), (int, float))
        and isinstance(airport.get("lon"), (int, float))
        and (airport.get("iata") or airport.get("icao"))
    ]
    if len(airports) < 2:
        return {}

    origin, destination = airports[0], airports[-1]
    first_code = origin.get("iata") or origin.get("icao")
    last_code = destination.get("iata") or destination.get("icao")
    if first_code == last_code and len(airports) > 2 and isinstance(track, (int, float)):
        segments = list(zip(airports, airports[1:]))

        def segment_score(segment):
            start, end = segment
            start_bearing = bearing_degrees(latitude, longitude, start["lat"], start["lon"])
            end_bearing = bearing_degrees(latitude, longitude, end["lat"], end["lon"])
            return heading_difference(track, end_bearing) - 0.25 * heading_difference(track, start_bearing)

        origin, destination = min(segments, key=segment_score)

    def code(airport):
        return airport.get("iata") or airport.get("icao")

    return {
        "origin": code(origin),
        "destination": code(destination),
        "_origin_lat": origin["lat"],
        "_origin_lon": origin["lon"],
        "_destination_lat": destination["lat"],
        "_destination_lon": destination["lon"],
    }


def lookup(hex_code, callsign, latitude=None, longitude=None, track=None):
    key = (hex_code, callsign)
    info = {}
    route_found = False
    lookup_failed = False
    try:
        if CONFIG.get("use_adsbdb", True):
            try:
                info, route_found = adsbdb_aircraft_info(hex_code, callsign)
            except (URLError, TimeoutError, ValueError, OSError) as exc:
                lookup_failed = True
                LOG.warning("ADSBDB lookup failed for %s: %s", key, exc)
        if CONFIG.get("use_adsb_im_routes", True) and callsign:
            try:
                route_info = adsb_im_route_info(callsign, latitude, longitude, track)
                if route_info:
                    info.update(route_info)
                    route_found = True
            except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
                lookup_failed = True
                LOG.warning("adsb.im route lookup failed for %s: %s", key, exc)
        try:
            local_info = skyaware_aircraft_info(hex_code)
            for field, value in local_info.items():
                if value and not info.get(field):
                    info[field] = value
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            lookup_failed = True
            LOG.warning("SkyAware database lookup failed for %s: %s", hex_code, exc)
        needs_aircraft_details = not info.get("registration") or not info.get("aircraft_type") or not info.get("model")
        if CONFIG.get("use_hexdb", True) and needs_aircraft_details:
            try:
                hexdb_info = hexdb_aircraft_info(hex_code)
                for field, value in hexdb_info.items():
                    if value and not info.get(field):
                        info[field] = value
            except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
                lookup_failed = True
                LOG.warning("HexDB lookup failed for %s: %s", hex_code, exc)
        needs_aircraft_details = not info.get("registration") or not info.get("aircraft_type") or not info.get("model")
        if CONFIG.get("use_adsb_fi", True) and needs_aircraft_details:
            try:
                adsb_fi_info = adsb_fi_aircraft_info(hex_code)
                for field, value in adsb_fi_info.items():
                    if value and not info.get(field):
                        info[field] = value
            except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
                lookup_failed = True
                LOG.warning("adsb.fi lookup failed for %s: %s", hex_code, exc)
        needs_aircraft_details = not info.get("registration") or not info.get("model")
        if CONFIG.get("use_faa_registry", True) and needs_aircraft_details:
            try:
                faa_info = faa_aircraft_info(hex_code)
                for field, value in faa_info.items():
                    if value and not info.get(field):
                        info[field] = value
            except (ValueError, OSError) as exc:
                lookup_failed = True
                LOG.warning("FAA registry lookup failed for %s: %s", hex_code, exc)
        ttl = 12 * 3600 if route_found else 3600 if any(info.values()) else 5 * 60 if lookup_failed else 6 * 3600
        cache_set(key, info, ttl)
        logo_code = info.get("airline_icao") or info.get("airline_iata")
        cache_airline_logo(logo_code, info.get("airline"))
    finally:
        with LOCK:
            PENDING.discard(key)


def distance_miles(lat1, lon1, lat2, lon2):
    a1, a2 = math.radians(lat1), math.radians(lat2)
    da, dl = a2 - a1, math.radians(lon2 - lon1)
    h = math.sin(da / 2) ** 2 + math.cos(a1) * math.cos(a2) * math.sin(dl / 2) ** 2
    return 3958.8 * 2 * math.asin(min(1, math.sqrt(h)))


def cardinal_direction(track):
    if isinstance(track, bool) or not isinstance(track, (int, float)) or not math.isfinite(track):
        return None
    points = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return points[int(((track % 360) + 22.5) // 45) % 8]


def vertical_direction(barometric_rate, geometric_rate=None):
    rate = barometric_rate if isinstance(barometric_rate, (int, float)) else geometric_rate
    if not isinstance(rate, (int, float)):
        return None
    if rate >= 128:
        return "climbing"
    if rate <= -128:
        return "descending"
    return None


def motion_direction(hex_code, distance, sample_time):
    if distance is None or not isinstance(sample_time, (int, float)):
        return None
    entry = MOTION_HISTORY.setdefault(hex_code, {"samples": [], "state": None, "last_seen": sample_time})
    entry["last_seen"] = sample_time
    samples = [(stamp, value) for stamp, value in entry["samples"] if sample_time - stamp <= 30]
    if samples and sample_time <= samples[-1][0]:
        samples[-1] = (sample_time, distance)
    else:
        samples.append((sample_time, distance))
    entry["samples"] = samples
    if len(samples) < 3 or samples[-1][0] - samples[0][0] < 6:
        return None

    origin = samples[0][0]
    times = [stamp - origin for stamp, _ in samples]
    distances = [value for _, value in samples]
    mean_time = sum(times) / len(times)
    mean_distance = sum(distances) / len(distances)
    denominator = sum((value - mean_time) ** 2 for value in times)
    if denominator == 0:
        return None
    slope = sum((stamp - mean_time) * (value - mean_distance) for stamp, value in zip(times, distances)) / denominator
    trend = slope * (times[-1] - times[0])
    state = entry["state"]
    threshold = 0.15 if state in ("approaching", "away") else 0.35
    if trend <= -threshold:
        state = "approaching"
    elif trend >= threshold:
        state = "away"
    else:
        state = "crossing"
    entry["state"] = state
    return state


def clean_motion_history(sample_time):
    expired = [code for code, entry in MOTION_HISTORY.items() if sample_time - entry["last_seen"] > 120]
    for code in expired:
        MOTION_HISTORY.pop(code, None)


def heading_difference(first, second):
    return abs((first - second + 180) % 360 - 180)


def bearing_degrees(lat1, lon1, lat2, lon2):
    lat1, lat2 = math.radians(lat1), math.radians(lat2)
    delta_lon = math.radians(lon2 - lon1)
    y = math.sin(delta_lon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def correct_reversed_route(row):
    private_fields = (
        "_lat", "_lon", "_track", "_baro_rate", "_origin_lat", "_origin_lon",
        "_destination_lat", "_destination_lon",
    )
    values = {field: row.get(field) for field in private_fields}
    try:
        if not row.get("origin") or not row.get("destination"):
            return
        required = ("_lat", "_lon", "_origin_lat", "_origin_lon", "_destination_lat", "_destination_lon")
        if not all(isinstance(values[field], (int, float)) for field in required):
            return
        lat, lon = values["_lat"], values["_lon"]
        origin_distance = distance_miles(lat, lon, values["_origin_lat"], values["_origin_lon"])
        destination_distance = distance_miles(lat, lon, values["_destination_lat"], values["_destination_lon"])
        rate = values["_baro_rate"]
        track = values["_track"]

        near_origin_arrival = (
            isinstance(rate, (int, float)) and rate <= -300 and origin_distance <= 75
            and destination_distance >= max(150, origin_distance * 3)
        )
        near_destination_departure = (
            isinstance(rate, (int, float)) and rate >= 300 and destination_distance <= 75
            and origin_distance >= max(150, destination_distance * 3)
        )

        heading_to_origin = False
        if isinstance(track, (int, float)):
            origin_bearing = bearing_degrees(lat, lon, values["_origin_lat"], values["_origin_lon"])
            destination_bearing = bearing_degrees(lat, lon, values["_destination_lat"], values["_destination_lon"])
            heading_to_origin = (
                heading_difference(track, origin_bearing) <= 35
                and heading_difference(track, destination_bearing) >= 120
            )

        if near_origin_arrival or near_destination_departure or heading_to_origin:
            row["origin"], row["destination"] = row["destination"], row["origin"]
    finally:
        for field in private_fields:
            row.pop(field, None)


def process(source):
    entries = source.get("aircraft", [])
    if not isinstance(entries, list):
        raise ValueError("Invalid aircraft array")
    lat0, lon0 = CONFIG.get("receiver_lat"), CONFIG.get("receiver_lon")
    rows = []
    sample_time = source.get("now")
    if not isinstance(sample_time, (int, float)):
        sample_time = time.time()
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        hex_code = str(raw.get("hex", "")).lower()
        if not HEX.fullmatch(hex_code):
            continue
        seen = raw.get("seen")
        if not isinstance(seen, (float, int)) or seen > CONFIG.get("max_seen_seconds", 45):
            continue
        flight = str(raw.get("flight") or "").strip().upper()
        callsign = flight if CALLSIGN.fullmatch(flight) else ""
        position_age = raw.get("seen_pos")
        lat, lon = raw.get("lat"), raw.get("lon")
        has_position = isinstance(lat, (float, int)) and isinstance(lon, (float, int)) and isinstance(position_age, (float, int)) and position_age <= CONFIG.get("max_position_age_seconds", 90)
        distance = distance_miles(lat0, lon0, lat, lon) if has_position and isinstance(lat0, (float, int)) and isinstance(lon0, (float, int)) else None
        radius = CONFIG.get("max_distance_miles")
        if radius is not None and (distance is None or distance > radius):
            continue
        row = {
            "hex": hex_code, "callsign": callsign or hex_code.upper(), "_lookup_callsign": callsign,
            "_distance_exact": distance,
            "_receiver_aircraft_type": raw.get("t"),
            "category": str(raw.get("category") or "").strip().upper(),
            "altitude": raw.get("alt_baro"), "speed": raw.get("gs"),
            "distance_miles": round(distance, 1) if distance is not None else None,
            "seen": seen,
            "_lat": lat, "_lon": lon, "_track": raw.get("track"),
            "_baro_rate": raw.get("baro_rate"),
        }
        direction = cardinal_direction(raw.get("track"))
        if direction:
            row["direction"] = direction
        altitude_direction = vertical_direction(raw.get("baro_rate"), raw.get("geom_rate"))
        if altitude_direction:
            row["vertical_direction"] = altitude_direction
        rows.append(row)
    rows.sort(key=lambda x: (x["distance_miles"] is None, x["distance_miles"] if x["distance_miles"] is not None else x["seen"]))
    selected = rows[:CONFIG.get("max_aircraft", 12)]
    for row in selected:
        key = (row["hex"], row.pop("_lookup_callsign"))
        enrichment = cache_get(key)
        enrichment_enabled = CONFIG.get("use_adsbdb", True) or skyaware_database_url(row["hex"])
        if enrichment_enabled and enrichment is None:
            with LOCK:
                if key not in PENDING:
                    PENDING.add(key)
                    POOL.submit(lookup, *key, row.get("_lat"), row.get("_lon"), row.get("_track"))
        row.update(enrichment or {})
        correct_reversed_route(row)
        receiver_aircraft_type = row.pop("_receiver_aircraft_type", None)
        if not row.get("aircraft_type") and receiver_aircraft_type:
            row["aircraft_type"] = receiver_aircraft_type
        row["aircraft_display"] = aircraft_display_name(
            row.get("manufacturer"), row.get("model"), row.get("aircraft_type")
        )
        row["aircraft_icon"] = aircraft_icon_kind(
            row.get("category"), row.get("aircraft_type"), row.get("manufacturer"), row.get("model")
        )
        logo_code = row.get("airline_icao") or row.get("airline_iata") or callsign_logo_code(row.get("callsign"))
        if logo_code:
            row["logo_code"] = logo_code
        extension = logo_extension(logo_code)
        if extension:
            row["logo_ext"] = extension
            asset_code = BUNDLED_LOGO_ALIASES.get(logo_code, logo_code)
            row["logo_kind"] = "symbol" if asset_code in {"AAL", "AAY", "DAL", "DLH", "RPA", "SWA", "UAL", "UPS"} else "wordmark"
            row["logo_asset_code"] = asset_code
        motion = motion_direction(row["hex"], row.pop("_distance_exact"), sample_time)
        if motion:
            row["motion"] = motion
    clean_motion_history(sample_time)
    return selected


def poll():
    while True:
        try:
            source = fetch_json(CONFIG["receiver_url"])
            generated = source.get("now")
            if not isinstance(generated, (float, int)) or abs(time.time() - generated) > 120:
                raise ValueError("Receiver data is stale or lacks a valid 'now' field")
            aircraft = process(source)
            with LOCK:
                STATE.update(aircraft=aircraft, receiver_ok=True, updated=generated, error=None)
        except (URLError, TimeoutError, ValueError, OSError, KeyError) as exc:
            LOG.warning("Receiver unavailable: %s", exc)
            with LOCK:
                STATE.update(aircraft=[], receiver_ok=False, error=str(exc))
        time.sleep(max(1, float(CONFIG.get("poll_seconds", 3))))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/aircraft":
            with LOCK:
                snapshot = dict(STATE)
            snapshot["max_distance_miles"] = CONFIG.get("max_distance_miles")
            payload = json.dumps(snapshot).encode()
            return self.send_bytes(payload, "application/json; charset=utf-8", "no-store")
        if path == "/":
            path = "/index.html"
        if path.startswith("/logos/"):
            name = path.removeprefix("/logos/")
            match = LOGO_FILE.fullmatch(name)
            if match:
                code, extension = match.groups()
                bundled_file = ROOT / "static" / "logos" / name
                file = bundled_file if bundled_file.is_file() else LOGO_CACHE / name
                if file.is_file():
                    kind = "image/svg+xml" if extension == "svg" else "image/png"
                    return self.send_bytes(file.read_bytes(), kind, "public, max-age=86400")
        if path in ("/index.html", "/style.css", "/motion.css", "/app.js"):
            file = ROOT / "static" / path.lstrip("/")
            kind = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}[file.suffix]
            cache = "no-store" if path == "/index.html" else "no-cache"
            return self.send_bytes(file.read_bytes(), kind + "; charset=utf-8", cache)
        self.send_error(404)

    def send_bytes(self, payload, kind, cache):
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not CONFIG["receiver_url"].startswith(("http://", "https://")):
        sys.exit("receiver_url must start with http:// or https://")
    try:
        validate_config(CONFIG)
    except ValueError as exc:
        sys.exit(f"Invalid configuration: {exc}")
    threading.Thread(target=poll, daemon=True).start()
    server = ThreadingHTTPServer((CONFIG.get("listen_host", "127.0.0.1"), int(CONFIG.get("listen_port", 8765))), Handler)
    LOG.info("FlightWall at http://%s:%s", *server.server_address)
    server.serve_forever()
