-- Redshift ML: CREATE / SHOW / DROP MODEL and svv_ml_model_info.
--
-- The wire proxy rewrites the MODEL statements into calls to these functions (see
-- proxy/redshift_ml.py). CREATE MODEL validates and records the model as TRAINING;
-- the redshift-ml agent (started by the entrypoint) trains it in a container on
-- the host Docker daemon and publishes the prediction function, as Redshift does
-- asynchronously with SageMaker. Idempotent, so 10_ re-applies it per database.

CREATE EXTENSION IF NOT EXISTS plpython3u;
-- pg_ marks the schema as the system's, as Redshift's own internal schemas are,
-- so SQLAlchemy, Alembic and schema browsers leave it out; creating one needs
-- allow_system_table_mods (superuser).
SET allow_system_table_mods = on;
-- every node creates these itself; Citus must not replay them (a worker refuses
-- pg_catalog and pg_ schemas from a replay). A placeholder without Citus.
SET citus.enable_ddl_propagation = off;
CREATE SCHEMA IF NOT EXISTS pg_oblako;

CREATE TABLE IF NOT EXISTS pg_oblako.models (
    schema_name       text NOT NULL,
    model_name        text NOT NULL,
    owner             text NOT NULL,
    function_name     text NOT NULL,
    target            text NOT NULL,
    query             text NOT NULL,
    features          text NOT NULL,   -- JSON list of feature column names
    spec              text NOT NULL,   -- JSON: the parsed CREATE MODEL statement
    model_state       text NOT NULL,   -- TRAINING | READY | FAILED
    failure_reason    text,
    model_type        text,
    problem_type      text,
    model             text,            -- JSON export the prediction function reads
    metrics           text,            -- JSON: validation metric, Autopilot candidates
    version           text NOT NULL UNIQUE,
    training_job_name text,
    train_seconds     integer,
    created_at        timestamp NOT NULL DEFAULT now()::timestamp(0),
    claimed_at        timestamp,
    trained_at        timestamp,
    PRIMARY KEY (schema_name, model_name)
);

-- The functions below run as the caller, so the training query is checked
-- against the caller's own privileges; users record and drop models, and only
-- the agent (superuser) updates them.
GRANT USAGE ON SCHEMA pg_oblako TO PUBLIC;
GRANT SELECT, INSERT, DELETE ON pg_oblako.models TO PUBLIC;

-- The functions and svv_ml_model_info are Redshift's built-ins, so they go to
-- pg_catalog (see 99_system_catalog.sql). The agent re-runs this file on every
-- start, after the entrypoint's migration, so it must not create them in public.
SET search_path = pg_catalog, public;

CREATE OR REPLACE FUNCTION oblako_ml_create_model(stmt text)
RETURNS void AS $$
import redshift_ml
redshift_ml.create_model(plpy, stmt)
$$ LANGUAGE plpython3u;

CREATE OR REPLACE FUNCTION oblako_ml_drop_model(name text, if_exists boolean)
RETURNS void AS $$
import redshift_ml
redshift_ml.drop_model(plpy, name, if_exists)
$$ LANGUAGE plpython3u;

CREATE OR REPLACE FUNCTION oblako_ml_show_model(name text)
RETURNS TABLE ("Key" text, "Value" text) AS $$
import redshift_ml
return redshift_ml.show_model(plpy, name)
$$ LANGUAGE plpython3u;

CREATE OR REPLACE FUNCTION oblako_ml_show_models()
RETURNS TABLE (schema_name text, model_name text) AS $$
import redshift_ml
return redshift_ml.show_models(plpy)
$$ LANGUAGE plpython3u;

-- The columns Redshift's SVV_ML_MODEL_INFO has. model_state reads
-- "Model is Ready" once trained; a failure puts the reason there instead.
CREATE OR REPLACE VIEW svv_ml_model_info AS
SELECT current_database()::char(128) AS database_name,
       schema_name::char(128)        AS schema_name,
       owner::char(128)              AS user_name,
       model_name::char(128)         AS model_name,
       'PERSISTED'::char(20)         AS life_cycle,
       1                             AS is_refreshable,
       (CASE model_state
            WHEN 'READY'    THEN 'Model is Ready'
            WHEN 'TRAINING' THEN 'TRAINING'
            ELSE coalesce(failure_reason, model_state)
        END)::char(128)              AS model_state
FROM pg_oblako.models;

-- back to the session defaults, for whoever runs this file next in the session
RESET search_path;
RESET allow_system_table_mods;
RESET citus.enable_ddl_propagation;
