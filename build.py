"""Build the local review page: index.template.html + substack_outreach/ui.* -> public/index.html.

Standard library only. The worker (substack_outreach/server.py) serves the result at
http://127.0.0.1:8765/. The hosted app renders the same page in memory (substack_outreach/page.py).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from substack_outreach.page import render_page  # noqa: E402

try:
    html = render_page()
except ValueError as exc:
    raise SystemExit(str(exc))

os.makedirs(os.path.join(HERE, 'public'), exist_ok=True)
out = os.path.join(HERE, 'public', 'index.html')
with open(out, 'w', encoding='utf-8') as fh:
    fh.write(html)
print('public/index.html', len(html) // 1024, 'KB')
