import base64
import copy
import datetime
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

import app
import update_faa_registry


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class Headers:
    def __init__(self, content_type):
        self.content_type = content_type

    def get_content_type(self):
        return self.content_type


class Response:
    def __init__(self, payload, content_type="image/png"):
        self.payload = payload
        self.headers = Headers(content_type)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit=-1):
        return self.payload if limit < 0 else self.payload[:limit]


class DownloadResponse:
    def __init__(self, payload, content_type="application/zip", content_length=None, content_range=None, status=200):
        self.payload = io.BytesIO(payload)
        self.headers = {"Content-Length": str(content_length if content_length is not None else len(payload)), "Content-Type": content_type}
        if content_range:
            self.headers["Content-Range"] = content_range
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit=-1):
        return self.payload.read(limit)


class ConfigTests(unittest.TestCase):
    def test_valid_coordinates(self):
        config = copy.deepcopy(app.CONFIG)
        config.update(receiver_lat=35.0, receiver_lon=-82.0)
        app.validate_config(config)

    def test_coordinates_must_be_supplied_together(self):
        config = copy.deepcopy(app.CONFIG)
        config.update(receiver_lat=None, receiver_lon=-82.0)
        with self.assertRaisesRegex(ValueError, "both be set"):
            app.validate_config(config)

    def test_coordinate_ranges_and_types(self):
        invalid = (
            {"receiver_lat": -91.0, "receiver_lon": -82.0},
            {"receiver_lat": 35.0, "receiver_lon": 181.0},
            {"receiver_lat": True, "receiver_lon": -82.0},
        )
        for values in invalid:
            with self.subTest(values=values):
                config = copy.deepcopy(app.CONFIG)
                config.update(values)
                with self.assertRaises(ValueError):
                    app.validate_config(config)


class AircraftTests(unittest.TestCase):
    def test_distance_miles(self):
        self.assertEqual(app.distance_miles(35.0, -82.0, 35.0, -82.0), 0)
        self.assertAlmostEqual(app.distance_miles(35.0, -82.0, 35.1, -82.0), 6.91, places=1)

    def test_airport_display_name_removes_only_trailing_words(self):
        self.assertEqual(app.airport_display_name("Seattle Tacoma International Airport"), "Seattle Tacoma International")
        self.assertEqual(app.airport_display_name("Love Field"), "Love")
        self.assertEqual(app.airport_display_name("Springfield Airfield"), "Springfield Airfield")
        self.assertEqual(app.airport_display_name("Savannah Hilton Head International Airport"), "Savannah Hilton Head International")
        long_name = app.airport_display_name("Baltimore Washington International Thurgood Marshall Airport")
        self.assertEqual(len(long_name), app.AIRPORT_NAME_MAX_LENGTH)
        self.assertTrue(long_name.endswith("…"))
        self.assertIsNone(app.airport_display_name(None))


    def test_airline_display_name_prefers_brand_before_legal_expansion(self):
        self.assertEqual(app.airline_display_name("Avianca - Aerovias Nacionales de Colombia, S.A."), "Avianca")
        self.assertEqual(app.airline_display_name("Delta Air Lines"), "Delta Air Lines")
        self.assertEqual(app.airline_display_name("TAP - Transportes Aereos Portugueses"), "TAP")
        self.assertIsNone(app.airline_display_name(None))
    def test_cardinal_direction(self):
        expected = {0: "N", 45: "NE", 90: "E", 135: "SE", 180: "S", 225: "SW", 270: "W", 315: "NW", 360: "N"}
        for track, direction in expected.items():
            with self.subTest(track=track):
                self.assertEqual(app.cardinal_direction(track), direction)
        self.assertIsNone(app.cardinal_direction(None))

    def test_vertical_direction_ignores_level_flight_noise(self):
        self.assertEqual(app.vertical_direction(640), "climbing")
        self.assertEqual(app.vertical_direction(-512), "descending")
        self.assertIsNone(app.vertical_direction(64))
        self.assertIsNone(app.vertical_direction(None))
        self.assertEqual(app.vertical_direction(None, -256), "descending")

    def test_reverses_stale_opposite_leg_when_arriving_at_listed_origin(self):
        row = {
            "origin": "GSP", "destination": "PHL",
            "origin_name": "Greenville Spartanburg International Airport",
            "destination_name": "Philadelphia International Airport",
            "_lat": 34.802, "_lon": -82.117, "_track": 216, "_baro_rate": -1024,
            "_origin_lat": 34.895699, "_origin_lon": -82.218903,
            "_destination_lat": 39.871899, "_destination_lon": -75.241097,
        }
        app.correct_reversed_route(row)
        self.assertEqual((row["origin"], row["destination"]), ("PHL", "GSP"))
        self.assertEqual(
            (row["origin_name"], row["destination_name"]),
            ("Philadelphia International Airport", "Greenville Spartanburg International Airport"),
        )
        self.assertFalse(any(key.startswith("_") for key in row))

    def test_keeps_route_when_departing_from_listed_origin(self):
        row = {
            "origin": "GSP", "destination": "PHL",
            "_lat": 34.9, "_lon": -82.2, "_track": 45, "_baro_rate": 1200,
            "_origin_lat": 34.895699, "_origin_lon": -82.218903,
            "_destination_lat": 39.871899, "_destination_lon": -75.241097,
        }
        app.correct_reversed_route(row)
        self.assertEqual((row["origin"], row["destination"]), ("GSP", "PHL"))
        self.assertFalse(any(key.startswith("_") for key in row))

    def test_process_filters_sorts_and_enriches(self):
        config = {
            **app.CONFIG,
            "receiver_lat": 35.0,
            "receiver_lon": -82.0,
            "max_seen_seconds": 45,
            "max_position_age_seconds": 90,
            "max_distance_miles": None,
            "max_aircraft": 12,
            "show_airport_names": True,
            "use_adsbdb": False,
            "receiver_url": "http://receiver.test/custom.json",
        }
        source = {"aircraft": [
            {"hex": "abcdef", "flight": "DAL123", "seen": 1, "seen_pos": 1, "lat": 35.01, "lon": -82.0, "alt_baro": 12000, "track": 45, "baro_rate": 640},
            {"hex": "123456", "flight": "N123AB", "seen": 2, "seen_pos": 1, "lat": 35.20, "lon": -82.0, "alt_baro": 5000, "t": "B738"},
            {"hex": "fedcba", "flight": "OLD123", "seen": 90, "seen_pos": 1, "lat": 35.0, "lon": -82.0},
            {"hex": "invalid", "flight": "BAD1", "seen": 1},
        ]}
        enrichment = {
            "airline": "Delta Air Lines - Delta Air Lines, Inc.", "airline_icao": "DAL", "display_callsign": "DL123",
            "origin": "ATL", "destination": "CLT", "aircraft_type": "A321",
            "origin_name": "Hartsfield Jackson Atlanta International",
            "destination_name": "Charlotte Douglas International",
        }
        cache = {("abcdef", "DAL123"): (enrichment, app.time.time() + 60)}
        with patch.object(app, "CONFIG", config), patch.object(app, "CACHE", cache), patch.object(app, "PENDING", set()), patch.object(app, "MOTION_HISTORY", {}):
            rows = app.process(source)
        self.assertEqual([row["hex"] for row in rows], ["abcdef", "123456"])
        self.assertEqual(rows[0]["display_callsign"], "DL123")
        self.assertEqual(rows[0]["airline"], "Delta Air Lines")
        self.assertEqual(rows[0]["logo_ext"], "svg")
        self.assertEqual(rows[0]["logo_kind"], "symbol")
        self.assertEqual(rows[0]["aircraft_display"], "A321")
        self.assertEqual(rows[0]["origin_name"], "Hartsfield Jackson Atlanta International")
        self.assertEqual(rows[0]["destination_name"], "Charlotte Douglas International")
        self.assertEqual(rows[0]["direction"], "NE")
        self.assertEqual(rows[0]["vertical_direction"], "climbing")
        self.assertLess(rows[0]["distance_miles"], rows[1]["distance_miles"])
        self.assertEqual(rows[1]["display_callsign"], "N123AB")
        self.assertEqual(rows[1]["display_secondary"], "123456")
        self.assertEqual(rows[1]["aircraft_type"], "B738")
        self.assertEqual(rows[1]["aircraft_display"], "B738")
        config["show_airport_names"] = False
        with patch.object(app, "CONFIG", config), patch.object(app, "CACHE", cache), patch.object(app, "PENDING", set()), patch.object(app, "MOTION_HISTORY", {}):
            rows_without_names = app.process(source)
        self.assertNotIn("origin_name", rows_without_names[0])
        self.assertNotIn("destination_name", rows_without_names[0])


    def test_private_aircraft_registration_replaces_hex_as_primary_identity(self):
        row = {"hex": "a1b2c3", "callsign": "A1B2C3", "registration": "N6112G"}
        self.assertEqual(app.display_identity(row), ("N6112G", "A1B2C3"))

    def test_hex_like_operator_code_does_not_override_private_registration(self):
        config = {
            **app.CONFIG,
            "receiver_lat": 35.0,
            "receiver_lon": -82.0,
            "max_distance_miles": None,
            "use_adsbdb": False,
        }
        source = {"aircraft": [{
            "hex": "abc123", "seen": 1, "seen_pos": 1,
            "lat": 35.01, "lon": -82.0,
        }]}
        cache = {
            ("abc123", ""): (
                {"registration": "N6112G", "aircraft_type": "B350"},
                app.time.time() + 60,
            )
        }
        with patch.object(app, "CONFIG", config), patch.object(app, "CACHE", cache), \
                patch.object(app, "PENDING", set()), patch.object(app, "MOTION_HISTORY", {}):
            row = app.process(source)[0]
        self.assertEqual(row["display_callsign"], "N6112G")
        self.assertEqual(row["display_secondary"], "ABC123")
        self.assertNotIn("logo_code", row)

    def test_operator_callsign_remains_primary_identity(self):
        row = {
            "hex": "a1b2c3", "callsign": "EJM285", "display_callsign": "EJM285",
            "registration": "N285FA", "airline": "Executive Jet Management",
        }
        self.assertEqual(
            app.display_identity(row, "EJM"),
            ("EJM285", "Executive Jet Management"),
        )

    def test_aircraft_display_name_uses_manufacturer_and_model(self):
        self.assertEqual(app.aircraft_display_name("BOEING", "737-8", "B38M"), "Boeing 737 MAX 8")
        self.assertEqual(app.aircraft_display_name("BOEING", "717 2BD", "B712"), "Boeing 717-200")
        self.assertEqual(app.aircraft_display_name("BOEING", "737NG 7H4/W", "B737"), "Boeing 737-700")
        self.assertEqual(app.aircraft_display_name("BOEING", "737NG 8AS/W", "B738"), "Boeing 737-800")
        self.assertEqual(app.aircraft_display_name("THE BOEING COMPANY", "747 4H6", "B744"), "Boeing 747-400")
        self.assertEqual(app.aircraft_display_name("BOEING", "767 332", "B763"), "Boeing 767-300")
        self.assertEqual(app.aircraft_display_name("BOEING", "777 3DZ(ER)", "B77W"), "Boeing 777-300ER")
        self.assertEqual(app.aircraft_display_name("BOEING", "717 2BD", None), "Boeing 717-200")
        self.assertEqual(app.aircraft_display_name("BOEING", "737-9", None), "Boeing 737 MAX 9")
        self.assertEqual(app.aircraft_display_name(None, "ERJ 170-200 LR", "E75L"), "Embraer E175")
        self.assertEqual(app.aircraft_display_name("AIRBUS", "A300 F4-622R", "A306"), "Airbus A300-600")
        self.assertEqual(app.aircraft_display_name("AIRBUS", "A321 211SL", "A321"), "Airbus A321")
        self.assertEqual(app.aircraft_display_name("AIRBUS SAS", "A321-253NY", None), "Airbus A321")
        self.assertEqual(app.aircraft_display_name("IAI LTD", "GULFSTREAM G280", None), "Gulfstream G280")
        self.assertEqual(app.aircraft_display_name("PILATUS AIRCRAFT LTD", "PC-12/47E", "PC12"), "Pilatus PC-12")
        self.assertEqual(app.aircraft_display_name("Avions de Transport Regional", "ATR 72 212F", "AT73"), "ATR 72")
        self.assertEqual(app.aircraft_display_name("Avions de Transport Regional", "ATR-42-600", "AT46"), "ATR 42")
        self.assertEqual(app.aircraft_display_name("BRM AERO S R O", "BRISTELL LSA", "NG5"), "Bristell LSA")
        self.assertEqual(app.aircraft_display_name("Raytheon Aircraft Company", "King Air B350", "B350"), "Beechcraft King Air 350")
        self.assertEqual(app.aircraft_display_name("CIRRUS DESIGN CORP", "SR22", None), "Cirrus SR22")
        self.assertEqual(app.aircraft_display_name("AIRBUS CANADA LP", "BD-500-1A11", "BCS3"), "Airbus A220-300")
        self.assertEqual(app.aircraft_display_name("BOMBARDIER", "BD-500-1A10", None), "Airbus A220-100")
        self.assertEqual(app.aircraft_display_name(None, None, "C172"), "C172")
        self.assertEqual(app.aircraft_display_name(None, None, "ZZZZ"), "ZZZZ")
        self.assertEqual(app.aircraft_display_name(None, None, None), "-")

    def test_piaware_categories_select_matching_fallback_icons(self):
        expected = {
            "A1": "light", "A2": "small-jet", "A3": "airliner", "A4": "heavy-twin",
            "A5": "heavy-four", "A6": "high-performance", "A7": "helicopter",
            "B2": "balloon", "C2": "ground",
        }
        for category, icon in expected.items():
            with self.subTest(category=category):
                self.assertEqual(app.aircraft_icon_kind(category), icon)
        self.assertEqual(app.aircraft_icon_kind(""), "unknown")
        self.assertEqual(app.aircraft_icon_kind("B6"), "unknown")

    def test_small_propeller_aircraft_use_propeller_silhouette(self):
        self.assertEqual(app.aircraft_icon_kind("A1", "C172", "Cessna", "172 Skyhawk"), "single-prop")
        self.assertEqual(app.aircraft_icon_kind("A1", "SR22", "Cirrus", "SR22"), "single-prop")
        self.assertEqual(app.aircraft_icon_kind("A1", "C25A", "Cessna", "Citation CJ2"), "light")
        self.assertEqual(app.aircraft_icon_kind("A1", "NG5", "BRM AERO S R O", "BRISTELL LSA"), "single-prop")

    def test_twin_turboprops_override_generic_piaware_category_icon(self):
        self.assertEqual(app.aircraft_icon_kind("A2", "AT73", "Avions de Transport Regional", "ATR 72 212F"), "turboprop")
        self.assertEqual(app.aircraft_icon_kind("A1", "B350", "Raytheon Aircraft Company", "King Air B350"), "turboprop")


    def test_radius_excludes_aircraft_without_fresh_position(self):
        config = {
            **app.CONFIG,
            "receiver_lat": 35.0,
            "receiver_lon": -82.0,
            "max_seen_seconds": 45,
            "max_position_age_seconds": 90,
            "max_distance_miles": 60,
            "max_aircraft": 12,
            "use_adsbdb": False,
            "receiver_url": "http://receiver.test/custom.json",
        }
        source = {"aircraft": [{"hex": "abcdef", "flight": "N123AB", "seen": 1}]}
        with patch.object(app, "CONFIG", config), patch.object(app, "MOTION_HISTORY", {}):
            self.assertEqual(app.process(source), [])


class SkyAwareDatabaseTests(unittest.TestCase):
    def test_faa_download_rejects_error_page_instead_of_parsing_it_as_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "faa.zip"
            response = DownloadResponse(b"<html>temporary FAA error</html>", "text/html")
            with patch.object(update_faa_registry.urllib.request, "urlopen", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "not a ZIP archive"):
                    update_faa_registry.download("https://registry.faa.gov/test.zip", destination, attempts=1)
            self.assertFalse(destination.exists())

    def test_faa_download_accepts_valid_zip(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("MASTER.txt", "N-NUMBER,MODE S CODE HEX\n")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "faa.zip"
            response = DownloadResponse(payload.getvalue())
            with patch.object(update_faa_registry.urllib.request, "urlopen", return_value=response):
                update_faa_registry.download("https://registry.faa.gov/test.zip", destination, attempts=1)
            self.assertTrue(zipfile.is_zipfile(destination))

    def test_faa_download_resumes_truncated_zip(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("MASTER.txt", "N-NUMBER,MODE S CODE HEX\n333QK,A3A25C\n")
        archive_bytes = payload.getvalue()
        split = len(archive_bytes) // 2
        responses = [
            DownloadResponse(archive_bytes[:split], content_length=len(archive_bytes)),
            DownloadResponse(
                archive_bytes[split:], content_length=len(archive_bytes) - split,
                content_range=f"bytes {split}-{len(archive_bytes) - 1}/{len(archive_bytes)}", status=206,
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "faa.zip"
            with patch.object(update_faa_registry.urllib.request, "urlopen", side_effect=responses) as fetch:
                with patch.object(update_faa_registry.time, "sleep"):
                    update_faa_registry.download("https://registry.faa.gov/test.zip", destination, attempts=2)
            self.assertEqual(destination.read_bytes(), archive_bytes)
            resumed_request = fetch.call_args_list[1].args[0]
            self.assertEqual(resumed_request.get_header("Range"), f"bytes={split}-")

    def test_faa_registry_parser_keeps_only_aircraft_identity_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "faa.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "ACFTREF.txt",
                    "CODE,MFR,MODEL,TYPE-ACFT\n1234567,CIRRUS DESIGN CORP,SR22,4\n",
                )
                archive.writestr(
                    "MASTER.txt",
                    "N-NUMBER,SERIAL NUMBER,MFR MDL CODE,YEAR MFR,NAME,STREET,MODE S CODE HEX\n"
                    "333QK,11014,1234567,2025,PRIVATE OWNER,PRIVATE ADDRESS,A3A25C\n",
                )
            index = update_faa_registry.build_index(archive_path)
        self.assertEqual(index, {
            "a3a25c": {
                "registration": "N333QK", "manufacturer": "CIRRUS DESIGN CORP",
                "model": "SR22",
            }
        })
        self.assertNotIn("name", index["a3a25c"])
        self.assertNotIn("street", index["a3a25c"])

    def test_faa_registry_index_reloads_after_atomic_update(self):
        with tempfile.TemporaryDirectory() as directory:
            index_path = Path(directory) / "faa-aircraft.json"
            index_path.write_text(json.dumps({"a3a25c": {
                "registration": "N333QK", "manufacturer": "CIRRUS DESIGN CORP",
                "model": "SR22",
            }}), encoding="utf-8")
            with (
                patch.object(app, "FAA_INDEX", index_path),
                patch.object(app, "FAA_INDEX_CACHE", {"mtime_ns": None, "data": {}}),
            ):
                info = app.faa_aircraft_info("A3A25C")
        self.assertEqual(info["registration"], "N333QK")
        self.assertEqual(info["model"], "SR22")
        self.assertNotIn("owner", info)

    def test_faa_registry_download_is_skipped_when_index_is_newer_than_latest_refresh(self):
        now = datetime.datetime(2026, 9, 24, 13, 0, tzinfo=datetime.timezone.utc)
        latest_refresh = update_faa_registry.latest_faa_refresh(now)
        self.assertEqual(
            latest_refresh,
            datetime.datetime(2026, 9, 24, 4, 30, tzinfo=datetime.timezone.utc),
        )
        with tempfile.TemporaryDirectory() as directory:
            index_path = Path(directory) / "faa-aircraft.json"
            index_path.write_text("{}", encoding="utf-8")
            current = latest_refresh + datetime.timedelta(minutes=5)
            os.utime(index_path, (current.timestamp(), current.timestamp()))
            self.assertFalse(update_faa_registry.index_is_current(index_path, now))
            index_path.with_suffix(".meta.json").write_text(
                json.dumps({"schema_version": update_faa_registry.INDEX_SCHEMA_VERSION}),
                encoding="utf-8",
            )
            self.assertTrue(update_faa_registry.index_is_current(index_path, now))
            stale = latest_refresh - datetime.timedelta(minutes=5)
            os.utime(index_path, (stale.timestamp(), stale.timestamp()))
            self.assertFalse(update_faa_registry.index_is_current(index_path, now))

    def test_database_url_is_derived_from_receiver_url(self):
        config = {**app.CONFIG, "receiver_url": "http://piaware.local/skyaware/data/aircraft.json"}
        with patch.object(app, "CONFIG", config):
            self.assertEqual(
                app.skyaware_database_url("a8de10"),
                "http://piaware.local/skyaware/db/A8.json",
            )
            self.assertIsNone(app.skyaware_database_url("invalid"))

    def test_local_database_returns_registration_and_type(self):
        config = {**app.CONFIG, "receiver_url": "http://piaware.local/skyaware/data/aircraft.json"}
        shard = {"DE10": {"r": "N12345", "t": "B738", "desc": "L2J"}}
        with (
            patch.object(app, "CONFIG", config),
            patch.object(app, "SKYAWARE_DB_CACHE", {}),
            patch.object(app, "fetch_json", return_value=shard) as fetch,
        ):
            self.assertEqual(
                app.skyaware_aircraft_info("a8de10"),
                {"registration": "N12345", "aircraft_type": "B738"},
            )
        fetch.assert_called_once_with("http://piaware.local/skyaware/db/A8.json", timeout=3)

    def test_missing_database_shard_is_an_empty_lookup(self):
        config = {**app.CONFIG, "receiver_url": "http://piaware.local/skyaware/data/aircraft.json"}
        missing = HTTPError("http://piaware.local/skyaware/db/FF.json", 404, "Not Found", {}, None)
        with (
            patch.object(app, "CONFIG", config),
            patch.object(app, "SKYAWARE_DB_CACHE", {}),
            patch.object(app, "fetch_json", side_effect=missing),
        ):
            self.assertEqual(app.skyaware_aircraft_info("ff0001"), {})

    def test_adsbdb_values_take_priority_over_local_database(self):
        remote = {
            "manufacturer": "Airbus",
            "model": "A321",
            "aircraft_type": "A321",
            "registration": "NREMOTE",
        }
        local = {"aircraft_type": "B738", "registration": "NLOCAL"}
        cache = {}
        with (
            patch.object(app, "CONFIG", {**app.CONFIG, "use_adsbdb": True}),
            patch.object(app, "CACHE", cache),
            patch.object(app, "PENDING", {("a8de10", "DAL123")}),
            patch.object(app, "adsbdb_aircraft_info", return_value=(remote, False)),
            patch.object(app, "adsb_im_route_info", return_value={}),
            patch.object(app, "skyaware_aircraft_info", return_value=local),
            patch.object(app, "cache_airline_logo"),
        ):
            app.lookup("a8de10", "DAL123")
        info = cache[("a8de10", "DAL123")][0]
        self.assertEqual(info["aircraft_type"], "A321")
        self.assertEqual(info["registration"], "NREMOTE")

    def test_adsbdb_keeps_aircraft_identity_when_callsign_route_is_unknown(self):
        combined_missing = HTTPError("combined", 404, "unknown callsign", {}, None)
        route_missing = HTTPError("route", 404, "unknown callsign", {}, None)
        aircraft_response = {"response": {"aircraft": {
            "registration": "N32069", "manufacturer": "Piper",
            "type": "PA-28-151", "icao_type": "P28A",
        }}}
        with patch.object(
            app, "fetch_json",
            side_effect=[combined_missing, aircraft_response, route_missing],
        ) as fetch:
            info, route_found = app.adsbdb_aircraft_info("a3719c", "N32069")
        self.assertFalse(route_found)
        self.assertEqual(info["registration"], "N32069")
        self.assertEqual(info["manufacturer"], "Piper")
        self.assertEqual(info["model"], "PA-28-151")
        self.assertEqual(info["aircraft_type"], "P28A")
        self.assertEqual(fetch.call_count, 3)


    def test_adsbdb_returns_full_airport_names(self):
        response = {"response": {"flightroute": {
            "callsign_iata": "DL869",
            "origin": {
                "iata_code": "SEA", "name": "Seattle Tacoma International Airport",
                "latitude": 47.449001, "longitude": -122.308998,
            },
            "destination": {
                "iata_code": "DTW", "name": "Detroit Metropolitan Wayne County Airport",
                "latitude": 42.212399, "longitude": -83.353401,
            },
        }}}
        with patch.object(app, "fetch_json", return_value=response):
            info, route_found = app.adsbdb_aircraft_info("a4463e", "DAL869")
        self.assertTrue(route_found)
        self.assertEqual(info["origin"], "SEA")
        self.assertEqual(info["origin_name"], "Seattle Tacoma International")
        self.assertEqual(info["destination"], "DTW")
        self.assertEqual(
            info["destination_name"], "Detroit Metropolitan Wayne County",
        )
    def test_position_aware_route_selects_active_leg_of_reused_flight_number(self):
        routes = [{
            "callsign": "AAL2683",
            "plausible": True,
            "_airports": [
                {"iata": "DFW", "icao": "KDFW", "name": "Dallas Fort Worth International Airport", "lat": 32.896801, "lon": -97.038002},
                {"iata": "RDU", "icao": "KRDU", "name": "Raleigh Durham International Airport", "lat": 35.877602, "lon": -78.787498},
                {"iata": "DFW", "icao": "KDFW", "name": "Dallas Fort Worth International Airport", "lat": 32.896801, "lon": -97.038002},
            ],
        }]
        with patch.object(app, "post_json", return_value=routes):
            route = app.adsb_im_route_info("AAL2683", 35.027481, -81.905542, 100.1)
        self.assertEqual(route["origin"], "DFW")
        self.assertEqual(route["destination"], "RDU")
        self.assertEqual(route["origin_name"], "Dallas Fort Worth International")
        self.assertEqual(route["destination_name"], "Raleigh Durham International")

    def test_position_aware_route_rejects_implausible_match(self):
        routes = [{
            "callsign": "AAL2683", "plausible": False,
            "_airports": [
                {"iata": "CLT", "lat": 35.214, "lon": -80.943},
                {"iata": "LGA", "lat": 40.777, "lon": -73.873},
            ],
        }]
        with patch.object(app, "post_json", return_value=routes):
            self.assertEqual(app.adsb_im_route_info("AAL2683", 35.027, -81.906, 100.1), {})

    def test_hexdb_fills_remaining_aircraft_details_and_is_cached(self):
        response = {
            "ModeS": "A2E10C",
            "Registration": "N285FA",
            "Manufacturer": "Gulfstream Aerospace",
            "ICAOTypeCode": "G280",
            "Type": "G280",
            "RegisteredOwners": "Executive Jet Management",
            "OperatorFlagCode": "EJM",
        }
        with (
            patch.object(app, "HEXDB_CACHE", {}),
            patch.object(app, "fetch_json", return_value=response) as fetch,
        ):
            first = app.hexdb_aircraft_info("a2e10c")
            second = app.hexdb_aircraft_info("a2e10c")
        self.assertEqual(first["registration"], "N285FA")
        self.assertEqual(first["aircraft_type"], "G280")
        self.assertEqual(first["manufacturer"], "Gulfstream Aerospace")
        self.assertEqual(first["airline_icao"], "EJM")
        self.assertEqual(second, first)
        fetch.assert_called_once_with("https://hexdb.io/api/v1/aircraft/A2E10C", timeout=5)

    def test_lookup_uses_hexdb_after_adsbdb_and_skyaware_miss(self):
        cache = {}
        hexdb = {
            "registration": "N285FA", "aircraft_type": "G280", "model": "G280",
            "manufacturer": "Gulfstream Aerospace", "airline": "Executive Jet Management",
            "airline_icao": "EJM",
        }
        with (
            patch.object(app, "CONFIG", {**app.CONFIG, "use_adsbdb": True, "use_hexdb": True}),
            patch.object(app, "CACHE", cache),
            patch.object(app, "PENDING", {("a2e10c", "EJM285")}),
            patch.object(app, "adsbdb_aircraft_info", return_value=({}, False)),
            patch.object(app, "skyaware_aircraft_info", return_value={}),
            patch.object(app, "hexdb_aircraft_info", return_value=hexdb),
            patch.object(app, "cache_airline_logo"),
        ):
            app.lookup("a2e10c", "EJM285")
        info = cache[("a2e10c", "EJM285")][0]
        self.assertEqual(info["registration"], "N285FA")
        self.assertEqual(info["aircraft_type"], "G280")
        self.assertEqual(info["airline"], "Executive Jet Management")

    def test_lookup_uses_faa_registry_after_other_aircraft_sources_miss(self):
        cache = {}
        faa = {
            "registration": "N333QK", "manufacturer": "CIRRUS DESIGN CORP",
            "model": "SR22",
        }
        with (
            patch.object(app, "CONFIG", {**app.CONFIG, "use_adsbdb": True, "use_hexdb": True, "use_faa_registry": True}),
            patch.object(app, "CACHE", cache),
            patch.object(app, "PENDING", {("a3a25c", "N333QK")}),
            patch.object(app, "adsbdb_aircraft_info", return_value=({}, False)),
            patch.object(app, "skyaware_aircraft_info", return_value={}),
            patch.object(app, "hexdb_aircraft_info", return_value={}),
            patch.object(app, "adsb_fi_aircraft_info", return_value={}),
            patch.object(app, "faa_aircraft_info", return_value=faa),
            patch.object(app, "cache_airline_logo"),
        ):
            app.lookup("a3a25c", "N333QK")
        info = cache[("a3a25c", "N333QK")][0]
        self.assertEqual(info["registration"], "N333QK")
        self.assertEqual(info["manufacturer"], "CIRRUS DESIGN CORP")
        self.assertEqual(info["model"], "SR22")

    def test_adsb_fi_fills_foreign_aircraft_identity(self):
        response = {"ac": [{
            "hex": "04022c", "r": "ET-BAC", "t": "B77L", "desc": "BOEING 777-200LR",
        }]}
        with (
            patch.object(app, "fetch_json", return_value=response) as fetch,
            patch.object(app, "ADSB_FI_LAST_REQUEST", 0.0),
        ):
            info = app.adsb_fi_aircraft_info("04022c")
        self.assertEqual(info, {
            "registration": "ET-BAC", "aircraft_type": "B77L", "model": "BOEING 777-200LR",
        })
        fetch.assert_called_once_with("https://opendata.adsb.fi/api/v2/hex/04022c", timeout=5)


class MotionTests(unittest.TestCase):
    def test_requires_three_samples_over_six_seconds(self):
        with patch.object(app, "MOTION_HISTORY", {}):
            self.assertIsNone(app.motion_direction("abc123", 10.0, 100))
            self.assertIsNone(app.motion_direction("abc123", 9.7, 103))
            self.assertEqual(app.motion_direction("abc123", 9.2, 106), "approaching")

    def test_classifies_approaching_moving_away_and_crossing(self):
        sequences = {
            "approaching": [10.0, 9.7, 9.2],
            "away": [10.0, 10.3, 10.8],
            "crossing": [10.0, 10.05, 9.98],
        }
        with patch.object(app, "MOTION_HISTORY", {}):
            for index, (expected, distances) in enumerate(sequences.items()):
                code = f"code{index}"
                result = None
                for offset, distance in enumerate(distances):
                    result = app.motion_direction(code, distance, 100 + offset * 3)
                self.assertEqual(result, expected)

    def test_history_cleanup_removes_stale_aircraft(self):
        history = {"abc123": {"samples": [(100, 10.0)], "state": None, "last_seen": 100}}
        with patch.object(app, "MOTION_HISTORY", history):
            app.clean_motion_history(221)
            self.assertNotIn("abc123", history)


class LogoSelectionTests(unittest.TestCase):
    def test_callsign_prefix_can_select_airline_logo(self):
        self.assertEqual(app.callsign_logo_code("AAL527"), "AAL")
        self.assertEqual(app.callsign_logo_code("IBE0312"), "IBE")
        self.assertIsNone(app.callsign_logo_code("N123AB"))
        self.assertIsNone(app.callsign_logo_code("12ABC"))

    def test_candidate_prefers_symbol_then_accepts_corporate_wordmark(self):
        pages = [
            {"title": "File:Unrelated airline logo.svg", "imageinfo": [{"thumbmime": "image/png", "thumburl": "https://upload.wikimedia.org/wrong.png"}]},
            {"title": "File:Lufthansa aircraft.jpg", "imageinfo": [{"thumbmime": "image/png", "thumburl": "https://upload.wikimedia.org/plane.png"}]},
            {"title": "File:Lufthansa wordmark.svg", "imageinfo": [{"thumbmime": "image/png", "thumburl": "https://upload.wikimedia.org/right.png"}]},
            {"title": "File:Lufthansa Logo Crane symbol.svg", "imageinfo": [{"thumbmime": "image/png", "thumburl": "https://upload.wikimedia.org/symbol.png"}]},
        ]
        title, info = app.choose_logo_candidate(pages, "Lufthansa German Airlines")
        self.assertEqual(title, "File:Lufthansa Logo Crane symbol.svg")
        self.assertTrue(info["thumburl"].endswith("symbol.png"))
        self.assertIsNone(app.choose_logo_candidate(pages, "Delta Air Lines"))
        wordmark_title, _ = app.choose_logo_candidate(pages[:3], "Lufthansa German Airlines")
        self.assertEqual(wordmark_title, "File:Lufthansa wordmark.svg")

    def test_fetch_png_accepts_wikimedia_png(self):
        for host in ("upload.wikimedia.org", "thumb.wikimedia.org"):
            with self.subTest(host=host), patch.object(app, "urlopen", return_value=Response(PNG_1X1)):
                self.assertEqual(app.fetch_png(f"https://{host}/logo.png"), PNG_1X1)

    def test_fetch_png_rejects_untrusted_or_invalid_content(self):
        with self.assertRaisesRegex(ValueError, "host"):
            app.fetch_png("https://example.com/logo.png")
        with patch.object(app, "urlopen", return_value=Response(PNG_1X1, "image/jpeg")):
            with self.assertRaisesRegex(ValueError, "not PNG"):
                app.fetch_png("https://upload.wikimedia.org/logo.png")
        with patch.object(app, "urlopen", return_value=Response(b"not a png")):
            with self.assertRaisesRegex(ValueError, "signature"):
                app.fetch_png("https://upload.wikimedia.org/logo.png")

    def test_cache_downloads_once_and_records_source(self):
        result = {"query": {"pages": [{
            "title": "File:Example Air symbol.svg",
            "imageinfo": [{
                "thumbmime": "image/png",
                "thumburl": "https://thumb.wikimedia.org/example.png",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:Example_Air_logo.svg",
            }],
        }]}}
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            with (
                patch.object(app, "LOGO_CACHE", cache_dir),
                patch.object(app, "fetch_json", return_value=result) as fetch_json,
                patch.object(app, "fetch_png", return_value=PNG_1X1) as fetch_png,
                patch.dict(app.CONFIG, {"auto_download_logos": True}),
            ):
                app.LOGO_ATTEMPTS.clear()
                self.assertTrue(app.cache_airline_logo("ZZZ", "Example Air"))
                self.assertEqual(app.logo_extension("ZZZ"), "png")
                self.assertEqual((cache_dir / "ZZZ.png").read_bytes(), PNG_1X1)
                source = json.loads((cache_dir / "sources.json").read_text(encoding="utf-8"))["ZZZ"]
                self.assertEqual(source["source_title"], "File:Example Air symbol.svg")
                self.assertEqual(source["asset_kind"], "symbol")
                self.assertTrue(app.cache_airline_logo("ZZZ", "Example Air"))
                fetch_json.assert_called_once()
                fetch_png.assert_called_once()

    def test_auto_download_can_be_disabled(self):
        with patch.dict(app.CONFIG, {"auto_download_logos": False}), patch.object(app, "fetch_json") as fetch_json:
            self.assertFalse(app.cache_airline_logo("ZZZ", "Example Air"))
            fetch_json.assert_not_called()

    def test_vetted_symbols_and_corporate_wordmarks_are_selected(self):
        self.assertEqual(app.logo_extension("DAL"), "svg")
        self.assertEqual(app.logo_extension("UPS"), "svg")
        self.assertEqual(app.logo_extension("FDX"), "svg")
        self.assertEqual(app.logo_extension("RPA"), "svg")
        self.assertEqual(app.logo_extension("ENY"), "svg")
        self.assertEqual(app.logo_extension("UCA"), "svg")
        self.assertEqual(app.logo_extension("EDV"), "svg")
        self.assertEqual(app.logo_extension("FFT"), "svg")
        self.assertEqual(app.logo_extension("JBU"), "svg")
        self.assertEqual(app.logo_extension("JIA"), "svg")
        self.assertEqual(app.logo_extension("GJS"), "svg")
        self.assertEqual(app.logo_extension("ASH"), "svg")
        self.assertEqual(app.logo_extension("PDT"), "png")


class LogoServingTests(unittest.TestCase):
    def test_bundled_piedmont_png_is_served(self):
        class QuietHandler(app.Handler):
            def log_message(self, *_):
                pass

        server = app.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/logos/PDT.png", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "image/png")
                self.assertEqual(response.read(), (app.ROOT / "static" / "logos" / "PDT.png").read_bytes())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_cached_png_is_served_with_image_content_type(self):
        class QuietHandler(app.Handler):
            def log_message(self, *_):
                pass

        with tempfile.TemporaryDirectory() as directory, patch.object(app, "LOGO_CACHE", Path(directory)):
            (Path(directory) / "ZZZ.png").write_bytes(PNG_1X1)
            server = app.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=5) as response:
                    self.assertEqual(response.headers.get("Cache-Control"), "no-store")
                    self.assertIn(b"LOCAL AIR TRAFFIC", response.read())
                with patch.dict(app.CONFIG, {"max_distance_miles": 50}):
                    with urlopen(f"http://127.0.0.1:{server.server_port}/api/aircraft", timeout=5) as response:
                        self.assertEqual(json.load(response)["max_distance_miles"], 50)
                with urlopen(f"http://127.0.0.1:{server.server_port}/logos/ZZZ.png", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get_content_type(), "image/png")
                    self.assertEqual(response.read(), PNG_1X1)
                with self.assertRaises(HTTPError) as missing:
                    urlopen(f"http://127.0.0.1:{server.server_port}/logos/NOPE.png", timeout=5)
                self.assertEqual(missing.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class StaticDisplayTests(unittest.TestCase):
    def test_client_uses_backend_selected_identity_order(self):
        content = (app.ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("item.display_callsign || item.callsign || item.registration", content)
        self.assertIn("item.display_secondary || item.airline || item.registration", content)

    def test_client_contains_no_flightwall_branding(self):
        for name in ("index.html", "app.js", "style.css"):
            with self.subTest(file=name):
                content = (app.ROOT / "static" / name).read_text(encoding="utf-8")
                self.assertNotIn("flightwall", content.lower())

    def test_motion_wording_avoids_departure_implication(self):
        content = (app.ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("MOVING AWAY", content)
        self.assertNotIn("DEPARTING", content)

    def test_route_arrow_is_vertically_centered(self):
        base_style = (app.ROOT / "static" / "style.css").read_text(encoding="utf-8")
        arrow_style = (app.ROOT / "static" / "motion.css").read_text(encoding="utf-8")
        script = (app.ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn(".route{display:flex;align-items:center", base_style)
        self.assertIn("function routeArrow()", script)
        self.assertIn("viewBox', '0 0 36 24", script)
        self.assertIn("M2 12h30M24 4l8 8-8 8", script)
        self.assertIn(".route-arrow-icon", arrow_style)
        self.assertIn("transform: none", arrow_style)

    def test_unknown_route_uses_dash(self):
        content = (app.ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("el('span', 'unknown', '-')", content)
        self.assertNotIn("ROUTE UNKNOWN", content)

    def test_units_use_title_case_abbreviations(self):
        content = (app.ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("+ ' Ft'", content)
        self.assertIn("'Mi'", content)
        self.assertIn("+ ' Kt'", content)
        self.assertNotIn("+ ' FT'", content)
        self.assertNotIn("'MI'", content)

    def test_aircraft_details_include_cardinal_direction(self):
        content = (app.ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function cardinalDirection(track)", content)
        self.assertIn("['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']", content)
        self.assertIn("if (direction) details.push", content)
        self.assertIn("document.createTextNode(' · ')", content)

    def test_altitude_includes_climb_or_descent_arrow(self):
        content = (app.ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("{climbing: '↑', descending: '↓'}", content)
        self.assertIn("el('span', 'altitude-arrow', altitudeArrow)", content)

    def test_all_display_arrows_are_enlarged(self):
        script = (app.ROOT / "static" / "app.js").read_text(encoding="utf-8")
        style = (app.ROOT / "static" / "motion.css").read_text(encoding="utf-8")
        self.assertIn("font-size: 1.65em", style)
        self.assertIn(".route .arrow", style)
        self.assertIn("font-size: 1.4em", style)
        self.assertIn("rotate(${rotation}deg) scaleX(1.75)", script)
        self.assertIn("transform: scaleX(1.75)", style)

    def test_motion_arrow_follows_aircraft_heading(self):
        content = (app.ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function directionArrow(direction)", content)
        self.assertIn("N: 0, NE: 45, E: 90, SE: 135", content)
        self.assertIn("S: 180, SW: 225, W: 270, NW: 315", content)
        self.assertIn("motion.append(directionArrow(direction)", content)

    def test_motion_arrows_use_one_consistently_sized_rotated_glyph(self):
        script = (app.ROOT / "static" / "app.js").read_text(encoding="utf-8")
        style = (app.ROOT / "static" / "motion.css").read_text(encoding="utf-8")
        page = (app.ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("el('span', 'motion-arrow', '↑')", script)
        self.assertIn(".motion-arrow {", style)
        self.assertIn("width: 1.15em", style)
        self.assertIn('/motion.css?v=', page)

    def test_delta_logo_has_navy_background(self):
        content = (app.ROOT / "static" / "logos" / "DAL.svg").read_text(encoding="utf-8")
        self.assertIn('fill="#071D49"', content)

    def test_loaded_logo_nodes_are_reused_between_refreshes(self):
        content = (app.ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("const logoNodes = new Map()", content)
        self.assertIn("cached && cached.url === url", content)
        self.assertIn("flight.append(logoFor(item, code))", content)
        self.assertIn("!extension", content)
        self.assertNotIn("item.logo_ext || 'svg'", content)
        self.assertIn("const bundledLogoCodes", content)
        self.assertIn("const bundledLogoAliases", content)
        self.assertIn("['symbol', 'wordmark'].includes(item.logo_kind)", content)
        self.assertIn("item.logo_ext === 'png'", content)

    def test_missing_airline_logo_uses_piaware_category_silhouette(self):
        script = (app.ROOT / "static" / "app.js").read_text(encoding="utf-8")
        style = (app.ROOT / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn("const fallback = aircraftIconFor(item)", script)
        self.assertIn("'single-prop': 'Single-engine propeller aircraft'", script)
        self.assertIn("Adapted from PiAware SkyAware's straight-wing Cessna map marker", script)
        self.assertIn("'single-prop': '0 0 17 13'", script)
        self.assertIn("light: 'Light aircraft'", script)
        self.assertIn("helicopter: 'Helicopter'", script)
        self.assertIn("balloon: 'Balloon'", script)
        self.assertIn("ground: 'Ground vehicle'", script)
        self.assertIn(".aircraft-icon svg", style)

    def test_logo_urls_are_versioned_to_refresh_replaced_assets(self):
        content = (app.ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("const logoAssetVersion", content)
        self.assertIn("'?v=' + logoAssetVersion", content)

    def test_header_shows_configured_distance_scope_in_all_caps(self):
        html = (app.ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (app.ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("LOCAL AIR TRAFFIC", html)
        self.assertIn('id="distance-scope"', html)
        self.assertIn("'WITHIN ' + radius.toLocaleString", script)
        self.assertIn("+ ' MILES'", script)

    def test_updated_timestamp_marks_only_successful_data_refreshes(self):
        html = (app.ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (app.ROOT / "static" / "app.js").read_text(encoding="utf-8")
        style = (app.ROOT / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn('<time id="updated-at">UPDATED —</time>', html)
        self.assertIn("updatedAt.dateTime = now.toISOString()", script)
        self.assertIn("'UPDATED ' + now.toLocaleString", script)
        successful_update = script.index("    markUpdated();")
        self.assertGreater(successful_update, script.index("const data = await response.json();"))
        self.assertLess(successful_update, script.index("  } catch (_)"))
        self.assertIn("#updated-at{position:fixed;right:1vw;bottom:.65vh", style)

    def test_unattended_kiosk_restarts_backend_and_browser(self):
        service = (app.ROOT / "systemd" / "flightwall.service").read_text(encoding="utf-8")
        autostart = (app.ROOT / "systemd" / "openbox-autostart").read_text(encoding="utf-8")
        deploy = (app.ROOT / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("Restart=always", service)
        self.assertIn("RestartSec=5", service)
        self.assertIn("while true; do", autostart)
        self.assertIn("chromium --kiosk", autostart)
        self.assertIn("--password-store=basic", autostart)
        self.assertIn("autologin-user=$KIOSK_USER", deploy)
        self.assertIn("autologin-session=openbox", deploy)
        self.assertIn("user-session=openbox", deploy)
        self.assertIn('KIOSK_USER="kiosk"', deploy)
        self.assertIn("useradd --create-home --shell /bin/bash", deploy)
        self.assertIn("/etc/X11/default-display-manager", deploy)
        self.assertIn("systemctl enable --force lightdm.service", deploy)
        self.assertIn("systemctl set-default graphical.target", deploy)
        self.assertIn('find "$INSTALL_DIR/static" -type f -exec chmod 0644', deploy)
        self.assertIn('if payload.get("error"):', deploy)
        timer = (app.ROOT / "systemd" / "faa-registry-update.timer").read_text(encoding="utf-8")
        updater = (app.ROOT / "systemd" / "faa-registry-update.service").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 01:00:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("update_faa_registry.py", updater)
        self.assertIn("systemctl enable --now faa-registry-update.timer", deploy)
        self.assertIn('status "Checking and, when needed, updating the FAA aircraft index"', deploy)
        self.assertIn('runuser -u "$APP_USER" -- /usr/bin/python3', deploy)
        self.assertIn("deployment will continue using the other enrichment sources", deploy)
        self.assertIn('status "Verifying the display page and aircraft API"', deploy)



    def test_deploy_configures_kiosk_only_ssh_and_maintenance_commands(self):
        deploy = (app.ROOT / "deploy.sh").read_text(encoding="utf-8")
        self.assertNotIn(b"\r\n", (app.ROOT / "deploy.sh").read_bytes())
        attributes = (app.ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.sh text eol=lf", attributes)
        self.assertIn("systemd/openbox-autostart text eol=lf", attributes)
        self.assertIn("openssh-server git sudo", deploy)
        self.assertIn("MISSING_DEPENDENCIES", deploy)
        self.assertIn("rerun deploy.sh without --skip-packages", deploy)
        self.assertIn('REPO_DIR="$KIOSK_HOME/flight-wall-clone"', deploy)
        self.assertIn('runuser -u "$KIOSK_USER" -- git clone', deploy)
        self.assertIn("PermitRootLogin no", deploy)
        self.assertIn("PubkeyAuthentication yes", deploy)
        self.assertIn("PasswordAuthentication yes", deploy)
        self.assertIn("INITIAL_KIOSK_PASSWORD_SET=false", deploy)
        self.assertIn("printf '%s:%s\\n' \"$KIOSK_USER\" 'kiosk' | chpasswd", deploy)
        self.assertIn("INITIAL_KIOSK_PASSWORD_SET=true", deploy)
        self.assertNotIn('\n    passwd "$KIOSK_USER"\n', deploy)
        self.assertIn('AllowUsers $KIOSK_USER', deploy)
        self.assertIn("systemctl enable ssh.service", deploy)
        self.assertIn("systemctl restart ssh.service", deploy)
        self.assertIn("/usr/local/sbin/flightwall-deploy", deploy)
        self.assertIn("/usr/local/sbin/flightwall-reboot", deploy)
        self.assertIn("/etc/sudoers.d/flightwall-kiosk", deploy)
        self.assertIn("visudo -cf", deploy)
        self.assertIn("# BEGIN LOCAL AIR TRAFFIC ALIASES", deploy)
        self.assertIn("alias deploy='sudo /usr/local/sbin/flightwall-deploy --skip-packages'", deploy)
        self.assertIn("alias deploy-initial='sudo /usr/local/sbin/flightwall-deploy'", deploy)
        self.assertIn("sed -i '/^# BEGIN LOCAL AIR TRAFFIC ALIASES$/", deploy)
        self.assertNotIn("NOPASSWD: ALL", deploy)

if __name__ == "__main__":
    unittest.main()
