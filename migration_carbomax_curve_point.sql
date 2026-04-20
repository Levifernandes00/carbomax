CREATE TABLE IF NOT EXISTS carbomax_curve_point (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    batch_id BIGINT NOT NULL,
    device_id BIGINT NOT NULL,
    point_index INTEGER NOT NULL,
    seconds NUMERIC NOT NULL,
    temperature NUMERIC,
    derivative NUMERIC,
    company_id TEXT NOT NULL,
    CONSTRAINT carbomax_curve_point_batch_fk
        FOREIGN KEY (batch_id) REFERENCES batch(id) ON DELETE CASCADE,
    CONSTRAINT carbomax_curve_point_device_fk
        FOREIGN KEY (device_id) REFERENCES device(id) ON DELETE CASCADE,
    CONSTRAINT carbomax_curve_point_unique
        UNIQUE (batch_id, device_id, point_index)
);

CREATE INDEX IF NOT EXISTS carbomax_curve_point_batch_idx
    ON carbomax_curve_point (batch_id);

CREATE INDEX IF NOT EXISTS carbomax_curve_point_device_idx
    ON carbomax_curve_point (device_id);

CREATE INDEX IF NOT EXISTS carbomax_curve_point_company_idx
    ON carbomax_curve_point (company_id);
