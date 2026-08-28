import csv
import hashlib
import json
import os
import re
import shutil
import threading
import unicodedata
from pathlib import Path


def normalize_station_name(name):
    text = unicodedata.normalize("NFKD", str(name).strip().casefold())
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", "", text)


def station_slug(name):
    text = unicodedata.normalize("NFKD", str(name).strip().casefold())
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "sender"


def stable_station_id(name, source="local"):
    identity = "{}:{}".format(source, normalize_station_name(name))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return "{}-{}".format("rb" if source == "radio-browser" else "local", digest)


class StationStore:
    def __init__(self, json_file, csv_file, logo_dir):
        self.json_file = Path(json_file)
        self.csv_file = Path(csv_file)
        self.logo_dir = Path(logo_dir)
        self.backup_file = self.json_file.with_suffix(self.json_file.suffix + ".bak")
        self.lock = threading.RLock()
        self._stations = []
        self._load_or_migrate()

    def _load_or_migrate(self):
        with self.lock:
            loaded = self._read_valid_json(self.json_file)
            if loaded is not None:
                self._stations = loaded
                return

            backup = self._read_valid_json(self.backup_file)
            if backup is not None:
                self._stations = backup
                self._write_locked()
                return

            self._stations = self._migrate_csv()
            if self._stations:
                self._write_locked()

    @staticmethod
    def _validate_station(station):
        required = ("id", "name", "audio_url", "metadata_url", "logo_file", "source")
        if not isinstance(station, dict) or not all(key in station for key in required):
            return None
        cleaned = {key: str(station.get(key, "")).strip() for key in required}
        if not cleaned["id"] or not cleaned["name"] or not cleaned["audio_url"]:
            return None
        if not cleaned["metadata_url"]:
            cleaned["metadata_url"] = cleaned["audio_url"]
        cleaned["startup"] = str(station.get("startup", "")).strip().casefold() in (
            "1", "true", "yes", "on"
        )
        return cleaned

    def _read_valid_json(self, filename):
        if not filename.is_file():
            return None
        try:
            with filename.open("r", encoding="utf-8") as input_file:
                document = json.load(input_file)
            raw_stations = document.get("stations") if isinstance(document, dict) else document
            if not isinstance(raw_stations, list) or not raw_stations:
                return None
            stations = []
            ids = set()
            names = set()
            for raw_station in raw_stations:
                station = self._validate_station(raw_station)
                if station is None:
                    return None
                normalized_name = normalize_station_name(station["name"])
                if station["id"] in ids or normalized_name in names:
                    return None
                ids.add(station["id"])
                names.add(normalized_name)
                stations.append(station)
            startup_indexes = [
                index for index, station in enumerate(stations) if station.get("startup")
            ]
            selected_startup = startup_indexes[0] if startup_indexes else 0
            for index, station in enumerate(stations):
                station["startup"] = index == selected_startup
            return stations
        except (OSError, ValueError, TypeError):
            return None

    def _migrate_csv(self):
        stations = []
        seen_names = set()
        if not self.csv_file.is_file():
            return stations
        try:
            with self.csv_file.open("r", encoding="utf-8-sig", newline="") as csv_file:
                for row in csv.reader(csv_file):
                    if len(row) < 2:
                        continue
                    name, url = row[0].strip(), row[1].strip()
                    normalized_name = normalize_station_name(name)
                    if not name or not url or not normalized_name or normalized_name in seen_names:
                        continue
                    seen_names.add(normalized_name)
                    slug = station_slug(name)
                    logo_file = ""
                    for extension in (".png", ".jpg", ".jpeg", ".gif"):
                        candidate = self.logo_dir / (slug + extension)
                        if candidate.is_file():
                            logo_file = candidate.relative_to(self.json_file.parent).as_posix()
                            break
                    stations.append({
                        "id": stable_station_id(name),
                        "name": name,
                        "audio_url": url,
                        "metadata_url": url,
                        "logo_file": logo_file,
                        "source": "csv-import",
                        "startup": not stations,
                    })
        except OSError:
            return []
        return stations

    def list_stations(self):
        with self.lock:
            return [dict(station) for station in self._stations]

    def add_station(self, station):
        cleaned = self._validate_station(station)
        if cleaned is None:
            raise ValueError("Der Senderdatensatz ist unvollständig.")
        normalized_name = normalize_station_name(cleaned["name"])
        with self.lock:
            if not self._stations:
                cleaned["startup"] = True
            if any(
                existing["id"] == cleaned["id"]
                or normalize_station_name(existing["name"]) == normalized_name
                for existing in self._stations
            ):
                raise ValueError("Dieser Sender ist bereits vorhanden.")
            previous = self._stations
            self._stations = previous + [cleaned]
            try:
                self._write_locked()
            except Exception:
                self._stations = previous
                raise
        return dict(cleaned)

    def set_startup_station(self, station_id):
        with self.lock:
            if not any(station["id"] == station_id for station in self._stations):
                raise ValueError("Der Sender wurde nicht gefunden.")
            previous = self._stations
            updated = [dict(station) for station in previous]
            for station in updated:
                station["startup"] = station["id"] == station_id
            self._stations = updated
            try:
                self._write_locked()
            except Exception:
                self._stations = previous
                raise
            return True

    def delete_station(self, station_id):
        with self.lock:
            if len(self._stations) <= 1:
                raise ValueError("Mindestens ein Sender muss erhalten bleiben.")
            index = next(
                (position for position, station in enumerate(self._stations)
                 if station["id"] == station_id),
                None,
            )
            if index is None:
                raise ValueError("Der Sender wurde nicht gefunden.")
            previous = self._stations
            updated = list(previous)
            removed = updated.pop(index)
            if removed.get("startup") and updated:
                updated[0]["startup"] = True
            self._stations = updated
            try:
                self._write_locked()
            except Exception:
                self._stations = previous
                raise
            return dict(removed)

    def move_station(self, station_id, direction):
        with self.lock:
            index = next(
                (position for position, station in enumerate(self._stations)
                 if station["id"] == station_id),
                None,
            )
            if index is None:
                raise ValueError("Der Sender wurde nicht gefunden.")
            target = index + (-1 if direction == "up" else 1)
            if target < 0 or target >= len(self._stations):
                return False
            previous = self._stations
            updated = list(previous)
            updated[index], updated[target] = updated[target], updated[index]
            self._stations = updated
            try:
                self._write_locked()
            except Exception:
                self._stations = previous
                raise
            return True

    def update_logo(self, station_id, logo_file):
        with self.lock:
            index = next(
                (position for position, station in enumerate(self._stations)
                 if station["id"] == station_id),
                None,
            )
            if index is None:
                return False
            logo_file = str(logo_file).strip()
            if self._stations[index].get("logo_file", "") == logo_file:
                return False
            previous = self._stations
            updated = [dict(station) for station in previous]
            updated[index]["logo_file"] = logo_file
            self._stations = updated
            try:
                self._write_locked()
            except Exception:
                self._stations = previous
                raise
            return True

    def _write_locked(self):
        self.json_file.parent.mkdir(parents=True, exist_ok=True)
        document = {"version": 1, "stations": self._stations}
        temporary = self.json_file.with_suffix(self.json_file.suffix + ".tmp")
        backup_temporary = self.backup_file.with_suffix(self.backup_file.suffix + ".tmp")

        if self._read_valid_json(self.json_file) is not None:
            shutil.copy2(str(self.json_file), str(backup_temporary))
            os.replace(str(backup_temporary), str(self.backup_file))

        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as output_file:
                json.dump(document, output_file, ensure_ascii=False, indent=2)
                output_file.write("\n")
                output_file.flush()
                os.fsync(output_file.fileno())
            os.replace(str(temporary), str(self.json_file))
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
