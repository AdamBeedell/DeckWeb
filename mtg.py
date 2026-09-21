"""Everything Magic-specific lives here: decklist parsing, the Scryfall card DB,
print lookups, image caching and Scryfall query tags. The rest of the app only
sees generic items and tags."""
import json
import os
import re
import threading
import time
import unicodedata

import ijson
import requests

import db

SCRYFALL = "https://api.scryfall.com"
IMAGE_DIR = os.path.join(db.DATA_DIR, "images")
USER_AGENT = os.environ.get("SCRYFALL_USER_AGENT", "DeckWeb/1.0 (self-hosted)")
BULK_MAX_AGE_DAYS = float(os.environ.get("BULK_MAX_AGE_DAYS", "7"))
QUERY_CACHE_SECONDS = 24 * 3600
QUERY_MAX_PAGES = 20  # 175 cards per page

SUPERTYPES = {"legendary", "basic", "snow", "world", "ongoing", "host", "elite"}
SECTIONS = {  # header line -> tag to apply (None = no tag)
    "commander": "Commander", "commanders": "Commander", "companion": "Companion",
    "deck": None, "main": None, "mainboard": None, "maindeck": None,
    "sideboard": "Sideboard", "maybeboard": "Maybeboard", "considering": "Maybeboard",
}

# --------------------------------------------------------------------------- names

def normalize(name):
    """Case/punctuation/accent-insensitive key: 'Sneak-Attack' == 'sneak attack'."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[-_/]+", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def name_keys(name):
    """Full name plus each face of a double-faced 'A // B' card."""
    keys = [normalize(name)]
    if "//" in name:
        keys += [normalize(p) for p in name.split("//") if p.strip()]
    return keys

# --------------------------------------------------------------------------- parsing

QTY_RE = re.compile(r"^(\d+)\s*[xX]?\s+(.+)$")
SET_RE = re.compile(r"\(([A-Za-z0-9]{2,6})\)\s*([0-9A-Za-z★†\-]+)?")


def dedupe(tags):
    """Unique tags, case-insensitive, keeping the first spelling."""
    seen = {}
    for t in tags:
        seen.setdefault(normalize(t) or t, t)
    return list(seen.values())


def parse_line(rest, sets=True):
    """Pull markers out of one card line. Returns (name, set, number, tags)."""
    tags = []
    rest = re.sub(r"\^[^^]*\^", " ", rest)                     # Archidekt ^Have,#hex^
    for cats in re.findall(r"\[([^\]]*)\]", rest):              # Archidekt [Ramp,Draw]
        for c in cats.split(","):
            c = re.sub(r"\{[^}]*\}", "", c).strip()
            if c:
                tags.append(c)
    rest = re.sub(r"\[[^\]]*\]", " ", rest)
    if re.search(r"\*cmdr\*", rest, re.I):                     # TappedOut commander
        tags.append("Commander")
    rest = re.sub(r"\*[A-Za-z\-]+\*", " ", rest)                # *F*, *f*, *E*, *CMDR*
    tags += re.findall(r"(?:^|\s)#([^\s#]+)", rest)             # inline #tags
    rest = re.sub(r"(?:^|\s)#[^\s#]+", " ", rest)
    set_code = number = None
    m = SET_RE.search(rest) if sets else None
    if m:
        set_code = m.group(1).lower()
        number = m.group(2)
        rest = rest[:m.start()] + " " + rest[m.end():]
    name = re.sub(r"\s+", " ", rest).strip()
    return name, set_code, number, tags


def parse_text(text, mtg_mode=True):
    """Parse a pasted list (Moxfield / Archidekt / TappedOut / plain).
    Returns entries [{line, raw, qty, name, set, number, tags}] and errors."""
    entries, errors, warnings = [], [], []
    block_tag = None       # TappedOut '#tag' header, cleared by a blank line
    section_tag = None     # 'Commander', 'Sideboard', ... header
    seen_in_blocks = {}    # (name, set, number) -> entry, for cards repeated across #blocks
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            block_tag = None
            continue
        header = re.sub(r"^//\s*", "", line).rstrip(":").strip().lower()
        if header in SECTIONS:
            section_tag = SECTIONS[header]
            continue
        if line.startswith("#") and not QTY_RE.match(line[1:].strip()):
            block_tag = line.lstrip("#").strip() or None
            continue
        if line.startswith("//"):
            continue  # comment
        m = QTY_RE.match(line)
        qty, rest = (int(m.group(1)), m.group(2)) if m else (1, line)
        name, set_code, number, tags = parse_line(rest, sets=mtg_mode)
        if not name:
            errors.append({"line": i, "raw": raw, "reason": "Couldn't find an item name on this line."})
            continue
        if qty < 1:
            errors.append({"line": i, "raw": raw, "reason": "Quantity must be at least 1."})
            continue
        for t in (block_tag, section_tag):
            if t:
                tags.append(t)
        entry = {"line": i, "raw": raw, "qty": qty, "name": name,
                 "set": set_code, "number": number, "tags": dedupe(tags)}
        key = (normalize(name), set_code, number)
        if block_tag and key in seen_in_blocks:
            prev = seen_in_blocks[key]
            prev["qty"] = max(prev["qty"], qty)
            prev["tags"] = dedupe(prev["tags"] + entry["tags"])
            warnings.append({"line": i, "raw": raw,
                             "reason": f"'{name}' also appears on line {prev['line']}; treated as one card with both sets of tags."})
            continue
        if block_tag:
            seen_in_blocks[key] = entry
        entries.append(entry)
    return entries, errors, warnings

# --------------------------------------------------------------------------- Scryfall HTTP

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
_rate_lock = threading.Lock()
_last_call = [0.0]


def scryfall_get(url, **kw):
    """GET with Scryfall's requested spacing (~10 requests/second max)."""
    with _rate_lock:
        wait = 0.1 - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
    return _session.get(url, timeout=kw.pop("timeout", 30), **kw)


def image_uri(card):
    uris = card.get("image_uris") or (card.get("card_faces") or [{}])[0].get("image_uris") or {}
    return uris.get("normal") or uris.get("large") or uris.get("small")


def store_print(con, card):
    con.execute("INSERT OR REPLACE INTO prints VALUES (?,?,?,?,?,?)",
                (card["id"], card.get("oracle_id") or (card.get("card_faces") or [{}])[0].get("oracle_id"),
                 card.get("set"), card.get("collector_number"), card.get("set_name"), image_uri(card)))

# --------------------------------------------------------------------------- bulk card DB

_bulk_lock = threading.Lock()
bulk_status = {"state": "idle", "message": ""}


def split_types(type_line):
    sup, typ, sub = set(), set(), set()
    for face in (type_line or "").split("//"):
        left, _, right = face.partition("—")
        for w in left.split():
            (sup if w.lower() in SUPERTYPES else typ).add(w)
        sub.update(right.split())
    return " ".join(sorted(sup)), " ".join(sorted(typ)), " ".join(sorted(sub))


def load_bulk(force=False):
    """Download Scryfall 'Oracle Cards' bulk data and rebuild the cards tables."""
    if not _bulk_lock.acquire(blocking=False):
        return
    con = db.connect()
    try:
        last = float(db.get_meta(con, "bulk_loaded_at", 0))
        if not force and time.time() - last < BULK_MAX_AGE_DAYS * 86400:
            bulk_status.update(state="ready", message="Card database is up to date.")
            return
        bulk_status.update(state="loading", message="Downloading card data from Scryfall…")
        info = scryfall_get(f"{SCRYFALL}/bulk-data/oracle-cards").json()
        path = os.path.join(db.DATA_DIR, "oracle_cards.json")
        with _session.get(info["download_uri"], stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        bulk_status.update(message="Importing cards…")
        con.execute("DELETE FROM cards")
        con.execute("DELETE FROM card_names")
        skip = {"token", "double_faced_token", "emblem", "art_series", "vanguard", "scheme", "planar"}
        n = 0
        with open(path, "rb") as f:
            for c in ijson.items(f, "item"):
                if c.get("layout") in skip or not c.get("oracle_id"):
                    continue
                faces = c.get("card_faces") or []
                type_line = c.get("type_line") or " // ".join(x.get("type_line", "") for x in faces)
                text = c.get("oracle_text") or "\n//\n".join(x.get("oracle_text", "") for x in faces)
                cost = c.get("mana_cost") or " // ".join(x.get("mana_cost", "") for x in faces)
                sup, typ, sub = split_types(type_line)
                con.execute("INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                    c["oracle_id"], c["name"], sup, typ, sub, type_line, cost, float(c.get("cmc") or 0),
                    "".join(c.get("color_identity") or []), " ".join(c.get("keywords") or []), text, c["id"]))
                verb = "OR IGNORE" if c.get("set_type") == "funny" else "OR REPLACE"
                for k in name_keys(c["name"]):
                    con.execute(f"INSERT {verb} INTO card_names VALUES (?,?)", (k, c["oracle_id"]))
                store_print(con, c)
                n += 1
        try:
            con.execute("INSERT INTO cards_fts(cards_fts) VALUES('rebuild')")
        except Exception:
            pass
        db.set_meta(con, "bulk_loaded_at", time.time())
        con.commit()
        os.remove(path)
        bulk_status.update(state="ready", message=f"Loaded {n} cards.")
    except Exception as e:  # keep the app running; show the problem in admin
        con.rollback()
        bulk_status.update(state="error", message=f"Card data load failed: {e}")
    finally:
        con.close()
        _bulk_lock.release()


def start_bulk_thread(force=False):
    threading.Thread(target=load_bulk, kwargs={"force": force}, daemon=True).start()


def card_count(con):
    return con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]

# --------------------------------------------------------------------------- resolving

def lookup_oracle(con, name):
    for k in name_keys(name):
        row = con.execute("SELECT oracle_id FROM card_names WHERE name_norm=?", (k,)).fetchone()
        if row:
            return row["oracle_id"]
    return None


def lookup_print(con, set_code, number):
    """Exact printing from (SET) number: local cache first, then Scryfall."""
    row = con.execute("SELECT print_id, oracle_id FROM prints WHERE set_code=? AND collector_number=?",
                      (set_code, number)).fetchone()
    if row:
        return row["print_id"], row["oracle_id"]
    try:
        r = scryfall_get(f"{SCRYFALL}/cards/{set_code}/{number}")
        if r.status_code == 200:
            card = r.json()
            store_print(con, card)
            return card["id"], card.get("oracle_id")
    except requests.RequestException:
        pass
    return None, None


def resolve(con, entries):
    """Attach oracle_id / print_id / canonical name to parsed entries (MTG mode)."""
    errors, warnings = [], []
    for e in entries:
        oracle = lookup_oracle(con, e["name"])
        if not oracle:
            errors.append({"line": e["line"], "raw": e["raw"], "reason": f"No card named '{e['name']}'."})
            continue
        card = con.execute("SELECT name, default_print FROM cards WHERE oracle_id=?", (oracle,)).fetchone()
        e["oracle_id"], e["name"], e["print_id"] = oracle, card["name"], card["default_print"]
        if e["set"] and e["number"]:
            pid, poracle = lookup_print(con, e["set"], e["number"])
            if pid and poracle == oracle:
                e["print_id"] = pid
            else:
                warnings.append({"line": e["line"], "raw": e["raw"],
                                 "reason": f"Printing ({e['set'].upper()}) {e['number']} not found; using the default art."})
    return errors, warnings

# --------------------------------------------------------------------------- images and arts

def image_path(con, print_id):
    """Local path of a print's image, downloading it on first request."""
    try:
        return _image_path(con, print_id)
    except requests.RequestException:
        return None


def _image_path(con, print_id):
    if not re.fullmatch(r"[0-9a-f\-]{36}", print_id or ""):
        return None
    path = os.path.join(IMAGE_DIR, f"{print_id}.jpg")
    if os.path.exists(path):
        return path
    row = con.execute("SELECT image_url FROM prints WHERE print_id=?", (print_id,)).fetchone()
    url = row["image_url"] if row else None
    if not url:
        r = scryfall_get(f"{SCRYFALL}/cards/{print_id}")
        if r.status_code != 200:
            return None
        store_print(con, r.json())
        con.commit()
        url = image_uri(r.json())
    if not url:
        return None
    r = scryfall_get(url)
    if r.status_code != 200:
        return None
    os.makedirs(IMAGE_DIR, exist_ok=True)
    with open(path + ".part", "wb") as f:
        f.write(r.content)
    os.replace(path + ".part", path)
    return path


def list_prints(con, card_name):
    """All printings of a card, for the art picker."""
    q = f'!"{card_name}" unique:prints'
    out, url, params = [], f"{SCRYFALL}/cards/search", {"q": q, "order": "released"}
    for _ in range(5):
        r = scryfall_get(url, params=params)
        if r.status_code != 200:
            break
        data = r.json()
        for c in data["data"]:
            store_print(con, c)
            uris = c.get("image_uris") or (c.get("card_faces") or [{}])[0].get("image_uris") or {}
            out.append({"print_id": c["id"], "set": c["set"].upper(), "set_name": c.get("set_name"),
                        "number": c.get("collector_number"), "thumb": uris.get("small")})
        if not data.get("has_more"):
            break
        url, params = data["next_page"], None
    con.commit()
    return out

# --------------------------------------------------------------------------- query tags

def add_identity(query, identity):
    """Narrow big queries (otag:removal) to the commander's colour identity."""
    if identity is None or re.search(r"\b(id|identity|ci|commander)\s*[:<>=]", query, re.I):
        return query
    return f"({query}) id<={identity.lower() or 'c'}"


def search_names(con, query):
    """Set of normalised card names matching a Scryfall query (cached 24h)."""
    row = con.execute("SELECT fetched_at, names_json FROM query_cache WHERE query=?", (query,)).fetchone()
    if row and time.time() - row["fetched_at"] < QUERY_CACHE_SECONDS:
        return set(json.loads(row["names_json"]))
    names, url, params = set(), f"{SCRYFALL}/cards/search", {"q": query, "unique": "cards"}
    for _ in range(QUERY_MAX_PAGES):
        r = scryfall_get(url, params=params)
        if r.status_code == 404:
            break  # no results
        if r.status_code != 200:
            raise ValueError(r.json().get("details", "Scryfall rejected the query."))
        data = r.json()
        for c in data["data"]:
            names.update(name_keys(c["name"]))
        if not data.get("has_more"):
            break
        url, params = data["next_page"], None
    con.execute("INSERT OR REPLACE INTO query_cache VALUES (?,?,?)", (query, time.time(), json.dumps(sorted(names))))
    return names

# --------------------------------------------------------------------------- export

def export_lines(groups, fmt):
    """groups: [{qty, name, set, number, tags}] -> decklist text."""
    lines = []
    for gr in groups:
        tags = [t for t in gr["tags"] if t != "Commander"]
        cmdr = "Commander" in gr["tags"]
        setnum = f" ({gr['set'].upper()}) {gr['number']}" if gr.get("set") else ""
        if fmt == "archidekt":
            cats = (["Commander"] if cmdr else []) + tags
            lines.append(f"{gr['qty']}x {gr['name']}{setnum}" + (f" [{','.join(cats)}]" if cats else ""))
        elif fmt == "tappedout":
            extra = (" *CMDR*" if cmdr else "") + "".join(f" #{t.replace(' ', '-')}" for t in tags)
            lines.append(f"{gr['qty']}x {gr['name']}{setnum}{extra}")
        elif fmt == "plain":
            lines.append(f"{gr['qty']} {gr['name']}")
        else:  # moxfield
            lines.append(f"{gr['qty']} {gr['name']}{setnum}")
    if fmt == "moxfield":  # commander section first
        cmd = [l for l, gr in zip(lines, groups) if "Commander" in gr["tags"]]
        rest = [l for l, gr in zip(lines, groups) if "Commander" not in gr["tags"]]
        lines = (["Commander"] + cmd + ["", "Deck"] + rest) if cmd else rest
    return "\n".join(lines) + "\n"
