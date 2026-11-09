"""Reusable behaviour suite for a record store's notification history.

A record store keeps the notifications it was given: every notification
appended for a plan is still there on the next read, in the order it was
appended, and a read never clears or consumes it.  This module holds that
contract as a plain mixin so the same cases run under any harness and every
implementation is held to the same behaviour.

Like :mod:`pipeline.store_contract_transactions`, this module imports no test
framework: it uses plain ``assert`` statements.  No class here is named
``Test*`` -- pytest must never collect a mixin (it would call the unimplemented
``make_store``).

Inherit :class:`NotificationHistoryContract` in a concrete test class and
implement ``make_store()``.

The three operations
--------------------

``append_notification(namespace, plan_name, record)``
    Append one notification record (a dict carrying a string ``ts`` and a
    string ``message``) to the plan's history.  Anything else is a
    :class:`TypeError` raised before anything is written.

``get_notification_history(namespace, plan_name)``
    Return ``(records, malformed)``: the plan's history in append order
    (oldest first) plus the number of unreadable lines the store skipped.  A
    row store writes no JSONL sidecar and always reports ``0``; a plan with no
    notifications is ``([], 0)``.

``get_notification_lines(namespace, plan_name, limit=100)``
    Return the same history as free-text lines ``f'{ts} {message}'``, oldest
    first, at most ``limit`` of them -- the most recent ones.  A ``limit`` of
    zero or less means the last 100, the rule the legacy reader
    :meth:`pipeline.store.FileStore.get_notifications` already applies.
"""

from __future__ import annotations

from pipeline.store_records import DEFAULT_NAMESPACE, UnknownNamespaceError

__all__ = [
    'DEFAULT_LINE_LIMIT',
    'DEFAULT_NAMESPACE',
    'NOTIFICATION_PLAN',
    'NOTIFICATION_RECORDS',
    'OTHER_NAMESPACE',
    'NotificationHistoryContract',
    'notification_line',
]

#: The plan every case uses.
NOTIFICATION_PLAN = 'notification-plan'

#: A second, *valid* namespace: memory serves it, row and file stores refuse it
#: unless they serve every namespace.
OTHER_NAMESPACE = 'other-namespace'

#: How many lines a ``limit`` of zero or less asks for (the legacy rule).
DEFAULT_LINE_LIMIT = 100

#: The notification records the cases use, in append order.  They carry the
#: fields a writer puts in a record: a timestamp, a message, a severity, a
#: story key and an event.
NOTIFICATION_RECORDS = (
    {
        'ts': '2026-01-01T00:00:00+00:00',
        'message': 'first notification',
        'severity': 'info',
        'story_key': 'story-a',
        'event': 'story_started',
    },
    {
        'ts': '2026-01-01T00:01:00+00:00',
        'message': 'second notification',
        'severity': 'warning',
        'story_key': 'story-b',
        'event': 'story_parked',
    },
    {
        'ts': '2026-01-01T00:02:00+00:00',
        'message': 'third notification',
        'severity': 'error',
        'story_key': 'story-c',
        'event': 'story_failed',
    },
)


def notification_line(record: dict) -> str:
    """Return the free-text line a record renders as: ``f'{ts} {message}'``."""
    return f"{record['ts']} {record['message']}"


class NotificationHistoryContract:
    """Behaviour every record store's notification history must show.

    The history is *state*: it survives the call that wrote it and every call
    that reads it.  A read never clears it, never consumes it and never
    reorders it, so the same call answered twice gives the same answer.
    """

    namespace = DEFAULT_NAMESPACE
    plan_name = NOTIFICATION_PLAN

    #: Set to ``True`` by stores that serve every valid namespace.
    serves_other_namespaces = False

    def make_store(self):
        """Return a fresh, empty store under test."""
        raise NotImplementedError

    # -- helpers -------------------------------------------------------
    def _namespace(self, namespace=None):
        return self.namespace if namespace is None else namespace

    def _append(self, store, record, plan_name=None, namespace=None):
        store.append_notification(
            self._namespace(namespace),
            self.plan_name if plan_name is None else plan_name,
            record,
        )

    def _history(self, store, plan_name=None, namespace=None):
        """Return only the records of ``get_notification_history``."""
        records, _malformed = store.get_notification_history(
            self._namespace(namespace),
            self.plan_name if plan_name is None else plan_name,
        )
        return records

    def _malformed(self, store, plan_name=None, namespace=None):
        """Return only the malformed count of ``get_notification_history``."""
        _records, malformed = store.get_notification_history(
            self._namespace(namespace),
            self.plan_name if plan_name is None else plan_name,
        )
        return malformed

    def _lines(self, store, limit=100, plan_name=None, namespace=None):
        return store.get_notification_lines(
            self._namespace(namespace),
            self.plan_name if plan_name is None else plan_name,
            limit,
        )

    @staticmethod
    def _messages(records):
        return [record['message'] for record in records]

    # -- an empty history ---------------------------------------------
    def test_a_fresh_store_has_no_notification_history(self):
        """A store never told about a notification has none, and no junk."""
        store = self.make_store()

        assert store.get_notification_history(
            self.namespace, self.plan_name
        ) == ([], 0)
        assert self._lines(store) == []

    # -- one notification ----------------------------------------------
    def test_an_appended_notification_is_kept(self):
        """The history is the record that was written, with no junk."""
        store = self.make_store()
        first = NOTIFICATION_RECORDS[0]

        self._append(store, first)

        history = self._history(store)
        assert len(history) == 1, history
        assert history[0] == first
        assert self._malformed(store) == 0

    # -- the history accumulates ---------------------------------------
    def test_the_history_accumulates_in_append_order(self):
        """Two notifications are two entries, oldest first."""
        store = self.make_store()
        first, second, _ = NOTIFICATION_RECORDS

        self._append(store, first)
        self._append(store, second)

        assert self._messages(self._history(store)) == [
            first['message'],
            second['message'],
        ]
        assert self._malformed(store) == 0

    def test_reading_the_history_does_not_consume_it(self):
        """A read leaves the history exactly as it was."""
        store = self.make_store()
        first, second, _ = NOTIFICATION_RECORDS
        self._append(store, first)
        self._append(store, second)

        first_read = self._history(store)
        second_read = self._history(store)

        assert first_read == second_read
        assert len(second_read) == 2, second_read

    def test_the_history_keeps_growing_after_a_read(self):
        """A read does not end the history: the next append is the third entry."""
        store = self.make_store()
        first, second, third = NOTIFICATION_RECORDS
        self._append(store, first)
        self._append(store, second)
        self._history(store)

        self._append(store, third)

        assert self._messages(self._history(store)) == [
            first['message'],
            second['message'],
            third['message'],
        ]

    def test_the_history_of_one_plan_does_not_leak_into_another(self):
        """The history belongs to the plan it was appended for."""
        store = self.make_store()

        self._append(store, NOTIFICATION_RECORDS[0])

        assert self._history(store, self.plan_name + '-other') == []
        assert len(self._history(store)) == 1

    def test_a_plan_that_was_never_written_is_empty(self):
        """A plan nobody notified answers ``([], 0)`` -- it is not created."""
        store = self.make_store()

        assert store.get_notification_history(
            self.namespace, 'never-written-plan'
        ) == ([], 0)
        assert self._lines(store, plan_name='never-written-plan') == []

    # -- namespace scope -----------------------------------------------
    def test_an_unknown_namespace_is_refused(self):
        """A valid namespace the store does not serve is refused, not stored."""
        store = self.make_store()
        if self.serves_other_namespaces:
            self._append(store, NOTIFICATION_RECORDS[0], namespace=OTHER_NAMESPACE)
            assert self._history(store, namespace=OTHER_NAMESPACE) == [
                NOTIFICATION_RECORDS[0]
            ]
            return
        try:
            self._append(store, NOTIFICATION_RECORDS[0], namespace=OTHER_NAMESPACE)
        except UnknownNamespaceError:
            pass
        else:
            raise AssertionError(f'{OTHER_NAMESPACE!r} was accepted as a namespace')
        assert self._history(store) == []

    def test_another_namespace_keeps_an_independent_history(self):
        """Same plan, two namespaces: neither history sees the other."""
        store = self.make_store()
        if not self.serves_other_namespaces:
            return
        first, second, _ = NOTIFICATION_RECORDS

        self._append(store, first)
        self._append(store, second, namespace=OTHER_NAMESPACE)

        assert self._messages(self._history(store)) == [first['message']]
        assert self._messages(
            self._history(store, namespace=OTHER_NAMESPACE)
        ) == [second['message']]

    # -- the lines -----------------------------------------------------
    def test_the_lines_render_ts_and_message(self):
        """Every line is ``f'{ts} {message}'``, in append order."""
        store = self.make_store()
        first, second, _ = NOTIFICATION_RECORDS

        self._append(store, first)
        self._append(store, second)

        assert self._lines(store) == [
            notification_line(first),
            notification_line(second),
        ]

    def test_the_lines_honour_a_positive_limit(self):
        """``limit=1`` is the newest line only."""
        store = self.make_store()
        first, second, third = NOTIFICATION_RECORDS
        for record in (first, second, third):
            self._append(store, record)

        assert self._lines(store, limit=1) == [notification_line(third)]

    def test_a_zero_or_negative_limit_means_the_last_hundred(self):
        """``limit<=0`` is the last 100 lines, the legacy reader's rule."""
        store = self.make_store()
        first, second, third = NOTIFICATION_RECORDS
        for record in (first, second, third):
            self._append(store, record)

        expected = [
            notification_line(record) for record in (first, second, third)
        ]
        assert self._lines(store, limit=0) == expected
        assert self._lines(store, limit=-5) == expected

    def test_the_default_limit_is_the_last_hundred_lines(self):
        """101 appends read back as 100 lines: the oldest one has rolled off."""
        store = self.make_store()
        records = [
            {
                'ts': f'2026-01-01T00:00:{index:02d}+00:00',
                'message': f'notification {index}',
                'severity': 'info',
                'story_key': 'story-a',
                'event': 'story_started',
            }
            for index in range(DEFAULT_LINE_LIMIT + 1)
        ]
        for record in records:
            self._append(store, record)

        lines = self._lines(store)

        assert len(lines) == DEFAULT_LINE_LIMIT, len(lines)
        assert lines[-1] == notification_line(records[-1])
        assert notification_line(records[0]) not in lines

    # -- refused records ------------------------------------------------
    def test_a_non_dict_record_is_refused(self):
        """A record that is not a dict is a TypeError, and nothing is written."""
        store = self.make_store()

        for bad in ('not a record', ['ts', 'message'], 42, None):
            try:
                self._append(store, bad)
            except TypeError:
                continue
            raise AssertionError(f'{bad!r} was accepted as a record')

        assert self._history(store) == []
        assert self._lines(store) == []

    def test_a_record_without_a_string_ts_or_message_is_refused(self):
        """``ts`` and ``message`` must both be strings, or nothing is written."""
        store = self.make_store()
        first = NOTIFICATION_RECORDS[0]

        bad_records = (
            {key: value for key, value in first.items() if key != 'ts'},
            {key: value for key, value in first.items() if key != 'message'},
            {**first, 'ts': None},
            {**first, 'message': 42},
        )
        for bad in bad_records:
            try:
                self._append(store, bad)
            except TypeError:
                continue
            raise AssertionError(f'{bad!r} was accepted as a record')

        assert self._history(store) == []
        assert self._lines(store) == []

    def test_an_invalid_plan_name_is_refused(self):
        """A plan name that is not a flat key is refused, on reads and writes."""
        store = self.make_store()

        for bad in ('../escape', 'with/slash', ''):
            try:
                self._append(store, NOTIFICATION_RECORDS[0], bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f'{bad!r} was accepted as a plan name')
            try:
                store.get_notification_history(self.namespace, bad)
            except ValueError:
                continue
            raise AssertionError(f'{bad!r} was accepted as a plan name')

    def test_an_invalid_namespace_is_refused(self):
        """A namespace that is not an opaque key is refused before storage."""
        store = self.make_store()

        for bad in ('../escape', 'with/slash', ''):
            try:
                store.get_notification_history(bad, self.plan_name)
            except ValueError:
                continue
            raise AssertionError(f'{bad!r} was accepted as a namespace')

    def test_an_invalid_namespace_is_refused_on_append(self):
        """The same key rule guards writes: a malformed namespace stores nothing."""
        store = self.make_store()

        for bad in ('../escape', 'with/slash', ''):
            try:
                self._append(store, NOTIFICATION_RECORDS[0], namespace=bad)
            except ValueError:
                continue
            raise AssertionError(f'{bad!r} was accepted as a namespace')

        assert self._history(store) == []
        assert self._lines(store) == []
