#!/usr/bin/python3
"""
Web-Player nur – ohne Qt-GUI.
Nutze dies, wenn der Raspberry Pi headless läuft oder du nur
die Browser-Oberfläche brauchst.

    python3 web_player.py
    python3 web_player.py --port 8080
"""

import argparse
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOGO_DIR = BASE_DIR / "logos"
STATION_JSON_FILE = BASE_DIR / "radio_station_list.json"
STATION_CSV_FILE = BASE_DIR / "radio_station_list.csv"
IMMICH_CONFIG_FILE = BASE_DIR / "immich_config.json"

from station_store import StationStore
from web_player_server import WebPlayerServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
LOGGER = logging.getLogger("web_player")


def main():
    parser = argparse.ArgumentParser(description="Webradio Web-Player (ohne Qt-GUI)")
    parser.add_argument("--port", type=int, default=8089, help="HTTP-Port (Standard: 8089)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind-Adresse (Standard: 0.0.0.0)")
    args = parser.parse_args()

    store = StationStore(STATION_JSON_FILE, STATION_CSV_FILE, LOGO_DIR)
    stations = store.list_stations()
    LOGGER.info("%d Sender geladen", len(stations))

    server = WebPlayerServer(
        store=store,
        logo_dir=LOGO_DIR,
        base_dir=BASE_DIR,
        immich_config=IMMICH_CONFIG_FILE,
        host=args.host,
        port=args.port,
    )
    server.start()
    LOGGER.info("Web-Player unter http://<pi-ip>:%d erreichbar", args.port)
    LOGGER.info("Druecke STRG+C zum Beenden")

    try:
        while True:
            import time
            time.sleep(1)
    except KeyboardInterrupt:
        LOGGER.info("Beende Server …")
        server.stop()


if __name__ == "__main__":
    main()
