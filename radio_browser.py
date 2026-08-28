import json
import logging
import os
import random
import re
from html.parser import HTMLParser
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PyQt5.QtGui import QImage

from station_store import normalize_station_name, stable_station_id, station_slug


SERVER_DISCOVERY_HOST = "all.api.radio-browser.info"
FALLBACK_SERVERS = (
    "de1.api.radio-browser.info",
    "de2.api.radio-browser.info",
    "fi1.api.radio-browser.info",
)
USER_AGENT = "webradio-pi/1.0"
SEARCH_LIMIT = 100
MAX_IMAGE_BYTES = 3 * 1024 * 1024
API_TIMEOUT_SECONDS = 5
MAX_SERVER_ATTEMPTS = 3
SEARCH_CACHE_SECONDS = 180
SERVER_CACHE_SECONDS = 300
LOGGER = logging.getLogger(__name__)
RADIO_DE_API_SEARCH_URL = "https://prod.radio-api.net/stations/search?query={}"
RADIO_DE_SEARCH_URLS = (
    "https://www.radio.de/search?query={}",
    "https://www.radio.de/suche?query={}",
)
RADIO_DE_CONFIRMED = {
    "ndr2": (
        "https://www.radio.de/s/ndr2",
        "https://www.radio.de/175/ndr2.png?version=7c4a25055782174c8b455d200c528a08ed337e68",
    ),
}
_state_lock = threading.RLock()
_last_successful_server = None
_server_cache = (0.0, [])
_search_cache = {}


class RadioBrowserUnavailable(RuntimeError):
    pass


def _request(url, headers=None, timeout=8):
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=request_headers), timeout=timeout
    )


def search_api(query):
    global _last_successful_server
    query = str(query).strip()[:120]
    if not query:
        return []
    cache_key = query.casefold()
    with _state_lock:
        cached = _search_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] <= SEARCH_CACHE_SECONDS:
            return [dict(record) for record in cached[1]]

    parameters = urllib.parse.urlencode({
        "name": query,
        "hidebroken": "true",
        "order": "votes",
        "reverse": "true",
        "limit": str(SEARCH_LIMIT),
    })
    request_path = "/json/stations/search?" + parameters
    for server in available_servers()[:MAX_SERVER_ATTEMPTS]:
        url = "https://{}{}".format(server, request_path)
        try:
            with _request(url, timeout=API_TIMEOUT_SECONDS) as response:
                payload = response.read(2 * 1024 * 1024)
            result = json.loads(payload.decode("utf-8"))
            if not isinstance(result, list):
                raise ValueError("unexpected JSON document")
            with _state_lock:
                _last_successful_server = server
                _search_cache[cache_key] = (time.monotonic(), [dict(item) for item in result])
            return result
        except urllib.error.HTTPError as error:
            LOGGER.warning("Radio-Browser server %s returned HTTP %s", server, error.code)
            if error.code != 429 and not 500 <= error.code <= 599:
                break
        except (TimeoutError, ConnectionResetError, urllib.error.URLError, socket.gaierror) as error:
            LOGGER.warning("Radio-Browser server %s failed: %r", server, error)
        except (OSError, ValueError, TypeError) as error:
            LOGGER.warning("Radio-Browser server %s returned unusable data: %r", server, error)
    raise RadioBrowserUnavailable(
        "Die Sendersuche ist momentan nicht erreichbar. Bitte erneut versuchen."
    )


def available_servers():
    global _server_cache
    now = time.monotonic()
    with _state_lock:
        cached_at, cached_servers = _server_cache
        last_successful = _last_successful_server
    if cached_servers and now - cached_at <= SERVER_CACHE_SECONDS:
        servers = list(cached_servers)
    else:
        servers = discover_servers()
        if not servers:
            servers = list(FALLBACK_SERVERS)
        with _state_lock:
            _server_cache = (now, list(servers))

    servers = list(dict.fromkeys(server.casefold() for server in servers if server))
    random.shuffle(servers)
    if last_successful in servers:
        servers.remove(last_successful)
        servers.insert(0, last_successful)
    return servers


def discover_servers():
    try:
        addresses = {
            result[4][0]
            for result in socket.getaddrinfo(
                SERVER_DISCOVERY_HOST, 443, type=socket.SOCK_STREAM
            )
        }
    except (OSError, socket.gaierror) as error:
        LOGGER.warning("Radio-Browser DNS discovery failed: %r", error)
        return []

    servers = []
    for address in addresses:
        try:
            hostname = socket.gethostbyaddr(address)[0].rstrip(".").casefold()
        except (OSError, socket.gaierror) as error:
            LOGGER.warning("Radio-Browser reverse DNS failed for %s: %r", address, error)
            continue
        if (
            hostname.endswith(".api.radio-browser.info")
            and hostname != SERVER_DISCOVERY_HOST
            and hostname not in servers
        ):
            servers.append(hostname)
    random.shuffle(servers)
    return servers


def group_logical_stations(records):
    groups = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        name = str(record.get("name", "")).strip()
        key = normalize_station_name(name)
        if not name or not key:
            continue
        groups.setdefault(key, []).append(record)

    logical = []
    for key, matches in groups.items():
        representative = max(
            matches,
            key=lambda item: (
                _integer(item.get("votes")),
                _integer(item.get("clickcount")),
                -len(str(item.get("name", ""))),
            ),
        )
        favicons = [
            str(match.get("favicon", "")).strip()
            for match in matches
            if _is_http_url(match.get("favicon"))
        ]
        logical.append({
            "key": key,
            "name": str(representative.get("name", "")).strip(),
            "favicon": favicons[0] if favicons else "",
            "matches": matches,
            "votes": _integer(representative.get("votes")),
        })
    logical.sort(key=lambda item: (-item["votes"], item["name"].casefold()))
    return logical


def search_radio_de(query):
    url = RADIO_DE_API_SEARCH_URL.format(urllib.parse.quote(str(query).strip()[:120]))
    try:
        with _request(url, timeout=API_TIMEOUT_SECONDS) as response:
            document = json.load(response)
    except Exception as error:
        LOGGER.warning("radio.de station search failed: %r", error)
        return []
    results = []
    for item in document.get("playables", []):
        if item.get("type") != "STATION" or not item.get("hasValidStreams"):
            continue
        streams = [
            stream.get("url", "").strip()
            for stream in item.get("streams", [])
            if stream.get("status") == "VALID" and _is_http_url(stream.get("url"))
        ]
        if not streams:
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        results.append({
            "key": normalize_station_name(name),
            "name": name,
            "favicon": str(item.get("logo300x300") or item.get("logo175x175") or "").strip(),
            "audio_url": streams[0],
            "source": "radio.de",
        })
    return results


def search_logical_stations(query):
    results = []
    try:
        results.extend(
            {key: group[key] for key in ("key", "name", "favicon")}
            for group in group_logical_stations(search_api(query))
        )
    except RadioBrowserUnavailable:
        pass
    existing = {result["key"] for result in results}
    results.extend(result for result in search_radio_de(query) if result["key"] not in existing)
    if not results:
        return []
    return results


def _integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _is_http_url(value):
    try:
        parsed = urllib.parse.urlsplit(str(value).strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except ValueError:
        return False


def _working_streams(matches):
    streams = []
    seen_urls = set()
    for match in matches:
        if _integer(match.get("lastcheckok")) != 1:
            continue
        url = str(match.get("url_resolved") or match.get("url") or "").strip()
        if not _is_http_url(url) or url in seen_urls:
            continue
        bitrate = _integer(match.get("bitrate"))
        if bitrate < 0 or bitrate > 512:
            continue
        seen_urls.add(url)
        streams.append({"url": url, "bitrate": bitrate})
    return streams


def _https_score(url):
    return 1 if urllib.parse.urlsplit(url).scheme == "https" else 0


def _transient_score(url):
    lowered = url.casefold()
    suspicious = (
        "token=", "auth=", "signature=", "sig=", "expires=", "expiry=",
        "timestamp=", "time=", "session=", "jwt=", "hdnts=", "policy=",
    )
    return sum(marker in lowered for marker in suspicious)


def choose_audio_stream(matches):
    streams = _working_streams(matches)
    if not streams:
        raise ValueError("Für diesen Sender wurde kein funktionierender Stream gefunden.")
    streams.sort(
        key=lambda stream: (
            -stream["bitrate"],
            -_https_score(stream["url"]),
            _transient_score(stream["url"]),
            len(urllib.parse.urlsplit(stream["url"]).query),
            stream["url"],
        )
    )
    return streams[0]["url"]


def stream_has_icy_metadata(url):
    try:
        with _request(url, {"Icy-MetaData": "1"}, timeout=6) as response:
            interval = int(response.headers.get("icy-metaint"))
            return interval > 0
    except Exception:
        return False


def choose_metadata_stream(matches, audio_url):
    streams = _working_streams(matches)
    streams.sort(
        key=lambda stream: (
            stream["bitrate"] if stream["bitrate"] > 0 else 10000,
            -_https_score(stream["url"]),
            _transient_score(stream["url"]),
            stream["url"],
        )
    )
    for stream in streams:
        if stream_has_icy_metadata(stream["url"]):
            return stream["url"]
    return audio_url


def image_dimensions(data):
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        width, height = struct.unpack(">II", data[16:24])
        return width, height, ".png"
    if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
        width, height = struct.unpack("<HH", data[6:10])
        return width, height, ".gif"
    if len(data) >= 4 and data.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 9 <= len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            offset += 2
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(data):
                break
            length = struct.unpack(">H", data[offset:offset + 2])[0]
            if length < 2 or offset + length > len(data):
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF) and length >= 7:
                height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
                return width, height, ".jpg"
            offset += length
    return None


def _download_image(url, minimum_dimension=48):
    try:
        with _request(url, timeout=7) as response:
            data = response.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            return None
        dimensions = image_dimensions(data)
        if dimensions is None:
            return None
        width, height, extension = dimensions
        image = QImage.fromData(data)
        if image.isNull() or image.width() != width or image.height() != height:
            return None
        if (
            width < minimum_dimension or height < minimum_dimension
            or width > 10000 or height > 10000
        ):
            return None
        return data, width, height, extension
    except Exception:
        return None


def select_and_cache_logo(name, matches, logo_dir, base_dir):
    logo_dir = Path(logo_dir)
    base_dir = Path(base_dir)
    slug = station_slug(name)
    for extension in (".png", ".jpg", ".jpeg", ".gif"):
        existing = logo_dir / (slug + extension)
        if existing.is_file():
            try:
                data = existing.read_bytes()
            except OSError:
                continue
            if image_dimensions(data) is not None:
                return existing.relative_to(base_dir).as_posix()

    best = None
    favicon_urls = []
    for match in matches:
        favicon = str(match.get("favicon", "")).strip()
        if _is_http_url(favicon) and favicon not in favicon_urls:
            favicon_urls.append(favicon)
    for favicon in favicon_urls:
        downloaded = _download_image(favicon)
        if downloaded is None:
            continue
        data, width, height, extension = downloaded
        if best is None or width * height > best[1] * best[2]:
            best = data, width, height, extension
    if best is None:
        best = radio_de_logo(name)
    if best is None:
        return ""

    data, width, height, extension = best
    logo_dir.mkdir(parents=True, exist_ok=True)
    destination = logo_dir / (slug + extension)
    temporary = destination.with_suffix(
        destination.suffix + ".tmp." + str(threading.get_ident())
    )
    try:
        try:
            with temporary.open("wb") as output_file:
                output_file.write(data)
                output_file.flush()
                os.fsync(output_file.fileno())
            os.replace(str(temporary), str(destination))
        except OSError as error:
            LOGGER.warning("Logo could not be cached for %s: %r", name, error)
            return ""
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination.relative_to(base_dir).as_posix()


class RadioDePageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_h1 = False
        self.in_title = False
        self.in_json = False
        self.h1_parts = []
        self.title_parts = []
        self.current_json_parts = []
        self.json_documents = []
        self.images = []
        self.station_links = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "h1":
            self.in_h1 = True
        elif tag == "title":
            self.in_title = True
        elif tag == "script" and "ld+json" in attributes.get("type", "").casefold():
            self.in_json = True
            self.current_json_parts = []
        elif tag == "meta" and attributes.get("property", "").casefold() == "og:image":
            image = attributes.get("content", "").strip()
            if _is_http_url(image):
                self.images.append(image)
        elif tag == "a":
            href = attributes.get("href", "").strip()
            if href.startswith("/s/"):
                self.station_links.append(urllib.parse.urljoin("https://www.radio.de", href))

    def handle_endtag(self, tag):
        if tag == "h1":
            self.in_h1 = False
        elif tag == "title":
            self.in_title = False
        elif tag == "script" and self.in_json:
            self.in_json = False
            self.json_documents.append("".join(self.current_json_parts))

    def handle_data(self, data):
        if self.in_h1:
            self.h1_parts.append(data)
        if self.in_title:
            self.title_parts.append(data)
        if self.in_json:
            self.current_json_parts.append(data)


def _radio_de_name_variants(name):
    normalized = normalize_station_name(name)
    variants = {normalized}
    if normalized.startswith("radio") and len(normalized) > 5:
        variants.add(normalized[5:])
    return variants


def _radio_de_page_matches(name, parser):
    expected = _radio_de_name_variants(name)
    headings = [" ".join(parser.h1_parts).strip()]
    title = " ".join(parser.title_parts).strip()
    if title:
        title_prefix = re.split(r"[|:]", title, maxsplit=1)[0]
        title_prefix = re.sub(r"\s+Radio$", "", title_prefix, flags=re.IGNORECASE)
        headings.append(title_prefix)
    return any(
        _radio_de_name_variants(heading) & expected
        for heading in headings if heading
    )


def _json_image_urls(value):
    images = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in ("image", "logo", "thumbnailurl"):
                if isinstance(child, str) and _is_http_url(child):
                    images.append(child)
                else:
                    images.extend(_json_image_urls(child))
            else:
                images.extend(_json_image_urls(child))
    elif isinstance(value, list):
        for child in value:
            images.extend(_json_image_urls(child))
    return images


def _read_radio_de_page(url):
    with _request(url, timeout=5) as response:
        document = response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
    parser = RadioDePageParser()
    parser.feed(document)
    for raw_json in parser.json_documents:
        try:
            parser.images.extend(_json_image_urls(json.loads(raw_json)))
        except (TypeError, ValueError):
            continue
    return parser


def _radio_de_candidate_pages(name):
    normalized = normalize_station_name(name)
    candidates = []
    confirmed = RADIO_DE_CONFIRMED.get(normalized)
    if confirmed:
        candidates.append(confirmed[0])
    slugs = [normalized]
    if normalized.startswith("radio") and len(normalized) > 5:
        slugs.append(normalized[5:])
    candidates.extend("https://www.radio.de/s/" + slug for slug in slugs if slug)

    encoded_name = urllib.parse.quote_plus(name)
    for search_url in RADIO_DE_SEARCH_URLS:
        try:
            search_page = _read_radio_de_page(search_url.format(encoded_name))
            candidates.extend(search_page.station_links[:12])
        except Exception as error:
            LOGGER.warning("radio.de search failed: %r", error)
    return list(dict.fromkeys(candidates))


def radio_de_logo(name):
    normalized = normalize_station_name(name)
    confirmed = RADIO_DE_CONFIRMED.get(normalized)
    for page_url in _radio_de_candidate_pages(name):
        try:
            parser = _read_radio_de_page(page_url)
            if not _radio_de_page_matches(name, parser):
                continue
            image_urls = list(dict.fromkeys(parser.images))
            if confirmed and page_url == confirmed[0]:
                image_urls.append(confirmed[1])
            best = None
            for image_url in image_urls:
                downloaded = _download_image(image_url, minimum_dimension=64)
                if downloaded is None:
                    continue
                if best is None or downloaded[1] * downloaded[2] > best[1] * best[2]:
                    best = downloaded
            if best is not None:
                return best
        except Exception as error:
            LOGGER.warning("radio.de logo lookup failed for %s: %r", page_url, error)
    if confirmed:
        return _download_image(confirmed[1], minimum_dimension=64)
    return None


def build_station(query, logical_key, logo_dir, base_dir):
    try:
        groups = group_logical_stations(search_api(query))
    except RadioBrowserUnavailable:
        groups = []
    group = next((candidate for candidate in groups if candidate["key"] == logical_key), None)
    if group is not None:
        try:
            audio_url = choose_audio_stream(group["matches"])
            metadata_url = choose_metadata_stream(group["matches"], audio_url)
            logo_file = select_and_cache_logo(group["name"], group["matches"], logo_dir, base_dir)
            return {
                "id": stable_station_id(group["name"], "radio-browser"),
                "name": group["name"],
                "audio_url": audio_url,
                "metadata_url": metadata_url,
                "logo_file": logo_file,
                "source": "radio-browser",
            }
        except ValueError:
            pass

    radio_de_station = next(
        (station for station in search_radio_de(query) if station["key"] == logical_key),
        None,
    )
    if radio_de_station is None:
        raise ValueError("Der ausgewählte Sender wurde nicht mehr gefunden.")
    logo_matches = [{"favicon": radio_de_station["favicon"]}] if radio_de_station["favicon"] else []
    logo_file = select_and_cache_logo(
        radio_de_station["name"], logo_matches, logo_dir, base_dir
    )
    return {
        "id": stable_station_id(radio_de_station["name"], "radio.de"),
        "name": radio_de_station["name"],
        "audio_url": radio_de_station["audio_url"],
        "metadata_url": radio_de_station["audio_url"],
        "logo_file": logo_file,
        "source": "radio.de",
    }
