#!/usr/bin/env python3
import argparse
import csv
import datetime
import io
import json
import os
import re
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo


FAA_DATABASE_URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"
HEX = re.compile(r"^[0-9A-F]{6}$")
FAA_TIMEZONE = ZoneInfo("America/Chicago")
INDEX_SCHEMA_VERSION = 1


def normalized_row(row):
    return {str(key or "").strip().upper(): str(value or "").strip() for key, value in row.items()}


def zip_member(archive, basename):
    wanted = basename.casefold()
    for name in archive.namelist():
        if Path(name).name.casefold() == wanted:
            return name
    raise ValueError(f"FAA archive does not contain {basename}")


def csv_rows(archive, basename):
    member = zip_member(archive, basename)
    stream = io.TextIOWrapper(archive.open(member), encoding="utf-8-sig", errors="replace", newline="")
    yield from csv.DictReader(stream, skipinitialspace=True)


def build_index(archive_path, progress=False):
    with zipfile.ZipFile(archive_path) as archive:
        if progress:
            print("[FAA] Parsing aircraft make/model references...", flush=True)
        references = {}
        for raw in csv_rows(archive, "ACFTREF.txt"):
            row = normalized_row(raw)
            code = row.get("CODE")
            if code:
                references[code] = {
                    "manufacturer": row.get("MFR", ""),
                    "model": row.get("MODEL", ""),
                }

        if progress:
            print("[FAA] Parsing aircraft registrations...", flush=True)
        aircraft = {}
        for raw in csv_rows(archive, "MASTER.txt"):
            row = normalized_row(raw)
            raw_hex = row.get("MODE S CODE HEX", "").upper()
            if not raw_hex:
                continue
            hex_code = raw_hex.lstrip("0")
            hex_code = hex_code.zfill(6)
            if not HEX.fullmatch(hex_code):
                continue
            registration = row.get("N-NUMBER", "").upper()
            if not registration:
                continue
            reference = references.get(row.get("MFR MDL CODE"), {})
            record = {
                "registration": "N" + registration,
                "manufacturer": reference.get("manufacturer", ""),
                "model": reference.get("model", ""),
            }
            aircraft[hex_code.lower()] = {key: value for key, value in record.items() if value}
    if not aircraft:
        raise ValueError("FAA archive produced an empty aircraft index")
    return aircraft


def download(url, destination, attempts=15):
    base_headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) LocalAirTraffic/1.0",
        "Accept": "application/zip, application/x-zip-compressed, application/octet-stream, */*",
        "Referer": "https://www.faa.gov/licenses_certificates/aircraft_certification/aircraft_registry/releasable_aircraft_download",
    }
    destination.unlink(missing_ok=True)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            offset = destination.stat().st_size if destination.exists() else 0
            headers = dict(base_headers)
            if offset:
                headers["Range"] = f"bytes={offset}-"
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=120) as response:
                status = getattr(response, "status", 200)
                if offset and status != 206:
                    print("[FAA] Server did not honor resume request; restarting from byte zero.", flush=True)
                    offset = 0
                content_range = response.headers.get("Content-Range", "")
                range_match = re.search(r"/(\d+)$", content_range)
                response_length = int(response.headers.get("Content-Length", 0) or 0)
                total = int(range_match.group(1)) if range_match else response_length + offset
                content_type = response.headers.get("Content-Type", "unknown")
                downloaded = offset
                next_report = ((downloaded // (10 * 1024 * 1024)) + 1) * 10 * 1024 * 1024
                size = f" ({total / 1024 / 1024:.0f} MiB)" if total else ""
                action = "Resuming" if offset else "Downloading"
                print(f"[FAA] {action} registry archive{size} (attempt {attempt}/{attempts})...", flush=True)
                with destination.open("ab" if offset else "wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                        downloaded += len(chunk)
                        if downloaded > 200 * 1024 * 1024:
                            raise ValueError("FAA response exceeded the 200 MiB safety limit")
                        if downloaded >= next_report:
                            if total:
                                print(f"[FAA] Downloaded {downloaded / 1024 / 1024:.0f}/{total / 1024 / 1024:.0f} MiB...", flush=True)
                            else:
                                print(f"[FAA] Downloaded {downloaded / 1024 / 1024:.0f} MiB...", flush=True)
                            next_report += 10 * 1024 * 1024
            if total and downloaded != total:
                raise EOFError(f"connection ended at {downloaded:,} of {total:,} bytes")
            if not zipfile.is_zipfile(destination):
                prefix = destination.read_bytes()[:160]
                preview = "" if prefix.startswith(b"PK") else prefix.decode("utf-8", errors="replace").replace("\n", " ")
                raise ValueError(
                    f"FAA returned {downloaded:,} bytes of {content_type}, not a ZIP archive"
                    + ("; response appears to be a truncated or corrupt ZIP" if prefix.startswith(b"PK") else f": {preview}" if preview else "")
                )
            print(f"[FAA] Download complete: {downloaded / 1024 / 1024:.1f} MiB.", flush=True)
            return
        except (OSError, ValueError, EOFError) as exc:
            last_error = exc
            print(f"[FAA] Download attempt {attempt} failed: {exc}", flush=True)
            if attempt < attempts:
                resumable = isinstance(exc, EOFError) and destination.exists()
                delay = 2 if resumable else min(10 * attempt, 60)
                action = "Resuming" if resumable else "Retrying"
                if not resumable:
                    destination.unlink(missing_ok=True)
                print(f"[FAA] {action} in {delay} seconds...", flush=True)
                time.sleep(delay)
            else:
                destination.unlink(missing_ok=True)
    raise RuntimeError(f"FAA registry download failed after {attempts} attempts: {last_error}") from last_error


def write_index(index, output_path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as output:
        json.dump(index, output, separators=(",", ":"), sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(output_path)


def latest_faa_refresh(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    central = now.astimezone(FAA_TIMEZONE)
    refresh = central.replace(hour=23, minute=30, second=0, microsecond=0)
    if central < refresh:
        refresh -= datetime.timedelta(days=1)
    return refresh.astimezone(datetime.timezone.utc)


def index_is_current(output_path, now=None):
    try:
        metadata = json.loads(output_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        if metadata.get("schema_version") != INDEX_SCHEMA_VERSION:
            return False
        modified = datetime.datetime.fromtimestamp(output_path.stat().st_mtime, datetime.timezone.utc)
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return False
    return modified >= latest_faa_refresh(now)


def write_metadata(output_path, record_count, source_url):
    metadata_path = output_path.with_suffix(".meta.json")
    metadata = {
        "parsed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "record_count": record_count,
        "schema_version": INDEX_SCHEMA_VERSION,
        "source_url": source_url,
    }
    temporary = metadata_path.with_suffix(metadata_path.suffix + ".tmp")
    temporary.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(metadata_path)


def main():
    parser = argparse.ArgumentParser(description="Build a minimal local index from the FAA aircraft registry")
    parser.add_argument("--url", default=FAA_DATABASE_URL)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--force", action="store_true", help="download even when the local index is current")
    args = parser.parse_args()
    print("[FAA] Checking the local aircraft index...", flush=True)
    if not args.force and index_is_current(args.output):
        modified = datetime.datetime.fromtimestamp(args.output.stat().st_mtime, datetime.timezone.utc)
        print(f"[FAA] Index is current (parsed {modified.isoformat()}); skipping download.", flush=True)
        return
    print("[FAA] A current index is not available; an update is required.", flush=True)
    with tempfile.TemporaryDirectory(prefix="faa-registry-") as directory:
        archive_path = Path(directory) / "ReleasableAircraft.zip"
        download(args.url, archive_path)
        index = build_index(archive_path, progress=True)
    print("[FAA] Installing the new index atomically...", flush=True)
    write_index(index, args.output)
    write_metadata(args.output, len(index), args.url)
    print(f"[FAA] Installed {len(index):,} aircraft records at {args.output}.", flush=True)


if __name__ == "__main__":
    main()
