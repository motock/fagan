"""
Compatibility shim for the real‑repo harness driver.

The benchmark test suite expects a module named ``run_real_repo_task`` at the
repository root.  The actual implementation lives in
``tests/benchmark/run_real_repo_task.py`` to keep it isolated from other
benchmarks.  This file simply re‑exports the public symbols so that imports such
as ``import run_real_repo_task as rrt`` resolve correctly.
"""

from .tests.benchmark.run_real_repo_task import *  # noqa: F401,F403
