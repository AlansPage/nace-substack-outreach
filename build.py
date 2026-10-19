"""Build the local review page: index.template.html + substack_outreach/ui.* -> public/index.html.

Standard library only. The worker (substack_outreach/server.py) serves the result at
http://127.0.0.1:8765/. Only frontend source is embedded: the SQLite database, the source
workbook, the facts registry and local config never reach the built page.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(HERE, 'index.template.html'), encoding='utf-8') as fh:
    html = fh.read()

for placeholder, fname in (('/*__SUBSTACK_CSS__*/', 'ui.css'), ('/*__SUBSTACK_UI__*/', 'ui.js')):
    with open(os.path.join(HERE, 'substack_outreach', fname), encoding='utf-8') as fh:
        code = fh.read()
    # Inlining is only safe while the source cannot terminate its own host element.
    if '</script' in code or '</style' in code:
        raise SystemExit(f'substack_outreach/{fname} must not contain a closing script/style tag')
    if placeholder not in html:
        raise SystemExit(f'index.template.html lost the {placeholder} placeholder')
    html = html.replace(placeholder, code)

os.makedirs(os.path.join(HERE, 'public'), exist_ok=True)
out = os.path.join(HERE, 'public', 'index.html')
with open(out, 'w', encoding='utf-8') as fh:
    fh.write(html)
print('public/index.html', len(html) // 1024, 'KB')
