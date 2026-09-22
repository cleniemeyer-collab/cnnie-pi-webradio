#!/usr/bin/python3

import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import gi
import musicpd

gi.require_version("Gst", "1.0")
from gi.repository import Gst
from PyQt5.QtCore import QEvent, QRect, QSize, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QAbstractButton,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionToolButton,
    QStylePainter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from station_store import StationStore, station_slug
from web_admin import RadioAdminServer
from web_player_server import WebPlayerServer


BASE_DIR = Path(__file__).resolve().parent
STATION_FILE = BASE_DIR / "radio_station_list.csv"
STATION_JSON_FILE = BASE_DIR / "radio_station_list.json"
LOGO_DIR = BASE_DIR / "logos"
IMMICH_CONFIG_FILE = BASE_DIR / "immich_config.json"
SLIDESHOW_IDLE_MS = 2 * 60 * 1000
SLIDE_DURATION_SECONDS = 90
STREAM_WATCHDOG_INTERVAL_MS = 5000
STREAM_STALL_TIMEOUT_SECONDS = 25
LMS_SERVER_URL = "http://192.168.42.185:9000"
LMS_PLAYER_ID = "b8:27:eb:eb:19:b4"
LMS_STATUS_INTERVAL_MS = 2000


class ImmichApiError(Exception):
    def __init__(self, status_code, endpoint, response_text):
        super().__init__(response_text)
        self.status_code = status_code
        self.endpoint = endpoint
        self.response_text = response_text

    def __str__(self):
        return f"HTTP {self.status_code} bei {self.endpoint}: {self.response_text}"


class ImmichSlideshowLoader(QThread):
    image_received = pyqtSignal(bytes, object)
    status_received = pyqtSignal(str)

    def __init__(self, config_file, parent=None):
        super().__init__(parent)
        self.config_file = config_file
        self.stop_event = threading.Event()
        self.control_event = threading.Event()
        self.control_lock = threading.Lock()
        self.navigation_step = 0
        self.paused = False

    def stop(self):
        self.stop_event.set()
        self.control_event.set()

    def navigate(self, direction):
        with self.control_lock:
            self.navigation_step += direction
        self.control_event.set()

    def set_paused(self, paused):
        with self.control_lock:
            self.paused = paused
        self.control_event.set()

    @staticmethod
    def request(url, api_key, data=None):
        headers = {"x-api-key": api_key, "Accept": "application/json"}
        method = "GET"
        if data is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(data).encode("utf-8")
            method = "POST"
        request = urllib.request.Request(url, headers=headers, data=data)
        try:
            return urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as error:
            response_text = error.read().decode("utf-8", errors="replace")
            response_text = response_text.replace(api_key, "[API-SCHLUESSEL ENTFERNT]")
            print(
                f"Immich API: {method} {url} -> HTTP {error.code}: {response_text}",
                file=sys.stderr,
            )
            raise ImmichApiError(error.code, url, response_text) from error
        except urllib.error.URLError as error:
            print(f"Immich API: {method} {url} -> Netzwerkfehler: {error.reason}", file=sys.stderr)
            raise

    def request_json(self, url, api_key, data=None):
        with self.request(url, api_key, data) as response:
            return json.load(response)

    def load_assets(self, base_url, api_key):
        albums = self.request_json(base_url + "/api/albums", api_key)
        album = next(
            (item for item in albums if item.get("albumName") == "WEB Radio"),
            None,
        )
        if album is None:
            raise ValueError("Immich-Album WEB Radio nicht gefunden")

        album_id = album["id"]
        details_url = base_url + "/api/albums/" + urllib.parse.quote(album_id)
        details = self.request_json(details_url, api_key)
        print(
            f"Immich API: GET {details_url} -> HTTP 200, "
            f"Album {details.get('albumName', '')}, {details.get('assetCount', 0)} Einträge",
            file=sys.stderr,
        )

        asset_records = []
        page = 1
        search_url = base_url + "/api/search/metadata"
        while not self.stop_event.is_set():
            payload = {
                "albumIds": [album_id],
                "page": page,
                "size": 1000,
                "type": "IMAGE",
            }
            result = self.request_json(search_url, api_key, payload)
            assets = result.get("assets", {})
            items = assets.get("items", [])
            for item in items:
                asset_id = item.get("id")
                if not asset_id:
                    continue
                exif = item.get("exifInfo") or {}
                asset_records.append({
                    "id": asset_id,
                    "_metadataComplete": isinstance(item.get("exifInfo"), dict) and all(
                        key in exif
                        for key in (
                            "dateTimeOriginal", "city", "state", "country",
                            "latitude", "longitude",
                        )
                    ),
                    "localDateTime": item.get("localDateTime"),
                    "fileCreatedAt": item.get("fileCreatedAt"),
                    "exifInfo": {
                        key: exif.get(key)
                        for key in (
                            "dateTimeOriginal", "city", "state", "country",
                            "latitude", "longitude",
                        )
                    },
                })
            next_page = assets.get("nextPage")
            if not next_page or not items:
                break
            page = int(next_page)
        if not asset_records:
            raise ValueError("Immich-Album WEB Radio enthält keine Bilder")
        print(
            f"Immich API: POST {search_url} -> {len(asset_records)} Album-Bilder geladen",
            file=sys.stderr,
        )
        return asset_records

    def load_thumbnail(self, base_url, api_key, asset_id):
        url = base_url + "/api/assets/" + urllib.parse.quote(asset_id) + "/thumbnail?size=preview"
        with self.request(url, api_key) as response:
            return response.read()

    def safe_load_thumbnail(self, base_url, api_key, asset_id):
        try:
            return self.load_thumbnail(base_url, api_key, asset_id)
        except (ImmichApiError, OSError, urllib.error.URLError) as error:
            message = "Vorschaubild wird übersprungen: " + str(error)
            print(message, file=sys.stderr)
            self.status_received.emit(message)
            return None

    @staticmethod
    def metadata_is_complete(asset):
        return bool(asset.get("_metadataComplete"))

    def load_asset_details(self, base_url, api_key, asset):
        if self.metadata_is_complete(asset):
            return asset
        url = base_url + "/api/assets/" + urllib.parse.quote(asset["id"])
        details = self.request_json(url, api_key)
        detail_exif = details.get("exifInfo") or {}
        merged = dict(asset)
        merged["_metadataComplete"] = True
        merged["localDateTime"] = details.get("localDateTime") or asset.get("localDateTime")
        merged["fileCreatedAt"] = details.get("fileCreatedAt") or asset.get("fileCreatedAt")
        merged["exifInfo"] = {
            key: detail_exif.get(key)
            for key in (
                "dateTimeOriginal", "city", "state", "country", "latitude", "longitude"
            )
        }
        return merged

    def safe_load_asset_details(self, base_url, api_key, asset):
        try:
            return self.load_asset_details(base_url, api_key, asset)
        except (ImmichApiError, OSError, TypeError, ValueError, urllib.error.URLError) as error:
            print("Bildinformationen konnten nicht geladen werden: " + str(error), file=sys.stderr)
            return asset

    @staticmethod
    def format_date(value):
        text = str(value or "").strip()
        year_match = re.match(r"^(\d{4})", text)
        if not year_match:
            return ""
        year = int(year_match.group(1))
        full_date = re.match(r"^\d{4}-(\d{1,2})-(\d{1,2})(?:T|\s|$)", text)
        if not full_date:
            return str(year)
        month = int(full_date.group(1))
        day = int(full_date.group(2))
        if not 1 <= month <= 12 or not 1 <= day <= 31:
            return str(year)
        return f"{day:02d}.{month:02d}.{year:04d}"

    @staticmethod
    def format_location(exif):
        parts = []
        seen_parts = set()
        for key in ("city", "state", "country"):
            value = str(exif.get(key) or "").strip()
            for part in (item.strip() for item in value.split(",")):
                normalized = part.casefold()
                if part and normalized not in seen_parts:
                    parts.append(part)
                    seen_parts.add(normalized)
        if parts:
            return ", ".join(parts)
        try:
            latitude = float(exif.get("latitude"))
            longitude = float(exif.get("longitude"))
        except (TypeError, ValueError):
            return ""
        latitude_direction = "N" if latitude >= 0 else "S"
        longitude_direction = "E" if longitude >= 0 else "W"
        return (
            f"{abs(latitude):.5f}° {latitude_direction}, "
            f"{abs(longitude):.5f}° {longitude_direction}"
        )

    def display_metadata(self, asset):
        exif = asset.get("exifInfo") or {}
        date_value = (
            exif.get("dateTimeOriginal")
            or asset.get("localDateTime")
            or asset.get("fileCreatedAt")
        )
        return {
            "date": self.format_date(date_value),
            "location": self.format_location(exif),
        }

    def load_slide(self, base_url, api_key, asset):
        detailed_asset = self.safe_load_asset_details(base_url, api_key, asset)
        asset.update(detailed_asset)
        image = self.safe_load_thumbnail(base_url, api_key, detailed_asset["id"])
        if image is None:
            return None
        return image, self.display_metadata(detailed_asset)

    def run(self):
        stage = "Konfiguration"
        try:
            config = json.loads(self.config_file.read_text(encoding="utf-8"))
            base_url = config.get("immich_url", "").strip().rstrip("/")
            api_key = config.get("api_key", "").strip()
            if not base_url or not api_key:
                self.status_received.emit("Immich-Konfiguration fehlt")
                return
            stage = "Albumdaten"
            asset_records = self.load_assets(base_url, api_key)
            if not asset_records:
                self.status_received.emit("Keine Bilder in Immich gefunden")
                return
            random.shuffle(asset_records)
            index = 0
            slide_cache = {}
            while not self.stop_event.is_set():
                asset = asset_records[index]
                asset_id = asset["id"]
                stage = "Vorschaubild"
                if asset_id in slide_cache:
                    current_slide = slide_cache.pop(asset_id)
                else:
                    current_slide = self.load_slide(base_url, api_key, asset)
                if current_slide is None:
                    index = (index + 1) % len(asset_records)
                    continue
                if self.stop_event.is_set():
                    return
                current_image, current_metadata = current_slide
                self.image_received.emit(current_image, current_metadata)

                next_index = (index + 1) % len(asset_records)
                next_asset = asset_records[next_index]
                next_id = next_asset["id"]
                if next_id not in slide_cache:
                    next_slide = self.load_slide(base_url, api_key, next_asset)
                    if next_slide is not None:
                        slide_cache[next_id] = next_slide
                if self.stop_event.is_set():
                    return

                deadline = time.monotonic() + SLIDE_DURATION_SECONDS
                while not self.stop_event.is_set():
                    with self.control_lock:
                        paused = self.paused
                    timeout = None if paused else max(0, deadline - time.monotonic())
                    control_changed = self.control_event.wait(timeout)
                    self.control_event.clear()
                    if self.stop_event.is_set():
                        return
                    with self.control_lock:
                        step = self.navigation_step
                        self.navigation_step = 0
                        paused = self.paused
                    if step:
                        index = (index + step) % len(asset_records)
                        break
                    if not control_changed:
                        index = next_index
                        break
                    if not paused:
                        deadline = time.monotonic() + SLIDE_DURATION_SECONDS
                slide_cache = {
                    key: value for key, value in slide_cache.items()
                    if key in {
                        asset_records[index]["id"],
                        asset_records[(index + 1) % len(asset_records)]["id"],
                    }
                }
        except (ImmichApiError, OSError, ValueError, KeyError, urllib.error.URLError) as error:
            if stage == "Albumdaten":
                message = "Album WEB Radio konnte nicht geladen werden: " + str(error)
            elif stage == "Vorschaubild":
                message = "Vorschaubild konnte nicht geladen werden: " + str(error)
            else:
                message = "Immich-Konfiguration konnte nicht geladen werden: " + str(error)
            self.status_received.emit(message)


class SlideshowOverlay(QWidget):
    activated = pyqtSignal()

    def mousePressEvent(self, event):
        self.activated.emit()
        event.accept()


class CarouselWidget(QWidget):
    swiped = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.press_position = None
        self.last_position = None
        self.pointer_moved = False

    def watch(self, widget):
        widget.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self.press_position = event.globalPos()
            self.last_position = event.globalPos()
            self.pointer_moved = False
        elif event.type() == QEvent.MouseMove and self.press_position is not None:
            self.last_position = event.globalPos()
            distance = self.last_position - self.press_position
            if abs(distance.x()) > 12 or abs(distance.y()) > 12:
                self.pointer_moved = True
        elif event.type() == QEvent.MouseButtonRelease and self.press_position is not None:
            release_position = event.globalPos()
            distance = release_position - self.press_position
            horizontal_swipe = (
                abs(distance.x()) >= 70
                and abs(distance.x()) > abs(distance.y())
            )
            moved = self.pointer_moved
            self.press_position = None
            self.last_position = None
            self.pointer_moved = False

            if horizontal_swipe:
                if isinstance(watched, QAbstractButton):
                    watched.setDown(False)
                self.swiped.emit(1 if distance.x() < 0 else -1)
                return True
            if moved:
                if isinstance(watched, QAbstractButton):
                    watched.setDown(False)
                return True
        return super().eventFilter(watched, event)


class CompactStationButton(QToolButton):
    def paintEvent(self, _event):
        painter = QStylePainter(self)
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        icon = self.icon()
        text = self.text()
        option.icon = QIcon()
        option.text = ""
        painter.drawComplexControl(QStyle.CC_ToolButton, option)

        icon_rect = QRect((self.width() - 100) // 2, 10, 100, 100)
        if not icon.isNull():
            icon.paint(painter, icon_rect, Qt.AlignCenter)

        text_rect = QRect(5, 120, max(0, self.width() - 10), max(0, self.height() - 130))
        painter.setPen(self.palette().buttonText().color())
        painter.setFont(self.font())
        painter.drawText(text_rect, Qt.AlignHCenter | Qt.AlignVCenter | Qt.TextWordWrap, text)


class DarkOverlay(QWidget):
    activated = pyqtSignal()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.black)

    def mousePressEvent(self, event):
        self.activated.emit()
        event.accept()


class RadioWindow(QWidget):
    stations_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.station_store = StationStore(STATION_JSON_FILE, STATION_FILE, LOGO_DIR)
        self.station_records = self.station_store.list_stations()
        self.stations = self.station_tuples(self.station_records)
        self.selected_index = next(
            (
                index
                for index, station in enumerate(self.station_records)
                if station.get("startup")
            ),
            0,
        )
        self.playing_index = None
        self.radio_paused = False
        self.mode = "radio"
        self.current_artist = ""
        self.current_title = ""
        self.lms_cover_url = ""
        self.slideshow_loader = None
        self.slideshow_loaders = set()
        self.slideshow_pixmap = None
        self.slideshow_metadata = {"date": "", "location": ""}
        self.slideshow_paused = False

        Gst.init(None)
        self.player = Gst.ElementFactory.make("playbin", "radio-player")
        if self.player is None:
            raise RuntimeError("GStreamer-Element 'playbin' konnte nicht erstellt werden")
        self.player.set_property("volume", 1.0)
        self.player.connect("source-setup", self.configure_gstreamer_source)
        self.audio_monitor = Gst.ElementFactory.make("identity", "audio-buffer-monitor")
        self.last_audio_buffer_at = 0.0
        self.stream_started_at = 0.0
        if self.audio_monitor is not None:
            self.audio_monitor.set_property("signal-handoffs", True)
            self.audio_monitor.connect("handoff", self.on_audio_handoff)
            self.player.set_property("audio-filter", self.audio_monitor)
        else:
            print("GStreamer-Watchdog deaktiviert: identity fehlt", file=sys.stderr)
        self.gst_bus = self.player.get_bus()
        self.gst_bus_timer = QTimer(self)
        self.gst_bus_timer.timeout.connect(self.process_gstreamer_bus)
        self.gst_bus_timer.start(100)
        self.stream_watchdog_timer = QTimer(self)
        self.stream_watchdog_timer.timeout.connect(self.check_radio_stream)
        self.stream_watchdog_timer.start(STREAM_WATCHDOG_INTERVAL_MS)
        self.lms_status_timer = QTimer(self)
        self.lms_status_timer.timeout.connect(self.update_lms_status)
        self.lms_status_timer.start(LMS_STATUS_INTERVAL_MS)
        self.logo_cache = {}
        self.web_server = None
        self.stations_changed.connect(self.reload_stations)

        self.build_ui()
        self.apply_style()
        self.update_selection()
        self.set_bluetooth_enabled(False)
        self.set_lms_enabled(False)

        self.idle_timer = QTimer(self)
        self.idle_timer.setSingleShot(True)
        self.idle_timer.timeout.connect(self.start_slideshow)
        QApplication.instance().installEventFilter(self)
        self.reset_idle_timer()

        if self.stations:
            self.start_selected_station()
        else:
            self.now_station.setText("Keine Sender vorhanden")
            self.track_info.setText("radio_station_list.csv ist leer oder fehlt")
            for button in self.station_buttons:
                button.setEnabled(False)

        try:
            self.web_server = RadioAdminServer(
                self.station_store,
                LOGO_DIR,
                BASE_DIR,
                self.stations_changed.emit,
                port=8088,
            )
            self.web_server.start()
            print("Web-Admin-Server auf Port 8088 gestartet", file=sys.stderr)
        except OSError:
            self.web_server = None

        # Web-Player-Server (GUI via Browser)
        self.web_player_server = None
        if os.environ.get("WEBRADIO_START_WEB_PLAYER", "1") != "0":
            try:
                self.web_player_server = WebPlayerServer(
                    self.station_store,
                    LOGO_DIR,
                    BASE_DIR,
                    IMMICH_CONFIG_FILE,
                    self.stations_changed.emit,
                    port=8089,
                )
                self.web_player_server.start()
                print("Web-Player auf Port 8089 gestartet (http://<pi-ip>:8089)", file=sys.stderr)
            except OSError:
                self.web_player_server = None

    @staticmethod
    def station_tuples(records):
        return [(station["name"], station["audio_url"]) for station in records]

    def build_ui(self):
        self.setWindowTitle("Webradio")
        screen_size = QApplication.primaryScreen().availableGeometry().size()
        self.compact_layout = screen_size.width() <= 640 or screen_size.height() <= 480
        self.setMinimumSize(320, 240) if self.compact_layout else self.setMinimumSize(760, 560)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_StyledBackground, True)

        root = QVBoxLayout(self)
        if self.compact_layout:
            root.setContentsMargins(6, 5, 6, 5)
            root.setSpacing(5)
        else:
            root.setContentsMargins(20, 18, 20, 18)
            root.setSpacing(16)

        now_card = QFrame()
        now_card.setObjectName("nowCard")
        if self.compact_layout:
            now_card.setFixedHeight(38)
        now_layout = QHBoxLayout(now_card)
        if self.compact_layout:
            now_layout.setContentsMargins(6, 4, 6, 4)
            now_layout.setSpacing(8)
        else:
            now_layout.setContentsMargins(18, 14, 18, 14)
            now_layout.setSpacing(22)

        self.now_logo = QLabel()
        self.now_logo.setObjectName("logoTile")
        self.now_logo.setAlignment(Qt.AlignCenter)
        self.now_logo.setFixedSize(48, 28) if self.compact_layout else self.now_logo.setFixedSize(220, 145)
        now_layout.addWidget(self.now_logo)

        now_text = QVBoxLayout()
        caption = QLabel("JETZT LÄUFT")
        caption.setObjectName("caption")
        self.now_station = QLabel("–")
        self.now_station.setObjectName("nowStation")
        self.track_info = QLabel("Titelinformationen werden geladen …")
        self.track_info.setObjectName("trackInfo")
        self.track_info.setWordWrap(not self.compact_layout)
        self.track_info.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        if self.compact_layout:
            self.track_info.setStyleSheet(
                "font-family: 'DejaVu Sans'; font-size: 12px; "
                "font-weight: normal; padding: 0px;"
            )
        caption.hide()
        self.now_station.hide()
        now_text.addWidget(self.track_info, 1)
        now_layout.addLayout(now_text, 1)
        root.addWidget(now_card)

        self.carousel = CarouselWidget()
        self.carousel.setObjectName("carouselWidget")
        self.carousel.swiped.connect(self.browse)
        self.carousel.watch(self.carousel)
        chooser = QHBoxLayout(self.carousel)
        chooser.setContentsMargins(0, 0, 0, 0)
        chooser.setSpacing(8)

        self.station_buttons = []
        for offset in (-2, -1, 0, 1, 2):
            button = CompactStationButton() if self.compact_layout else QToolButton()
            button.setText("–")
            button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            button.setProperty("carouselOffset", offset)
            button.setObjectName("stationCard" if self.compact_layout else ("stationCenter" if offset == 0 else "stationPreview"))
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            button.clicked.connect(lambda checked=False, step=offset: self.select_carousel_station(step))
            self.carousel.watch(button)
            if self.compact_layout and abs(offset) == 2:
                button.hide()
                continue
            stretch = 1 if self.compact_layout else (4 if offset == 0 else (2 if abs(offset) == 1 else 1))
            chooser.addWidget(button, stretch)
            self.station_buttons.append(button)

        self.lms_cover = QLabel("Kein Albumcover verfügbar")
        self.lms_cover.setObjectName("lmsCover")
        self.lms_cover.setAlignment(Qt.AlignCenter)
        self.lms_cover.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.lms_cover.hide()
        chooser.addWidget(self.lms_cover, 1)

        root.addWidget(self.carousel, 1)

        controls = QGridLayout()
        controls.setHorizontalSpacing(5 if self.compact_layout else 16)
        controls.setVerticalSpacing(4 if self.compact_layout else 0)
        self.power_button = QPushButton("Aus")
        self.power_button.setObjectName("powerButton")
        self.power_button.clicked.connect(self.confirm_shutdown)
        controls.addWidget(self.power_button, 0, 0)

        self.dark_button = QPushButton("Dunkel")
        self.dark_button.setObjectName("darkButton")
        self.dark_button.clicked.connect(self.show_dark_screen)
        controls.addWidget(self.dark_button, 0, 1)

        self.mode_button = QPushButton("Quelle:\nRadio")
        self.mode_button.setObjectName("modeButton")
        self.mode_button.clicked.connect(self.toggle_mode)
        controls.addWidget(self.mode_button, 0, 2)

        self.slideshow_button = QPushButton("Diashow")
        self.slideshow_button.setObjectName("slideshowButton")
        self.slideshow_button.clicked.connect(self.start_slideshow)
        controls.addWidget(self.slideshow_button, 0, 3)

        self.stations_button = QPushButton("Stationen")
        self.stations_button.setObjectName("stationsButton")
        self.stations_button.clicked.connect(self.open_station_selection)
        controls.addWidget(self.stations_button, 0, 4)
        if self.compact_layout:
            for control_button in (
                self.power_button,
                self.dark_button,
                self.mode_button,
                self.slideshow_button,
                self.stations_button,
            ):
                control_button.setFixedHeight(44)
        for column in range(5):
            controls.setColumnStretch(column, 1)
        root.addLayout(controls)

        self.slideshow_overlay = SlideshowOverlay(self)
        self.slideshow_overlay.setObjectName("slideshowOverlay")
        self.slideshow_overlay.setAttribute(Qt.WA_StyledBackground, True)
        self.slideshow_image = QLabel("Bilder werden geladen …", self.slideshow_overlay)
        self.slideshow_image.setObjectName("slideshowImage")
        self.slideshow_image.setAlignment(Qt.AlignCenter)
        self.slideshow_date = QLabel(self.slideshow_overlay)
        self.slideshow_date.setObjectName("slideshowDate")
        self.slideshow_date.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.slideshow_date.setWordWrap(False)
        self.slideshow_location = QLabel(self.slideshow_overlay)
        self.slideshow_location.setObjectName("slideshowLocation")
        self.slideshow_location.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        self.slideshow_location.setWordWrap(True)
        assert self.slideshow_date.parent() is self.slideshow_overlay
        assert self.slideshow_location.parent() is self.slideshow_overlay
        self.slideshow_date.hide()
        self.slideshow_location.hide()
        self.slideshow_previous = QPushButton("‹", self.slideshow_overlay)
        self.slideshow_previous.setObjectName("slideshowNavigation")
        self.slideshow_previous.setFixedSize(48, 100) if self.compact_layout else self.slideshow_previous.setFixedSize(88, 150)
        self.slideshow_previous.clicked.connect(lambda: self.navigate_slideshow(-1))
        self.slideshow_next = QPushButton("›", self.slideshow_overlay)
        self.slideshow_next.setObjectName("slideshowNavigation")
        self.slideshow_next.setFixedSize(48, 100) if self.compact_layout else self.slideshow_next.setFixedSize(88, 150)
        self.slideshow_next.clicked.connect(lambda: self.navigate_slideshow(1))
        self.slideshow_pause = QPushButton("⏸", self.slideshow_overlay)
        self.slideshow_pause.setObjectName("slideshowControl")
        self.slideshow_pause.setFixedSize(44, 44) if self.compact_layout else self.slideshow_pause.setFixedSize(64, 64)
        self.slideshow_pause.clicked.connect(self.toggle_slideshow_pause)
        self.slideshow_close = QPushButton("×", self.slideshow_overlay)
        self.slideshow_close.setObjectName("slideshowControl")
        self.slideshow_close.setFixedSize(44, 44) if self.compact_layout else self.slideshow_close.setFixedSize(64, 64)
        self.slideshow_close.clicked.connect(self.stop_slideshow)
        self.slideshow_overlay.hide()

        self.dark_overlay = DarkOverlay(self)
        self.dark_overlay.setObjectName("darkOverlay")
        self.dark_overlay.setAttribute(Qt.WA_StyledBackground, True)
        self.dark_overlay.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.dark_overlay.activated.connect(self.hide_dark_screen)
        self.dark_overlay.hide()

    def apply_style(self):
        self.setStyleSheet("""
            QWidget {
                background: #10141c;
                color: #f5f7fb;
                font-family: DejaVu Sans;
            }
            QFrame#nowCard {
                background: #1a2230;
                border: 1px solid #2b374a;
                border-radius: 18px;
            }
            QLabel#logoTile {
                background: #273247;
                border-radius: 14px;
                color: #ffffff;
                font-size: 25px;
                font-weight: bold;
                padding: 4px;
            }
            QLabel#caption { color: #75baff; font-size: 16px; font-weight: bold; }
            QLabel#nowStation { font-size: 34px; font-weight: bold; }
            QLabel#trackInfo { color: #f5f7fb; font-size: 34px; font-weight: bold; }
            QPushButton {
                border: none;
                border-radius: 18px;
                color: white;
                font-weight: bold;
            }
            QPushButton:pressed { background: #3a76af; }
            QPushButton:disabled { color: #737b89; background: #242a34; }
            QToolButton#stationCenter {
                background: #1769aa;
                border: none;
                border-radius: 18px;
                color: white;
                font-size: 25px;
                font-weight: bold;
                min-height: 205px;
                padding: 10px 22px;
            }
            QToolButton#stationCenter:pressed { background: #3a76af; }
            QToolButton#stationPreview {
                background: #263348;
                border: none;
                border-radius: 18px;
                color: #d9e7f7;
                font-size: 17px;
                font-weight: bold;
                min-height: 175px;
                padding: 5px;
            }
            QToolButton#stationPreview:pressed {
                background: #36506f;
            }
            QToolButton:disabled { color: #737b89; background: #242a34; }
            QWidget#carouselWidget {
                background: transparent;
                border: none;
            }
            QPushButton#powerButton {
                background: #b6323b;
                font-size: 18px;
                min-height: 78px;
            }
            QPushButton#darkButton {
                background: #283242;
                font-size: 18px;
                min-height: 78px;
            }
            QPushButton#modeButton {
                background: #1769aa;
                font-size: 18px;
                min-height: 78px;
            }
            QPushButton#modeButton:checked { background: #7656b5; }
            QPushButton#slideshowButton {
                background: #287a58;
                font-size: 18px;
                min-height: 78px;
            }
            QPushButton#stationsButton {
                background: #7656b5;
                font-size: 18px;
                min-height: 78px;
            }
            QWidget#slideshowOverlay { background: #000000; }
            QLabel#slideshowImage { background: #000000; color: #c5ccda; font-size: 24px; }
            QLabel#slideshowDate, QLabel#slideshowLocation {
                background: rgba(0, 0, 0, 175);
                border-radius: 8px;
                color: #ffffff;
                font-family: sans-serif;
                font-size: 21px;
                font-weight: 400;
                padding: 7px 10px;
            }
            QPushButton#slideshowNavigation {
                background: rgba(35, 45, 60, 220);
                border: 2px solid #8fc9ff;
                border-radius: 30px;
                color: #ffffff;
                font-size: 76px;
                padding: 0;
            }
            QPushButton#slideshowNavigation:pressed { background: #3a76af; }
            QPushButton#slideshowControl {
                background: rgba(35, 45, 60, 180);
                border: 1px solid rgba(255, 255, 255, 190);
                border-radius: 16px;
                color: #ffffff;
                font-size: 34px;
                min-width: 64px;
                max-width: 64px;
                min-height: 64px;
                max-height: 64px;
                margin: 0;
                padding: 0;
            }
            QPushButton#slideshowControl:pressed { background: rgba(58, 76, 100, 210); }
            QWidget#darkOverlay { background: #000000; }
        """)
        if self.compact_layout:
            self.setStyleSheet(self.styleSheet() + """
                QFrame#nowCard { border-radius: 8px; }
                QLabel#logoTile { border-radius: 6px; font-size: 11px; padding: 1px; }
                QLabel#trackInfo { font-size: 12px; font-weight: normal; }
                QToolButton#stationCard, QToolButton#stationCenter, QToolButton#stationPreview {
                    background: #263348; border: none; border-radius: 7px;
                    color: #d9e7f7; font-size: 9px; font-weight: bold;
                    min-height: 0px; max-height: 16777215px; padding: 3px 1px;
                }
                QToolButton#stationCard:pressed, QToolButton#stationCenter:pressed,
                QToolButton#stationPreview:pressed { background: #36506f; }
                QPushButton#powerButton, QPushButton#darkButton,
                QPushButton#modeButton, QPushButton#slideshowButton,
                QPushButton#stationsButton {
                    border-radius: 9px; font-size: 10px; min-height: 44px; max-height: 44px;
                }
                QLabel#slideshowDate, QLabel#slideshowLocation { font-size: 14px; padding: 3px 5px; }
                QPushButton#slideshowNavigation { border-radius: 18px; font-size: 42px; }
                QPushButton#slideshowControl {
                    border-radius: 12px; font-size: 24px;
                    min-width: 44px; max-width: 44px; min-height: 44px; max-height: 44px;
                }
            """)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "slideshow_overlay"):
            self.slideshow_overlay.setGeometry(self.rect())
            self.layout_slideshow_overlay()
        if hasattr(self, "dark_overlay"):
            self.dark_overlay.setGeometry(self.rect())

    def eventFilter(self, watched, event):
        if event.type() in (
            QEvent.MouseButtonPress,
            QEvent.MouseButtonRelease,
            QEvent.MouseMove,
            QEvent.KeyPress,
            QEvent.TouchBegin,
        ):
            self.reset_idle_timer()
        return super().eventFilter(watched, event)

    def open_station_selection(self):
        if self.web_server is None:
            self.track_info.setText("Senderverwaltung ist nicht verfügbar")
            return
        if not QDesktopServices.openUrl(QUrl("http://127.0.0.1:8088/")):
            self.track_info.setText("Senderverwaltung konnte nicht geöffnet werden")

    def show_dark_screen(self):
        print("Dunkelmodus durch Button aktiviert", file=sys.stderr)
        self.stop_slideshow(restart_idle_timer=False)
        self.idle_timer.stop()
        self.dark_overlay.setGeometry(self.rect())
        self.dark_overlay.raise_()
        self.dark_overlay.show()
        self.dark_overlay.repaint()

    def hide_dark_screen(self):
        self.dark_overlay.hide()
        self.reset_idle_timer()

    def closeEvent(self, event):
        self.stop_background_tasks()
        super().closeEvent(event)

    def stop_background_tasks(self):
        self.set_bluetooth_enabled(False)
        self.set_lms_enabled(False)
        self.idle_timer.stop()
        self.stop_slideshow(restart_idle_timer=False)
        for loader in tuple(self.slideshow_loaders):
            loader.stop()
            loader.wait(31000)
        self.gst_bus_timer.stop()
        self.stream_watchdog_timer.stop()
        self.lms_status_timer.stop()
        self.player.set_state(Gst.State.NULL)
        if self.web_server is not None:
            self.web_server.stop()
            self.web_server = None
        if hasattr(self, "web_player_server") and self.web_player_server is not None:
            self.web_player_server.stop()
            self.web_player_server = None

    def reset_idle_timer(self):
        if not self.dark_overlay.isVisible() and not self.slideshow_overlay.isVisible():
            self.idle_timer.start(SLIDESHOW_IDLE_MS)

    def start_slideshow(self):
        if self.dark_overlay.isVisible() or self.slideshow_loader is not None:
            return
        self.idle_timer.stop()
        self.slideshow_pixmap = None
        self.slideshow_metadata = {"date": "", "location": ""}
        self.slideshow_paused = False
        self.slideshow_pause.setText("⏸")
        self.slideshow_date.hide()
        self.slideshow_location.hide()
        self.slideshow_image.setPixmap(QPixmap())
        self.slideshow_image.setText("")
        loader = ImmichSlideshowLoader(IMMICH_CONFIG_FILE, self)
        loader.image_received.connect(self.show_slideshow_image)
        loader.status_received.connect(self.show_slideshow_status)
        loader.finished.connect(lambda current=loader: self.slideshow_finished(current))
        self.slideshow_loader = loader
        self.slideshow_loaders.add(loader)
        loader.start()

    def navigate_slideshow(self, direction):
        if self.slideshow_loader is not None and self.slideshow_overlay.isVisible():
            self.slideshow_loader.navigate(direction)

    def toggle_slideshow_pause(self):
        if self.slideshow_loader is None or not self.slideshow_overlay.isVisible():
            return
        self.slideshow_paused = not self.slideshow_paused
        self.slideshow_pause.setText("▶" if self.slideshow_paused else "⏸")
        self.slideshow_loader.set_paused(self.slideshow_paused)

    def stop_slideshow(self, restart_idle_timer=True):
        loader = self.slideshow_loader
        self.slideshow_loader = None
        if loader is not None:
            loader.stop()
        self.slideshow_overlay.hide()
        self.slideshow_pixmap = None
        self.slideshow_metadata = {"date": "", "location": ""}
        self.slideshow_paused = False
        self.slideshow_pause.setText("⏸")
        if restart_idle_timer and not self.dark_overlay.isVisible():
            self.reset_idle_timer()

    def slideshow_finished(self, loader):
        if self.slideshow_loader is loader:
            self.slideshow_loader = None
        self.slideshow_loaders.discard(loader)
        loader.deleteLater()
        if not self.slideshow_overlay.isVisible() and not self.dark_overlay.isVisible():
            self.reset_idle_timer()

    def show_slideshow_status(self, message):
        print(message, file=sys.stderr)
        if self.slideshow_pixmap is None and self.slideshow_overlay.isVisible():
            self.slideshow_image.setText(message)

    def show_slideshow_image(self, image_data, metadata):
        if self.slideshow_loader is None or self.dark_overlay.isVisible():
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(image_data):
            self.slideshow_pixmap = pixmap
            self.slideshow_metadata = metadata if isinstance(metadata, dict) else {}
            self.slideshow_image.setText("")
            self.slideshow_overlay.setGeometry(self.rect())
            self.layout_slideshow_overlay()
            self.slideshow_overlay.raise_()
            self.slideshow_overlay.show()

    def layout_slideshow_overlay(self):
        if not hasattr(self, "slideshow_image"):
            return
        overlay_width = self.slideshow_overlay.width()
        overlay_height = self.slideshow_overlay.height()
        if overlay_width <= 0 or overlay_height <= 0:
            return

        edge_margin = 8 if self.compact_layout else 18
        control_size = self.slideshow_pause.width()

        pause_x = max(0, min(
            (overlay_width - control_size) // 2,
            overlay_width - control_size,
        ))
        self.slideshow_pause.setGeometry(pause_x, edge_margin, control_size, control_size)

        close_x = max(0, overlay_width - control_size - edge_margin)
        self.slideshow_close.setGeometry(close_x, edge_margin, control_size, control_size)

        navigation_y = max(
            0,
            min(
                (overlay_height - self.slideshow_previous.height()) // 2,
                overlay_height - self.slideshow_previous.height(),
            ),
        )
        self.slideshow_previous.move(edge_margin, navigation_y)
        self.slideshow_next.move(
            max(0, overlay_width - self.slideshow_next.width() - edge_margin),
            navigation_y,
        )

        image_width = min(800, overlay_width)
        image_height = min(600, overlay_height)
        image_x = (overlay_width - image_width) // 2
        image_y = (overlay_height - image_height) // 2
        self.slideshow_image.setGeometry(image_x, image_y, image_width, image_height)
        self.scale_slideshow_image()

        metadata_font = QFont("DejaVu Sans")
        metadata_font.setPixelSize(14 if self.compact_layout else 21)
        metadata_font.setWeight(QFont.Normal)

        date_text = str(self.slideshow_metadata.get("date") or "").strip()
        date_font = QFont(metadata_font)
        self.slideshow_date.setFont(date_font)
        self.slideshow_date.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.slideshow_date.setWordWrap(False)
        self.slideshow_date.setText(date_text)
        if date_text:
            date_width = 125 if self.compact_layout else 165
            date_height = 34 if self.compact_layout else 48
            self.slideshow_date.setGeometry(edge_margin, edge_margin, date_width, date_height)
            self.slideshow_date.show()
        else:
            self.slideshow_date.hide()

        location_text = str(self.slideshow_metadata.get("location") or "").strip()
        self.slideshow_location.setFont(metadata_font)
        self.slideshow_location.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        self.slideshow_location.setWordWrap(True)
        self.slideshow_location.setText(location_text)
        if location_text:
            location_width = min(600, max(0, overlay_width - 220))
            location_height = min(76, max(0, overlay_height - edge_margin))
            location_x = max(0, (overlay_width - location_width) // 2)
            location_y = max(0, overlay_height - location_height - edge_margin)
            self.slideshow_location.setGeometry(
                location_x,
                location_y,
                location_width,
                location_height,
            )
            self.slideshow_location.show()
        else:
            self.slideshow_location.hide()

        self.slideshow_image.lower()
        self.slideshow_date.raise_()
        self.slideshow_location.raise_()
        self.slideshow_previous.raise_()
        self.slideshow_next.raise_()
        self.slideshow_pause.raise_()
        self.slideshow_close.raise_()

    def scale_slideshow_image(self):
        if self.slideshow_pixmap is None or not hasattr(self, "slideshow_image"):
            return
        self.slideshow_image.setPixmap(
            self.slideshow_pixmap.scaled(
                self.slideshow_image.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )

    def browse(self, direction):
        if not self.stations:
            return
        self.selected_index = (self.selected_index + direction) % len(self.stations)
        self.update_selection()

    def select_carousel_station(self, offset):
        if not self.stations:
            return
        if offset != 0:
            self.selected_index = (self.selected_index + offset) % len(self.stations)
        self.start_selected_station()

    def update_selection(self):
        if not self.stations:
            return
        station_count = len(self.stations)
        visible_offsets = {
            1: {0},
            2: {0, 1},
            3: {-1, 0, 1},
            4: {-1, 0, 1, 2},
        }.get(station_count, {-2, -1, 0, 1, 2})
        if self.compact_layout:
            visible_offsets &= {-1, 0, 1}
        for button in self.station_buttons:
            offset = int(button.property("carouselOffset"))
            # Bei weniger als fünf Sendern keine Station doppelt anzeigen.
            if offset not in visible_offsets:
                button.setText("")
                button.setEnabled(False)
                continue

            index = (self.selected_index + offset) % station_count
            name = self.stations[index][0]
            button.setEnabled(True)
            self.set_card_logo(button, name, offset)
            if offset == 0 and not self.compact_layout:
                symbol = "▶"
                if self.mode == "radio" and index == self.playing_index and not self.radio_paused:
                    symbol = "❚❚"
                button.setText(name + "\n" + symbol)
            else:
                button.setText(name)

    def start_selected_station(self):
        if not self.stations:
            return
        if (
            self.mode == "radio"
            and self.selected_index == self.playing_index
            and self.playing_index is not None
        ):
            if self.radio_paused:
                name, _url = self.stations[self.playing_index]
                self.stream_started_at = time.monotonic()
                self.last_audio_buffer_at = 0.0
                self.player.set_state(Gst.State.PLAYING)
                self.radio_paused = False
                self.now_station.setText(name)
                self.set_logo(name)
            else:
                self.player.set_state(Gst.State.PAUSED)
                self.radio_paused = True
            self.update_selection()
            return
        if self.mode != "radio":
            if self.mode == "mpd":
                self.stop_mpd()
            elif self.mode == "lms":
                self.set_lms_enabled(False)
            self.set_bluetooth_enabled(False)
            self.mode = "radio"
            self.set_lms_view(False)
            self.mode_button.setText("Quelle:\nRadio")

        name, url = self.stations[self.selected_index]
        self.player.set_state(Gst.State.NULL)
        while self.gst_bus.pop() is not None:
            pass
        self.current_artist = ""
        self.current_title = ""
        self.player.set_property("uri", url)
        self.stream_started_at = time.monotonic()
        self.last_audio_buffer_at = 0.0
        self.player.set_state(Gst.State.PLAYING)
        self.playing_index = self.selected_index
        self.radio_paused = False
        self.now_station.setText(name)
        self.track_info.setText("Keine Titelinformationen")
        self.set_logo(name)
        self.update_selection()

    def toggle_mode(self):
        next_mode = {
            "radio": "mpd",
            "mpd": "lms",
            "lms": "bluetooth",
            "bluetooth": "radio",
        }.get(self.mode, "radio")

        if self.mode == "mpd":
            self.stop_mpd()
        elif self.mode == "lms":
            self.set_lms_enabled(False)

        self.player.set_state(Gst.State.NULL)
        bluetooth_ready = self.set_bluetooth_enabled(next_mode == "bluetooth")
        lms_ready = self.set_lms_enabled(next_mode == "lms")
        self.mode = next_mode
        self.set_lms_view(next_mode == "lms")

        if next_mode == "mpd":
            self.mode_button.setText("Quelle:\nMPD")
            self.now_station.setText("MPD")
            self.track_info.setText("Wiedergabe über Music Player Daemon")
            self.set_logo("MPD")
            self.start_mpd()
        elif next_mode == "lms":
            self.mode_button.setText("Quelle:\nLMS")
            self.now_station.setText("LMS")
            self.track_info.setText("LMS bereit – Wiedergabe über Handy-App steuern")
            self.update_lms_status()
        elif next_mode == "bluetooth":
            self.mode_button.setText("Quelle:\nBluetooth")
            self.now_station.setText("Bluetooth")
            self.track_info.setText("Bluetooth-Audio bereit – Wiedergabe am Gerät starten")
            self.set_logo("Bluetooth")
        else:
            self.mode_button.setText("Quelle:\nRadio")
            if self.playing_index is not None:
                self.selected_index = self.playing_index
                self.radio_paused = True
                self.start_selected_station()
            elif self.stations:
                self.start_selected_station()
        if not bluetooth_ready:
            action = "gestartet" if next_mode == "bluetooth" else "gestoppt"
            self.track_info.setText(f"Bluetooth-Dienst konnte nicht {action} werden")
        elif not lms_ready:
            action = "gestartet" if next_mode == "lms" else "gestoppt"
            self.track_info.setText(f"LMS-Player konnte nicht {action} werden")
        self.update_selection()

    def set_lms_view(self, enabled):
        for button in self.station_buttons:
            button.setVisible(not enabled)
        self.lms_cover.setVisible(enabled)
        self.now_logo.setVisible(not enabled)
        if not enabled:
            self.lms_cover_url = ""
            self.lms_cover.setPixmap(QPixmap())
            self.lms_cover.setText("Kein Albumcover verfügbar")

    def update_lms_status(self):
        if self.mode != "lms":
            return
        request_body = json.dumps({
            "id": 1,
            "method": "slim.request",
            "params": [
                LMS_PLAYER_ID,
                ["status", "-", 1, "tags:alKcu"],
            ],
        }).encode("utf-8")
        request = urllib.request.Request(
            LMS_SERVER_URL + "/jsonrpc.js",
            data=request_body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=1.5) as response:
                status = json.loads(response.read().decode("utf-8")).get("result", {})
        except (OSError, ValueError, urllib.error.URLError) as error:
            print(f"LMS-Status konnte nicht geladen werden: {error}", file=sys.stderr)
            return

        track = status.get("remoteMeta") or next(
            iter(status.get("playlist_loop") or ()), {}
        )
        title = str(track.get("title") or "").strip()
        artist = str(track.get("artist") or "").strip()
        if title or artist:
            self.track_info.setText(" — ".join(value for value in (title, artist) if value))
        elif status.get("mode") == "stop":
            self.track_info.setText("LMS bereit – keine Wiedergabe")

        artwork_url = str(track.get("artwork_url") or "").strip()
        if artwork_url and not urllib.parse.urlsplit(artwork_url).scheme:
            artwork_url = urllib.parse.urljoin(LMS_SERVER_URL + "/", artwork_url)
        if artwork_url == self.lms_cover_url:
            return
        self.lms_cover_url = artwork_url
        self.lms_cover.setPixmap(QPixmap())
        if not artwork_url:
            self.lms_cover.setText("Kein Albumcover verfügbar")
            return
        try:
            with urllib.request.urlopen(artwork_url, timeout=2) as response:
                image_data = response.read()
        except (OSError, urllib.error.URLError) as error:
            print(f"LMS-Cover konnte nicht geladen werden: {error}", file=sys.stderr)
            self.lms_cover.setText("Albumcover nicht verfügbar")
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(image_data):
            self.lms_cover.setText("")
            self.lms_cover.setPixmap(
                pixmap.scaled(
                    self.lms_cover.size(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )
        else:
            self.lms_cover.setText("Albumcover nicht verfügbar")

    @staticmethod
    def set_lms_enabled(enabled):
        action = "start" if enabled else "stop"
        command = ["sudo", "-n", "systemctl", action, "squeezelite.service"]
        try:
            result = subprocess.run(command, check=False, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f"LMS-Umschaltung fehlgeschlagen: {error}", file=sys.stderr)
            return False
        if result.returncode != 0:
            print(
                f"LMS-Umschaltung fehlgeschlagen: {' '.join(command)} "
                f"(Exit-Code {result.returncode})",
                file=sys.stderr,
            )
            return False
        return True

    @staticmethod
    def set_bluetooth_enabled(enabled):
        commands = (
            [
                ["sudo", "-n", "systemctl", "unmask", "--runtime", "bluetooth.service"],
                ["sudo", "-n", "rfkill", "unblock", "bluetooth"],
                ["sudo", "-n", "systemctl", "start", "bluetooth.service"],
            ]
            if enabled
            else [
                ["sudo", "-n", "rfkill", "block", "bluetooth"],
                [
                    "sudo",
                    "-n",
                    "systemctl",
                    "mask",
                    "--runtime",
                    "--now",
                    "bluetooth.service",
                ],
            ]
        )
        for command in commands:
            try:
                result = subprocess.run(command, check=False, timeout=10)
            except (OSError, subprocess.TimeoutExpired) as error:
                print(f"Bluetooth-Umschaltung fehlgeschlagen: {error}", file=sys.stderr)
                return False
            if result.returncode != 0:
                print(
                    f"Bluetooth-Umschaltung fehlgeschlagen: {' '.join(command)} "
                    f"(Exit-Code {result.returncode})",
                    file=sys.stderr,
                )
                return False
        return True

    @staticmethod
    def with_mpd(action):
        client = musicpd.MPDClient()
        try:
            client.timeout = 3
            client.connect("127.0.0.1", 6600)
            action(client)
        except (OSError, musicpd.MPDError):
            return False
        finally:
            try:
                client.close()
                client.disconnect()
            except (OSError, musicpd.MPDError):
                pass
        return True

    def start_mpd(self):
        queue_has_items = False

        def play_queue(client):
            nonlocal queue_has_items
            queue_has_items = bool(client.playlistinfo())
            if queue_has_items:
                client.play()

        if not self.with_mpd(play_queue):
            self.track_info.setText("MPD ist nicht erreichbar")
        elif not queue_has_items:
            self.track_info.setText("MPD bereit – Warteschlange leer")

    def stop_mpd(self):
        self.with_mpd(lambda client: client.stop())

    def on_audio_handoff(self, _identity, _buffer):
        self.last_audio_buffer_at = time.monotonic()

    def check_radio_stream(self):
        if (
            self.audio_monitor is None
            or self.mode != "radio"
            or self.radio_paused
            or self.playing_index is None
        ):
            return
        reference_time = self.last_audio_buffer_at or self.stream_started_at
        if reference_time <= 0:
            return
        stalled_for = time.monotonic() - reference_time
        if stalled_for < STREAM_STALL_TIMEOUT_SECONDS:
            return
        self.restart_radio_stream(stalled_for)

    def restart_radio_stream(self, stalled_for):
        if self.playing_index is None or self.playing_index >= len(self.stations):
            return
        name, url = self.stations[self.playing_index]
        print(
            f"Radiostream ohne Audiodaten seit {stalled_for:.1f}s; Neustart: {name}",
            file=sys.stderr,
        )
        self.track_info.setText("Stream wird neu verbunden …")
        self.player.set_state(Gst.State.NULL)
        while self.gst_bus.pop() is not None:
            pass
        self.current_artist = ""
        self.current_title = ""
        self.player.set_property("uri", url)
        self.stream_started_at = time.monotonic()
        self.last_audio_buffer_at = 0.0
        self.player.set_state(Gst.State.PLAYING)

    @staticmethod
    def configure_gstreamer_source(_player, source):
        if source.find_property("iradio-mode") is not None:
            source.set_property("iradio-mode", True)
        if source.find_property("user-agent") is not None:
            source.set_property("user-agent", "Webradio-PyQt5/1.0")

    def process_gstreamer_bus(self):
        while True:
            message = self.gst_bus.pop()
            if message is None:
                return
            if message.type == Gst.MessageType.TAG:
                self.update_gstreamer_metadata(message.parse_tag())
            elif message.type == Gst.MessageType.ERROR:
                error, _debug = message.parse_error()
                if self.mode == "radio":
                    self.track_info.setText(f"Wiedergabefehler: {error.message}")

    def update_gstreamer_metadata(self, tags):
        if self.mode != "radio" or self.radio_paused:
            return
        has_artist, artist = tags.get_string(Gst.TAG_ARTIST)
        has_title, title = tags.get_string(Gst.TAG_TITLE)
        if has_artist and self.is_useful_metadata(artist):
            self.current_artist = artist.strip()
        if has_title and self.is_useful_metadata(title):
            self.current_title = title.strip()
        metadata = "\n".join(value for value in (self.current_artist, self.current_title) if value)
        if metadata:
            self.track_info.setText(metadata)

    @staticmethod
    def is_useful_metadata(value):
        if not value:
            return False
        text = value.strip()
        lowered = text.casefold()
        if not text or re.search(r"(?:https?|icy)://", lowered):
            return False
        if re.search(r"(?:^|[/\\])[^/\\]+\.(?:mp3|aac|aacp|ogg|m3u8?|pls)(?:\?.*)?$", lowered):
            return False
        if lowered in {"play.mp3", "stream.mp3", "stream", "live"}:
            return False
        return True

    def reload_stations(self):
        old_records = self.station_records
        selected_id = (
            old_records[self.selected_index]["id"]
            if old_records and self.selected_index < len(old_records) else None
        )
        playing_id = (
            old_records[self.playing_index]["id"]
            if self.playing_index is not None and self.playing_index < len(old_records) else None
        )
        old_playing_index = self.playing_index

        self.station_records = self.station_store.list_stations()
        self.stations = self.station_tuples(self.station_records)
        id_to_index = {
            station["id"]: index for index, station in enumerate(self.station_records)
        }

        if not self.stations:
            self.playing_index = None
            self.selected_index = 0
            for button in self.station_buttons:
                button.setIcon(QIcon())
                button.setText("")
                button.setEnabled(False)
            return

        if selected_id in id_to_index:
            self.selected_index = id_to_index[selected_id]
        elif playing_id in id_to_index:
            self.selected_index = id_to_index[playing_id]
        else:
            fallback = old_playing_index if old_playing_index is not None else 0
            self.selected_index = min(fallback, len(self.stations) - 1)

        if playing_id in id_to_index:
            self.playing_index = id_to_index[playing_id]
        elif playing_id is not None:
            self.playing_index = None
            if self.mode == "radio":
                self.start_selected_station()
                return
        elif self.mode == "radio":
            self.start_selected_station()
            return
        self.update_selection()

    def set_logo(self, station_name):
        pixmap = self.station_logo(station_name)
        if pixmap is not None:
            self.now_logo.setPixmap(
                pixmap.scaled(self.now_logo.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
            self.now_logo.setText("")
            return
        self.now_logo.setPixmap(QPixmap())
        self.now_logo.setText(station_name)

    def station_logo(self, station_name):
        station = next(
            (record for record in self.station_records if record["name"] == station_name),
            None,
        )
        logo_file = station.get("logo_file", "") if station else ""
        if logo_file:
            candidate = (BASE_DIR / logo_file).resolve()
            try:
                candidate.relative_to(BASE_DIR.resolve())
            except ValueError:
                return None
        elif station is None:
            slug = station_slug(station_name)
            candidate = next(
                (LOGO_DIR / (slug + extension) for extension in (".png", ".jpg", ".jpeg", ".gif")
                 if (LOGO_DIR / (slug + extension)).is_file()),
                None,
            )
        else:
            candidate = None
        if candidate is None or not candidate.is_file():
            return None

        cache_key = str(candidate)
        cached = self.logo_cache.get(cache_key)
        if cached is not None and not cached.isNull():
            return cached
        pixmap = QPixmap(str(candidate))
        if pixmap.isNull():
            return None
        self.logo_cache[cache_key] = pixmap
        return pixmap

    def set_card_logo(self, button, station_name, offset):
        pixmap = self.station_logo(station_name)
        if pixmap is None:
            button.setIcon(QIcon())
            button.setIconSize(QSize(0, 0))
            return
        logo_size = QSize(100, 100) if self.compact_layout else QSize(96, 76)
        canvas = QPixmap(logo_size)
        canvas.fill(QColor("#f3f5f8"))
        inset = 8 if self.compact_layout else 10
        scaled = pixmap.scaled(
            logo_size.width() - inset,
            logo_size.height() - inset,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(
            (logo_size.width() - scaled.width()) // 2,
            (logo_size.height() - scaled.height()) // 2,
            scaled,
        )
        painter.end()
        button.setIcon(QIcon(canvas))
        button.setIconSize(logo_size)

    def confirm_shutdown(self):
        dialog = ShutdownDialog(self)
        if dialog.exec_() == QDialog.Accepted:
            self.stop_background_tasks()
            self.player.set_state(Gst.State.NULL)
            subprocess.run(["sudo", "/sbin/shutdown", "-h", "now"], check=False)


class ShutdownDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ausschalten")
        self.setModal(True)
        self.compact_layout = parent is not None and parent.width() <= 640
        self.setFixedSize(460, 280) if self.compact_layout else self.setFixedSize(620, 300)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QVBoxLayout(self)
        if self.compact_layout:
            layout.setContentsMargins(14, 12, 14, 12)
            layout.setSpacing(12)
        else:
            layout.setContentsMargins(30, 28, 30, 28)
            layout.setSpacing(28)

        question = QLabel("Raspberry Pi wirklich ausschalten?")
        question.setObjectName("shutdownQuestion")
        question.setAlignment(Qt.AlignCenter)
        question.setWordWrap(True)
        layout.addWidget(question, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(24)
        cancel_button = QPushButton("Abbrechen")
        cancel_button.setObjectName("cancelShutdownButton")
        cancel_button.setMinimumSize(180, 64) if self.compact_layout else cancel_button.setMinimumSize(220, 90)
        cancel_button.setDefault(True)
        cancel_button.setAutoDefault(True)
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(cancel_button)

        shutdown_button = QPushButton("Ausschalten")
        shutdown_button.setObjectName("confirmShutdownButton")
        shutdown_button.setMinimumSize(180, 64) if self.compact_layout else shutdown_button.setMinimumSize(220, 90)
        shutdown_button.setAutoDefault(False)
        shutdown_button.clicked.connect(self.accept)
        buttons.addWidget(shutdown_button)
        layout.addLayout(buttons)

        self.setStyleSheet("""
            QDialog {
                background: #1a2230;
                border: 3px solid #4a586d;
                border-radius: 22px;
                color: #f5f7fb;
            }
            QLabel#shutdownQuestion {
                color: #ffffff;
                font-family: DejaVu Sans;
                font-size: 30px;
                font-weight: bold;
            }
            QPushButton {
                border: 3px solid transparent;
                border-radius: 18px;
                color: #ffffff;
                font-family: DejaVu Sans;
                font-size: 25px;
                font-weight: bold;
            }
            QPushButton#cancelShutdownButton {
                background: #334258;
            }
            QPushButton#cancelShutdownButton:default {
                border-color: #8fc9ff;
            }
            QPushButton#cancelShutdownButton:pressed {
                background: #465a77;
            }
            QPushButton#confirmShutdownButton {
                background: #c52f3c;
            }
            QPushButton#confirmShutdownButton:pressed {
                background: #df4452;
            }
        """)
        if self.compact_layout:
            self.setStyleSheet(self.styleSheet() + """
                QLabel#shutdownQuestion { font-size: 22px; }
                QPushButton { border-radius: 12px; font-size: 18px; }
            """)
        cancel_button.setFocus(Qt.OtherFocusReason)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = RadioWindow()
    window.showFullScreen()
    sys.exit(app.exec_())
