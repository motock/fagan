"""Listing the namespaces that hold plans through an optional store capability.

``namespaces_with_plans`` asks a record store for its namespaces when the store
offers ``list_namespaces()``, and otherwise falls back to the bound request
namespace. A store without the capability keeps today's behaviour exactly.
"""

from __future__ import annotations

import pytest

from pipeline.actor import bind_namespace, current_namespace
from pipeline.store_memory import InMemoryRecordStore
from pipeline.store_namespaces import namespaces_with_plans
from pipeline.store_records import DEFAULT_NAMESPACE, InvalidRecordKeyError

NAMESPACE = 'a/b'
MANIFEST = {'summary': 'plan'}


class PlansOnlyStore:
    """A record store with ``list_plans`` and no ``list_namespaces``."""

    def __init__(self, plans_by_namespace: dict[str, list[str]]) -> None:
        self._plans = plans_by_namespace

    def list_plans(self, namespace: str) -> list[str]:
        return list(self._plans.get(namespace, []))


class ListingStore(PlansOnlyStore):
    """A record store that also offers the ``list_namespaces`` capability."""

    def __init__(self, namespaces: list[str]) -> None:
        super().__init__({})
        self._namespaces = namespaces

    def list_namespaces(self) -> list[str]:
        return list(self._namespaces)


class TestMemoryStoreListsNamespacesWithPlans:
    def test_returns_every_namespace_holding_a_plan_sorted(self):
        store = InMemoryRecordStore()
        store.put_manifest(DEFAULT_NAMESPACE, 'plan-one', MANIFEST)
        store.put_manifest(NAMESPACE, 'plan-two', MANIFEST)

        with bind_namespace('unrelated'):
            assert namespaces_with_plans(store) == sorted([DEFAULT_NAMESPACE, NAMESPACE])

    def test_omits_a_namespace_whose_plans_were_never_stored(self):
        store = InMemoryRecordStore()
        store.put_manifest(NAMESPACE, 'plan-two', MANIFEST)

        assert namespaces_with_plans(store) == [NAMESPACE]

    def test_returns_empty_list_when_no_plans_are_stored(self):
        assert namespaces_with_plans(InMemoryRecordStore()) == []


class TestStoreWithoutCapabilityFallsBackToCurrentNamespace:
    def test_returns_current_namespace_when_it_holds_plans(self):
        store = PlansOnlyStore({NAMESPACE: ['plan-two']})

        with bind_namespace(NAMESPACE):
            assert namespaces_with_plans(store) == [NAMESPACE]

    def test_returns_default_namespace_when_nothing_is_bound_and_it_holds_plans(self):
        store = PlansOnlyStore({DEFAULT_NAMESPACE: ['plan-one']})

        assert current_namespace() == DEFAULT_NAMESPACE
        assert namespaces_with_plans(store) == [DEFAULT_NAMESPACE]

    def test_returns_empty_list_when_current_namespace_holds_no_plans(self):
        store = PlansOnlyStore({DEFAULT_NAMESPACE: ['plan-one']})

        with bind_namespace(NAMESPACE):
            assert namespaces_with_plans(store) == []

    def test_does_not_report_other_namespaces_without_the_capability(self):
        store = PlansOnlyStore({NAMESPACE: ['plan-two']})

        assert namespaces_with_plans(store) == []


class TestListNamespacesCapability:
    def test_returns_the_capability_result_in_sorted_order(self):
        store = ListingStore([NAMESPACE, DEFAULT_NAMESPACE])

        assert namespaces_with_plans(store) == sorted([NAMESPACE, DEFAULT_NAMESPACE])

    def test_returns_empty_list_when_capability_reports_none(self):
        assert namespaces_with_plans(ListingStore([])) == []

    @pytest.mark.parametrize('invalid', ['Bad Namespace', '../escape', '', 'a//b', 42])
    def test_raises_on_an_invalid_namespace_instead_of_skipping_it(self, invalid):
        store = ListingStore([DEFAULT_NAMESPACE, invalid])

        with pytest.raises(InvalidRecordKeyError, match='invalid namespace'):
            namespaces_with_plans(store)

    def test_error_message_does_not_echo_the_offending_value(self):
        store = ListingStore(['../secret-path'])

        with pytest.raises(InvalidRecordKeyError) as raised:
            namespaces_with_plans(store)

        assert '../secret-path' not in str(raised.value)
