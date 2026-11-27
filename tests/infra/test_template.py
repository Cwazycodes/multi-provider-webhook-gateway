"""How template.yaml is wired: the specifics the guardrails' general rules don't cover (WG-13)."""

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from cfnlint.decode import decode_str

from webhook_gateway.common.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "template.yaml"


def template() -> dict[str, Any]:
    decoded, matches = decode_str(TEMPLATE.read_text())
    assert decoded is not None, matches
    loaded: dict[str, Any] = decoded
    return loaded


def resources(kind: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = template()["Resources"]
    return {name: r for name, r in found.items() if r["Type"] == kind}


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


def test_it_is_a_sam_template() -> None:
    assert template()["Transform"] == "AWS::Serverless-2016-10-31"


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


# The design note's numbers (section 9) ------------------------------------------------


def test_the_api_is_throttled_to_20_a_second_with_bursts_of_40() -> None:
    route = properties("WebhookApi")["DefaultRouteSettings"]

    assert default(route["ThrottlingRateLimit"]) == 20
    assert default(route["ThrottlingBurstLimit"]) == 40


def test_functions_run_the_projects_python() -> None:
    version = (ROOT / ".python-version").read_text().strip()

    assert template()["Globals"]["Function"]["Runtime"] == f"python{version}"
    for name, props in functions().items():
        assert "Runtime" not in props, name


def test_ingest_answers_within_5_seconds_on_256_mb() -> None:
    function_globals = template()["Globals"]["Function"]

    assert (function_globals["Timeout"], function_globals["MemorySize"]) == (5, 256)
    assert not {"Timeout", "MemorySize"} & set(functions()["IngestFunction"])


def test_ingest_reserves_10_concurrent_executions() -> None:
    assert default(functions()["IngestFunction"]["ReservedConcurrentExecutions"]) == 10


def test_the_main_queue_moves_a_message_to_the_dlq_after_5_receives() -> None:
    assert properties("EventsQueue")["RedrivePolicy"] == {
        "deadLetterTargetArn": {"Fn::GetAtt": ["DeadLetterQueue", "Arn"]},
        "maxReceiveCount": 5,
    }


def test_queue_timings_follow_the_design_note() -> None:
    assert properties("EventsQueue")["VisibilityTimeout"] == 180
    assert properties("DeadLetterQueue")["MessageRetentionPeriod"] == 14 * 24 * 60 * 60


# The table (design note section 8) -----------------------------------------------------


def test_the_table_is_keyed_by_pk_alone_with_no_indexes() -> None:
    table = properties("EventsTable")

    assert table["KeySchema"] == [{"AttributeName": "pk", "KeyType": "HASH"}]
    assert table["AttributeDefinitions"] == [{"AttributeName": "pk", "AttributeType": "S"}]
    assert not {"GlobalSecondaryIndexes", "LocalSecondaryIndexes"} & set(table)


def test_event_records_expire_by_ttl_on_expires_at() -> None:
    assert properties("EventsTable")["TimeToLiveSpecification"] == {
        "AttributeName": "expires_at",
        "Enabled": True,
    }


def test_point_in_time_recovery_is_off_until_production() -> None:
    # It bills per GB-month for backups nobody here needs. A production stack turns it on.
    assert properties("EventsTable")["PointInTimeRecoverySpecification"] == {
        "PointInTimeRecoveryEnabled": False
    }


# Wiring ---------------------------------------------------------------------------------


def test_ingest_may_send_to_the_main_queue_and_nothing_else() -> None:
    assert functions()["IngestFunction"]["Policies"] == [
        {"SQSSendMessagePolicy": {"QueueName": {"Fn::GetAtt": ["EventsQueue", "QueueName"]}}}
    ]


def test_each_log_group_is_named_after_its_function() -> None:
    for props in functions().values():
        group = properties(props["LoggingConfig"]["LogGroup"]["Ref"])
        function_name = props["FunctionName"]["Fn::Sub"]
        assert group["LogGroupName"] == {"Fn::Sub": f"/aws/lambda/{function_name}"}


def test_every_function_gets_every_setting() -> None:
    variables = template()["Globals"]["Function"]["Environment"]["Variables"]

    assert set(variables) == {name.upper() for name in Settings.model_fields}
    assert variables["QUEUE_URL"] == {"Ref": "EventsQueue"}
    assert variables["DEAD_LETTER_QUEUE_URL"] == {"Ref": "DeadLetterQueue"}
    assert variables["TABLE_NAME"] == {"Ref": "EventsTable"}
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
