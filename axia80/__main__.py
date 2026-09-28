"""Entry point: python3 -m axia80 [command]. Without a command it opens the graphical console."""

import sys

from .cli import main

sys.exit(main())
