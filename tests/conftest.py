import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVAL = os.path.join(ROOT, "eval")
for p in (ROOT, EVAL):
    if p not in sys.path:
        sys.path.insert(0, p)
