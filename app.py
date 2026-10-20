"""Vercel entry point for the hosted team workspace (see pyproject.toml [tool.vercel]).

Every request, including the page at /, goes to this WSGI app. The local worker does not use this file.
"""
from substack_outreach.hosted import app  # noqa: F401
