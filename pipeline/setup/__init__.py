"""Guided-setup support for the pipeline configuration.

This package holds the declarative description of every configuration key the
guided setup offers (``pipeline.setup.schema``) and, in later stories, the
prompting and env-file writing that consumes it. Importing this package has no
side effects: no environment reads, no filesystem access, no network.
"""
