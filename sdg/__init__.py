"""Synthetic Data Generation Tool (SDG) - proof of concept."""

from sdg import logsafe as _logsafe

__version__ = "0.1.0"

# Database error text never reaches a log: it can quote row values.
_logsafe.install()
