"""Quote Redshift's bare datepart keywords, for the wire proxy.

Redshift:   DATEADD(month, 1, ts)    DATEDIFF(day, a, b)    DATE_PART(dow, ts)
PostgreSQL: DATEADD('month', 1, ts)  DATEDIFF('day', a, b)  DATE_PART('dow', ts)

Redshift accepts the datepart either as a bare keyword or as a string; PostgreSQL
parses a bare word as a column reference ("column month does not exist"). The
DATEADD/DATEDIFF shims (initdb.d/03_date_functions.sql) and PostgreSQL's own
date_part take the string form, so the proxy quotes the keyword. Only known
datepart names are quoted, so a genuine column argument is left alone, and string
literals are copied verbatim.
"""

from __future__ import annotations

import re

# Redshift's datepart names and abbreviations (DATEADD/DATEDIFF/DATE_PART), see
# https://docs.aws.amazon.com/redshift/latest/dg/r_Dateparts_for_datetime_functions.html
DATEPARTS = frozenset(
    """
    millennium millennia mil mils
    century centuries c cent cents
    decade decades dec decs
    epoch
    year years y yr yrs
    quarter quarters q qtr qtrs
    month months mon mons mm
    week weeks w wk wks
    dayofweek dow dw weekday
    dayofyear doy dy yd
    day days d
    hour hours h hr hrs
    minute minutes m min mins
    second seconds s sec secs
    millisecond milliseconds ms msec msecs msecond mseconds millisec millisecs millisecon
    microsecond microseconds microsec microsecs usecond useconds us usec usecs
    timezone timezone_hour timezone_minute
    """.split()
)

_CALL = re.compile(r"(?i)\b(dateadd|datediff|date_part)(\s*\(\s*)([a-z_]+)(\s*,)")


def _quote(m: re.Match) -> str:
    """Quote a bare datepart keyword; leave anything else as written."""
    part = m.group(3)
    if part.lower() not in DATEPARTS:
        return m.group(0)
    return f"{m.group(1)}{m.group(2)}'{part.lower()}'{m.group(4)}"


def rewrite_dateparts(sql: str) -> str:
    """Quote the bare datepart of every DATEADD/DATEDIFF/DATE_PART call."""
    low = sql.lower()
    if "dateadd" not in low and "datediff" not in low and "date_part" not in low:
        return sql
    out: list[str] = []
    i, n, start = 0, len(sql), 0
    while i < n:
        if sql[i] != "'":
            i += 1
            continue
        out.append(_CALL.sub(_quote, sql[start:i]))  # code before the literal
        j = i + 1  # copy the literal verbatim ('' is an escaped quote)
        while j < n:
            if sql[j] == "'":
                if j + 1 < n and sql[j + 1] == "'":
                    j += 2
                    continue
                j += 1
                break
            j += 1
        out.append(sql[i:j])
        i = start = j
    out.append(_CALL.sub(_quote, sql[start:]))
    return "".join(out)
