"""Vercel Python function: every /api/* request is rewritten here (see vercel.json)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from substack_outreach.hosted import handler  # noqa: E402,F401
