"""DeckWeb: a small list/tag connection visualizer. Flask API + static front end."""
import csv
import functools
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import zipfile

from flask import Flask, abort, g, jsonify, request, send_file, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash

import db
import mtg
from db import MAX_ITEMS_PER_LIST, MAX_LISTS_PER_USER, MAX_TAGS_PER_LIST, get_db

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.teardown_appcontext(db.close_db)


def load_secret():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(db.DATA_DIR, "secret_key")
    if not os.path.exists(path):
        os.makedirs(db.DATA_DIR, exist_ok=True)
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    return open(path).read().strip()


def bootstrap():
    db.init_db()
    app.config["SECRET"] = load_secret().encode()
    user, pw = os.environ.get("ADMIN_USER", "admin"), os.environ.get("ADMIN_PASSWORD")
    con = db.connect()
    if pw:  # env is the source of truth for the admin password
        con.execute("INSERT INTO users(username, pw_hash, is_admin) VALUES (?,?,1) "
                    "ON CONFLICT(username) DO UPDATE SET pw_hash=excluded.pw_hash, is_admin=1",
                    (user, generate_password_hash(pw)))
    elif not con.execute("SELECT 1 FROM users WHERE is_admin=1").fetchone():
        print("WARNING: no ADMIN_PASSWORD set and no admin exists; set ADMIN_PASSWORD.", flush=True)
    con.commit()
    con.close()
    if os.environ.get("SCRYFALL_ENABLED", "1") == "1":
        mtg.start_bulk_thread()

# --------------------------------------------------------------------------- auth
# API keys are derived, not stored: key = "<user id>.<HMAC(secret, id:version)>".
# Regenerating bumps the version, which invalidates the old key.

def make_key(user):
    mac = hmac.new(app.config["SECRET"], f"{user['id']}:{user['key_version']}".encode(), hashlib.sha256)
    return f"{user['id']}.{mac.hexdigest()[:40]}"


def user_from_key(key):
    m = re.fullmatch(r"(\d+)\.([0-9a-f]{40})", key or "")
    if not m:
        return None
    user = get_db().execute("SELECT * FROM users WHERE id=?", (int(m.group(1)),)).fetchone()
    return user if user and hmac.compare_digest(make_key(user), key) else None


def auth(admin=False):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            user = user_from_key(request.headers.get("X-API-Key"))
            if not user:
                return err("Sign in again: your API key is missing or no longer valid.", 401)
            if admin and not user["is_admin"]:
                return err("Only admins can do that.", 403)
            g.user = user
            return fn(*a, **kw)
        return wrapper
    return deco


def err(message, status=400, **extra):
    return jsonify(error=message, **extra), status


def body():
    return request.get_json(silent=True) or {}


def own_list(list_id):
    row = get_db().execute("SELECT * FROM lists WHERE id=?", (list_id,)).fetchone()
    if not row or (row["user_id"] != g.user["id"] and not g.user["is_admin"]):
        abort(404)
    return row


def own_tag(tag_id):
    row = get_db().execute("SELECT * FROM tags WHERE id=?", (tag_id,)).fetchone()
    if not row:
        abort(404)
    own_list(row["list_id"])
    return row


@app.post("/api/login")
def login():
    b = body()
    user = get_db().execute("SELECT * FROM users WHERE username=?", (b.get("username", ""),)).fetchone()
    if not user or not check_password_hash(user["pw_hash"], b.get("password", "")):
        return err("Username or password is incorrect.", 401)
    return jsonify(key=make_key(user), username=user["username"], is_admin=bool(user["is_admin"]))


@app.get("/api/me")
@auth()
def me():
    return jsonify(username=g.user["username"], is_admin=bool(g.user["is_admin"]))


@app.post("/api/me/key")
@auth()
def regenerate_key():
    con = get_db()
    con.execute("UPDATE users SET key_version=key_version+1 WHERE id=?", (g.user["id"],))
    con.commit()
    user = con.execute("SELECT * FROM users WHERE id=?", (g.user["id"],)).fetchone()
    return jsonify(key=make_key(user))


@app.post("/api/me/password")
@auth()
def change_password():
    pw = body().get("password", "")
    if len(pw) < 6:
        return err("Use at least 6 characters.")
    con = get_db()
    con.execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(pw), g.user["id"]))
    con.commit()
    return jsonify(ok=True)

# --------------------------------------------------------------------------- lists

def parse_request(b, con):
    """Parse + (in MTG mode) resolve pasted text. Returns (entries, errors, warnings)."""
    mode = b.get("mode", "mtg")
    entries, errors, warnings = mtg.parse_text(b.get("text", ""), mtg_mode=(mode == "mtg"))
    if not b.get("import_tags", True):
        for e in entries:
            e["tags"] = [t for t in e["tags"] if t == "Commander"]
    if mode == "mtg":
        if mtg.card_count(con) == 0:
            return entries, [{"line": 0, "raw": "", "reason": "The card database is still loading. Try again in a few minutes, or use a plain list."}], warnings
        e2, w2 = mtg.resolve(con, entries)
        errors += e2
        warnings += w2
        bad = {e["line"] for e in errors}
        entries = [e for e in entries if e["line"] not in bad]
    total = sum(e["qty"] for e in entries)
    if total > MAX_ITEMS_PER_LIST:
        errors.append({"line": 0, "raw": "", "reason": f"The list has {total} items; the limit is {MAX_ITEMS_PER_LIST}."})
    ntags = len({mtg.normalize(t) for e in entries for t in e["tags"]})
    if ntags > MAX_TAGS_PER_LIST:
        errors.append({"line": 0, "raw": "", "reason": f"The list uses {ntags} tags; the limit is {MAX_TAGS_PER_LIST}."})
    return entries, sorted(errors, key=lambda e: e["line"]), warnings


@app.post("/api/lists/preview")
@auth()
def preview_list():
    entries, errors, warnings = parse_request(body(), get_db())
    return jsonify(count=sum(e["qty"] for e in entries), errors=errors, warnings=warnings,
                   tags=mtg.dedupe(sorted(t for e in entries for t in e["tags"])))


@app.get("/api/lists")
@auth()
def my_lists():
    rows = get_db().execute(
        "SELECT l.id, l.name, l.mode, l.created, COUNT(i.id) AS count FROM lists l "
        "LEFT JOIN items i ON i.list_id=l.id WHERE l.user_id=? GROUP BY l.id ORDER BY l.created DESC",
        (g.user["id"],)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/lists")
@auth()
def create_list():
    b, con = body(), get_db()
    name = (b.get("name") or "").strip()[:100]
    if not name:
        return err("Give the list a name.")
    n = con.execute("SELECT COUNT(*) FROM lists WHERE user_id=?", (g.user["id"],)).fetchone()[0]
    if n >= MAX_LISTS_PER_USER:
        return err(f"You already have {MAX_LISTS_PER_USER} lists. Delete one to add another.")
    entries, errors, warnings = parse_request(b, con)
    blocking = [e for e in errors if e["line"] == 0] if b.get("drop_errors") else errors
    if blocking or not entries:
        return err("Fix the highlighted lines, or drop them.", 422, errors=errors or
                   [{"line": 0, "raw": "", "reason": "No items found."}], warnings=warnings)
    cur = con.execute("INSERT INTO lists(user_id, name, mode, source_text) VALUES (?,?,?,?)",
                      (g.user["id"], name, b.get("mode", "mtg"), b.get("text", "")))
    list_id, tag_ids, pos = cur.lastrowid, {}, 0
    for e in entries:
        for t in e["tags"]:
            if mtg.normalize(t) not in tag_ids:
                tag_ids[mtg.normalize(t)] = con.execute("INSERT INTO tags(list_id, name, source) VALUES (?,?,'import')",
                                                 (list_id, t)).lastrowid
        for _ in range(e["qty"]):  # 8 Forest = 8 item rows
            item_id = con.execute("INSERT INTO items(list_id, name, oracle_id, print_id, position) VALUES (?,?,?,?,?)",
                                  (list_id, e["name"], e.get("oracle_id"), e.get("print_id"), pos)).lastrowid
            pos += 1
            con.executemany("INSERT INTO item_tags VALUES (?,?)", [(item_id, tag_ids[mtg.normalize(t)]) for t in e["tags"]])
    con.commit()
    return jsonify(id=list_id, warnings=warnings)


@app.post("/api/lists/unpack")
@auth()
def unpack_file():
    """Turn an uploaded export (.zip or tags.json) or decklist file into pasteable text.
    The text then goes through the normal check-and-save flow. Images in a zip are ignored."""
    f = request.files.get("file")
    if not f:
        return err("Choose a file to load.")
    raw, name = f.read(20_000_000), os.path.splitext(f.filename or "")[0]
    if raw[:2] == b"PK":
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            return err("That zip file couldn't be opened.")
        files = z.namelist()
        if "tags.json" in files:
            raw = z.read("tags.json")
        elif "list.txt" in files:
            return jsonify(name=name, mode=None, text=z.read("list.txt").decode("utf-8", "replace"))
        else:
            return err("That zip isn't a DeckWeb export: it has no tags.json or list.txt.")
    try:
        bundle = json.loads(raw)
        items = bundle["items"]
    except (ValueError, KeyError, TypeError):
        return jsonify(name=name, mode=None, text=raw.decode("utf-8", "replace"))  # plain decklist text
    groups = [{"qty": int(i.get("qty") or 1), "name": i["name"], "set": i.get("set"),
               "number": i.get("number"), "tags": i.get("tags") or []} for i in items if i.get("name")]
    mode = bundle.get("mode") or ("mtg" if any(gr["set"] for gr in groups) else None)
    return jsonify(name=bundle.get("list") or name, mode=mode, text=mtg.export_lines(groups, "archidekt"))


@app.get("/api/lists/<int:list_id>")
@auth()
def get_list(list_id):
    lst, con = own_list(list_id), get_db()
    items = con.execute(
        "SELECT i.id, i.name, i.oracle_id, i.print_id, c.type_line, c.mana_cost, c.color_identity "
        "FROM items i LEFT JOIN cards c ON c.oracle_id=i.oracle_id WHERE i.list_id=? ORDER BY i.position",
        (list_id,)).fetchall()
    tags = con.execute("SELECT id, name, source FROM tags WHERE list_id=? ORDER BY name COLLATE NOCASE",
                       (list_id,)).fetchall()
    links = con.execute("SELECT it.item_id, it.tag_id FROM item_tags it JOIN items i ON i.id=it.item_id "
                        "WHERE i.list_id=?", (list_id,)).fetchall()
    return jsonify(id=lst["id"], name=lst["name"], mode=lst["mode"],
                   items=[dict(r) for r in items], tags=[dict(r) for r in tags],
                   links=[[r["item_id"], r["tag_id"]] for r in links])


@app.patch("/api/lists/<int:list_id>")
@auth()
def rename_list(list_id):
    own_list(list_id)
    name = (body().get("name") or "").strip()[:100]
    if not name:
        return err("Give the list a name.")
    con = get_db()
    con.execute("UPDATE lists SET name=? WHERE id=?", (name, list_id))
    con.commit()
    return jsonify(ok=True)


@app.delete("/api/lists/<int:list_id>")
@auth()
def delete_list(list_id):
    own_list(list_id)
    con = get_db()
    con.execute("DELETE FROM lists WHERE id=?", (list_id,))
    con.commit()
    return jsonify(ok=True)

# --------------------------------------------------------------------------- tags

def get_or_create_tag(con, list_id, name, source="manual"):
    name = name.strip().lstrip("#").strip()[:150]
    if not name:
        raise ValueError("Tag names can't be empty.")
    key = mtg.normalize(name)  # 'Sneak Attack' and 'sneak-attack' are the same tag
    for row in con.execute("SELECT id, name FROM tags WHERE list_id=?", (list_id,)).fetchall():
        if mtg.normalize(row["name"]) == key:
            return row["id"]
    if con.execute("SELECT COUNT(*) FROM tags WHERE list_id=?", (list_id,)).fetchone()[0] >= MAX_TAGS_PER_LIST:
        raise ValueError(f"This list already has {MAX_TAGS_PER_LIST} tags. Delete one first.")
    return con.execute("INSERT INTO tags(list_id, name, source) VALUES (?,?,?)", (list_id, name, source)).lastrowid


@app.post("/api/lists/<int:list_id>/tags")
@auth()
def create_tag(list_id):
    own_list(list_id)
    con = get_db()
    try:
        tag_id = get_or_create_tag(con, list_id, body().get("name", ""))
    except ValueError as e:
        return err(str(e))
    con.commit()
    return jsonify(id=tag_id)


@app.patch("/api/tags/<int:tag_id>")
@auth()
def rename_tag(tag_id):
    tag, con = own_tag(tag_id), get_db()
    name = (body().get("name") or "").strip().lstrip("#").strip()[:60]
    if not name:
        return err("Tag names can't be empty.")
    if con.execute("SELECT 1 FROM tags WHERE list_id=? AND name=? AND id<>?", (tag["list_id"], name, tag_id)).fetchone():
        return err("A tag with that name already exists.")
    con.execute("UPDATE tags SET name=? WHERE id=?", (name, tag_id))
    con.commit()
    return jsonify(ok=True)


@app.delete("/api/tags/<int:tag_id>")
@auth()
def delete_tag(tag_id):
    own_tag(tag_id)
    con = get_db()
    con.execute("DELETE FROM tags WHERE id=?", (tag_id,))
    con.commit()
    return jsonify(ok=True)


@app.post("/api/lists/<int:list_id>/item-tags")
@auth()
def set_item_tags(list_id):
    """{item_ids: [...], tag_id or tag_name, on: bool} — applies to every listed item."""
    own_list(list_id)
    b, con = body(), get_db()
    try:
        tag_id = b.get("tag_id") or get_or_create_tag(con, list_id, b.get("tag_name", ""))
    except ValueError as e:
        return err(str(e))
    if own_tag(tag_id)["list_id"] != list_id:
        abort(404)
    ids = [r[0] for r in con.execute(
        f"SELECT id FROM items WHERE list_id=? AND id IN ({','.join('?' * len(b.get('item_ids', [])))})",
        [list_id, *b.get("item_ids", [])]).fetchall()] if b.get("item_ids") else []
    if b.get("on", True):
        con.executemany("INSERT OR IGNORE INTO item_tags VALUES (?,?)", [(i, tag_id) for i in ids])
    else:
        con.executemany("DELETE FROM item_tags WHERE item_id=? AND tag_id=?", [(i, tag_id) for i in ids])
    con.commit()
    return jsonify(ok=True, tag_id=tag_id)


def commander_identity(con, list_id):
    rows = con.execute(
        "SELECT c.color_identity FROM items i JOIN item_tags it ON it.item_id=i.id "
        "JOIN tags t ON t.id=it.tag_id JOIN cards c ON c.oracle_id=i.oracle_id "
        "WHERE i.list_id=? AND t.name='Commander'", (list_id,)).fetchall()
    if not rows:
        return None
    return "".join(c for c in "WUBRG" if any(c in (r[0] or "") for r in rows))


@app.post("/api/lists/<int:list_id>/query-tag")
@auth()
def query_tag(list_id):
    """Tag every item matching a Scryfall query, e.g. otag:ramp."""
    lst, b, con = own_list(list_id), body(), get_db()
    if lst["mode"] != "mtg":
        return err("Scryfall queries only work on Magic lists.")
    query = (b.get("query") or "").strip()
    if not query:
        return err("Enter a Scryfall query, for example otag:ramp.")
    if b.get("use_identity", True):
        query = mtg.add_identity(query, commander_identity(con, list_id))
    try:
        names = mtg.search_names(con, query)
        tag_id = get_or_create_tag(con, list_id, b.get("tag") or query, source="query")
    except ValueError as e:
        return err(str(e))
    items = con.execute("SELECT id, name FROM items WHERE list_id=?", (list_id,)).fetchall()
    hits = [i["id"] for i in items if any(k in names for k in mtg.name_keys(i["name"]))]
    con.executemany("INSERT OR IGNORE INTO item_tags VALUES (?,?)", [(i, tag_id) for i in hits])
    con.commit()
    return jsonify(matched=len(hits), query=query, tag_id=tag_id)

# --------------------------------------------------------------------------- art

@app.get("/api/lists/<int:list_id>/items/<int:item_id>/prints")
@auth()
def item_prints(list_id, item_id):
    own_list(list_id)
    con = get_db()
    item = con.execute("SELECT * FROM items WHERE id=? AND list_id=?", (item_id, list_id)).fetchone()
    if not item or not item["oracle_id"]:
        abort(404)
    return jsonify(mtg.list_prints(con, item["name"]))


@app.post("/api/lists/<int:list_id>/art")
@auth()
def set_art(list_id):
    own_list(list_id)
    b, con = body(), get_db()
    p = con.execute("SELECT * FROM prints WHERE print_id=?", (b.get("print_id"),)).fetchone()
    if not p:
        return err("Unknown printing.")
    for item_id in b.get("item_ids", []):
        con.execute("UPDATE items SET print_id=? WHERE id=? AND list_id=? AND oracle_id=?",
                    (p["print_id"], item_id, list_id, p["oracle_id"]))
    con.commit()
    return jsonify(ok=True)


@app.get("/images/<print_id>.jpg")
def image(print_id):
    # Card art is public data, so images are served without a key (<img> can't send headers).
    path = mtg.image_path(get_db(), print_id)
    if not path:
        abort(404)
    return send_file(path, mimetype="image/jpeg", max_age=30 * 86400)

# --------------------------------------------------------------------------- export

@app.get("/api/lists/<int:list_id>/export")
@auth()
def export(list_id):
    lst, con = own_list(list_id), get_db()
    fmt = request.args.get("format", "moxfield" if lst["mode"] == "mtg" else "plain")
    data = get_list(list_id).get_json()
    tag_name = {t["id"]: t["name"] for t in data["tags"]}
    item_tags = {}
    for item_id, tag_id in data["links"]:
        item_tags.setdefault(item_id, []).append(tag_name[tag_id])
    groups = {}
    for it in data["items"]:
        p = con.execute("SELECT set_code, collector_number FROM prints WHERE print_id=?", (it["print_id"],)).fetchone()
        tags = sorted(item_tags.get(it["id"], []))
        key = (it["name"], it["print_id"], tuple(tags))
        gr = groups.setdefault(key, {"qty": 0, "name": it["name"], "tags": tags, "print_id": it["print_id"],
                                     "set": p["set_code"] if p else None, "number": p["collector_number"] if p else None})
        gr["qty"] += 1
    groups = list(groups.values())

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("list.txt", mtg.export_lines(groups, fmt))
        z.writestr("tags.json", json.dumps({
            "list": data["name"], "mode": lst["mode"], "tags": [t["name"] for t in data["tags"]],
            "items": [{k: gr[k] for k in ("qty", "name", "set", "number", "tags")} for gr in groups]}, indent=2))
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["qty", "item", "set", "number", "tag"])
        for gr in groups:
            for t in gr["tags"] or [""]:
                w.writerow([gr["qty"], gr["name"], gr["set"] or "", gr["number"] or "", t])
        z.writestr("tags.csv", out.getvalue())
        for pid in {gr["print_id"] for gr in groups if gr["print_id"]}:
            path = mtg.image_path(con, pid)
            if path:
                name = next(gr["name"] for gr in groups if gr["print_id"] == pid)
                z.write(path, f"images/{re.sub(r'[^A-Za-z0-9]+', '_', name)}_{pid[:8]}.jpg")
    buf.seek(0)
    fname = re.sub(r"[^A-Za-z0-9]+", "_", data["name"]) + ".zip"
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=fname)

# --------------------------------------------------------------------------- admin

@app.get("/api/admin/users")
@auth(admin=True)
def admin_users():
    con = get_db()
    users = []
    for u in con.execute("SELECT id, username, is_admin FROM users ORDER BY username").fetchall():
        lists = con.execute("SELECT l.id, l.name, l.created, COUNT(i.id) AS count FROM lists l "
                            "LEFT JOIN items i ON i.list_id=l.id WHERE l.user_id=? GROUP BY l.id", (u["id"],)).fetchall()
        users.append({**dict(u), "lists": [dict(l) for l in lists]})
    return jsonify(users)


@app.post("/api/admin/users")
@auth(admin=True)
def admin_create_user():
    b, con = body(), get_db()
    name, pw = (b.get("username") or "").strip(), b.get("password") or ""
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{2,40}", name) or len(pw) < 6:
        return err("Usernames use letters, numbers, . _ - (2–40 characters); passwords need 6+ characters.")
    try:
        con.execute("INSERT INTO users(username, pw_hash, is_admin) VALUES (?,?,?)",
                    (name, generate_password_hash(pw), 1 if b.get("is_admin") else 0))
    except Exception:
        return err("That username is taken.")
    con.commit()
    return jsonify(ok=True)


@app.delete("/api/admin/users/<int:user_id>")
@auth(admin=True)
def admin_delete_user(user_id):
    if user_id == g.user["id"]:
        return err("You can't delete your own account.")
    con = get_db()
    con.execute("DELETE FROM users WHERE id=?", (user_id,))
    con.commit()
    return jsonify(ok=True)


@app.get("/api/status")
@auth()
def status():
    return jsonify(cards=mtg.card_count(get_db()), **mtg.bulk_status)


@app.post("/api/admin/cards/refresh")
@auth(admin=True)
def refresh_cards():
    mtg.start_bulk_thread(force=True)
    return jsonify(ok=True)

# --------------------------------------------------------------------------- front end

@app.get("/")
def index():
    return send_from_directory("static", "index.html")


bootstrap()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
