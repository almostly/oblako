"""Unit tests for the bundled Step Functions ML templates and mock-config builder."""

from oblako.services import sfn_templates


def _task_states(definition: dict) -> set[str]:
    return {name for name, s in definition["States"].items() if s.get("Type") == "Task"}


def test_public_templates_shape():
    tpls = {t["id"]: t for t in sfn_templates.public_templates()}
    assert set(tpls) == {
        "preprocess-train",
        "train-batch-transform",
        "hpo-batch-transform",
        "bedrock-reason-codes",
    }
    # All four are runnable. The 3 SageMaker ones run mock-mode; Bedrock runs live.
    for tid in tpls:
        assert tpls[tid]["runnable"] is True
    assert tpls["bedrock-reason-codes"]["execMode"] == "live"
    assert tpls["bedrock-reason-codes"]["testCase"] is None
    for tid in ("preprocess-train", "train-batch-transform", "hpo-batch-transform"):
        assert tpls[tid]["execMode"] == "mock"


def test_definitions_are_well_formed():
    for tpl in sfn_templates.TEMPLATES.values():
        d = tpl["definition"]
        states = d["States"]
        assert d["StartAt"] in states
        for name, state in states.items():
            target = state.get("Next") or state.get("Default")
            if target is not None:
                assert target in states, (
                    f"{name} -> {target} (missing) in {tpl['name']}"
                )
            for choice in state.get("Choices", []):
                assert choice["Next"] in states


def test_mock_keys_map_to_task_states():
    # Every mocked state must be a real Task state in the same definition.
    for tpl in sfn_templates.TEMPLATES.values():
        if not tpl.get("mock"):  # live templates (Bedrock) carry no mock
            continue
        tasks = _task_states(tpl["definition"])
        for state_name in tpl["mock"]:
            assert state_name in tasks, f"{state_name} not a Task in {tpl['name']}"
        for short in tpl["mock"].values():
            assert short in tpl["responses"], (
                f"missing response {short} in {tpl['name']}"
            )


def test_build_mock_config_only_mock_templates_and_resolvable():
    cfg = sfn_templates.build_mock_config()
    mock_sms = {t["name"] for t in sfn_templates.TEMPLATES.values() if t.get("mock")}
    assert set(cfg["StateMachines"]) == mock_sms
    # Bedrock runs live (no mock), so it must not appear in the mock config.
    assert "credit-bedrock-reason-codes" not in cfg["StateMachines"]
    # Every response referenced by a test case must exist and carry a Return at retry 0.
    for sm in cfg["StateMachines"].values():
        for case in sm["TestCases"].values():
            for resp_key in case.values():
                assert resp_key in cfg["MockedResponses"]
                assert "Return" in cfg["MockedResponses"][resp_key]["0"]


def test_build_mock_config_subset():
    cfg = sfn_templates.build_mock_config(names=["credit-preprocess-train"])
    assert set(cfg["StateMachines"]) == {"credit-preprocess-train"}
