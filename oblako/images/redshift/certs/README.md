# redshift-local TLS cert (local dev only)

A **fixed, self-signed** certificate baked into the image so every container
presents the *same* cert (stable across `docker compose down -v` and fresh
clones), which keeps a pinned `sslrootcert` or `oblako trust` valid.

`CN=localhost`, `subjectAltName=DNS:localhost,IP:127.0.0.1`. Clients that trust it
(via `sslrootcert=server.crt` or `oblako trust`) can use `sslmode=verify-full`.

This is intentionally committed. It is **not a secret**: it's a self-signed cert
that only fronts `localhost` in local development, trusted only by clients that
explicitly opt in. Do not reuse this key anywhere real. Override by mounting your
own cert at `/etc/oblako-redshift/`.
