"""Shared fixtures. Stream/dedupe tests run against the REAL Valkey from the
compose stack (per repo test discipline: never mock Valkey). The Slack Web API
and socket transport are faked; `_black_hole_api` is not a fake but a real
loopback socket standing in for an endpoint that never answers."""

import logging
import socket
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
import redis
from curie_dispatcher.approval_actions import ResolveOutcome
from curie_dispatcher.config import DispatcherConfig
from curie_test_support.valkey import (
    VALKEY_HOST as _VALKEY_HOST,
)
from curie_test_support.valkey import (
    VALKEY_PORT as _VALKEY_PORT,
)
from curie_test_support.valkey import (
    VALKEY_PW as _VALKEY_PW,
)
from curie_test_support.valkey import (
    connect_or_skip,
)
from slack_bolt.authorization import AuthorizeResult


def _authorize(**_kwargs: Any) -> AuthorizeResult:
    """Shared authorization stub: Bolt's ``authorize`` callback resolved to a
    fixed bot identity, so Socket Mode tests skip the real auth.test call."""
    return AuthorizeResult(
        enterprise_id=None,
        team_id="T1",
        bot_token="xoxb-test",
        bot_id="B1",
        bot_user_id="U0BOT",
    )


class FakeSocketClient:
    """Captures the envelope acks Bolt sends back over the socket."""

    def __init__(self) -> None:
        self.logger = logging.getLogger("fake-socket")
        self.acked_envelope_ids: list[str] = []
        # The ack BODY, not just the id (#1053). A block_actions ack is empty,
        # but a view_submission ack is a channel in its own right: it is where a
        # refused submission's reason is rendered, since the approver is standing
        # in an open modal and an ephemeral would post behind it. A test cannot
        # assert that from the envelope id alone.
        self.ack_payloads: dict[str, Any] = {}

    def send_socket_mode_response(self, response: Any) -> None:
        self.acked_envelope_ids.append(response.envelope_id)
        self.ack_payloads[response.envelope_id] = getattr(response, "payload", None)

    def ack_payload_for(self, envelope_id: str) -> Any:
        """The body this envelope was acked with, or None."""

        return self.ack_payloads.get(envelope_id)


def deliver_once(
    handler: Any,
    sock: FakeSocketClient,
    app: Any,
    request: Any,
) -> None:
    """Handle exactly one Socket Mode envelope and return.

    It does not walk other connections and does not stop at the first ack.
    Owner-only proof is one delivery to the
    non-owner (no ack, no mutate) then one delivery to the owner, not a loop
    until someone acks (#2307).
    """

    handler.handle(sock, request)
    app.listener_runner.listener_executor.shutdown(wait=True)


@contextmanager
def _black_hole_api() -> Iterator[str]:
    """A real port that completes the TCP handshake and then never answers.

    Nothing accepts the connection; the kernel's listen backlog completes the
    handshake, so `connect` succeeds and the client is left reading from a
    socket no one writes to. A refused connection fails instantly and can
    never show a probe running past its deadline, so this is the fixture for
    the opposite case: a probe whose read phase is what has to give up,
    exactly the scenario an unbounded probe overshoots on. The preflight
    suite is one consumer of this.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        sock.close()


# Compose defaults and connection params come from the shared curie_test_support.valkey helper.


@pytest.fixture
def redis_client() -> Iterator[redis.Redis]:
    client = connect_or_skip(decode_responses=True)
    yield client
    client.close()


@pytest.fixture
def config(redis_client: redis.Redis) -> Iterator[DispatcherConfig]:
    """A config with a per-test-unique stream and dedupe prefix so tests do not
    collide, cleaned up afterwards."""
    token = uuid.uuid4().hex
    cfg = DispatcherConfig(
        slack_app_token="xapp-test",
        slack_bot_token="xoxb-test",
        valkey_host=_VALKEY_HOST,
        valkey_port=_VALKEY_PORT,
        valkey_password=_VALKEY_PW,
        stream=f"test:curie:runs:{token}",
        dedupe_prefix=f"test:curie:dedupe:{token}:",
        dedupe_ttl_seconds=60,
        placeholder_text="Working on it.",
        # ADR-0106: this is deliberately distinct from the platform API key.
        # Socket Mode interactions become a chat principal only when the
        # dispatcher can sign an attestation with this dedicated credential.
        approval_chat_attester_secret="dispatcher-attester-test-secret",
    )
    yield cfg
    keys = list(redis_client.scan_iter(f"test:curie:dedupe:{token}:*"))
    keys.append(cfg.stream)
    if keys:
        redis_client.delete(*keys)


class ScriptedResolver:
    """Stands in for the platform API: returns a scripted outcome per call."""

    def __init__(self, outcome: ResolveOutcome) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, str]] = []

    def resolve(
        self,
        approval_id: str,
        *,
        decision: str,
        attested_user: str,
        attested_channel: str,
        note: str | None = None,
    ) -> ResolveOutcome:
        # `note` is recorded, not ignored: the dialog path's whole point is that
        # the approver's reason reaches the record, and a stand-in that dropped
        # it would let that regress silently (#1053).
        self.calls.append(
            {
                "approval_id": approval_id,
                "decision": decision,
                "attested_user": attested_user,
                "attested_channel": attested_channel,
                "note": note,
            }
        )
        return self.outcome

    def exists(self, approval_id: str) -> bool | None:
        # Mirror the production ownership probe: only the exact API row-miss
        # is "not this release". Any other outcome means this release has a
        # row (or the probe failed open).
        del approval_id
        return not (
            self.outcome.status_code == 404
            and self.outcome.detail.strip().casefold() == "approval not found"
        )
