"""Check the Python version before loading d1-copy modules. See the README."""

import sys

# Checked here, before any other module of the package is imported, so an older
# interpreter gets this message rather than an import error from a newer name.
if sys.version_info < (3, 12):  # noqa: UP036 - the point is to run on older ones
    sys.exit("d1-copy needs Python 3.12 or newer")
