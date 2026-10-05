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


def test_two_machines_get_different_keys_and_subjects(tmp_path):
    a = redshift.ensure_cert(tmp_path / "a")
    b = redshift.ensure_cert(tmp_path / "b")
    assert (a / "server.key").read_bytes() != (b / "server.key").read_bytes()
    # OpenSSL finds a trusted self-signed cert by subject: two must not share one
    subject_a, subject_b = (
        x509.load_pem_x509_certificate((d / "server.crt").read_bytes()).subject
        for d in (a, b)
    )
    assert subject_a != subject_b


def test_trust_drops_stale_oblako_certs(tmp_path, monkeypatch):
    current = (redshift.ensure_cert(tmp_path / "tls") / "server.crt").read_text()
    stale = (redshift.ensure_cert(tmp_path / "gone") / "server.crt").read_text()
    site = tmp_path / "site"
    bundle = site / "redshift_connector" / "files" / "redshift-ca-bundle.crt"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("# amazon\n" + stale)
    monkeypatch.setattr(redshift, "_redshift_connector_bundle", lambda exe: str(bundle))
    monkeypatch.setattr(redshift, "_site_packages", lambda exe: site)
    monkeypatch.setattr(
        redshift.RedshiftService, "server_certs", lambda self: [current]
    )
    redshift.RedshiftService.trust_cert(object.__new__(redshift.RedshiftService), "py")
    text = bundle.read_text()
    assert current.strip() in text and stale.strip() not in text and "# amazon" in text
    assert redshift.kept_certs(site) == [current.strip()]


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


def test_the_keeper_restores_trust_after_a_reinstall(tmp_path, monkeypatch):
    """The keeper re-appends the certificates a redshift-connector reinstall dropped."""
    import importlib
    import sys

    cert = (redshift.ensure_cert(tmp_path / "tls") / "server.crt").read_text()
    # a stand-in redshift_connector package with its pristine CA bundle
    pkg = tmp_path / "site" / "redshift_connector"
    (pkg / "files").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    bundle = pkg / "files" / "redshift-ca-bundle.crt"
    bundle.write_text("# amazon\n")
    site = tmp_path / "site"
    redshift.write_trust_keeper(site, [cert])
    assert (site / f"{redshift.KEEPER_MODULE}.pth").read_text() == (
        f"import {redshift.KEEPER_MODULE}\n"
    )
    assert redshift.kept_certs(site) == [cert.strip()]
    monkeypatch.syspath_prepend(str(site))
    # an imported redshift_connector (other tests') would be found first
    for module in [m for m in sys.modules if m.split(".")[0] == "redshift_connector"]:
        monkeypatch.delitem(sys.modules, module)
    sys.modules.pop(redshift.KEEPER_MODULE, None)
    importlib.import_module(redshift.KEEPER_MODULE)  # what the .pth does at start
    assert cert.strip() in bundle.read_text() and "# amazon" in bundle.read_text()
    importlib.reload(sys.modules[redshift.KEEPER_MODULE])  # idempotent
    assert bundle.read_text().count("BEGIN CERTIFICATE") == 1
    redshift.remove_trust_keeper(site)
    assert not (site / f"{redshift.KEEPER_MODULE}.py").exists()
    assert redshift.kept_certs(site) == []
