"""Tests for the local AppConfig: rule evaluator (pure) + the in-process server.

The server tests start the AppConfig ASGI app in a thread (a local port, no
Docker) and drive it with real boto3 appconfig / appconfigdata clients, then the
bundled agent — so they run in the unit job like the CloudFormation server tests.
"""

from __future__ import annotations

import json

import boto3
import pytest

from oblako.engines.appconfig import evaluate_config

# A generic feature-flags config (no domain specifics).
FLAGS = {
    "version": "1",
    "values": {
        "new_checkout": {
            "enabled": True,
            "_variants": [
                {"name": "treatment", "enabled": True,
                 "attributeValues": {"discount": 0.1},
                 "rule": '(and (eq $tier "vip") (split by::$userId pct::50 seed::"co"))'},
                {"name": "control", "enabled": True},
            ],
        }
    },
}


# Rule evaluator — pure, no server.
def test_eq_guard_and_default_variant():
    vip = evaluate_config(FLAGS["values"], {"tier": "vip", "userId": "u-1"})
    assert vip["new_checkout"]["_variant"] in ("treatment", "control")
    basic = evaluate_config(FLAGS["values"], {"tier": "basic", "userId": "u-1"})
    assert basic["new_checkout"]["_variant"] == "control"  # eq guard fails -> default


def test_split_is_deterministic_and_roughly_pct():
    def variant(uid):
        return evaluate_config(FLAGS["values"], {"tier": "vip", "userId": uid})[
            "new_checkout"]["_variant"]

    assert variant("u-42") == variant("u-42")  # deterministic
    treat = sum(variant(f"u-{i}") == "treatment" for i in range(2000))
    assert 40 <= treat / 2000 * 100 <= 60  # rule pct::50


def test_treatment_promotes_attribute_values():
    # find a userId that lands in the treatment bucket
    uid = next(f"u-{i}" for i in range(2000)
               if evaluate_config(FLAGS["values"], {"tier": "vip", "userId": f"u-{i}"})
               ["new_checkout"]["_variant"] == "treatment")
    flag = evaluate_config(FLAGS["values"], {"tier": "vip", "userId": uid})["new_checkout"]
    assert flag["discount"] == 0.1


# In-process server — real boto3 clients + the agent.
@pytest.fixture(scope="module")
def appconfig_url():
    from oblako.engines.appconfig import AppConfigStore, start_in_thread

    return start_in_thread(port=8913, store=AppConfigStore())


def _client(service, url):
    return boto3.client(service, endpoint_url=url, region_name="us-east-1",
                        aws_access_key_id="test", aws_secret_access_key="test")


def test_control_data_and_agent_end_to_end(appconfig_url):
    ac = _client("appconfig", appconfig_url)
    app = ac.create_application(Name="demo-app")
    ac.create_environment(ApplicationId=app["Id"], Name="dev")
    prof = ac.create_configuration_profile(
        ApplicationId=app["Id"], Name="feature-flags", LocationUri="hosted",
        Type="AWS.AppConfig.FeatureFlags")
    ver = ac.create_hosted_configuration_version(
        ApplicationId=app["Id"], ConfigurationProfileId=prof["Id"],
        Content=json.dumps(FLAGS).encode(), ContentType="application/json")
    assert ver["VersionNumber"] == 1

    # data plane returns RAW content (AWS-faithful — agent does the evaluation)
    data = _client("appconfigdata", appconfig_url)
    sess = data.start_configuration_session(
        ApplicationIdentifier="demo-app", EnvironmentIdentifier="dev",
        ConfigurationProfileIdentifier="feature-flags")
    latest = data.get_latest_configuration(
        ConfigurationToken=sess["InitialConfigurationToken"])
    raw = json.loads(latest["Configuration"].read())
    assert "_variants" in raw["values"]["new_checkout"]

    # the agent evaluates with context
    from oblako.engines.appconfig import AppConfigClient
    agent = AppConfigClient(endpoint_url=appconfig_url, region="us-east-1",
                            aws_access_key_id="test", aws_secret_access_key="test")
    assert agent.get_variant("demo-app", "dev", "feature-flags",
                             {"tier": "basic", "userId": "u-1"}, "new_checkout") == "control"
