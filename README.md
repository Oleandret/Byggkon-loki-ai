# Loki AI for Byggkon

Privat AI-kunnskapsmotor som synkroniserer **OneDrive og SharePoint
tenant-wide** inn i en **Pinecone**-vektorindeks via **Unstructured**, med
embeddings fra **OpenAI text-embedding-3-large**. Levert som én Docker-tjeneste
med admin-UI, designet i Byggkons visuelle språk.

## Hva er dette

Hver fil i tenanten parses, deles opp i meningsfulle biter, og lagres som
vektorer du kan søke mot. Resultatet er at Byggkon kan stille spørsmål som
"hva har vi gjort av tilsvarende prosjekter de siste 5 årene?" mot all sin
egen historikk — og få treff i selve innholdet, ikke bare filnavn.

## Tre sider i appen

| Side | Hva |
|---|---|
| **`/`** | Landing — forklarer hele systemet, hvordan det fungerer, hvilke funksjoner det har. |
| **`/help`** | Steg-for-steg oppsetts­veileder (Entra ID, OpenAI, Pinecone, første kjøring). |
| **`/admin`** | Innstillinger, kjøringer, test-tilkoblinger, manuell sync. Krever passord. |

## Pipeline

1. **Discovery** — `GET /users` enumererer alle, deretter
   `/users/{id}/drive` per bruker. Med `SYNC_SCOPE=all_users_and_sharepoint`
   også alle SharePoint-områder.
2. **Delta sync per drive** — `/drives/{id}/root/delta`, lagrer
   `@odata.deltaLink` i SQLite så neste runde kun henter endringer.
3. **Per-fil pipeline** — stream-download → Unstructured (`hi_res` for
   PDF/bilder, `fast` ellers) → OpenAI batched embeddings → Pinecone upsert.
   Eksisterende filer får sine gamle vektorer slettet før nye legges inn.
4. **Checkpoint** — ny deltaLink skrives først når siden er ferdig prosessert,
   så crash midt i en runde replayes fra siste gode punkt.
5. **Schedule** — APScheduler kjører på konfigurerbart intervall (default 10 min).
   `POST /api/sync` (eller knappen i admin-UI) trigger en runde umiddelbart.

## Prosjektstruktur

```
loki-ai/
├── Dockerfile                    # Multi-stage med poppler/tesseract/libreoffice
├── railway.json                  # Railway deploy config
├── push.sh                       # Init + push til GitHub
├── requirements.txt
├── .env.example                  # Alle miljøvariabler dokumentert
├── LICENSE                       # MIT
├── app/
│   ├── main.py                   # FastAPI lifespan + scheduler + routes
│   ├── config.py                 # pydantic-settings + reload_settings()
│   ├── settings_store.py         # SQLite-overrides for runtime-redigering + Fernet-kryptering
│   ├── auth.py                   # Admin-passord + signert sesjons-cookie
│   ├── admin_routes.py           # HTML-sider og JSON API for admin
│   ├── logging_config.py         # structlog → JSON
│   ├── graph_client.py           # MSAL + httpx, retries, delta queries
│   ├── drive_discovery.py        # Tenant-wide drive-enumerering
│   ├── state.py                  # SQLite: delta-tokens + file→vector map
│   ├── unstructured_proc.py      # Unstructured wrapper + chunking
│   ├── embeddings.py             # OpenAI batched embeddings
│   ├── pinecone_store.py         # Pinecone upsert/delete + index bootstrap
│   ├── processor.py              # Per-fil pipeline
│   ├── sync.py                   # Orchestrator
│   ├── templates/                # Jinja2: base, landing, help, admin, login
│   └── static/                   # CSS + JS (vanilla, ingen build-step)
└── scripts/
    └── bootstrap_pinecone.py     # Opprett Pinecone-indeks (kjør én gang)
```

## Kom i gang lokalt

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env             # fyll inn de viktigste verdiene
python -m scripts.bootstrap_pinecone
uvicorn app.main:app --reload
```

Åpne `http://localhost:8000` for landing, `/help` for veileder, `/admin` for
innstillinger.

## Push til GitHub

Repoet er allerede `git init`-et med en initial commit. Opprett et tomt repo
på GitHub (`Byggkon/loki-ai` anbefales), så:

```bash
bash push.sh git@github.com:Byggkon/loki-ai.git
```

Skriptet håndterer både første push og senere remote-bytte.

## Deploy på Railway

```bash
railway init
railway link                                                # link til prosjektet
railway variables set $(grep -v '^#' .env | xargs)          # push miljøvariabler
railway volume create --mount-path /data                    # SQLite-state
railway up
```

`railway.json` har Dockerfile-bygg, `/healthz`-helsesjekk og
restart-on-failure-policy klare.

## Innstillinger

Innstillinger kan styres på to måter:

1. **Admin-UI** (`/admin`) — alle felt eksponert, organisert i grupper. Felt
   merket `restart` krever omstart for å ta effekt; resten oppdaterer live.
   Hemmeligheter krypteres med Fernet før de skrives til SQLite.
2. **Miljøvariabler** — bootstrap-defaults. Se `.env.example` for komplett
   liste. Hvis et felt finnes både i env og DB-overrides, vinner DB.

Påkrevde for første boot:

| Variabel | Hva |
|---|---|
| `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` | App registration |
| `OPENAI_API_KEY` | OpenAI |
| `PINECONE_API_KEY`, `PINECONE_INDEX` | Pinecone |
| `ADMIN_PASSWORD` | Passord for admin-UI (uten dette er UI-et utilgjengelig) |
| `ADMIN_SESSION_SECRET` | Lang tilfeldig streng (auto-genereres hvis blank) |

Resten kan stå som default eller redigeres senere i UI-et.

## Pinecone metadata-skjema

Hver vektor lagres med:

```jsonc
{
  "drive_id": "...",
  "drive_type": "personal | business | documentLibrary",
  "drive_owner": "alice@byggkon.no",
  "file_id": "01ABC...",
  "file_name": "Q4 plan.pdf",
  "file_path": "/drive/root:/Documents/Q4 plan.pdf",
  "web_url": "https://byggkon-my.sharepoint.com/...",
  "last_modified": "2026-04-29T10:14:00Z",
  "mime_type": "application/pdf",
  "chunk_index": 3,
  "text": "<chunk text>",
  "page_number": 5,
  "filetype": ".pdf"
}
```

Bruk `drive_id` eller `drive_owner` som filtre ved query-tid for å begrense
til en bruker eller et team.

## Sikkerhet

* **Admin-UI** beskyttes av et passord du setter via `ADMIN_PASSWORD`.
  Sesjonen er en signert cookie (itsdangerous) som varer
  `ADMIN_SESSION_HOURS` timer.
* **Hemmeligheter at-rest** krypteres symmetrisk (Fernet) med en nøkkel
  derivert fra `ADMIN_SESSION_SECRET`. Ikke beskyttelse mot host-kompromiss,
  men stopper tilfeldig disk-inspeksjon.
* **Cookies** settes med `Secure` automatisk når requesten kommer over
  HTTPS (også via `X-Forwarded-Proto` fra Railways proxy).
* **Graph-credentials** bør i produksjon kun stå i Railway-env, ikke i DB —
  klart skille mellom drift- og runtime-overstyringer er bevisst designet.

## Operasjonelt

* **Første kjøring på en stor tenant tar tid.** Initial delta returnerer
  *alle* nåværende filer per drive. Følg `/admin` → Kjøringer.
* **Delta-token utløpt (410 Gone)** håndteres automatisk: vi nullstiller
  drivens delta og full-resyncer den drivven på neste runde.
* **Rate limits:** alle Graph- og Pinecone-kall retries med eksponensiell
  backoff. Graphs `Retry-After` respekteres.
* **Per-fil isolasjon:** én feilende fil logges og videreføres — bryter ikke
  hele runden, og deltaLink avanserer ikke forbi feilet fil i samme side.

## Lisens

MIT — se [LICENSE](./LICENSE).
