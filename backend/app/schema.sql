CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS incidents (
    id BIGSERIAL PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE,
    lat DOUBLE PRECISION NOT NULL,
    lng DOUBLE PRECISION NOT NULL,
    geom geography(Point, 4326) NOT NULL,
    category TEXT NOT NULL CHECK (
        category IN (
            'violent',
            'property',
            'disorder',
            'alarm',
            'traffic',
            'medical',
            'admin',
            'other'
        )
    ),
    severity DOUBLE PRECISION NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS incidents_geom_gix ON incidents USING GIST (geom);
CREATE INDEX IF NOT EXISTS incidents_timestamp_idx ON incidents (timestamp);

CREATE TABLE IF NOT EXISTS street_lights (
    id BIGSERIAL PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE,
    lat DOUBLE PRECISION NOT NULL,
    lng DOUBLE PRECISION NOT NULL,
    geom geography(Point, 4326) NOT NULL,
    severity DOUBLE PRECISION NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS street_lights_geom_gix ON street_lights USING GIST (geom);
CREATE INDEX IF NOT EXISTS street_lights_timestamp_idx ON street_lights (timestamp);

ALTER TABLE incidents ADD COLUMN IF NOT EXISTS headline TEXT;
ALTER TABLE street_lights ADD COLUMN IF NOT EXISTS headline TEXT;

CREATE TABLE IF NOT EXISTS sim_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    sim_now TIMESTAMPTZ NOT NULL,
    replay_start TIMESTAMPTZ NOT NULL,
    replay_end TIMESTAMPTZ NOT NULL
);

ALTER TABLE incidents ADD COLUMN IF NOT EXISTS is_transit BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS incidents_transit_idx ON incidents (timestamp) WHERE is_transit;

CREATE TABLE IF NOT EXISTS subway_stations (
    complex_id TEXT NOT NULL,
    station_id TEXT NOT NULL,
    gtfs_stop_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    borough TEXT,
    routes TEXT[] NOT NULL DEFAULT '{}',
    structure TEXT,
    ada BOOLEAN NOT NULL DEFAULT FALSE,
    lat DOUBLE PRECISION NOT NULL,
    lng DOUBLE PRECISION NOT NULL,
    geom geography(Point, 4326) NOT NULL
);

CREATE INDEX IF NOT EXISTS subway_stations_geom_gix ON subway_stations USING GIST (geom);
ALTER TABLE subway_stations DROP CONSTRAINT IF EXISTS subway_stations_pkey;
CREATE UNIQUE INDEX IF NOT EXISTS subway_stations_gtfs_stop_idx ON subway_stations (gtfs_stop_id);
CREATE INDEX IF NOT EXISTS subway_stations_complex_idx ON subway_stations (complex_id);

CREATE TABLE IF NOT EXISTS station_complaints (
    id BIGSERIAL PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE,
    complex_id TEXT,
    lat DOUBLE PRECISION NOT NULL,
    lng DOUBLE PRECISION NOT NULL,
    geom geography(Point, 4326) NOT NULL,
    category TEXT NOT NULL,
    severity DOUBLE PRECISION NOT NULL,
    law_cat TEXT,
    offense TEXT,
    timestamp TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS station_complaints_geom_gix ON station_complaints USING GIST (geom);
CREATE INDEX IF NOT EXISTS station_complaints_complex_ts_idx ON station_complaints (complex_id, timestamp);

CREATE TABLE IF NOT EXISTS station_ridership_hourly (
    complex_id TEXT NOT NULL,
    hour TIMESTAMPTZ NOT NULL,
    ridership INTEGER NOT NULL,
    PRIMARY KEY (complex_id, hour)
);

CREATE TABLE IF NOT EXISTS station_status_snapshots (
    id BIGSERIAL PRIMARY KEY,
    observed_at TIMESTAMPTZ NOT NULL,
    complex_id TEXT,
    route_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('alert', 'elevator', 'escalator')),
    severity DOUBLE PRECISION NOT NULL,
    summary TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS station_status_observed_idx ON station_status_snapshots (observed_at);

CREATE TABLE IF NOT EXISTS street_complaints (
    id BIGSERIAL PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE,
    lat DOUBLE PRECISION NOT NULL,
    lng DOUBLE PRECISION NOT NULL,
    geom geography(Point, 4326) NOT NULL,
    category TEXT NOT NULL,
    severity DOUBLE PRECISION NOT NULL,
    law_cat TEXT,
    offense TEXT,
    premise TEXT,
    local_hour SMALLINT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS street_complaints_geom_gix ON street_complaints USING GIST (geom);
CREATE INDEX IF NOT EXISTS street_complaints_hour_idx ON street_complaints (local_hour, timestamp);

CREATE TABLE IF NOT EXISTS ingest_runs (
    source TEXT NOT NULL,
    range_start TIMESTAMPTZ NOT NULL,
    range_end TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    rows_written INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ingest_runs_source_idx ON ingest_runs (source, range_start, range_end);

CREATE TABLE IF NOT EXISTS bus_ridership_hourly (
    bus_route TEXT NOT NULL,
    day_type TEXT NOT NULL,
    local_hour SMALLINT NOT NULL,
    riders_per_hour DOUBLE PRECISION NOT NULL,
    sample_hours INTEGER NOT NULL,
    PRIMARY KEY (bus_route, day_type, local_hour)
);
