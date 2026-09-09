"""Makes `python3 -m loupe_kernel <socket_path>` run the kernel.

The real implementation lives in `__init__.py`; this just wires it to `-m`
invocation, per PLAN.md's Architecture section and `internal/kernel/doc.go`.
"""

import sys

from loupe_kernel import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
