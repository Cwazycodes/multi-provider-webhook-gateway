"""GitHub-style fakes: push and pull_request events, signed as GitHub signs them."""

import hmac
import json
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha1
from random import Random

from webhook_gateway.synthetic.fakes import (
    FakeEvent,
    FakeProvider,
    Payload,
    fake_sha,
    fake_uuid,
    hmac_sha256,
)

# A pull request that is opened, not merged, isn't in the gateway's vocabulary.
KINDS = ("push", "pull_request.merged", "pull_request.opened")
OWNERS = ("acme", "octo-org", "northwind")
REPOSITORIES = ("widgets", "billing-api", "docs-site", "infra")
LOGINS = ("ada-l", "grace-h", "linus-t", "margaret-h")
BRANCHES = ("main", "release/2.4", "feature/retry-queue")
MESSAGES = ("Fix flaky retry test", "Add order export", "Bump dependencies", "Tidy README")
# Commit timestamps carry the committer's own UTC offset.
OFFSETS = (timedelta(hours=-5), timedelta(0), timedelta(hours=1), timedelta(hours=5, minutes=30))


def event(rng: Random, at: datetime) -> FakeEvent:
    owner, name = rng.choice(OWNERS), rng.choice(REPOSITORIES)
    repository = {
        "id": rng.randrange(10**8, 10**9),
        "name": name,
        "full_name": f"{owner}/{name}",
        "private": False,
        "owner": {"login": owner, "id": rng.randrange(10**6, 10**8)},
        "default_branch": "main",
    }
    sender = {"login": rng.choice(LOGINS), "id": rng.randrange(10**6, 10**8)}
    kind = rng.choice(KINDS)
    if kind == "push":
        payload = _push(rng, at, repository, sender)
    else:
        payload = _pull_request(rng, at, repository, sender, merged=kind == "pull_request.merged")
    delivery = fake_uuid(rng)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"GitHub-Hookshot/{rng.randbytes(4).hex()[:7]}",
        "X-GitHub-Delivery": delivery,
        "X-GitHub-Event": kind.split(".")[0],
        "X-GitHub-Hook-ID": str(rng.randrange(10**8, 10**9)),
        "X-GitHub-Hook-Installation-Target-ID": str(repository["id"]),
        "X-GitHub-Hook-Installation-Target-Type": "repository",
    }
    return FakeEvent(provider_event_id=delivery, headers=headers, payload=payload)


def _push(rng: Random, at: datetime, repository: Payload, sender: Payload) -> Payload:
    commits = [_commit(rng, at, sender) for _ in range(rng.randint(1, 3))]
    before, after = fake_sha(rng), commits[-1]["id"]
    return {
        "ref": f"refs/heads/{rng.choice(BRANCHES)}",
        "before": before,
        "after": after,
        "created": False,
        "deleted": False,
        "forced": False,
        "base_ref": None,
        "compare": f"https://github.com/{repository['full_name']}/compare/{before[:12]}...{after[:12]}",
        "commits": commits,
        "head_commit": commits[-1],
        # Push events give pushed_at as Unix seconds, unlike every other timestamp here.
        "repository": repository | {"pushed_at": int(at.timestamp())},
        "pusher": {"name": sender["login"]},
        "sender": sender,
    }


def _commit(rng: Random, at: datetime, sender: Payload) -> Payload:
    committed = at - timedelta(seconds=rng.randint(60, 3600))
    return {
        "id": fake_sha(rng),
        "message": rng.choice(MESSAGES),
        "timestamp": committed.astimezone(timezone(rng.choice(OFFSETS))).isoformat("T", "seconds"),
        "author": {"name": sender["login"], "username": sender["login"]},
        "distinct": True,
    }


def _pull_request(
    rng: Random, at: datetime, repository: Payload, sender: Payload, *, merged: bool
) -> Payload:
    number = rng.randint(1, 999)
    happened = _z(at - timedelta(seconds=rng.randint(0, 60)))
    opened = _z(at - timedelta(hours=rng.randint(1, 72))) if merged else happened
    return {
        "action": "closed" if merged else "opened",
        "number": number,
        "pull_request": {
            "id": rng.randrange(10**9, 10**10),
            "number": number,
            "title": rng.choice(MESSAGES),
            "state": "closed" if merged else "open",
            "merged": merged,
            "merged_at": happened if merged else None,
            "merge_commit_sha": fake_sha(rng) if merged else None,
            "created_at": opened,
            "updated_at": happened,
            "closed_at": happened if merged else None,
            "user": sender,
            "head": {"ref": "feature/retry-queue", "sha": fake_sha(rng)},
            "base": {"ref": "main", "sha": fake_sha(rng)},
        },
        "repository": repository,
        "sender": sender,
    }


def _z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def render(payload: Payload) -> str:
    return json.dumps(payload, separators=(",", ":"))


def sign(secret: str, body: str, signed_at: datetime) -> dict[str, str]:
    # GitHub still sends the SHA-1 header for old receivers; verify the SHA-256 one.
    legacy = hmac.new(secret.encode(), body.encode(), sha1).hexdigest()
    return {
        "X-Hub-Signature": f"sha1={legacy}",
        "X-Hub-Signature-256": f"sha256={hmac_sha256(secret, body).hex()}",
    }


FAKE = FakeProvider(
    event=event, render=render, sign=sign, required_field="repository", signs_timestamp=False
)
