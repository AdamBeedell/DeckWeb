# DeckWeb

A small self-hosted site for mapping how items in a list connect. It was built for Magic decklists but works with any list.

- **Ring view:** items sit on a ring. Two kinds of tags draw connections:
  - A tag named after an item in the list (`#sneak-attack`) draws a gold curve to that item. These are your card-to-card interactions.
  - Any other tag (`#ramp`) becomes a blue hub on the outer ring.
- **Columns view:** one column per tag. Nodes animate between the two layouts.
- **Hover** lights up connections and dims everything else. **Click** keeps them lit and opens the sidebar.
- **Tagging:** use the `#` beside any item, the checkboxes in the sidebar, or Quick-tag (pick a tag, then tick items).
- **Scryfall:** Tag from Scryfall runs any search (`otag:ramp`, `o:"sacrifice a creature"`) and tags the matches in your list.
- **Export:** a zip containing a decklist (Moxfield, Archidekt, TappedOut or plain), tags as JSON and CSV, and card images.

Limits: 10 lists per user, 250 items per list, 100 tags per list.

## Deploy on ZimaOS (Portainer)

1. Push this folder to a Git repo.
2. In Portainer, go to **Stacks → Add stack → Repository**, point it at the repo, and set the compose path to `compose.yml`.
3. Set the environment variables:

| Variable | Needed | Purpose |
| --- | --- | --- |
| `ADMIN_PASSWORD` | yes | Admin password. It is re-applied on every start, so change it here. |
| `ADMIN_USER` | no | Admin username, default `admin`. |
| `SECRET_KEY` | no | Signs API keys. If blank, one is generated into `/data/secret_key`. Changing it invalidates every key. |
| `SCRYFALL_USER_AGENT` | no | Sent to Scryfall. Scryfall asks for something that identifies your app. |

Data lives in `/DATA/AppData/deckweb`. Edit the volume line in `compose.yml` if you keep app data elsewhere.

On first start the app downloads Scryfall's Oracle Cards bulk file in the background. The file is roughly 150 MB and is deleted after import. The download refreshes weekly. Magic lists can be created once this finishes, and progress shows in **Admin**. Plain lists work immediately.

To run locally without Docker:

```
pip install -r requirements.txt
DATA_DIR=./data ADMIN_PASSWORD=changeme python app.py   # http://localhost:8080
```

## Pasting lists

- **Quantities:** `8x Forest`, `8 Forest` and `8 x Forest` all give 8 separate Forest items. Identical copies stack into one ×8 node unless you untick *Stack copies*.
- **Printings:** `(SET) 123` picks that exact printing. Otherwise the card's default printing is used.
- **Foil markers:** `*F*` and other foil markers are ignored.
- **Commander markers:** `*CMDR*`, an Archidekt `[Commander]` category, and a `Commander` section header all become a `Commander` tag.
- **Archidekt categories:** `[Ramp,Draw]` become tags when *Keep tags* is ticked.
- **TappedOut blocks:** a `#tag` line tags every line below it until a blank line. Inline `#tag`s also work. A card repeated across blocks counts as one card with all of those tags.
- **Problem lines:** these are listed with the reason. Click one to jump to it, fix it in place and check again, or choose *Save without problem lines*.

## API

Every `/api` call needs the header `X-API-Key`. Get your key from **Account → API key and password**, or from the login call:

```
curl -s -X POST localhost:8080/api/login -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"..."}'
curl -s localhost:8080/api/lists -H "X-API-Key: $KEY"
curl -s -X POST localhost:8080/api/lists -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"name":"My deck","mode":"mtg","text":"1 Sol Ring\n8 Forest"}'
```

Main routes:

- `GET/POST /api/lists`
- `GET/PATCH/DELETE /api/lists/<id>`
- `POST /api/lists/<id>/tags`
- `POST /api/lists/<id>/item-tags`
- `POST /api/lists/<id>/query-tag`
- `GET /api/lists/<id>/export?format=moxfield|archidekt|tappedout|plain`
- `/api/admin/*`

**Security model (deliberately light):**
- Passwords are hashed.
- API keys are not stored. Each key is an HMAC of your user id and a key version, so *Make a new key* invalidates the old one everywhere.
- Card images at `/images/…` are public Scryfall art and need no key.
- If you expose the site beyond your LAN, put it behind HTTPS (for example your reverse proxy).

## Code map

```
app.py        Flask routes: auth, lists, tags, Scryfall query tags, art, export, admin
db.py         SQLite schema (one file in /data) and limits
mtg.py        Magic-only code: list parsing, Scryfall bulk import, printings, image cache
static/
  index.html  page shell and dialogs
  chart.js    ring/columns layout, drawing, hover/click highlighting (D3, vendored)
  app.js      sign-in, sidebar, tagging, import, export, admin
  style.css   styles, light and dark
tests/        parser tests (python -m pytest tests)
```
