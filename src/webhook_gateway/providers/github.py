"""GitHub: verifying X-Hub-Signature-256, and normalising push and pull_request events."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from webhook_gateway.common.envelope import (
    Envelope,
    EventType,
    NonEmptyStr,
    Provider,
    UtcDatetime,
)
from webhook_gateway.common.errors import SignatureError
from webhook_gateway.providers.normalising import (
    PayloadModel,
    Receipt,
    UnixSeconds,
    build_envelope,
    from_unix,
    parse,
    required_header,
    unhandled,
)
from webhook_gateway.providers.signatures import SignedRequest, signed_by_any

HEADER = "X-Hub-Signature-256"

# Kept with the raw body on the queue and in the archive (lowercased). Never a signature.
ARCHIVED_HEADERS = frozenset(
    {"content-type", "user-agent", "x-github-delivery", "x-github-event", "x-github-hook-id"}
)


class GitHubVerifier:
    def verify(self, request: SignedRequest, secrets: Sequence[str], now: datetime) -> None:
        # GitHub also sends a legacy SHA-1 X-Hub-Signature header: never trust that one.
        scheme, _, signature = request.header(HEADER).partition("=")
        if scheme != "sha256":
            raise SignatureError(f"{HEADER} isn't sha256=<hex>")
        if not signed_by_any(secrets, request.body, [signature], encode=bytes.hex):
            raise SignatureError(f"{HEADER} matches none of the secrets")


class Repository(PayloadModel):
    id: int
    full_name: NonEmptyStr


class PushedRepository(Repository):
    pushed_at: UnixSeconds  # push events give Unix seconds, unlike GitHub's other times


class Push(PayloadModel):
    ref: NonEmptyStr
    after: NonEmptyStr
    commits: list[dict[str, Any]]
    repository: PushedRepository


class MergeState(PayloadModel):
    merged: bool


class PullRequestAction(PayloadModel):
    """Just enough of a pull_request event to tell whether the gateway handles it."""

    action: NonEmptyStr
    pull_request: MergeState


class Branch(PayloadModel):
    ref: NonEmptyStr


class MergedPullRequest(PayloadModel):
    id: int
    number: int
    title: str
    merged_at: UtcDatetime
    merge_commit_sha: NonEmptyStr
    base: Branch


class PullRequestMerged(PayloadModel):
    pull_request: MergedPullRequest
    repository: Repository


def normalise(request: SignedRequest, receipt: Receipt) -> Envelope:
    parse(PayloadModel, request.body)  # not JSON (400) is reported before any 422
    delivery = required_header(request, "X-GitHub-Delivery")
    event = required_header(request, "X-GitHub-Event")
    if event == "push":
        push = parse(Push, request.body)
        return build_envelope(
            Provider.GITHUB,
            receipt,
            provider_event_id=delivery,
            event_type=EventType.CODE_PUSHED,
            subject_id=f"github:repo:{push.repository.id}",
            occurred_at=from_unix(push.repository.pushed_at),
            data={
                "repository": push.repository.full_name,
                "ref": push.ref,
                "head": push.after,
                "commits": len(push.commits),
            },
        )
    if event == "pull_request":
        state = parse(PullRequestAction, request.body)
        if state.action == "closed" and state.pull_request.merged:
            merged = parse(PullRequestMerged, request.body)
            pull_request = merged.pull_request
            return build_envelope(
                Provider.GITHUB,
                receipt,
                provider_event_id=delivery,
                event_type=EventType.PULL_REQUEST_MERGED,
                subject_id=f"github:pr:{pull_request.id}",
                occurred_at=pull_request.merged_at,
                data={
                    "repository": merged.repository.full_name,
                    "number": pull_request.number,
                    "title": pull_request.title,
                    "base": pull_request.base.ref,
                    "merge_commit": pull_request.merge_commit_sha,
                },
            )
    return unhandled(Provider.GITHUB, receipt, provider_event_id=delivery, provider_type=event)
