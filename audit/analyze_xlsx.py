import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter


NS = {
    "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
REL_NS = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}


def cell_col(ref):
    m = re.match(r"([A-Z]+)", ref or "")
    if not m:
        return 0
    col = 0
    for ch in m.group(1):
        col = col * 26 + ord(ch) - 64
    return col - 1


def load_shared_strings(zf):
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    values = []
    for si in root.findall("a:si", NS):
        values.append("".join(t.text or "" for t in si.findall(".//a:t", NS)))
    return values


def load_sheets(zf):
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rel_targets = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels.findall("rel:Relationship", REL_NS)
    }
    sheets = []
    for sheet in workbook.findall(".//a:sheet", NS):
        rid = sheet.attrib[f"{{{NS['r']}}}id"]
        target = rel_targets[rid].lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target
        sheets.append((sheet.attrib["name"], target))
    return sheets


def value_from_cell(cell, shared):
    cell_type = cell.attrib.get("t")
    value = cell.find("a:v", NS)
    if cell_type == "inlineStr":
        return "".join(t.text or "" for t in cell.findall(".//a:t", NS)).strip()
    if value is None or value.text is None:
        return ""
    text = value.text
    if cell_type == "s":
        idx = int(text)
        return shared[idx] if idx < len(shared) else ""
    return text


def iter_rows(zf, path, shared):
    root = ET.fromstring(zf.read(path))
    for row in root.findall(".//a:sheetData/a:row", NS):
        cells = {}
        max_col = -1
        for cell in row.findall("a:c", NS):
            idx = cell_col(cell.attrib.get("r", ""))
            cells[idx] = value_from_cell(cell, shared)
            max_col = max(max_col, idx)
        yield [cells.get(i, "") for i in range(max_col + 1)]


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: python analyze_xlsx.py workbook.xlsx")

    with zipfile.ZipFile(sys.argv[1]) as zf:
        shared = load_shared_strings(zf)
        sheets = load_sheets(zf)
        print(f"Workbook: {sys.argv[1]}")
        print(f"Sheets: {len(sheets)}")
        for name, path in sheets:
            rows = list(iter_rows(zf, path, shared))
            non_empty = [r for r in rows if any(str(v).strip() for v in r)]
            width = max((len(r) for r in non_empty), default=0)
            print("\n===")
            print(f"Sheet: {name}")
            print(f"Rows: {len(non_empty)}  Columns: {width}")
            if non_empty:
                print("Header:", " | ".join(str(v) for v in non_empty[0]))
                for row in non_empty[1:6]:
                    print("Row:", " | ".join(str(v) for v in row))
                first_col = Counter(str(r[0]).strip() for r in non_empty[1:] if r and str(r[0]).strip())
                if first_col:
                    print("First-column sample:", ", ".join(f"{k}={v}" for k, v in first_col.most_common(6)))


if __name__ == "__main__":
    main()
