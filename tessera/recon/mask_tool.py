"""Mask personal identifiers in a CSV before it is used or shared. Amounts, dates and headers are left alone.

Design points that matter for reconciliation:
- Tokens are an HMAC of the value under a secret key, so the SAME value always becomes the SAME token. A payee that appears many times still
  appears many times (the anomaly rules need that) and a UTR in the bank file still equals the UTR in the gateway file, as long as you mask
  every file with the same key file.
- Without the key, tokens cannot be reversed or brute-forced (a bare hash of a 10-digit phone number could be, which is why a key is used).
- Free-text narrations cannot be masked perfectly. The tool reports what it still sees, and you should read a sample yourself."""
from __future__ import annotations
import csv, hashlib, hmac, re, secrets, sys
from pathlib import Path

TEXT_HEADERS = re.compile(r"narration|description|particulars|remarks|details|memo|name|payee|beneficiary|note|reference|utr|ref", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
UPI = re.compile(r"\b[\w.-]{2,}@[a-z]{2,}\b", re.I)
PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
AADHAAR = re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}\b")       # grouped form only: a bare 12-digit run is usually a UPI reference (masked as REF)
CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
PHONE = re.compile(r"(?<![\d.])(?:\+?91[ -]?)?[6-9]\d{9}(?!\d)")
TOKEN = re.compile(r"\b(?:NAME|EMAIL|UPI|PAN|AADHAAR|CARD|PHONE|REF|ID)-[a-z]{4,}(?:@masked(?:\.invalid)?)?")
LONG = re.compile(r"(?<!\d)\d{9,}(?!\d|\.\d)")      # a 9+ digit run anywhere (after a comma or period too), except the integer part of a decimal amount

def load_key(path: str | Path) -> bytes:
    p = Path(path)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(secrets.token_bytes(32)); p.chmod(0o600)
    return p.read_bytes()

class Masker:
    def __init__(self, key: bytes, names: list[str] | None = None):
        self.key = key; self.names = sorted({n.strip() for n in (names or []) if n.strip()}, key=len, reverse=True); self.counts: dict[str, int] = {}
    def tok(self, kind: str, value: str, n: int = 8) -> str:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        d = hmac.new(self.key, re.sub(r"\D", "", value).encode() if kind in ("PHONE", "CARD", "AADHAAR", "REF") else value.lower().encode(), hashlib.sha256).digest()
        return f"{kind}-" + "".join(chr(97 + b % 26) for b in d[:n])        # letters only: a token can never look like a number, so it cannot be mistaken for a leak
    def text(self, s: str) -> str:
        for name in self.names: s = re.sub(re.escape(name), lambda m: self.tok("NAME", m.group(), 6), s, flags=re.I)
        s = EMAIL.sub(lambda m: self.tok("EMAIL", m.group(), 8) + "@masked.invalid", s)
        s = UPI.sub(lambda m: self.tok("UPI", m.group(), 8) + "@masked", s)
        s = PAN.sub(lambda m: self.tok("PAN", m.group(), 6), s)
        s = AADHAAR.sub(lambda m: self.tok("AADHAAR", m.group(), 6), s)
        s = CARD.sub(lambda m: self.tok("CARD", m.group(), 8), s)
        s = PHONE.sub(lambda m: self.tok("PHONE", m.group(), 8), s)
        return LONG.sub(lambda m: self.tok("REF", m.group(), 10), s)
    def cell(self, s: str, whole_value: bool = False) -> str:
        return self.tok("ID", s, 10) if whole_value and s.strip() else self.text(s)

def residual(rows: list[list[str]], header: list[str] | None = None) -> dict:
    """Scan MASKED output for identifiers that are still visible, in EVERY cell (a header-based filter would hide exactly the cells that
    were never classified as text). High-severity items should be zero before sharing anything."""
    high = {"email": 0, "long_digits": 0, "pan": 0}; names = []
    for r in rows:
        for v in r:
            v = TOKEN.sub("", v)
            if EMAIL.search(v) or re.search(r"@", v): high["email"] += 1
            if LONG.search(v): high["long_digits"] += 1
            if PAN.search(v): high["pan"] += 1
            for m in re.finditer(r"\b[A-Z][a-z]{2,}\s+[A-Z][a-z]{2,}\b", v):
                if len(names) < 8: names.append(m.group())
    return {"high_severity": high, "possible_names_to_review": names, "clean": not any(high.values())}

class MaskError(ValueError): ...

def mask_file(src: str | Path, dest: str | Path, key_file: str | Path, text_columns: list[str] | None = None, id_columns: list[str] | None = None,
              names: list[str] | None = None) -> dict:
    """Bank exports usually start with a title/account block and end with a footer, and those lines carry account numbers and names too.
    The real header is found first; title and footer lines are masked in every cell; data rows are masked in text columns only (amounts and dates
    are never touched). If no text column can be identified the tool refuses, because doing nothing while reporting success is the worst outcome.
    text_columns: free-text columns to scan (default: any header that looks like narration/description/name/reference).
    id_columns: columns whose WHOLE value is replaced by a token (e.g. account holder name, customer id)."""
    from .trial_checks import read_table
    m = Masker(load_key(key_file), names); t = read_table(src); header = t["header"]
    tcols = {i for i, h in enumerate(header) if (h in text_columns if text_columns else TEXT_HEADERS.search(h))}
    icols = {header.index(c) for c in (id_columns or []) if c in header}
    if not tcols and not icols: raise MaskError(f"no free-text columns recognised in the header {header}. Pass --text-columns with the narration/description column names.")
    pre = [[m.text(v) for v in r] for r in t["preamble"]]; foot = [[m.text(v) for v in r] for r in t["footer"]]
    body = [[m.cell(v, True) if i in icols else m.text(v) if i in tcols else v for i, v in enumerate(r)] for r in t["data"]]
    with open(dest, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=t["delimiter"]); w.writerows(pre); w.writerow(header); w.writerows(body); w.writerows(foot)
    Path(dest).chmod(0o600)
    return {"rows": len(body), "text_columns": [header[i] for i in sorted(tcols)], "id_columns": [header[i] for i in sorted(icols)], "title_lines_masked": len(pre), "footer_lines_masked": len(foot),
            "replaced": m.counts, "residual": residual(pre + body + foot)}

def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser("mask-statement", description="Mask identifiers in CSV files with a consistent secret key.")
    ap.add_argument("files", nargs="+"); ap.add_argument("--out-dir", required=True); ap.add_argument("--key-file", default="data/trial/mask.key", help="created if missing; KEEP SECRET, never share it with the masked files")
    ap.add_argument("--text-columns", help="comma-separated; default: auto-detect"); ap.add_argument("--id-columns", help="comma-separated columns to tokenise entirely")
    ap.add_argument("--names-file", help="one name per line (yours, family, staff, customers) to replace everywhere")
    a = ap.parse_args(argv); names = Path(a.names_file).read_text().splitlines() if a.names_file else []
    Path(a.out_dir).mkdir(parents=True, exist_ok=True); bad = False
    for f in a.files:
        try: r = mask_file(f, Path(a.out_dir) / (Path(f).stem + ".masked.csv"), a.key_file, a.text_columns.split(",") if a.text_columns else None, a.id_columns.split(",") if a.id_columns else None, names)
        except MaskError as e: print(f"{f}: REFUSED. {e}"); bad = True; continue
        print(f"{f}: {r['rows']} rows | scanned {r['text_columns']} | title/footer lines masked {r['title_lines_masked']}/{r['footer_lines_masked']} | replaced {r['replaced']}"); res = r["residual"]
        print("   still visible:", res["high_severity"], "| possible names to read through:", res["possible_names_to_review"]); bad |= not res["clean"]
    print("\nRead a sample of the masked files yourself before sharing anything. Automatic masking of free text is never complete." if not bad else "\nHIGH-SEVERITY identifiers remain; do not use or share these files yet.")
    return 2 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
