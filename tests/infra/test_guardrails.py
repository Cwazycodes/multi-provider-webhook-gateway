"""Cost and safety guardrails over template.yaml, as rules for every resource of a type (WG-14).

A rule names the resource types it covers, so it applies to the first table or bucket
the moment a later ticket adds one. A failure names the resource, its line and the
rule it broke.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from cfnlint.decode import decode_str

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "template.yaml"

FUNCTIONS = frozenset({"AWS::Serverless::Function", "AWS::Lambda::Function"})
LOG_GROUPS = frozenset({"AWS::Logs::LogGroup"})
REST_APIS = frozenset({"AWS::Serverless::Api", "AWS::ApiGateway::RestApi"})
HTTP_APIS = frozenset({"AWS::Serverless::HttpApi"})
QUEUES = frozenset({"AWS::SQS::Queue"})
TABLES = frozenset({"AWS::DynamoDB::Table", "AWS::Serverless::SimpleTable"})
BUCKETS = frozenset({"AWS::S3::Bucket"})
TAGGED = FUNCTIONS | LOG_GROUPS | HTTP_APIS | QUEUES | TABLES | BUCKETS
# The Globals section that applies to each SAM resource type.
GLOBALS = {
    "AWS::Serverless::Function": "Function",
    "AWS::Serverless::HttpApi": "HttpApi",
    "AWS::Serverless::SimpleTable": "SimpleTable",
}
PROJECT_TAG = ("project", "webhook-gateway")


@dataclass(frozen=True)
class Resource:
    name: str
    type: str
    properties: Mapping[str, Any]  # with SAM's Globals merged in, as SAM merges them
    attributes: Mapping[str, Any]  # DeletionPolicy and the like
    template: Mapping[str, Any]


@dataclass(frozen=True)
class Rule:
    id: str
    says: str  # what every resource it covers must do; failures quote it
    types: frozenset[str]
    holds: Callable[[Resource], bool]


def forbidden(resource: Resource) -> bool:
    return False


def merged(globals_: Mapping[str, Any], own: Mapping[str, Any]) -> dict[str, Any]:
    """SAM's rule: primitives are replaced, maps are merged, lists are additive."""
    result = dict(globals_)
    for key, value in own.items():
        base = result.get(key)
        if isinstance(base, Mapping) and isinstance(value, Mapping):
            result[key] = {**base, **value}
        elif isinstance(base, list) and isinstance(value, list):
            result[key] = [*base, *value]
        else:
            result[key] = value
    return result


def resources(template: Mapping[str, Any]) -> list[Resource]:
    found = []
    for name, body in template.get("Resources", {}).items():
        section = GLOBALS.get(body["Type"])
        defaults = template.get("Globals", {}).get(section, {}) if section else {}
        properties = merged(defaults, body.get("Properties", {}))
        attributes = {
            key: value for key, value in body.items() if key not in {"Type", "Properties"}
        }
        found.append(Resource(name, body["Type"], properties, attributes, template))
    return found


def logs_to_a_declared_group(function: Resource) -> bool:
    group = function.properties.get("LoggingConfig", {}).get("LogGroup")
    declared = function.template.get("Resources", {})
    return (
        isinstance(group, Mapping) and declared.get(group.get("Ref"), {}).get("Type") in LOG_GROUPS
    )


def throttled(api: Resource) -> bool:
    route = api.properties.get("DefaultRouteSettings", {})
    return {"ThrottlingRateLimit", "ThrottlingBurstLimit"} <= set(route)


def on_demand(table: Resource) -> bool:
    if table.type == "AWS::Serverless::SimpleTable":
        return "ProvisionedThroughput" not in table.properties  # SAM's default is on-demand
    return table.properties.get("BillingMode") == "PAY_PER_REQUEST"


def expires_within_30_days(bucket: Resource) -> bool:
    rules = bucket.properties.get("LifecycleConfiguration", {}).get("Rules", [])
    return any(r.get("Status") == "Enabled" and r.get("ExpirationInDays", 31) <= 30 for r in rules)


def blocks_public_access(bucket: Resource) -> bool:
    flags = ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets")
    block = bucket.properties.get("PublicAccessBlockConfiguration", {})
    return all(block.get(flag) is True for flag in flags)


def tagged(resource: Resource) -> bool:
    tags = resource.properties.get("Tags", {})
    if isinstance(tags, list):  # plain CloudFormation: [{Key, Value}]; SAM: a map
        tags = {tag["Key"]: tag["Value"] for tag in tags}
    return PROJECT_TAG in tags.items()


RULES = [
    # Lambda functions (design note section 12)
    Rule(
        "arm64",
        "every function runs on arm64",
        FUNCTIONS,
        lambda r: r.properties.get("Architectures") == ["arm64"],
    ),
    Rule(
        "reserved-concurrency",
        "every function has a reserved concurrency cap",
        FUNCTIONS,
        lambda r: "ReservedConcurrentExecutions" in r.properties,
    ),
    Rule(
        "no-vpc",
        "no function runs in a VPC: it would need a NAT gateway",
        FUNCTIONS,
        lambda r: "VpcConfig" not in r.properties,
    ),
    Rule(
        "declared-log-group",
        "every function logs to a log group the stack declares",
        FUNCTIONS,
        logs_to_a_declared_group,
    ),
    # Logs
    Rule(
        "seven-day-logs",
        "every log group keeps logs for 7 days",
        LOG_GROUPS,
        lambda r: r.properties.get("RetentionInDays") == 7,
    ),
    Rule(
        "logs-deleted-with-stack",
        "every log group is deleted with the stack",
        LOG_GROUPS,
        lambda r: (
            {r.attributes.get("DeletionPolicy"), r.attributes.get("UpdateReplacePolicy")}
            <= {None, "Delete"}
        ),
    ),
    # API
    Rule(
        "http-api-only",
        "no REST API: an HTTP API is cheaper per request",
        REST_APIS,
        forbidden,
    ),
    Rule(
        "throttled-api",
        "every HTTP API throttles its default route",
        HTTP_APIS,
        throttled,
    ),
    # Things that cost money just by existing
    Rule(
        "no-nat-gateway",
        "no NAT gateway: one costs more than everything else here",
        frozenset({"AWS::EC2::NatGateway"}),
        forbidden,
    ),
    Rule(
        "no-secrets-manager",
        "no Secrets Manager secret: SSM's standard parameters are free",
        frozenset({"AWS::SecretsManager::Secret"}),
        forbidden,
    ),
    Rule(
        "no-kms-keys",
        "no customer-managed KMS key: each has a monthly charge",
        frozenset({"AWS::KMS::Key"}),
        forbidden,
    ),
    Rule(
        "sse-sqs",
        "every queue is encrypted with SSE-SQS, not a KMS key",
        QUEUES,
        lambda r: (
            r.properties.get("SqsManagedSseEnabled") is True
            and "KmsMasterKeyId" not in r.properties
        ),
    ),
    # Storage: none yet; WG-15 adds the table and WG-17 the bucket
    Rule(
        "on-demand-table",
        "every table bills on demand",
        TABLES,
        on_demand,
    ),
    Rule(
        "bucket-lifecycle",
        "every bucket expires its objects within 30 days",
        BUCKETS,
        expires_within_30_days,
    ),
    Rule(
        "bucket-not-public",
        "every bucket blocks all public access",
        BUCKETS,
        blocks_public_access,
    ),
    # Cost tracking
    Rule(
        "tagged",
        "everything taggable carries the tag project=webhook-gateway",
        TAGGED,
        tagged,
    ),
]
BY_ID = {rule.id: rule for rule in RULES}
# Resource types with nothing to guard. Add one here only after deciding it needs no rule.
NO_RULES_NEEDED: frozenset[str] = frozenset()


def load(text: str) -> dict[str, Any]:
    """The template as cfn-lint reads it: !Ref, !GetAtt and !Sub in their long forms."""
    template, matches = decode_str(text)
    assert template is not None, matches
    loaded: dict[str, Any] = template
    return loaded


def broken(rule: Rule, template: Mapping[str, Any]) -> list[str]:
    """`Name (line N): rule` for every resource the rule covers that breaks it."""
    lines = {str(name): getattr(name, "start_mark", None) for name in template["Resources"]}
    failures = []
    for resource in resources(template):
        if resource.type in rule.types and not rule.holds(resource):
            mark = lines[resource.name]
            where = f" (line {mark.line + 1})" if mark else ""
            failures.append(f"{resource.name}{where}: {rule.says}")
    return failures


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    return load(TEMPLATE.read_text())


@pytest.mark.parametrize("rule", RULES, ids=[rule.id for rule in RULES])
def test_the_template_keeps_every_guardrail(rule: Rule, template: dict[str, Any]) -> None:
    failures = broken(rule, template)

    assert not failures, "\n".join(failures)


def test_every_resource_type_in_the_template_has_been_considered(template: dict[str, Any]) -> None:
    considered = {kind for rule in RULES for kind in rule.types} | NO_RULES_NEEDED
    used = {resource.type for resource in resources(template)}

    assert used <= considered, f"decide which guardrails apply to {sorted(used - considered)}"


# Each rule catches what it forbids, including on resource types the template doesn't
# have yet. These templates are tiny: one resource, and Globals where that matters.


def one(kind: str, properties: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    return {"Resources": {"Thing": {"Type": kind, "Properties": dict(properties)} | extra}}


def with_globals(
    section: str, values: Mapping[str, Any], template: dict[str, Any]
) -> dict[str, Any]:
    return template | {"Globals": {section: dict(values)}}


SAM_FUNCTION = "AWS::Serverless::Function"
LOG_GROUP = {"RetentionInDays": 7, "Tags": [{"Key": "project", "Value": "webhook-gateway"}]}
BUCKET_RULE = {"Status": "Enabled", "ExpirationInDays": 30}
PUBLIC_BLOCK = dict.fromkeys(
    ["BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets"], True
)

BREAKS = [
    pytest.param("arm64", one(SAM_FUNCTION, {"Architectures": ["x86_64"]}), id="arm64-x86"),
    pytest.param("arm64", one("AWS::Lambda::Function", {}), id="arm64-lambda-default"),
    pytest.param(
        "arm64",
        with_globals("Function", {"Architectures": ["x86_64"]}, one(SAM_FUNCTION, {})),
        id="arm64-from-globals",
    ),
    pytest.param("reserved-concurrency", one(SAM_FUNCTION, {}), id="no-reserved-concurrency"),
    pytest.param("no-vpc", one(SAM_FUNCTION, {"VpcConfig": {}}), id="vpc"),
    pytest.param(
        "no-vpc",
        with_globals("Function", {"VpcConfig": {}}, one(SAM_FUNCTION, {})),
        id="vpc-from-globals",
    ),
    pytest.param("declared-log-group", one(SAM_FUNCTION, {}), id="lambdas-own-log-group"),
    pytest.param(
        "declared-log-group",
        one(SAM_FUNCTION, {"LoggingConfig": {"LogGroup": "/aws/lambda/thing"}}),
        id="log-group-by-name",
    ),
    pytest.param("seven-day-logs", one("AWS::Logs::LogGroup", {}), id="logs-kept-forever"),
    pytest.param(
        "seven-day-logs", one("AWS::Logs::LogGroup", {"RetentionInDays": 14}), id="logs-14-days"
    ),
    pytest.param(
        "logs-deleted-with-stack",
        one("AWS::Logs::LogGroup", LOG_GROUP, DeletionPolicy="Retain"),
        id="log-group-retained",
    ),
    pytest.param("http-api-only", one("AWS::Serverless::Api", {}), id="sam-rest-api"),
    pytest.param("http-api-only", one("AWS::ApiGateway::RestApi", {}), id="rest-api"),
    pytest.param("throttled-api", one("AWS::Serverless::HttpApi", {}), id="unthrottled"),
    pytest.param("no-nat-gateway", one("AWS::EC2::NatGateway", {}), id="nat-gateway"),
    pytest.param("no-secrets-manager", one("AWS::SecretsManager::Secret", {}), id="secret"),
    pytest.param("no-kms-keys", one("AWS::KMS::Key", {}), id="kms-key"),
    pytest.param("sse-sqs", one("AWS::SQS::Queue", {}), id="queue-unencrypted"),
    pytest.param(
        "sse-sqs",
        one("AWS::SQS::Queue", {"SqsManagedSseEnabled": True, "KmsMasterKeyId": "alias/x"}),
        id="queue-kms",
    ),
    pytest.param(
        "on-demand-table", one("AWS::DynamoDB::Table", {}), id="table-provisioned-default"
    ),
    pytest.param(
        "on-demand-table",
        one("AWS::Serverless::SimpleTable", {"ProvisionedThroughput": {}}),
        id="simple-table-provisioned",
    ),
    pytest.param("bucket-lifecycle", one("AWS::S3::Bucket", {}), id="bucket-kept-forever"),
    pytest.param(
        "bucket-lifecycle",
        one(
            "AWS::S3::Bucket",
            {"LifecycleConfiguration": {"Rules": [BUCKET_RULE | {"Status": "Disabled"}]}},
        ),
        id="bucket-rule-disabled",
    ),
    pytest.param(
        "bucket-lifecycle",
        one(
            "AWS::S3::Bucket",
            {"LifecycleConfiguration": {"Rules": [BUCKET_RULE | {"ExpirationInDays": 90}]}},
        ),
        id="bucket-90-days",
    ),
    pytest.param("bucket-not-public", one("AWS::S3::Bucket", {}), id="bucket-public"),
    pytest.param(
        "bucket-not-public",
        one(
            "AWS::S3::Bucket",
            {"PublicAccessBlockConfiguration": PUBLIC_BLOCK | {"RestrictPublicBuckets": False}},
        ),
        id="bucket-three-of-four",
    ),
    pytest.param("tagged", one("AWS::SQS::Queue", {}), id="queue-untagged"),
    pytest.param(
        "tagged",
        with_globals("Function", {"Tags": {"team": "x"}}, one(SAM_FUNCTION, {})),
        id="function-untagged",
    ),
]

KEEPS = [
    pytest.param(
        "arm64",
        with_globals("Function", {"Architectures": ["arm64"]}, one(SAM_FUNCTION, {})),
        id="arm64-from-globals",
    ),
    pytest.param(
        "tagged",
        with_globals("Function", {"Tags": dict([PROJECT_TAG])}, one(SAM_FUNCTION, {"Tags": {}})),
        id="tags-merge-with-globals",
    ),
    pytest.param(
        "on-demand-table",
        one("AWS::DynamoDB::Table", {"BillingMode": "PAY_PER_REQUEST"}),
        id="table-on-demand",
    ),
    pytest.param("on-demand-table", one("AWS::Serverless::SimpleTable", {}), id="simple-table"),
    pytest.param(
        "bucket-lifecycle",
        one("AWS::S3::Bucket", {"LifecycleConfiguration": {"Rules": [BUCKET_RULE]}}),
        id="bucket-30-days",
    ),
    pytest.param(
        "bucket-not-public",
        one("AWS::S3::Bucket", {"PublicAccessBlockConfiguration": PUBLIC_BLOCK}),
        id="bucket-private",
    ),
]


@pytest.mark.parametrize(("rule_id", "template"), BREAKS)
def test_each_rule_names_the_resource_that_breaks_it(
    rule_id: str, template: dict[str, Any]
) -> None:
    assert broken(BY_ID[rule_id], template) == [f"Thing: {BY_ID[rule_id].says}"]


@pytest.mark.parametrize(("rule_id", "template"), KEEPS)
def test_each_rule_accepts_what_it_asks_for(rule_id: str, template: dict[str, Any]) -> None:
    assert broken(BY_ID[rule_id], template) == []


def test_a_failure_points_at_the_line_in_template_yaml() -> None:
    text = TEMPLATE.read_text().replace("RetentionInDays: 7", "RetentionInDays: 14")
    line = next(n for n, row in enumerate(text.splitlines(), 1) if row == "  IngestLogGroup:")

    assert broken(BY_ID["seven-day-logs"], load(text)) == [
        f"IngestLogGroup (line {line}): every log group keeps logs for 7 days"
    ]
