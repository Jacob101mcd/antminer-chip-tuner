"""Shared test initialization.

Production imports are intentionally side-effect free. Tests that exercise
configuration helpers still need the in-code defaults populated, so the test
suite does that explicitly here rather than relying on importing the app.
"""

from tuner_app.config.defaults import apply_defaults

apply_defaults()
