import json
import logging
import mimetypes
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from radio_browser import (
    RadioBrowserUnavailable,
    build_station,
    search_logical_stations,
    select_and_cache_logo,
)
from station_store import normalize_station_name, stable_station_id


LOGGER = logging.getLogger(__name__)
SEARCH_ERROR = "Die Sendersuche ist momentan nicht erreichbar. Bitte erneut versuchen."
ACTION_ERROR = "Die Aktion konnte nicht abgeschlossen werden. Bitte erneut versuchen."

PAGE_STYLE = """
* { box-sizing: border-box; }
body { margin: 0; background: #10141c; color: #f5f7fb; font-family: sans-serif; }
main { max-width: 920px; margin: auto; padding: 18px; }
h1 { font-size: 2rem; margin: 8px 0 20px; } h2 { margin-top: 30px; }
.search { display: flex; gap: 10px; }
.manual { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.manual button { grid-column: 1 / -1; }
.hint { color: #c5ccda; }
input, button { min-height: 52px; border: 0; border-radius: 13px; font-size: 1.05rem; }
input { min-width: 0; flex: 1; padding: 0 15px; background: #fff; color: #111; }
button { padding: 9px 17px; background: #1769aa; color: #fff; font-weight: bold; cursor: pointer; }
button:disabled { background: #4a5260; color: #c6cad0; cursor: default; }
button.danger { background: #b6323b; } button.order { min-width: 56px; font-size: 1.35rem; background: #334258; }
.card { display: flex; align-items: center; gap: 14px; margin: 12px 0; padding: 13px;
        background: #1a2230; border: 1px solid #2b374a; border-radius: 16px; }
.logo { flex: 0 0 72px; width: 72px; height: 72px; border-radius: 12px; object-fit: contain; background: #273247; }
.placeholder { display: flex; align-items: center; justify-content: center; padding: 6px;
               color: #fff; font-weight: bold; text-align: center; overflow: hidden; }
.name { flex: 1; min-width: 0; font-size: 1.22rem; font-weight: bold; overflow-wrap: anywhere; }
.actions { display: flex; gap: 8px; }
.message { padding: 13px; border-radius: 12px; background: #214d36; }
.message.error { background: #6e2930; } .hidden { display: none; }
@media (max-width: 600px) {
  main { padding: 12px; } h1 { font-size: 1.55rem; }
  .card { align-items: stretch; flex-wrap: wrap; } .name { align-self: center; font-size: 1.1rem; }
  .actions { flex-basis: 100%; } .actions button { flex: 1; } .search { flex-direction: column; }
}
"""

PAGE_TEMPLATE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Webradio-Sender</title><style>__STYLE__</style></head><body><main>
<h1>Webradio-Sender verwalten</h1>
<p id="message" class="message hidden" role="status"></p>
<form class="search" id="searchForm">
<input id="searchInput" placeholder="Sender suchen" aria-label="Sender suchen" required autofocus>
<button id="searchButton" type="submit">Suchen</button></form>
<p class="hint">Weltweite Suche. Falls ein Sender fehlt: <a id="radioDeLink" href="https://www.radio.de/" target="_blank" rel="noopener">bei radio.de suchen</a> und unten manuell eintragen.</p>
<section id="searchSection" class="hidden"><h2>Suchergebnisse</h2><div id="searchResults"></div></section>
<section><h2>Sender manuell hinzufügen</h2><form class="manual" id="manualForm">
<input name="name" placeholder="Sendername" required>
<input name="audio_url" type="url" placeholder="Stream-URL (http/https)" required>
<input name="metadata_url" type="url" placeholder="Metadaten-URL (optional)">
<input name="logo_url" type="url" placeholder="Logo-URL (optional)">
<button id="manualButton" type="submit">Manuell speichern</button></form></section>
<section><h2>Gespeicherte Sender</h2><div id="stationList"></div></section>
</main><script>
const message=document.getElementById('message'), searchForm=document.getElementById('searchForm');
const searchInput=document.getElementById('searchInput'), searchButton=document.getElementById('searchButton');
const searchSection=document.getElementById('searchSection'), searchResults=document.getElementById('searchResults');
const stationList=document.getElementById('stationList');
const manualForm=document.getElementById("manualForm"), manualButton=document.getElementById("manualButton");
const radioDeLink=document.getElementById("radioDeLink");
let currentQuery='', latestResults=[], existingKeys=new Set(), existingLogos=new Map();

function setMessage(text,isError=false){
  message.textContent=text; message.classList.toggle('hidden',!text);
  message.classList.toggle('error',Boolean(text)&&isError);
}
function placeholder(name){
  const tile=document.createElement('div'); tile.className='logo placeholder';
  tile.textContent=name.split(/\\s+/).filter(Boolean).slice(0,3).map(word=>word[0]).join('').toUpperCase()||'RADIO';
  return tile;
}
function logo(source,name){
  if(!source)return placeholder(name); const image=document.createElement('img');
  image.className='logo'; image.alt=''; image.src=source;
  image.addEventListener('error',()=>image.replaceWith(placeholder(name)),{once:true}); return image;
}
function cardBase(item){
  const card=document.createElement('article'); card.className='card';
  card.append(logo(item.logo_url||item.favicon||'',item.name));
  const name=document.createElement('div'); name.className='name'; name.textContent=item.name; card.append(name); return card;
}
async function requestJson(url,options={}){
  const response=await fetch(url,Object.assign({cache:'no-store'},options)); let data={};
  try{data=await response.json();}catch(_){ }
  if(!response.ok||data.ok===false)throw new Error(data.error||'__ACTION_ERROR__'); return data;
}
function actionButton(text,className,disabled,handler){
  const button=document.createElement('button'); button.type='button'; button.textContent=text;
  button.className=className; button.disabled=disabled; button.addEventListener('click',()=>handler(button)); return button;
}
function renderStations(stations){
  existingKeys=new Set(stations.map(station=>station.key)); const fragment=document.createDocumentFragment();
  existingLogos=new Map(stations.map(station=>[station.key,station.logo_url||'']));
  stations.forEach((station,index)=>{
    const card=cardBase(station), actions=document.createElement('div'); actions.className='actions';
    const up=actionButton('↑','order',index===0,b=>mutate(b,'/api/move',{id:station.id,direction:'up'}));
    up.setAttribute('aria-label','Nach oben');
    const down=actionButton('↓','order',index===stations.length-1,b=>mutate(b,'/api/move',{id:station.id,direction:'down'}));
    down.setAttribute('aria-label','Nach unten');
    const startup=actionButton(station.startup?"Startsender ✓":"Als Startsender","",station.startup,
      b=>mutate(b,"/api/startup",{id:station.id}));
    const remove=actionButton('Löschen','danger',stations.length<=1,b=>{
      if(window.confirm('Sender wirklich löschen?'))mutate(b,'/api/delete',{id:station.id});
    });
    actions.append(up,down,startup,remove); card.append(actions); fragment.append(card);
  });
  stationList.replaceChildren(fragment); if(latestResults.length)renderSearchResults(latestResults);
}
function renderSearchResults(results){
  latestResults=results; const fragment=document.createDocumentFragment();
  if(!results.length){const empty=document.createElement('p');empty.textContent='Keine passenden Sender gefunden.';fragment.append(empty);}
  results.forEach(result=>{
    const present=existingKeys.has(result.key), shown=Object.assign({},result);
    if(present&&existingLogos.get(result.key))shown.logo_url=existingLogos.get(result.key);
    const card=cardBase(shown);
    const add=actionButton(present?'Bereits vorhanden':'Hinzufügen','',present,
      b=>mutate(b,'/api/add',{query:currentQuery,key:result.key},true));
    card.append(add); fragment.append(card);
  });
  searchResults.replaceChildren(fragment); searchSection.classList.remove('hidden');
}
async function refreshStations(showErrors=false){
  try{const data=await requestJson('/api/stations');renderStations(data.stations);}
  catch(error){if(showErrors)throw error;}
}
async function runSearch(query){
  currentQuery=query.trim(); if(!currentQuery)return;
  radioDeLink.href="https://www.radio.de/search?query="+encodeURIComponent(currentQuery);
  searchButton.disabled=true; searchInput.disabled=true; searchButton.textContent='Suche …';
  try{const data=await requestJson('/api/search?q='+encodeURIComponent(currentQuery));
      await refreshStations(false);renderSearchResults(data.results);setMessage('');}
  catch(_){setMessage('__SEARCH_ERROR__',true);}
  finally{searchButton.disabled=false;searchInput.disabled=false;searchButton.textContent='Suchen';}
}
async function mutate(button,path,values,refreshSearch=false){
  button.disabled=true;
  try{
    const body=new URLSearchParams(values);
    const data=await requestJson(path,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded;charset=UTF-8'},body:body.toString()});
    await refreshStations(true); if(refreshSearch&&currentQuery)await runSearch(currentQuery);
    setMessage(data.message||'Änderung gespeichert.');
  }catch(error){setMessage(error.message||'__ACTION_ERROR__',true);}
  finally{button.disabled=false;}
}
searchForm.addEventListener('submit',event=>{event.preventDefault();runSearch(searchInput.value);});
manualForm.addEventListener("submit",async event=>{
  event.preventDefault(); manualButton.disabled=true;
  try{
    const body=new URLSearchParams(new FormData(manualForm));
    const data=await requestJson("/api/add-manual",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded;charset=UTF-8"},body:body.toString()});
    manualForm.reset(); await refreshStations(true); setMessage(data.message||"Sender gespeichert.");
  }catch(error){setMessage(error.message||"__ACTION_ERROR__",true);}
  finally{manualButton.disabled=false;}
});
refreshStations(true).catch(()=>setMessage('__ACTION_ERROR__',true));
window.setInterval(()=>refreshStations(false),5000);
</script></body></html>"""


def page_html():
    return (PAGE_TEMPLATE.replace("__STYLE__", PAGE_STYLE)
            .replace("__SEARCH_ERROR__", SEARCH_ERROR)
            .replace("__ACTION_ERROR__", ACTION_ERROR).encode("utf-8"))


class RadioAdminServer:
    def __init__(self, store, logo_dir, base_dir, on_change, host="0.0.0.0", port=8088):
        self.store, self.logo_dir, self.base_dir = store, Path(logo_dir), Path(base_dir)
        self.on_change, self.host, self.port = on_change, host, port
        self.httpd = self.thread = None

    def start(self):
        self.httpd = ThreadingHTTPServer((self.host, self.port), self._handler_class())
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       name="webradio-webverwaltung", daemon=True)
        self.thread.start()

    def stop(self):
        server, self.httpd = self.httpd, None
        if server is not None:
            server.shutdown(); server.server_close()
        thread, self.thread = self.thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def _handler_class(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(parsed.query)
                if parsed.path == "/": self.send_html(page_html())
                elif parsed.path == "/api/stations": self.send_json({"ok": True, "stations": self.station_payload()})
                elif parsed.path == "/api/search": self.api_search(query.get("q", [""])[0])
                elif parsed.path.startswith("/assets/"):
                    self.serve_asset(urllib.parse.unquote(parsed.path[len("/assets/"):]))
                else: self.send_error(404)

            def do_POST(self):
                parsed, form = urllib.parse.urlsplit(self.path), self.read_form()
                try:
                    if parsed.path == "/api/add":
                        station = build_station(form.get("query", [""])[0], form.get("key", [""])[0],
                                                owner.logo_dir, owner.base_dir)
                        owner.store.add_station(station); owner.on_change()
                        self.send_json({"ok": True, "message": "Sender hinzugefügt."})
                    elif parsed.path == "/api/add-manual":
                        name = form.get("name", [""])[0].strip()
                        audio_url = form.get("audio_url", [""])[0].strip()
                        metadata_url = form.get("metadata_url", [""])[0].strip() or audio_url
                        logo_url = form.get("logo_url", [""])[0].strip()
                        if not name:
                            raise ValueError("Bitte einen Sendernamen eingeben.")
                        for label, url in (("Stream-URL", audio_url), ("Metadaten-URL", metadata_url)):
                            parsed_url = urllib.parse.urlsplit(url)
                            if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
                                raise ValueError(label + " ist keine gültige HTTP-/HTTPS-Adresse.")
                        if logo_url:
                            parsed_logo = urllib.parse.urlsplit(logo_url)
                            if parsed_logo.scheme not in ("http", "https") or not parsed_logo.netloc:
                                raise ValueError("Die Logo-URL ist ungültig.")
                        logo_file = select_and_cache_logo(
                            name,
                            [{"favicon": logo_url}] if logo_url else [],
                            owner.logo_dir,
                            owner.base_dir,
                        )
                        owner.store.add_station({
                            "id": stable_station_id(name, "manual"),
                            "name": name,
                            "audio_url": audio_url,
                            "metadata_url": metadata_url,
                            "logo_file": logo_file,
                            "source": "manual",
                        })
                        owner.on_change()
                        self.send_json({"ok": True, "message": "Sender manuell hinzugefügt."})
                    elif parsed.path == "/api/startup":
                        owner.store.set_startup_station(form.get("id", [""])[0])
                        owner.on_change()
                        self.send_json({"ok": True, "message": "Startsender gespeichert."})
                    elif parsed.path == "/api/delete":
                        owner.store.delete_station(form.get("id", [""])[0]); owner.on_change()
                        self.send_json({"ok": True, "message": "Sender gelöscht."})
                    elif parsed.path == "/api/move":
                        owner.store.move_station(form.get("id", [""])[0], form.get("direction", [""])[0]); owner.on_change()
                        self.send_json({"ok": True, "message": "Reihenfolge gespeichert."})
                    else: self.send_error(404)
                except RadioBrowserUnavailable:
                    self.send_json({"ok": False, "error": SEARCH_ERROR}, status=503)
                except ValueError as error:
                    self.send_json({"ok": False, "error": str(error)}, status=400)
                except Exception as error:
                    LOGGER.exception("Webradio administration request failed: %r", error)
                    self.send_json({"ok": False, "error": ACTION_ERROR}, status=500)

            def api_search(self, query):
                try:
                    results = search_logical_stations(query)
                    self.resolve_missing_logos(results)
                    self.send_json({"ok": True, "results": results})
                except RadioBrowserUnavailable:
                    self.send_json({"ok": False, "error": SEARCH_ERROR}, status=503)
                except Exception as error:
                    LOGGER.exception("Radio search failed unexpectedly: %r", error)
                    self.send_json({"ok": False, "error": SEARCH_ERROR}, status=503)

            def resolve_missing_logos(self, results):
                existing = {
                    normalize_station_name(station["name"]): station
                    for station in owner.store.list_stations()
                    if not station.get("logo_file")
                }
                changed = False
                for result in results:
                    station = existing.get(result["key"])
                    if station is None:
                        continue
                    try:
                        logo_file = select_and_cache_logo(
                            station["name"], [], owner.logo_dir, owner.base_dir
                        )
                        if logo_file and owner.store.update_logo(station["id"], logo_file):
                            changed = True
                    except Exception as error:
                        LOGGER.warning("Existing station logo resolution failed: %r", error)
                if changed:
                    owner.on_change()

            def station_payload(self):
                payload = []
                for station in owner.store.list_stations():
                    logo_url, logo_file = "", station.get("logo_file", "")
                    if logo_file:
                        try:
                            version = (owner.base_dir / logo_file).resolve().stat().st_mtime_ns
                            logo_url = "/assets/{}?v={}".format(urllib.parse.quote(logo_file, safe="/"), version)
                        except OSError: pass
                    payload.append({"id": station["id"], "name": station["name"],
                                    "key": normalize_station_name(station["name"]), "logo_url": logo_url,
                                    "startup": bool(station.get("startup"))})
                return payload

            def read_form(self):
                try: length = min(int(self.headers.get("Content-Length", "0")), 16384)
                except ValueError: length = 0
                return urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))

            def serve_asset(self, relative_name):
                try:
                    requested = (owner.base_dir / relative_name).resolve()
                    requested.relative_to(owner.logo_dir.resolve())
                    if not requested.is_file(): raise FileNotFoundError
                    data = requested.read_bytes()
                except (OSError, ValueError): self.send_error(404); return
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(str(requested))[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers(); self.wfile.write(data)

            def send_html(self, data):
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store")
                self.end_headers(); self.wfile.write(data)

            def send_json(self, document, status=200):
                data = json.dumps(document, ensure_ascii=False).encode("utf-8")
                self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store")
                self.end_headers(); self.wfile.write(data)

            def log_message(self, format_string, *args):
                pass

        return Handler
