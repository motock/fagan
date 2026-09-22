"""Lazily-resolved references to attributes of ``pipeline.server``.

WHY this proxy exists: the review-orchestration body moved out of
``pipeline/server.py`` reads server-sourced names (``_store``,
``_run_reviewer``, ``_open_pr``, ``_validate_key``, ...) as bare module
globals, and the test suite patches those names on ``pipeline.server``. A
module-load copy (``from pipeline.server import _run_reviewer``) would freeze
the real function into this module and every
``monkeypatch.setattr(pipeline.server, "NAME", ...)`` would be silently
ignored. ``_ServerRef`` re-reads ``pipeline.server.<name>`` on every access,
so patches land and the value is always the current one.

WHY it is a separate class rather than ``pipeline.service._ServerRef``: the
two are deliberately independent copies so that this module depends only on
``pipeline.server``. ``pipeline.service._ServerRef`` re-imports
``pipeline.server`` inside ``_value``; this one binds it once at module
import. Both resolve the name against ``pipeline.server`` at call time, so a
``monkeypatch.setattr(pipeline.server, ...)`` lands either way.
"""

import pipeline.server as _server


class _ServerRef:
    """Delegates to the *current* ``pipeline.server`` binding for a name.

    The moved body references server-sourced names as bare module globals.
    The test suite patches ``pipeline.server`` for those names, so these
    bindings must read the live ``pipeline.server`` value at call time rather
    than hold a copy imported at module load.
    """

    def __init__(self, name: str):
        self._name = name

    def _value(self):
        return getattr(_server, self._name)

    def __getattr__(self, attr: str):
        return getattr(self._value(), attr)

    def __call__(self, *args, **kwargs):
        return self._value()(*args, **kwargs)

    def __truediv__(self, other):
        return self._value() / other

    def __contains__(self, item):
        return item in self._value()

    def __iter__(self):
        return iter(self._value())

    def __sub__(self, other):
        return self._value() - other

    def __rsub__(self, other):
        return other - self._value()

    def __len__(self):
        return len(self._value())

    def __eq__(self, other):
        if isinstance(other, _ServerRef):
            return self._value() == other._value()
        return self._value() == other

    def __lt__(self, other):
        return self._value() < other

    def __le__(self, other):
        return self._value() <= other

    def __gt__(self, other):
        return self._value() > other

    def __ge__(self, other):
        return self._value() >= other

    def __hash__(self):
        return hash(self._value())

    def __str__(self):
        return str(self._value())

    def __repr__(self):
        return repr(self._value())
