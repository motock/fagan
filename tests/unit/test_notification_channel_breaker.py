"""Per-channel circuit breaker behaviour of ``as_sender`` (NTF-01).

A channel that answers RETRY (or raises) must not be tried again until a
growing delay has passed.  Every fake here is local and obvious: no network,
no credentials, no webhook URLs.
"""

from pipeline import notification_channels as nc

RECORD = {"notification_id": "n1", "recipient": "fake-recipient"}


class FakeChannel:
    """Minimal channel double: counts sends and returns a configurable result."""

    def __init__(self, name="slack-a", result=None, raise_error=None):
        self.name = name
        self.outbox_suffix = f".outbox.{name}.jsonl"
        self.result = (
            result if result is not None else nc.DeliveryResult(nc.DeliveryStatus.RETRY)
        )
        self.raise_error = raise_error
        self.calls = 0

    def send(self, record):
        self.calls += 1
        if self.raise_error is not None:
            raise self.raise_error
        return self.result


class FakeClock:
    """Hand-advanced monotonic clock: nothing sleeps."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _retry():
    return nc.DeliveryResult(nc.DeliveryStatus.RETRY)


def test_first_retry_blocks_send_for_sixty_seconds():
    channel = FakeChannel(result=_retry())
    clock = FakeClock()
    sender = nc.as_sender(channel, clock=clock)

    assert sender(RECORD) is False
    assert channel.calls == 1

    clock.advance(59)

    assert sender(RECORD) is False
    assert channel.calls == 1


def test_send_happens_again_once_the_first_backoff_expires():
    channel = FakeChannel(result=_retry())
    clock = FakeClock()
    sender = nc.as_sender(channel, clock=clock)

    assert sender(RECORD) is False

    channel.result = nc.DeliveryResult(nc.DeliveryStatus.DELIVERED)
    clock.advance(61)

    assert sender(RECORD) is True
    assert channel.calls == 2


def test_second_consecutive_retry_blocks_for_one_hundred_and_twenty_seconds():
    channel = FakeChannel(result=_retry())
    clock = FakeClock()
    sender = nc.as_sender(channel, clock=clock)

    assert sender(RECORD) is False

    clock.advance(61)
    assert sender(RECORD) is False
    assert channel.calls == 2

    clock.advance(119)
    assert sender(RECORD) is False
    assert channel.calls == 2

    clock.advance(2)
    assert sender(RECORD) is False
    assert channel.calls == 3


def test_backoff_never_exceeds_one_hour_after_twenty_retries():
    channel = FakeChannel(result=_retry())
    clock = FakeClock()
    sender = nc.as_sender(channel, clock=clock)

    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    clock.advance(3600)
    assert sender(RECORD) is False
    assert channel.calls == 20

    clock.advance(3599)
    assert sender(RECORD) is False
    assert channel.calls == 20

    clock.advance(2)
    assert sender(RECORD) is False
    assert channel.calls == 21


def test_delivered_clears_breaker_state_so_next_retry_backs_off_sixty_seconds():
    channel = FakeChannel(result=_retry())
    clock = FakeClock()
    sender = nc.as_sender(channel, clock=clock)

    assert sender(RECORD) is False

    channel.result = nc.DeliveryResult(nc.DeliveryStatus.DELIVERED)
    clock.advance(61)
    assert sender(RECORD) is True

    channel.result = _retry()
    clock.advance(1)
    assert sender(RECORD) is False

    clock.advance(59)
    assert sender(RECORD) is False
    assert channel.calls == 3

    clock.advance(2)
    assert sender(RECORD) is False
    assert channel.calls == 4


def test_rejected_returns_true_and_does_not_open_the_breaker():
    channel = FakeChannel(
        result=nc.DeliveryResult(nc.DeliveryStatus.REJECTED, reason="no_route")
    )
    clock = FakeClock()
    sender = nc.as_sender(channel, clock=clock)

    assert sender(RECORD) is True
    assert channel.calls == 1

    clock.advance(1)

    assert sender(RECORD) is True
    assert channel.calls == 2


def test_retry_after_seconds_three_hundred_is_honoured():
    channel = FakeChannel(
        result=nc.DeliveryResult(nc.DeliveryStatus.RETRY, retry_after_seconds=300.0)
    )
    clock = FakeClock()
    sender = nc.as_sender(channel, clock=clock)

    assert sender(RECORD) is False

    clock.advance(299)
    assert sender(RECORD) is False
    assert channel.calls == 1

    clock.advance(2)
    assert sender(RECORD) is False
    assert channel.calls == 2


def test_retry_after_seconds_is_capped_at_one_hour():
    channel = FakeChannel(
        result=nc.DeliveryResult(nc.DeliveryStatus.RETRY, retry_after_seconds=99999.0)
    )
    clock = FakeClock()
    sender = nc.as_sender(channel, clock=clock)

    assert sender(RECORD) is False

    clock.advance(3599)
    assert sender(RECORD) is False
    assert channel.calls == 1

    clock.advance(2)
    assert sender(RECORD) is False
    assert channel.calls == 2


def test_two_channel_names_keep_independent_breaker_state():
    slack_a = FakeChannel(name="slack-a", result=_retry())
    slack_b = FakeChannel(
        name="slack-b", result=nc.DeliveryResult(nc.DeliveryStatus.DELIVERED)
    )
    clock = FakeClock()
    sender_a = nc.as_sender(slack_a, clock=clock)
    sender_b = nc.as_sender(slack_b, clock=clock)

    assert sender_a(RECORD) is False

    clock.advance(1)

    assert sender_b(RECORD) is True
    assert slack_a.calls == 1
    assert slack_b.calls == 1

    clock.advance(1)

    assert sender_a(RECORD) is False
    assert slack_a.calls == 1


def test_raising_send_opens_the_breaker():
    channel = FakeChannel(raise_error=RuntimeError("provider unavailable"))
    clock = FakeClock()
    sender = nc.as_sender(channel, clock=clock)

    assert sender(RECORD) is False
    assert channel.calls == 1

    clock.advance(30)

    assert sender(RECORD) is False
    assert channel.calls == 1


def test_blocked_call_returns_false_and_leaves_the_record_queued():
    channel = FakeChannel(result=_retry())
    clock = FakeClock()
    sender = nc.as_sender(channel, clock=clock)

    assert sender(RECORD) is False
    calls_before = channel.calls

    assert sender(RECORD) is False
    assert channel.calls == calls_before
