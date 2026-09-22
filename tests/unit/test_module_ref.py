"""TDD tests for ``pipeline.module_ref._ModuleRef``.

``_ModuleRef`` is a lazily resolved proxy for ``<module>.<name>``: it reads the
target binding fresh on every use and never caches it. Epic 2's extraction
stories use it so code moved out of an oversized module keeps reading the
ORIGINAL module's live bindings, which tests monkeypatch.

These tests are RED until ``pipeline/module_ref.py`` lands. They drive a
throwaway probe module registered in ``sys.modules`` so nothing here depends on
the real ``pipeline`` namespace.
"""

from __future__ import annotations

import pathlib
import sys
import types

import pytest

# Import pipeline.server FIRST: pipeline.advance -> server -> advance is a
# circular import, so importing a pipeline submodule standalone can raise
# ImportError. Importing the server module first breaks the cycle.
import pipeline.server
import pipeline.service
from pipeline import module_ref as module_ref_mod

_ModuleRef = module_ref_mod._ModuleRef

PROBE = "pipeline_module_ref_probe"
INNER_PROBE = "pipeline_module_ref_probe_inner"
ABSENT_MODULE = "pipeline_module_ref_probe_absent"


@pytest.fixture
def probe(monkeypatch):
    """A throwaway module registered in ``sys.modules`` under ``PROBE``."""
    module = types.ModuleType(PROBE)
    monkeypatch.setitem(sys.modules, PROBE, module)
    return module


# ---------------------------------------------------------------------------
# Shape / wiring
# ---------------------------------------------------------------------------


def test_module_ref_subclasses_service_server_ref():
    """``_ModuleRef`` extends ``pipeline.service._ServerRef``."""
    assert issubclass(_ModuleRef, pipeline.service._ServerRef)


def test_module_ref_defines_the_three_extra_dunders_itself():
    """``__bool__``/``__ne__``/``__getitem__`` live on ``_ModuleRef``.

    ``pipeline.service._ServerRef`` does not define them, so they must be
    defined on ``_ModuleRef`` itself rather than relied upon from the base.
    """
    for name in ("__bool__", "__ne__", "__getitem__"):
        assert name in vars(_ModuleRef), f"_ModuleRef must define {name}"


def test_module_ref_defines_init_and_value_itself():
    """``__init__`` and ``_value`` are overridden on ``_ModuleRef``."""
    for name in ("__init__", "_value"):
        assert name in vars(_ModuleRef), f"_ModuleRef must define {name}"


def test_other_dunders_are_inherited_unchanged():
    """Every other dunder is inherited from ``_ServerRef`` unchanged."""
    inherited = (
        "__call__",
        "__getattr__",
        "__contains__",
        "__iter__",
        "__len__",
        "__eq__",
        "__hash__",
        "__str__",
        "__repr__",
        "__truediv__",
        "__sub__",
        "__rsub__",
        "__lt__",
        "__le__",
        "__gt__",
        "__ge__",
    )
    for name in inherited:
        assert getattr(_ModuleRef, name) is getattr(
            pipeline.service._ServerRef, name
        ), f"{name} must be inherited from _ServerRef unchanged"


def test_init_records_module_name_and_name(probe):
    """``__init__`` stores the module name and the attribute name."""
    ref = _ModuleRef(PROBE, "fn")
    assert ref._module_name == PROBE
    assert ref._name == "fn"


def test_module_and_class_docstrings_explain_the_why():
    """Both docstrings exist and name the monkeypatch seam they exist for."""
    assert module_ref_mod.__doc__, "pipeline/module_ref.py needs a module docstring"
    assert "monkeypatch" in module_ref_mod.__doc__.lower()
    assert _ModuleRef.__doc__, "_ModuleRef needs a class docstring"
    assert "monkeypatch" in _ModuleRef.__doc__.lower()


# ---------------------------------------------------------------------------
# Positive: delegation
# ---------------------------------------------------------------------------


def test_call_delegates_args_and_kwargs(probe, monkeypatch):
    """Calling the ref delegates to the target function and returns its result."""
    calls = []

    def fn(*args, **kwargs):
        calls.append((args, kwargs))
        return "result"

    monkeypatch.setattr(probe, "fn", fn, raising=False)
    ref = _ModuleRef(PROBE, "fn")

    assert ref(1, 2, key="value") == "result"
    assert calls == [((1, 2), {"key": "value"})]


def test_value_is_read_live_not_cached(probe, monkeypatch):
    """A patch applied AFTER construction is seen on the next use."""
    monkeypatch.setattr(probe, "fn", lambda: "first", raising=False)
    ref = _ModuleRef(PROBE, "fn")
    assert ref() == "first"

    monkeypatch.setattr(probe, "fn", lambda: "second", raising=False)
    assert ref() == "second"
    assert ref._value()() == "second"


def test_attribute_access_delegates(probe, monkeypatch):
    """``ref.<attr>`` resolves against the live target value."""
    monkeypatch.setattr(probe, "text", "hello", raising=False)
    ref = _ModuleRef(PROBE, "text")

    assert ref.upper() == "HELLO"


def test_container_protocols_delegate(probe, monkeypatch):
    """``in``, iteration, ``len()`` and ``ref[0]`` delegate on a list target."""
    monkeypatch.setattr(probe, "items", [10, 20, 30], raising=False)
    ref = _ModuleRef(PROBE, "items")

    assert 20 in ref
    assert 99 not in ref
    assert list(ref) == [10, 20, 30]
    assert len(ref) == 3
    assert ref[0] == 10
    assert ref[-1] == 30


def test_comparisons_delegate_both_operand_orders(probe, monkeypatch):
    """``==``, ``!=``, ``<``, ``<=``, ``>``, ``>=`` delegate both ways."""
    monkeypatch.setattr(probe, "n", 5, raising=False)
    ref = _ModuleRef(PROBE, "n")

    assert ref == 5
    assert (ref != 5) is False
    assert ref < 6
    assert ref <= 5
    assert ref > 4
    assert ref >= 5

    # Reflected order: the int's own comparison returns NotImplemented and the
    # ref's reflected dunder is used instead.
    assert 6 > ref
    assert 5 >= ref
    assert 4 < ref
    assert 5 <= ref


def test_hash_str_and_repr_match_the_target(probe, monkeypatch):
    """``hash``, ``str`` and ``repr`` equal the target's."""
    monkeypatch.setattr(probe, "n", 7, raising=False)
    ref = _ModuleRef(PROBE, "n")

    assert hash(ref) == hash(7)
    assert str(ref) == str(7)
    assert repr(ref) == repr(7)


def test_truediv_delegates_on_path_target(probe, monkeypatch):
    """``ref / "x"`` delegates on a ``pathlib.Path`` target."""
    monkeypatch.setattr(probe, "base", pathlib.Path("/tmp/base"), raising=False)
    ref = _ModuleRef(PROBE, "base")

    assert ref / "x" == pathlib.Path("/tmp/base/x")


def test_sub_and_rsub_delegate_on_int_target(probe, monkeypatch):
    """``ref - n`` and ``n - ref`` delegate on an int target."""
    monkeypatch.setattr(probe, "n", 10, raising=False)
    ref = _ModuleRef(PROBE, "n")

    assert ref - 3 == 7
    assert 30 - ref == 20


def test_chained_module_ref_still_delegates_a_call(probe, monkeypatch):
    """A ref whose target is itself a ref still delegates a call."""
    inner = types.ModuleType(INNER_PROBE)
    monkeypatch.setitem(sys.modules, INNER_PROBE, inner)
    monkeypatch.setattr(inner, "fn", lambda: "deep", raising=False)

    monkeypatch.setattr(probe, "inner", _ModuleRef(INNER_PROBE, "fn"), raising=False)
    ref = _ModuleRef(PROBE, "inner")

    assert ref() == "deep"


# ---------------------------------------------------------------------------
# Negative / boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [[], 0, ""])
def test_bool_is_false_for_falsy_targets(probe, monkeypatch, value):
    """``bool(ref)`` is False for ``[]``, ``0`` and ``""``."""
    monkeypatch.setattr(probe, "v", value, raising=False)
    ref = _ModuleRef(PROBE, "v")

    assert bool(ref) is False


def test_bool_is_true_for_callable_target(probe, monkeypatch):
    """``bool(ref)`` is True for a callable target.

    Without an explicit ``__bool__`` this would fall back to the inherited
    ``__len__`` and raise ``TypeError`` for a callable.
    """
    monkeypatch.setattr(probe, "fn", lambda: None, raising=False)
    ref = _ModuleRef(PROBE, "fn")

    assert bool(ref) is True


def test_bool_is_true_for_truthy_target(probe, monkeypatch):
    """``bool(ref)`` is True for a non-empty container target."""
    monkeypatch.setattr(probe, "items", [1], raising=False)
    ref = _ModuleRef(PROBE, "items")

    assert bool(ref) is True


def test_missing_module_does_not_raise_at_construction(monkeypatch):
    """A ref to a nonexistent module constructs fine; use raises."""
    monkeypatch.delitem(sys.modules, ABSENT_MODULE, raising=False)
    assert ABSENT_MODULE not in sys.modules

    ref = _ModuleRef(ABSENT_MODULE, "fn")

    with pytest.raises(ModuleNotFoundError):
        ref()


def test_missing_attribute_raises_on_use_not_at_construction(probe, monkeypatch):
    """A ref to a missing attribute raises AttributeError only on use."""
    monkeypatch.setattr(probe, "present", 1, raising=False)

    ref = _ModuleRef(PROBE, "absent")

    with pytest.raises(AttributeError):
        ref()
    with pytest.raises(AttributeError):
        _ = ref.anything


def test_ne_is_false_when_equal_and_true_when_not(probe, monkeypatch):
    """``ref != value`` is False when equal and True when not."""
    monkeypatch.setattr(probe, "n", 3, raising=False)
    ref = _ModuleRef(PROBE, "n")

    assert (ref != 3) is False
    assert (ref != 4) is True
