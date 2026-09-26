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

CREATE TABLE IF NOT EXISTS sim_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    sim_now TIMESTAMPTZ NOT NULL,
    replay_start TIMESTAMPTZ NOT NULL,
    replay_end TIMESTAMPTZ NOT NULL
);
