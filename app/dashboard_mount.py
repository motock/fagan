"""Mount operator-selected dashboard extensions.

An extension is code running with the dashboard server's privileges, chosen
ONLY by the operator environment variable::

    PIPELINE_DASHBOARD_PLUGINS=module:attr,module:attr

The trust model is the one the merged plugin loader already sets out
(:mod:`app.plugin_loader`): off when the variable is unset, a bad entry raises
:class:`~app.plugin_loader.PluginError` and stops startup rather than being
silently skipped, and whoever can set the variable runs code in the server.

Two surfaces are on offer, deliberately different in their authentication:

* :attr:`DashboardContext.router` is included at :data:`EXT_API_PREFIX`
  (``/api/ext``), inside the ``/api/`` prefix that ``app/dashboard.py``'s
  middleware authenticates — OIDC session, bearer token or shared key. Every
  JSON route a plugin adds is authenticated for free.
* :meth:`DashboardContext.mount_static` mounts files at
  ``/ext/<name>``, which is *not* authenticated (see that method's docstring).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter
from starlette.staticfiles import StaticFiles

from app.plugin_loader import load_plugin_list

logger = logging.getLogger(__name__)

#: Where a plugin's ``ctx.router`` routes are included: behind the ``/api/``
#: authentication middleware.
EXT_API_PREFIX = "/api/ext"
#: Where a plugin's static directories are mounted. NOT authenticated.
EXT_STATIC_PREFIX = "/ext"

#: A mount name is a short lower-case slug: safe in a URL, safe as a path.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class DashboardContext:
    """The handle a dashboard extension registrar is called with.

    A registrar is ``def register(ctx: DashboardContext) -> None``; it adds
    routes to :attr:`router` and/or pages with :meth:`mount_static`.
    """

    def __init__(self, app) -> None:
        self.app = app
        self.router = APIRouter()
        self._mounted: set[str] = set()

    def mount_static(self, name: str, directory) -> None:
        """Serve ``directory``'s files at ``/ext/<name>``.

        SECURITY: static files are served WITHOUT authentication, exactly like
        the core pages. An extension's HTML/JS/CSS therefore must carry no
        secrets — no tokens, keys or internal URLs. Data belongs in routes on
        :attr:`router` (served under ``/api/ext``), which *are* authenticated;
        the page fetches them with whatever credential the browser already has.

        ``name`` must match :data:`_NAME_RE` and may be mounted only once.
        ``directory`` must be an existing directory. Every rejection is a
        :class:`ValueError` whose message does not echo the path: an operator's
        directory can itself be a sensitive deployment detail.
        """
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ValueError("static mount name must match ^[a-z][a-z0-9_-]{0,31}$")
        if name in self._mounted:
            raise ValueError(f"static mount {name!r} is already mounted")
        try:
            resolved = Path(directory).resolve(strict=True)
        except OSError:
            raise ValueError(
                f"static mount {name!r} directory does not exist"
            ) from None
        if not resolved.is_dir():
            raise ValueError(
                f"static mount {name!r} path is not a directory"
            )
        # follow_symlink stays False: a symlink inside the mount that points
        # outside it must not become a read of an arbitrary file.
        self.app.mount(
            f"{EXT_STATIC_PREFIX}/{name}",
            StaticFiles(directory=str(resolved), html=True),
            name=f"ext_{name}",
        )
        self._mounted.add(name)


def mount_routes(app) -> None:
    """Call every configured dashboard registrar, then include their routes.

    With no plugin configured this adds nothing to ``app`` beyond the core
    routers other stories include here. A registrar that raises propagates and
    startup fails: a half-mounted dashboard is not a safe state.
    """
    from app.dashboard_routes_approval import router as approval_router

    app.include_router(approval_router, prefix="/api")
    ctx = DashboardContext(app)
    registrars = load_plugin_list("dashboard")
    for registrar in registrars:
        registrar(ctx)
    app.include_router(ctx.router, prefix=EXT_API_PREFIX)
    if registrars:
        logger.info(
            "dashboard extensions mounted",
            extra={"plugin_seam": "dashboard", "plugin_count": len(registrars)},
        )
