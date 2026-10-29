# Sample repository

The smoke stack seeds its bare origin from this directory: `deploy/entrypoint.sh`
commits it once into the `git-origin` volume and never again, so the refs a
story pushes survive a container restart.

Keep it free of `test_*.py` files. A bare `pytest` at the repo root collects
everything under it, and these files are fixtures for a container, not tests for
this project.
