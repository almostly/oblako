"""Unit test for the redshift trust-cert CA-bundle append (no services)."""

from oblako.services.redshift import append_cert_to_bundle

CERT = "-----BEGIN CERTIFICATE-----\nMIIBoblakoTESTcert==\n-----END CERTIFICATE-----"


def test_append_is_idempotent(tmp_path):
    bundle = tmp_path / "redshift-ca-bundle.crt"
    bundle.write_text(
        "-----BEGIN CERTIFICATE-----\nEXISTING\n-----END CERTIFICATE-----\n"
    )
    # first append adds it, keeps the existing cert
    assert append_cert_to_bundle(str(bundle), CERT) is True
    text = bundle.read_text()
    assert CERT in text
    assert "EXISTING" in text
    # second append is a no-op (already trusted)
    assert append_cert_to_bundle(str(bundle), CERT) is False
    assert bundle.read_text() == text
