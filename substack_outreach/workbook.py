"""Minimal read-only XLSX reader (no Excel, pandas or network dependency)."""
import posixpath
import re
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zipfile import ZipFile

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
EXPECTED = {"name", "author_name", "author_handle", "url", "substack_profile", "categories",
            "language", "hero_text", "author_bio", "free_subscribers", "free_subscribers_label",
            "pub_id", "author_id"}


def find_workbook(configured=None):
    if configured:
        p = Path(configured)
        if p.is_file():
            return p
        raise ValueError(f"Workbook missing: {p}. Set input_path in config or pass --input.")
    for name in ("All channels (1).xlsx", "All channels .xlsx", "All channels.xlsx"):
        if Path(name).is_file():
            return Path(name)
    raise ValueError("Workbook missing. Pass --input 'C:\\path\\All channels (1).xlsx' or configure input_path.")


def identifier(value):
    if value is None or value == "":
        return ""
    try:
        d = Decimal(str(value))
        return str(int(d)) if d.is_finite() and d == d.to_integral() else str(value).strip()
    except (InvalidOperation, ValueError):
        return str(value).strip()


def read_workbook(path, sheet_name="substack"):
    with ZipFile(path) as z:
        if any(i.file_size > 150_000_000 for i in z.infolist()):
            raise ValueError("Workbook member exceeds the 150 MB safety limit")
        sheets = ET.fromstring(z.read("xl/workbook.xml"))
        sheet = next((s for s in sheets.findall("s:sheets/s:sheet", NS) if s.get("name", "").lower() == sheet_name.lower()), None)
        if sheet is None:
            raise ValueError(f"Sheet {sheet_name!r} not found")
        rid = sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = next(r.get("Target") for r in rels if r.get("Id") == rid)
        member = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            strings = ["".join(t.text or "" for t in s.iter("{" + NS["s"] + "}t")) for s in ET.fromstring(z.read("xl/sharedStrings.xml"))]
        headers, records = None, []
        with z.open(member) as stream:
            for _, row in ET.iterparse(stream, events=("end",)):
                if row.tag != "{" + NS["s"] + "}row":
                    continue
                cells = {}
                for c in row:
                    col = re.sub(r"\d", "", c.get("r", ""))
                    v = c.findtext("s:v", None, NS)
                    if c.get("t") == "s" and v is not None:
                        v = strings[int(v)]
                    elif c.get("t") == "inlineStr":
                        v = "".join(t.text or "" for t in c.findall(".//s:t", NS))
                    # Preserve literal source/cached values; never evaluate formulas.
                    cells[col] = v
                if headers is None:
                    headers = {k: str(v).strip() for k, v in cells.items() if v is not None}
                    if len(set(headers.values())) != len(headers):
                        raise ValueError("Duplicate workbook headers; map them explicitly before importing")
                    if not {"name", "url", "pub_id"}.intersection(headers.values()):
                        raise ValueError(f"Unrecognized headers: {list(headers.values())}")
                elif any(v not in (None, "") for v in cells.values()):
                    records.append((int(row.get("r", 0)), {h: cells.get(col) for col, h in headers.items()}))
                row.clear()
    return list((headers or {}).values()), records
