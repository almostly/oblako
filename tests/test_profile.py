"""oblako configure writes a profile that points boto3 at oblako (no services)."""

import os

import boto3
import pytest

from oblako import ports
from oblako.profile import write_profile


@pytest.fixture
def aws_files(tmp_path, monkeypatch):
    config, credentials = tmp_path / "config", tmp_path / "credentials"
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    for var in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)
    for var in [v for v in os.environ if v.startswith("AWS_ENDPOINT_URL")]:
        monkeypatch.delenv(var)
    return config, credentials


def test_profile_points_each_service_at_oblako(aws_files):
    write_profile()
    session = boto3.Session(profile_name="oblako")
    expected = {
        "s3": ports.S3,
        "dynamodb": ports.DYNAMODB_VECTORS,
        "redshift-data": ports.REDSHIFT_DATA,
        "sts": ports.MOTO,
        "mwaa": ports.MWAA,
    }
    for service, port in expected.items():
        client = session.client(service)
        assert client.meta.endpoint_url == f"http://localhost:{port}", service
    s3 = session.client("s3")
    assert s3.meta.config.s3["addressing_style"] == "path"
    assert session.get_credentials().access_key.startswith("OBLAKO")


def test_other_profiles_stay_and_keys_survive_a_rerun(aws_files):
    config, credentials = aws_files
    config.write_text(
        "[default]\nregion = eu-west-1\n\n[profile prod]\nregion = us-west-2\n"
    )
    credentials.write_text(
        "[default]\naws_access_key_id = AKIAREAL\naws_secret_access_key = s\n"
    )
    write_profile()
    first = credentials.read_text()
    write_profile()
    assert credentials.read_text() == first  # same generated keys
    text = config.read_text()
    assert text.count("[profile oblako]") == 1 and text.count("[services oblako]") == 1
    assert "[profile prod]\nregion = us-west-2" in text
    assert "aws_access_key_id = AKIAREAL" in credentials.read_text()
    assert boto3.Session(profile_name="prod").region_name == "us-west-2"


def test_new_credentials_file_is_private(aws_files):
    _, credentials = aws_files
    write_profile()
    assert credentials.stat().st_mode & 0o777 == 0o600
