"""Minimal subject hook with deliberately incomplete dependency declarations."""

from adapter import run
from local_env.runtime import Environment


def evaluate():
    return run(Environment())
