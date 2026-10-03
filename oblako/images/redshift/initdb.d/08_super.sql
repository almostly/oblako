-- Amazon Redshift SUPER (semi-structured) type, backed by PostgreSQL jsonb.
--
-- A domain over jsonb makes `col SUPER` parse and behave like Redshift's SUPER:
-- the jsonb operators (`->`, `->>`, `#>`, `#>>`) and, on PG14+, native
-- subscripting all work on it. That gives the *bracket* form of PartiQL for free
-- (`data['a']['b']`, `data['arr'][0]`); the *dot* form (`data.a.b`) is turned into
-- a jsonb path by the wire proxy, since PostgreSQL parses `a.b.c` as table.column.
CREATE DOMAIN super AS jsonb;

-- Redshift's built-ins live in pg_catalog, so these are created there (see
-- 99_system_catalog.sql); allow_system_table_mods permits it (superuser).
SET allow_system_table_mods = on;
-- every node creates these itself; Citus must not replay them (a worker refuses
-- pg_catalog and pg_ schemas from a replay). A placeholder without Citus.
SET citus.enable_ddl_propagation = off;
SET search_path = pg_catalog, public;

-- JSON_PARSE('...') -> SUPER: parse (and validate) JSON text into a SUPER value.
CREATE OR REPLACE FUNCTION json_parse(s text)
RETURNS super IMMUTABLE AS $$
    SELECT s::jsonb;
$$ LANGUAGE sql;

-- JSON_SERIALIZE(super) -> text: the JSON text of a SUPER value.
CREATE OR REPLACE FUNCTION json_serialize(s super)
RETURNS text IMMUTABLE AS $$
    SELECT s::jsonb::text;
$$ LANGUAGE sql;

-- JSON_TYPEOF(super) -> text: 'object' | 'array' | 'string' | 'number' | ...
CREATE OR REPLACE FUNCTION json_typeof(s super)
RETURNS text IMMUTABLE AS $$
    SELECT jsonb_typeof(s::jsonb);
$$ LANGUAGE sql;

-- Text overloads: PartiQL navigation (data.a.b) yields JSON *text*, so a SUPER
-- value passed through it reaches these functions as text. Accept it: serialize
-- is identity (already JSON text), typeof parses it.
CREATE OR REPLACE FUNCTION json_serialize(s text)
RETURNS text IMMUTABLE AS $$
    SELECT s;
$$ LANGUAGE sql;

CREATE OR REPLACE FUNCTION json_typeof(s text)
RETURNS text IMMUTABLE AS $$
    SELECT jsonb_typeof(s::jsonb);
$$ LANGUAGE sql;

-- IS_VALID_JSON / IS_VALID_JSON_ARRAY: does the text parse as a JSON value / array?
CREATE OR REPLACE FUNCTION is_valid_json(s text)
RETURNS boolean IMMUTABLE AS $$
import json
try:
    json.loads(s)
    return True
except Exception:
    return False
$$ LANGUAGE plpython3u;

CREATE OR REPLACE FUNCTION is_valid_json_array(s text)
RETURNS boolean IMMUTABLE AS $$
import json
try:
    return isinstance(json.loads(s), list)
except Exception:
    return False
$$ LANGUAGE plpython3u;

-- back to the session defaults, for whoever runs this file next in the session
RESET search_path;
RESET allow_system_table_mods;
RESET citus.enable_ddl_propagation;
