"""Redshift's case folding of quoted identifiers, imported by the proxy.

Redshift folds the ASCII letters of a database, schema, table or column name to
lower case even inside double quotes ("Mixed" names the table mixed), unless the
session sets enable_case_sensitive_identifier to true. A user name in double
quotes always keeps its case ("IAM:alice"); redshift-local keeps role and group
names as written too. PostgreSQL keeps every quoted identifier's case, so the
proxy lower-cases quoted object names before the engine sees them.

A quoted name is a principal, and kept, when it follows USER, ROLE, GROUP,
AUTHORIZATION or OWNER TO (and the names listed after it), when it is a grantee
(after TO or FROM in GRANT, REVOKE, ATTACH or DETACH), or anywhere in CREATE,
ALTER or DROP USER / ROLE / GROUP. Strings, dollar quotes and comments are left
alone. Pure-stdlib.
"""

from __future__ import annotations

import re

# matched at the start of each statement
_PRINCIPAL_STATEMENT = re.compile(
    r"(?is)\s*(?:create|alter|drop)\s+(?:user|role|group)\b"
)
_GRANTEE_STATEMENT = re.compile(
    r"(?is)\s*(?:grant|revoke|attach|detach|alter\s+default\s+privileges)\b"
)
_SETTING = re.compile(
    r"(?is)\s*(?:set\s+(?:session\s+)?enable_case_sensitive_identifier\s*(?:to|=)\s*"
    r"'?(\w+)'?|reset\s+(enable_case_sensitive_identifier|all))\s*(?:;|$)"
)
_TRUE = {"true", "on", "1", "yes"}
_PRINCIPAL_BEFORE = {"user", "role", "group", "authorization"}
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_DOLLAR = re.compile(r"\$([A-Za-z_]\w*)?\$")


def fold(sql: str, sensitive: bool = False) -> tuple[str, bool]:
    """Lower-case the quoted object names in ``sql``, as Redshift does.

    ``sensitive`` is the session's enable_case_sensitive_identifier; a SET or
    RESET of it in ``sql`` takes effect from the next statement. Returns the
    folded SQL and the setting after it.
    """
    out: list[str] = []
    i, n = 0, len(sql)

    def start(pos: int) -> None:
        """Begin a statement at ``pos``: reset what the scan knows about it."""
        nonlocal sensitive, keep_all, grantees, after_to, words, named, depth
        if m := _SETTING.match(sql, pos):
            # RESET goes back to Redshift's default, off
            sensitive = (m.group(1) or "").lower() in _TRUE
        keep_all = sensitive or bool(_PRINCIPAL_STATEMENT.match(sql, pos))
        grantees = bool(_GRANTEE_STATEMENT.match(sql, pos))
        after_to = False  # past a GRANT/REVOKE/ATTACH/DETACH's TO or FROM: grantees
        words = [""]  # significant tokens so far, words lower-cased
        named = [False]  # per token: a principal's name
        depth = 0

    keep_all = grantees = after_to = False
    words: list[str] = []
    named: list[bool] = []
    depth = 0
    start(0)

    def principal() -> bool:
        """Whether the name about to be read names a user, role or group."""
        return (
            after_to
            or words[-1] in _PRINCIPAL_BEFORE
            or words[-2:] == ["owner", "to"]
            or (words[-1] == "," and named[-2])
        )

    while i < n:
        ch = sql[i]
        if ch == "'":  # string literal; '' is an escaped quote
            escapes = sql[i - 1 : i] in ("e", "E") and not sql[i - 2 : i - 1].isalnum()
            j = i + 1
            while j < n:
                if sql[j : j + 2] == "''" or (escapes and sql[j] == "\\"):
                    j += 2
                elif sql[j] == "'":
                    break
                else:
                    j += 1
            j = min(j, n - 1)
            token, kind = sql[i : j + 1], "'"
        elif ch == "$" and (m := _DOLLAR.match(sql, i)):
            end = sql.find(m.group(0), m.end())
            j = n - 1 if end < 0 else end + len(m.group(0)) - 1
            token, kind = sql[i : j + 1], "$"
        elif sql.startswith("--", i) or sql.startswith("/*", i):
            close = "\n" if ch == "-" else "*/"
            end = sql.find(close, i + 2)
            j = n - 1 if end < 0 else end + len(close) - 1
            out.append(sql[i : j + 1])
            i = j + 1
            continue
        elif ch == '"':
            j = i + 1
            while j < n and (sql[j] != '"' or sql[j : j + 2] == '""'):
                j += 2 if sql[j : j + 2] == '""' else 1
            ident = sql[i : j + 1]
            keep = keep_all or principal()
            out.append(ident if keep else ident.translate(_ASCII_LOWER))
            words.append('"')
            named.append(keep)
            i = j + 1
            continue
        elif ch.isalpha() or ch == "_":
            j = i
            while j + 1 < n and (sql[j + 1].isalnum() or sql[j + 1] in "_$"):
                j += 1
            token, kind = sql[i : j + 1], sql[i : j + 1].lower()
            keep = principal()
            if grantees and depth == 0 and kind in ("to", "from"):
                after_to = True
            out.append(token)
            words.append(kind)
            named.append(keep)
            i = j + 1
            continue
        elif ch == ";" and depth == 0:
            out.append(ch)
            i += 1
            start(i)
            continue
        else:
            j, token, kind = i, ch, ch
            depth += (ch == "(") - (ch == ")")
        out.append(token)
        if not token.isspace():
            words.append(kind)
            named.append(False)
        i = j + 1
    return "".join(out), sensitive
