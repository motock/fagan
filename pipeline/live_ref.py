"""Lazily-resolved named references to attributes of ``pipeline.server``.

WHY this indirection exists: the test suite patches attributes on
``pipeline.server`` for these names (``PLAN_DIR``, ``WORKTREE_ROOT``,
``_plan_lock``, ...). If callers took a module-load copy instead — e.g.
``from pipeline.server import PLAN_DIR`` — that copy would freeze the real
``~/.claude/plans`` path into every test run, and
``monkeypatch.setattr(pipeline.server, "PLAN_DIR", ...)`` would never be
seen. A :class:`LiveRef` re-reads ``pipeline.server.<name>`` on every
access, so patches land and the value is always the current one.
"""


class LiveRef:
    """A reference to ``pipeline.server.<name>`` resolved lazily, per access.

    The only persistent instance attribute is the string ``_name``. The
    target is looked up fresh on every access — never snapshotted in
    ``__init__`` and never memoized — so the ref always reflects the
    current ``pipeline.server`` binding.
    """

    def __init__(self, name):
        self._name = name

    def _value(self):
        from . import server as _server

        return getattr(_server, self._name)

    def __getattr__(self, name):
        return getattr(self._value(), name)

    def __call__(self, *args, **kwargs):
        return self._value()(*args, **kwargs)

    def __truediv__(self, other):
        return self._value() / other