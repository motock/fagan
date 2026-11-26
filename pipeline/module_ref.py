"""Lazily resolved proxies for ``<module>.<name>`` bindings.

``_ModuleRef`` is a proxy for a named attribute of a named module. It resolves
the target FRESH on every use and never caches it, so code moved out of an
oversized module keeps reading the ORIGINAL module's live bindings. That is
what lets the test suite keep patching the origin module —
``monkeypatch.setattr(<origin module>, NAME, ...)`` and patches applied to
``pipeline.server`` at call time — and have the moved code observe the patch.

It subclasses ``pipeline.service._ServerRef`` (which resolves against
``pipeline.server``) and overrides ``_value`` to import an arbitrary module via
``importlib.import_module`` instead. The three extra dunders (``__bool__``,
``__ne__``, ``__getitem__``) mirror the private ``_ServerRef`` in
``pipeline.advance``: without ``__bool__``, ``bool(ref)`` would fall back to the
inherited ``__len__`` and raise ``TypeError`` for a callable target.
"""

import importlib

from .service import _ServerRef


class _ModuleRef(_ServerRef):
    """Proxy for ``<module_name>.<name>`` resolved fresh at every use.

    WHY: extraction stories move code out of an oversized module while the
    names it reads stay behind in the ORIGINAL module. The test suite
    monkeypatches those names on the origin module (and on ``pipeline.server``)
    at call time, so the proxy must never cache the resolved value: every use
    re-imports the module and re-reads the attribute, and a
    ``monkeypatch.setattr(<origin module>, NAME, ...)`` made after this proxy
    is constructed is still observed.
    """

    def __init__(self, module_name: str, name: str):
        super().__init__(name)
        self._module_name = module_name

    def _value(self):
        return getattr(importlib.import_module(self._module_name), self._name)

    def __bool__(self):
        return bool(self._value())

    def __ne__(self, other):
        return self._value() != other

    def __getitem__(self, key):
        return self._value()[key]