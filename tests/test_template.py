"""Guardrails over template.yaml: every cost and safety rule the design note sets (WG-13)."""

import importlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from webhook_gateway.common.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "template.yaml"
TAG = {"Key": "project", "Value": "webhook-gateway"}


class CloudFormationLoader(yaml.SafeLoader):
    """Reads CloudFormation's short forms (!Ref, !Sub, !GetAtt) as their long forms."""


def _intrinsic(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> dict[str, Any]:
    value: Any
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node, deep=True)
    if suffix == "GetAtt" and isinstance(value, str):
        value = value.split(".", 1)
    return {"Ref" if suffix == "Ref" else f"Fn::{suffix}": value}


CloudFormationLoader.add_multi_constructor("!", _intrinsic)


def template() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.load(TEMPLATE.read_text(), Loader=CloudFormationLoader)
    return loaded


def resources(kind: str | None = None) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = template()["Resources"]
    return {name: r for name, r in found.items() if kind is None or r["Type"] == kind}


def properties(name: str) -> dict[str, Any]:
    found: dict[str, Any] = template()["Resources"][name]["Properties"]
    return found


def functions() -> dict[str, dict[str, Any]]:
    return {name: r["Properties"] for name, r in resources("AWS::Serverless::Function").items()}


def default(value: Any) -> Any:
    """A literal, or the default of the parameter it refers to."""
    if isinstance(value, dict) and "Ref" in value:
        return template()["Parameters"][value["Ref"]]["Default"]
    return value


def keys(value: Any) -> Iterator[str]:
    """Every mapping key anywhere in the template."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from keys(item)


def test_it_is_a_sam_template() -> None:
    assert template()["Transform"] == "AWS::Serverless-2016-10-31"


# HTTP API, not REST API ---------------------------------------------------------------


def test_the_only_api_is_an_http_api() -> None:
    types = {r["Type"] for r in resources().values()}

    assert "AWS::Serverless::HttpApi" in types
    assert not types & {"AWS::Serverless::Api", "AWS::ApiGateway::RestApi"}


def test_ingest_is_routed_from_post_webhooks_provider() -> None:
    assert functions()["IngestFunction"]["Events"] == {
        "Webhook": {
            "Type": "HttpApi",
            "Properties": {
                "ApiId": {"Ref": "WebhookApi"},
                "Method": "POST",
                "Path": "/webhooks/{provider}",
                "PayloadFormatVersion": "2.0",
            },
        }
    }


# API throttling ---------------------------------------------------------------------


def test_the_api_is_throttled_to_20_a_second_with_bursts_of_40() -> None:
    route = properties("WebhookApi")["DefaultRouteSettings"]

    assert default(route["ThrottlingRateLimit"]) == 20
    assert default(route["ThrottlingBurstLimit"]) == 40


# arm64, reserved concurrency, short timeout ----------------------------------------------


def test_functions_run_the_projects_python_on_arm64() -> None:
    function_globals = template()["Globals"]["Function"]
    version = (ROOT / ".python-version").read_text().strip()

    assert function_globals["Runtime"] == f"python{version}"
    assert function_globals["Architectures"] == ["arm64"]
    for name, props in functions().items():
        assert not {"Runtime", "Architectures"} & set(props), name


def test_ingest_answers_within_5_seconds_on_256_mb() -> None:
    function_globals = template()["Globals"]["Function"]

    assert (function_globals["Timeout"], function_globals["MemorySize"]) == (5, 256)
    assert not {"Timeout", "MemorySize"} & set(functions()["IngestFunction"])


def test_every_function_has_a_reserved_concurrency_cap() -> None:
    for name, props in functions().items():
        assert "ReservedConcurrentExecutions" in props, name

    assert default(functions()["IngestFunction"]["ReservedConcurrentExecutions"]) == 10


# Queues ----------------------------------------------------------------------------------


def test_the_main_queue_moves_a_message_to_the_dlq_after_5_receives() -> None:
    assert properties("EventsQueue")["RedrivePolicy"] == {
        "deadLetterTargetArn": {"Fn::GetAtt": ["DeadLetterQueue", "Arn"]},
        "maxReceiveCount": 5,
    }


def test_queue_timings_follow_the_design_note() -> None:
    assert properties("EventsQueue")["VisibilityTimeout"] == 180
    assert properties("DeadLetterQueue")["MessageRetentionPeriod"] == 14 * 24 * 60 * 60


def test_every_queue_is_encrypted_with_sse_sqs() -> None:
    queues = resources("AWS::SQS::Queue")

    assert set(queues) == {"EventsQueue", "DeadLetterQueue"}
    for name, queue in queues.items():
        assert queue["Properties"]["SqsManagedSseEnabled"] is True, name


def test_ingest_may_send_to_the_main_queue_and_nothing_else() -> None:
    assert functions()["IngestFunction"]["Policies"] == [
        {"SQSSendMessagePolicy": {"QueueName": {"Fn::GetAtt": ["EventsQueue", "QueueName"]}}}
    ]


# 7-day log retention ---------------------------------------------------------------------


def test_every_function_logs_to_a_7_day_group_the_stack_owns() -> None:
    for name, props in functions().items():
        group = template()["Resources"][props["LoggingConfig"]["LogGroup"]["Ref"]]
        assert group["Type"] == "AWS::Logs::LogGroup", name
        assert group["Properties"]["RetentionInDays"] == 7, name
        assert {group.get("DeletionPolicy"), group.get("UpdateReplacePolicy")} <= {None, "Delete"}


def test_each_log_group_is_named_after_its_function() -> None:
    for props in functions().values():
        group = properties(props["LoggingConfig"]["LogGroup"]["Ref"])
        function_name = props["FunctionName"]["Fn::Sub"]
        assert group["LogGroupName"] == {"Fn::Sub": f"/aws/lambda/{function_name}"}


# No VPC, no NAT gateway --------------------------------------------------------------------


def test_nothing_runs_in_a_vpc() -> None:
    assert "VpcConfig" not in set(keys(template()))
    assert not [r for r in resources().values() if r["Type"].startswith("AWS::EC2::")]


# Tags ----------------------------------------------------------------------------------------


def test_everything_is_tagged_for_cost_tracking() -> None:
    for kind in ("Function", "HttpApi"):
        settings = template()["Globals"][kind]
        assert settings["Tags"] == {"project": "webhook-gateway"}, kind
        assert settings["PropagateTags"] is True, kind
    for name, resource in resources().items():
        if not resource["Type"].startswith("AWS::Serverless::"):  # those get Globals' tags
            assert TAG in resource["Properties"]["Tags"], name


# Settings, handlers and outputs --------------------------------------------------------------


def test_every_function_gets_every_setting() -> None:
    variables = template()["Globals"]["Function"]["Environment"]["Variables"]

    assert set(variables) == {name.upper() for name in Settings.model_fields}
    assert variables["QUEUE_URL"] == {"Ref": "EventsQueue"}
    assert variables["DEAD_LETTER_QUEUE_URL"] == {"Ref": "DeadLetterQueue"}
    assert variables["PARAMETER_PATH"] == {"Fn::Sub": "/webhook-gateway/${Stage}"}


def test_the_stage_parameter_allows_only_stages_settings_accepts() -> None:
    (constraints,) = Settings.model_fields["stage"].metadata

    assert template()["Parameters"]["Stage"]["AllowedPattern"] == constraints.pattern


def test_every_handler_module_is_in_its_code_uri() -> None:
    for name, props in functions().items():
        module = props["Handler"].rpartition(".")[0]
        source = ROOT / props["CodeUri"] / f"{module.replace('.', '/')}.py"
        assert source.is_file(), name


@pytest.mark.xfail(strict=True, reason="the entry point arrives with the SSM secret source, WG-24")
def test_every_handler_resolves_to_a_function() -> None:
    for name, props in functions().items():
        module, _, attribute = props["Handler"].rpartition(".")
        assert callable(getattr(importlib.import_module(module), attribute, None)), name


def test_no_output_can_leak_the_endpoint() -> None:
    # sam deploy prints outputs, and the URL must stay out of CI logs (design note section 10).
    assert "WebhookApi" not in json.dumps(template().get("Outputs", {}))
