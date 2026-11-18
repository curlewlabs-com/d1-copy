"""Copy one D1 database into another and prove the copy exact. See the README.

The command is the interface (cli.py). `snapshot` loads, compares and rewrites
dumps locally, `rest` makes the D1 API calls, and `workdir` keeps the working
files private and removes them.
"""

import sys

# Checked here, before any other module of the package is imported, so an older
# interpreter gets this message rather than an import error from a newer name.
if sys.version_info < (3, 12):  # noqa: UP036 - the point is to run on older ones
    sys.exit("d1-copy needs Python 3.12 or newer")
