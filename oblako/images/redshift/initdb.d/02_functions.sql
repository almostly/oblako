-- Amazon Redshift JSON / scalar UDFs, ported from hearthsim/pgredshift to
-- plpython3u (the originals targeted Python 2 plpythonu).

-- NB: `json_array` can't be a parameter name on PG16 (JSON_ARRAY is now a
-- reserved SQL/JSON keyword), so the array params are named `arr`.
CREATE OR REPLACE FUNCTION json_extract_array_element_text(arr text, array_index int)
RETURNS text IMMUTABLE AS $$
import json
return json.dumps(json.loads(arr)[array_index])
$$ LANGUAGE plpython3u;

CREATE OR REPLACE FUNCTION json_extract_path_text(json_string text, VARIADIC path_elems text[])
RETURNS text IMMUTABLE AS $$
import json
result = json.loads(json_string)
for elem in path_elems:
    if not isinstance(result, dict) or elem not in result:
        return ""
    result = result[elem]
return result if isinstance(result, str) else json.dumps(result)
$$ LANGUAGE plpython3u;

CREATE OR REPLACE FUNCTION json_array_length(arr text)
RETURNS int IMMUTABLE AS $$
import json
return len(json.loads(arr))
$$ LANGUAGE plpython3u;

-- Redshift DECODE(expr, search, result, default) — distinct from PostgreSQL's
-- binary decode(), so this overloads on the all-integer signature.
CREATE OR REPLACE FUNCTION decode(expression int, search int, result int, "default" int)
RETURNS int IMMUTABLE AS $$
	SELECT CASE WHEN expression = search THEN result ELSE "default" END;
$$ LANGUAGE sql;

-- MEDIAN aggregate.
CREATE OR REPLACE FUNCTION _final_median(numeric[]) RETURNS numeric IMMUTABLE AS $$
	SELECT AVG(val) FROM (
		SELECT val FROM unnest($1) val
		ORDER BY 1
		LIMIT 2 - MOD(array_upper($1, 1), 2)
		OFFSET CEIL(array_upper($1, 1) / 2.0) - 1
	) sub;
$$ LANGUAGE sql;

DROP AGGREGATE IF EXISTS median(numeric);
CREATE AGGREGATE median(numeric) (
	SFUNC=array_append, STYPE=numeric[], FINALFUNC=_final_median, INITCOND='{}'
);
