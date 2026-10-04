"""Tests for the per-machine Redshift TLS certificate and `oblako trust`."""

from __future__ import annotations

import hashlib
import ssl

from cryptography import x509

from oblako.services import redshift


def test_ensure_cert_makes_a_server_only_cert_once(tmp_path):
    cert_dir = redshift.ensure_cert(tmp_path / "tls")
    crt = (cert_dir / "server.crt").read_bytes()
    assert oct((cert_dir / "server.key").stat().st_mode)[-3:] == "600"
    cert = x509.load_pem_x509_certificate(crt)
    assert (
        cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    )
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.DNSName) == ["localhost"]
    # kept across calls, so `oblako trust` stays valid
    assert (redshift.ensure_cert(tmp_path / "tls") / "server.crt").read_bytes() == crt


def test_two_machines_get_different_keys(tmp_path):
    a = redshift.ensure_cert(tmp_path / "a") / "server.key"
    b = redshift.ensure_cert(tmp_path / "b") / "server.key"
    assert a.read_bytes() != b.read_bytes()


def test_trust_replaces_the_legacy_cert(tmp_path):
    new = (redshift.ensure_cert(tmp_path / "tls") / "server.crt").read_text()
    old = (redshift.ensure_cert(tmp_path / "old") / "server.crt").read_text()
    bundle = tmp_path / "bundle.crt"
    bundle.write_text("# amazon\n" + old)
    der = ssl.PEM_cert_to_DER_cert(old)
    legacy = hashlib.sha256(der).hexdigest()
    assert redshift.remove_certs_from_bundle(str(bundle), {legacy}) == 1
    assert redshift.append_cert_to_bundle(str(bundle), new)
    text = bundle.read_text()
    assert old.strip() not in text and new.strip() in text and "# amazon" in text
    assert redshift.remove_certs_from_bundle(str(bundle), {legacy}) == 0
