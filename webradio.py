#!/usr/bin/python3

import json
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
from PyQt5.QtCore import QEvent, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QPainter, QPixmap
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
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from station_store import StationStore, station_slug
from web_admin import RadioAdminServer


BASE_DIR = Path(__file__).resolve().parent
STATION_FILE = BASE_DIR / "radio_station_list.csv"
STATION_JSON_FILE = BASE_DIR / "radio_station_list.json"
LOGO_DIR = BASE_DIR / "logos"
IMMICH_CONFIG_FILE = BASE_DIR / "immich_config.json"
SLIDESHOW_IDLE_MS = 2 * 60 * 1000
SLIDE_DURATION_SECONDS = 90


class ImmichApiError(Exception):
    def __init__(self, status_code, endpoint, response_text):
        super().__init__(response_text)
        self.status_code = status_code
        self.endpoint = endpoint
        self.response_text = response_text

    def __str__(self):
        return f"HTTP {self.status_code} bei {self.endpoint}: {self.response_text}"


class ImmichSlideshowLoader(QThread):
    image_received = pyqtSignal(bytes)
    status_received = pyqtSignal(str)

    def __init__(self, config_file, parent=None):
        super().__init__(parent)
        self.config_file = config_file
        self.stop_event = threading.Event()
        self.navigation_event = threading.Event()
        self.navigation_lock = threading.Lock()
        self.navigation_step = 0

    def stop(self):
        self.stop_event.set()
        self.navigation_event.set()

    def navigate(self, direction):
        with self.navigation_lock:
            self.navigation_step += direction
        self.navigation_event.set()

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

    def load_asset_ids(self, base_url, api_key):
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

        image_ids = []
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
            image_ids.extend(item["id"] for item in items if item.get("id"))
            next_page = assets.get("nextPage")
            if not next_page or not items:
                break
            page = int(next_page)
        if not image_ids:
            raise ValueError("Immich-Album WEB Radio enthält keine Bilder")
        print(
            f"Immich API: POST {search_url} -> {len(image_ids)} Album-Bilder geladen",
            file=sys.stderr,
        )
        return image_ids

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
            asset_ids = self.load_asset_ids(base_url, api_key)
            if not asset_ids:
                self.status_received.emit("Keine Bilder in Immich gefunden")
                return
            random.shuffle(asset_ids)
            index = 0
            thumbnail_cache = {}
            while not self.stop_event.is_set():
                asset_id = asset_ids[index]
                stage = "Vorschaubild"
                if asset_id in thumbnail_cache:
                    current_image = thumbnail_cache.pop(asset_id)
                else:
                    current_image = self.safe_load_thumbnail(base_url, api_key, asset_id)
                if current_image is None:
                    index = (index + 1) % len(asset_ids)
                    continue
                if self.stop_event.is_set():
                    return
                self.image_received.emit(current_image)

                next_index = (index + 1) % len(asset_ids)
                next_id = asset_ids[next_index]
                if next_id not in thumbnail_cache:
                    next_image = self.safe_load_thumbnail(base_url, api_key, next_id)
                    if next_image is not None:
                        thumbnail_cache[next_id] = next_image
                if self.stop_event.is_set():
                    return

                manually_navigated = self.navigation_event.wait(SLIDE_DURATION_SECONDS)
                self.navigation_event.clear()
                if self.stop_event.is_set():
                    return
                if manually_navigated:
                    with self.navigation_lock:
                        step = self.navigation_step
                        self.navigation_step = 0
                    index = (index + step) % len(asset_ids)
                else:
                    index = next_index
                thumbnail_cache = {
                    key: value for key, value in thumbnail_cache.items()
                    if key in {asset_ids[index], asset_ids[(index + 1) % len(asset_ids)]}
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
        self.slideshow_loader = None
        self.slideshow_loaders = set()
        self.slideshow_pixmap = None

        Gst.init(None)
        self.player = Gst.ElementFactory.make("playbin", "radio-player")
        if self.player is None:
            raise RuntimeError("GStreamer-Element 'playbin' konnte nicht erstellt werden")
        self.player.set_property("volume", 1.0)
        self.player.connect("source-setup", self.configure_gstreamer_source)
        self.gst_bus = self.player.get_bus()
        self.gst_bus_timer = QTimer(self)
        self.gst_bus_timer.timeout.connect(self.process_gstreamer_bus)
        self.gst_bus_timer.start(100)
        self.logo_cache = {}
        self.web_server = None
        self.stations_changed.connect(self.reload_stations)

        self.build_ui()
        self.apply_style()
        self.update_selection()

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
        except OSError:
            self.web_server = None

    @staticmethod
    def station_tuples(records):
        return [(station["name"], station["audio_url"]) for station in records]

    def build_ui(self):
        self.setWindowTitle("Webradio")
        self.setMinimumSize(760, 560)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_StyledBackground, True)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(16)

        now_card = QFrame()
        now_card.setObjectName("nowCard")
        now_layout = QHBoxLayout(now_card)
        now_layout.setContentsMargins(18, 14, 18, 14)
        now_layout.setSpacing(22)

        self.now_logo = QLabel()
        self.now_logo.setObjectName("logoTile")
        self.now_logo.setAlignment(Qt.AlignCenter)
        self.now_logo.setFixedSize(220, 145)
        now_layout.addWidget(self.now_logo)

        now_text = QVBoxLayout()
        caption = QLabel("JETZT LÄUFT")
        caption.setObjectName("caption")
        self.now_station = QLabel("–")
        self.now_station.setObjectName("nowStation")
        self.track_info = QLabel("Titelinformationen werden geladen …")
        self.track_info.setObjectName("trackInfo")
        self.track_info.setWordWrap(True)
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

        self.previous_button = QPushButton("‹")
        self.previous_button.setObjectName("carouselNavigation")
        self.previous_button.setMinimumSize(70, 100)
        self.previous_button.setMaximumWidth(76)
        self.previous_button.clicked.connect(lambda: self.browse(-1))
        self.carousel.watch(self.previous_button)
        chooser.addWidget(self.previous_button)

        self.station_buttons = []
        for offset in (-2, -1, 0, 1, 2):
            button = QToolButton()
            button.setText("–")
            button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            button.setProperty("carouselOffset", offset)
            button.setObjectName("stationCenter" if offset == 0 else "stationPreview")
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            button.clicked.connect(lambda checked=False, step=offset: self.select_carousel_station(step))
            self.carousel.watch(button)
            stretch = 4 if offset == 0 else (2 if abs(offset) == 1 else 1)
            chooser.addWidget(button, stretch)
            self.station_buttons.append(button)

        self.next_button = QPushButton("›")
        self.next_button.setObjectName("carouselNavigation")
        self.next_button.setMinimumSize(70, 100)
        self.next_button.setMaximumWidth(76)
        self.next_button.clicked.connect(lambda: self.browse(1))
        self.carousel.watch(self.next_button)
        chooser.addWidget(self.next_button)
        root.addWidget(self.carousel, 1)

        controls = QGridLayout()
        controls.setHorizontalSpacing(16)
        self.power_button = QPushButton("Ausschalten")
        self.power_button.setObjectName("powerButton")
        self.power_button.clicked.connect(self.confirm_shutdown)
        controls.addWidget(self.power_button, 0, 0)

        self.dark_button = QPushButton("Dunkel")
        self.dark_button.setObjectName("darkButton")
        self.dark_button.clicked.connect(self.show_dark_screen)
        controls.addWidget(self.dark_button, 0, 1)

        self.mode_button = QPushButton("Quelle: Webradio")
        self.mode_button.setObjectName("modeButton")
        self.mode_button.setCheckable(True)
        self.mode_button.clicked.connect(self.toggle_mode)
        controls.addWidget(self.mode_button, 0, 2)

        self.slideshow_button = QPushButton("Diashow")
        self.slideshow_button.setObjectName("slideshowButton")
        self.slideshow_button.clicked.connect(self.start_slideshow)
        controls.addWidget(self.slideshow_button, 0, 3)
        controls.setColumnStretch(0, 1)
        controls.setColumnStretch(1, 1)
        controls.setColumnStretch(2, 2)
        controls.setColumnStretch(3, 1)
        root.addLayout(controls)

        self.slideshow_overlay = SlideshowOverlay(self)
        self.slideshow_overlay.setObjectName("slideshowOverlay")
        self.slideshow_overlay.setAttribute(Qt.WA_StyledBackground, True)
        self.slideshow_image = QLabel("Bilder werden geladen …", self.slideshow_overlay)
        self.slideshow_image.setObjectName("slideshowImage")
        self.slideshow_image.setAlignment(Qt.AlignCenter)
        self.slideshow_previous = QPushButton("‹", self.slideshow_overlay)
        self.slideshow_previous.setObjectName("slideshowNavigation")
        self.slideshow_previous.setFixedSize(88, 150)
        self.slideshow_previous.clicked.connect(lambda: self.navigate_slideshow(-1))
        self.slideshow_next = QPushButton("›", self.slideshow_overlay)
        self.slideshow_next.setObjectName("slideshowNavigation")
        self.slideshow_next.setFixedSize(88, 150)
        self.slideshow_next.clicked.connect(lambda: self.navigate_slideshow(1))
        self.slideshow_overlay.activated.connect(self.stop_slideshow)
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
            QPushButton#carouselNavigation {
                background: rgba(25, 32, 44, 215);
                border: 1px solid #4b596c;
                border-radius: 22px;
                color: #ffffff;
                font-size: 62px;
                font-weight: bold;
                padding: 0;
            }
            QPushButton#carouselNavigation:pressed {
                background: rgba(58, 76, 100, 235);
            }
            QPushButton#powerButton {
                background: #b6323b;
                font-size: 22px;
                min-height: 78px;
            }
            QPushButton#darkButton {
                background: #283242;
                font-size: 22px;
                min-height: 78px;
            }
            QPushButton#modeButton {
                background: #1769aa;
                font-size: 25px;
                min-height: 78px;
            }
            QPushButton#modeButton:checked { background: #7656b5; }
            QPushButton#slideshowButton {
                background: #287a58;
                font-size: 22px;
                min-height: 78px;
            }
            QWidget#slideshowOverlay { background: #000000; }
            QLabel#slideshowImage { background: #000000; color: #c5ccda; font-size: 24px; }
            QPushButton#slideshowNavigation {
                background: rgba(35, 45, 60, 220);
                border: 2px solid #8fc9ff;
                border-radius: 30px;
                color: #ffffff;
                font-size: 76px;
                padding: 0;
            }
            QPushButton#slideshowNavigation:pressed { background: #3a76af; }
            QWidget#darkOverlay { background: #000000; }
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
        self.idle_timer.stop()
        self.stop_slideshow(restart_idle_timer=False)
        for loader in tuple(self.slideshow_loaders):
            loader.stop()
            loader.wait(31000)
        self.gst_bus_timer.stop()
        self.player.set_state(Gst.State.NULL)
        if self.web_server is not None:
            self.web_server.stop()
            self.web_server = None

    def reset_idle_timer(self):
        if not self.dark_overlay.isVisible() and not self.slideshow_overlay.isVisible():
            self.idle_timer.start(SLIDESHOW_IDLE_MS)

    def start_slideshow(self):
        if self.dark_overlay.isVisible() or self.slideshow_loader is not None:
            return
        self.idle_timer.stop()
        self.slideshow_pixmap = None
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

    def stop_slideshow(self, restart_idle_timer=True):
        loader = self.slideshow_loader
        self.slideshow_loader = None
        if loader is not None:
            loader.stop()
        self.slideshow_overlay.hide()
        self.slideshow_pixmap = None
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

    def show_slideshow_image(self, image_data):
        if self.slideshow_loader is None or self.dark_overlay.isVisible():
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(image_data):
            self.slideshow_pixmap = pixmap
            self.slideshow_image.setText("")
            self.slideshow_overlay.setGeometry(self.rect())
            self.layout_slideshow_overlay()
            self.slideshow_overlay.raise_()
            self.slideshow_overlay.show()

    def layout_slideshow_overlay(self):
        if not hasattr(self, "slideshow_image"):
            return
        image_width = min(800, self.slideshow_overlay.width())
        image_height = min(600, self.slideshow_overlay.height())
        image_x = (self.slideshow_overlay.width() - image_width) // 2
        image_y = (self.slideshow_overlay.height() - image_height) // 2
        self.slideshow_image.setGeometry(image_x, image_y, image_width, image_height)
        button_y = (self.slideshow_overlay.height() - self.slideshow_previous.height()) // 2
        self.slideshow_previous.move(18, button_y)
        self.slideshow_next.move(
            self.slideshow_overlay.width() - self.slideshow_next.width() - 18,
            button_y,
        )
        self.slideshow_previous.raise_()
        self.slideshow_next.raise_()
        self.scale_slideshow_image()

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
        if offset == 0:
            self.start_selected_station()
            return
        self.browse(offset)

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
        for button, offset in zip(self.station_buttons, (-2, -1, 0, 1, 2)):
            # Bei weniger als fünf Sendern keine Station doppelt anzeigen.
            if offset not in visible_offsets:
                button.setText("")
                button.setEnabled(False)
                continue

            index = (self.selected_index + offset) % station_count
            name = self.stations[index][0]
            button.setEnabled(True)
            self.set_card_logo(button, name, offset)
            if offset == 0:
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
                self.player.set_state(Gst.State.PLAYING)
                self.radio_paused = False
            else:
                self.player.set_state(Gst.State.PAUSED)
                self.radio_paused = True
            self.update_selection()
            return
        if self.mode == "mpd":
            self.stop_mpd()
            self.mode = "radio"
            self.mode_button.setChecked(False)
            self.mode_button.setText("Quelle: Webradio")

        name, url = self.stations[self.selected_index]
        self.player.set_state(Gst.State.NULL)
        while self.gst_bus.pop() is not None:
            pass
        self.current_artist = ""
        self.current_title = ""
        self.player.set_property("uri", url)
        self.player.set_state(Gst.State.PLAYING)
        self.playing_index = self.selected_index
        self.radio_paused = False
        self.now_station.setText(name)
        self.track_info.setText("Keine Titelinformationen")
        self.set_logo(name)
        self.update_selection()

    def toggle_mode(self, use_mpd):
        if use_mpd:
            self.player.set_state(Gst.State.NULL)
            self.mode = "mpd"
            self.mode_button.setText("Quelle: MPD")
            self.now_station.setText("MPD")
            self.track_info.setText("Wiedergabe über Music Player Daemon")
            self.set_logo("MPD")
            self.start_mpd()
        else:
            self.stop_mpd()
            self.mode = "radio"
            self.mode_button.setText("Quelle: Webradio")
            if self.playing_index is not None:
                self.selected_index = self.playing_index
                self.radio_paused = True
                self.start_selected_station()
            elif self.stations:
                self.start_selected_station()
        self.update_selection()

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
        if not self.with_mpd(lambda client: client.play()):
            self.track_info.setText("MPD ist nicht erreichbar")

    def stop_mpd(self):
        self.with_mpd(lambda client: client.stop())

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
        logo_size = QSize(96, 76)
        canvas = QPixmap(logo_size)
        canvas.fill(QColor("#f3f5f8"))
        scaled = pixmap.scaled(
            86, 66, Qt.KeepAspectRatio, Qt.SmoothTransformation
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
        self.setFixedSize(620, 300)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QVBoxLayout(self)
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
        cancel_button.setMinimumSize(220, 90)
        cancel_button.setDefault(True)
        cancel_button.setAutoDefault(True)
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(cancel_button)

        shutdown_button = QPushButton("Ausschalten")
        shutdown_button.setObjectName("confirmShutdownButton")
        shutdown_button.setMinimumSize(220, 90)
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
                background: #e04450;
            }
        """)
        cancel_button.setFocus(Qt.OtherFocusReason)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = RadioWindow()
    window.showFullScreen()
    sys.exit(app.exec_())
