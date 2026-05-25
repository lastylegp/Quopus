"""Quopus Commander - modular package, inspired by Directory Opus 4."""
# Install the .qpe import hook FIRST, so that any subsequent
# import from this package (whether driven by us via 'from
# .telnet_client import ...' or by an external caller doing
# 'import quopus_lib.sid_player') can resolve encrypted modules
# in a trial build. Safe at dev time too: when a real .py is
# present, the default Python import finder picks it up before
# the hook is consulted.
from . import _qpe_loader  # noqa: F401
