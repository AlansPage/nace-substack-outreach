"""The review page: index.template.html with substack_outreach/ui.css and ui.js inlined.

Used by build.py (public/index.html for the local worker) and by the hosted app, which serves it from memory.
Only frontend source is embedded: the database, workbook, facts registry and local config never reach it.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def render_page():
    html = (ROOT / "index.template.html").read_text(encoding="utf-8")
    for placeholder, name in (("/*__SUBSTACK_CSS__*/", "ui.css"), ("/*__SUBSTACK_UI__*/", "ui.js")):
        code = (ROOT / "substack_outreach" / name).read_text(encoding="utf-8")
        # Inlining is only safe while the source cannot terminate its own host element.
        if "</script" in code or "</style" in code:
            raise ValueError(f"substack_outreach/{name} must not contain a closing script/style tag")
        if placeholder not in html:
            raise ValueError(f"index.template.html lost the {placeholder} placeholder")
        html = html.replace(placeholder, code)
    return html
