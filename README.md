# NYC Safe Routing

Crime-informed routing for New York. NYPD Calls for Service are stored in PostGIS and replayed on a simulation clock, so a historical hour can play out on the map in the length of a pitch.

## Storage

Each CAD event becomes one `incidents` row:

| column | source |
| --- | --- |
| `source_id` | `cad_evnt_id` (unique; re-ingest updates the row) |
| `lat`, `lng` | call coordinates |
| `geom` | `geography(Point, 4326)` with a GIST index |
| `category` | closed set derived from `typ_desc` |
| `severity` | numeric weight, raised slightly for critical CIP jobs |
| `timestamp` | `add_ts`, read as `America/New_York` |

`sim_state` holds the shared clock (`sim_now`, replay start, replay end). Map playback and later route scoring both filter `timestamp <= sim_now`. The Open Data API is read when you load a window, not polled while the map is playing.

## Run

Postgres with PostGIS, then:

```bash
createdb safepath
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

```bash
cd frontend
npm install
cp .env.example .env.local
# set NEXT_PUBLIC_GOOGLE_MAPS_API_KEY
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). Pick a start time inside the dataset (currently through June 30, 2026), a history window, and a playback length, then Load and Play.

Docker alternative, from the repo root: `docker compose up -d`, and point `DATABASE_URL` at `postgresql://safepath:safepath@localhost:5432/safepath`.
