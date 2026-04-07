#!/usr/bin/env python3
"""
I2Localization binary parser — supports UABEA RAW exports (.dat) and Unity .assets files directly.
No external dependencies required: reads Unity SerializedFile format in pure Python.
Works with PC, PS4, Switch and any platform that uses standard Unity assets files.

Parses binary TermData structure discovered by reverse-engineering:
  TermData = Term(str) + TermType(int) + Languages(str[21]) + DescBlob(str) + Trailing(int)

  Languages[21] layout (3 header cols + 18 language cols):
    [0]  = Description (dev notes)
    [1]  = Comments
    [2]  = Max Char Limit
    [3]  = Language 0  (e.g. English)
    [4]  = Language 1  (e.g. Japanese)
    ...
    [20] = Language 17 (e.g. VO)

Usage (CLI):
  python i2_localization.py <file.dat|resources.assets> --export-json <out.json>
  python i2_localization.py <file.dat|resources.assets> --export-csv  <out.csv>
  python i2_localization.py <file.dat|resources.assets> --import-json <in.json> --output <patched.dat>
  python i2_localization.py <file.dat|resources.assets> --stats
  python i2_localization.py <file.dat|resources.assets> --find "search term"
  python i2_localization.py resources.assets --path-id 27659 --export-json out.json

Usage (Python):
  from i2_localization import parse_dat, export_json, import_json, export_csv
  terms, languages = parse_dat("file.dat")           # UABEA RAW export
  terms, languages = parse_dat("resources.assets")   # Unity assets file (auto-detect)
  export_json(terms, languages, "out.json")
"""

from __future__ import annotations

import struct
import json
import csv
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Union

# ─── constants ────────────────────────────────────────────────────────────────

_HEADER_SIZE   = 44   # UABEA header (12 zero bytes + version info + name "I2Languages")
_TERMS_COUNT_OFF = 56  # offset of mTerms array count (int32)
_TERMS_DATA_OFF  = 60  # offset of first TermData

_HEADER_COLS = 3       # Languages[0..2] are metadata, not real translations
# Languages[0] = Description (dev notes)
# Languages[1] = Comments
# Languages[2] = Max Char Limit


# ─── data classes ─────────────────────────────────────────────────────────────

@dataclass
class I2Language:
    name: str
    code: str          # e.g. "en", "ja", "ko"
    index: int = 0     # index in mLanguages list


@dataclass
class I2Term:
    key:       str
    term_type: int
    languages: list   # all columns including metadata
    desc_blob: bytes  # trailing binary descriptor (usually 21 bytes of zeros)
    trailing:  int = 0

    def translation(self, lang_index: int) -> str:
        """Return translation for language at mLanguages[lang_index]."""
        col = lang_index + _HEADER_COLS
        return self.languages[col] if col < len(self.languages) else ""

    def translation_by_code(self, code: str, languages: list) -> str:
        for i, lang in enumerate(languages):
            if lang.code == code:
                return self.translation(i)
        return ""

    @property
    def english(self) -> str:
        """Convenience: return Languages[3] (first real language, usually English)."""
        return self.languages[_HEADER_COLS] if len(self.languages) > _HEADER_COLS else ""

    @property
    def comments(self) -> str:
        return self.languages[1] if len(self.languages) > 1 else ""

    @property
    def description_note(self) -> str:
        return self.languages[0] if self.languages else ""

    @property
    def max_char_limit(self) -> str:
        return self.languages[2] if len(self.languages) > 2 else ""


# ─── binary read helpers ──────────────────────────────────────────────────────

def _r_str(data: bytes, off: int):
    """Read Unity length-prefixed string (4-byte LE length + bytes + align4)."""
    if off + 4 > len(data):
        return None, off
    n = struct.unpack_from('<i', data, off)[0]
    if n < 0 or n > 500_000:
        return None, off
    raw = data[off + 4: off + 4 + n]
    text = raw.decode('utf-8', errors='replace')
    return text, off + 4 + n + (-n % 4)


def _r_int(data: bytes, off: int):
    return struct.unpack_from('<i', data, off)[0], off + 4


# ─── binary write helpers ─────────────────────────────────────────────────────

def _w_str(s: str) -> bytes:
    """Encode a string as Unity length-prefixed bytes with 4-byte alignment."""
    enc = s.encode('utf-8')
    n = len(enc)
    return struct.pack('<i', n) + enc + b'\x00' * ((-n) % 4)


def _w_raw_str(raw: bytes) -> bytes:
    """Encode raw bytes as a Unity length-prefixed string (for desc_blob)."""
    n = len(raw)
    return struct.pack('<i', n) + raw + b'\x00' * ((-n) % 4)


def _w_int(v: int) -> bytes:
    return struct.pack('<i', v)


# ─── term parse / serialise ───────────────────────────────────────────────────

def _parse_term(data: bytes, off: int):
    """Parse one I2Term from binary data at offset.  Returns (I2Term, next_off)."""
    start = off

    key, off = _r_str(data, off)
    if key is None:
        return None, start

    term_type, off = _r_int(data, off)

    lang_count, off = _r_int(data, off)
    if not (0 <= lang_count <= 200):
        return None, start

    langs = []
    for _ in range(lang_count):
        s, off = _r_str(data, off)
        if s is None:
            return None, start
        langs.append(s)

    # Trailing binary descriptor (usually 21-byte blob; stored as a "string")
    desc_text, off = _r_str(data, off)
    if desc_text is None:
        return None, start
    desc_blob = desc_text.encode('latin-1', errors='replace')

    trailing, off = _r_int(data, off)

    return I2Term(key, term_type, langs, desc_blob, trailing), off


def _serialise_term(term: I2Term) -> bytes:
    """Serialise an I2Term back to binary."""
    buf = bytearray()
    buf += _w_str(term.key)
    buf += _w_int(term.term_type)
    buf += _w_int(len(term.languages))
    for s in term.languages:
        buf += _w_str(s)
    buf += _w_raw_str(term.desc_blob)
    buf += _w_int(term.trailing)
    return bytes(buf)


# ─── language list parse ──────────────────────────────────────────────────────

def _parse_languages(data: bytes, off: int) -> tuple:
    """Parse mLanguages list.  Returns (list[I2Language], next_off)."""
    languages = []
    idx = 0
    while off < len(data) - 20:
        name, off2 = _r_str(data, off)
        if name is None or len(name) > 60:
            break
        # Validate: readable name
        if name and not all(ord(c) < 0x10000 for c in name):
            break
        code, off3 = _r_str(data, off2)
        if code is None or len(code) > 15:
            break
        # Code should be letters / hyphens / underscores (or empty)
        if code and not all(c.isalpha() or c in '-_' for c in code):
            break
        _extra, off4 = _r_int(data, off3)
        languages.append(I2Language(name=name, code=code, index=idx))
        idx += 1
        off = off4
        if len(languages) > 50:
            break
    return languages, off


# ─── Unity .assets extraction (pure Python, no IL2CPP tools needed) ───────────

def _read_assets_raw(assets_path: str, path_id: Optional[int] = None) -> bytes:
    """Pure-Python Unity SerializedFile reader.

    Parses the Unity SerializedFile binary format directly — no UnityPy,
    no GameAssembly.dll, no global-metadata.dat required.  Works with PC,
    PS4, Switch and any other platform that uses the standard Unity assets
    format (versions 9–22, i.e. Unity 2019–2022+).

    Args:
        assets_path: Path to the .assets file (e.g. resources.assets).
        path_id:     Optional pathID to extract directly.  When omitted the
                     function auto-detects the I2Languages MonoBehaviour.

    Returns:
        Raw bytes of the MonoBehaviour (identical layout to a UABEA RAW .dat).

    Raises:
        ValueError: if the file is not a valid SerializedFile or I2Languages
                    cannot be located.
    """
    with open(assets_path, 'rb') as f:
        data = f.read()

    # ── Header (always big-endian) ────────────────────────────────────────────
    if len(data) < 20:
        raise ValueError("File too small to be a Unity assets file")

    fmt = struct.unpack_from('>I', data, 8)[0]       # format version

    if fmt >= 22:
        # v22+: extended 64-bit header fields start at offset 20
        data_off  = struct.unpack_from('>q', data, 32)[0]
        big_end   = data[16] != 0
        pos       = data.index(b'\x00', 48) + 1      # skip unity_version\0
    elif fmt >= 9:
        data_off  = struct.unpack_from('>I', data, 12)[0]
        big_end   = data[13] != 0
        pos       = data.index(b'\x00', 17) + 1
    else:
        raise ValueError(f"Unsupported Unity SerializedFile format version {fmt}")

    E = '>' if big_end else '<'

    # ── Metadata header ───────────────────────────────────────────────────────
    pos += 4                                          # target_platform (int32)
    has_tree = data[pos]; pos += 1                    # enable_type_tree (uint8)

    # ── Type table ────────────────────────────────────────────────────────────
    tc = struct.unpack_from(f'{E}i', data, pos)[0]; pos += 4
    cids: list = []

    for _ in range(tc):
        cid = struct.unpack_from(f'{E}i', data, pos)[0]; pos += 4
        pos += 1                                      # is_stripped (uint8)
        pos += 2                                      # script_type_index (int16)

        # MonoBehaviour script GUID (present in format version >= 17)
        if fmt >= 17 and cid == 114:
            pos += 16                                 # script_id (16 bytes)

        pos += 16                                     # old_type_hash (always)

        if has_tree:
            nc  = struct.unpack_from(f'{E}i', data, pos)[0]; pos += 4
            sbs = struct.unpack_from(f'{E}i', data, pos)[0]; pos += 4
            # TypeTreeNode: 32 bytes for fmt >= 19, 24 bytes for older
            pos += nc * (32 if fmt >= 19 else 24) + sbs

        cids.append(cid)

    # ── Object table ──────────────────────────────────────────────────────────
    # Note: object_count is read WITHOUT pre-alignment; the 4-byte alignment
    # is applied per-entry (before each path_id), not before the count itself.
    oc = struct.unpack_from(f'{E}i', data, pos)[0]; pos += 4

    MONO = 114   # MonoBehaviour class ID
    all_objects: list = []

    for _ in range(oc):
        pos    = (pos + 3) & ~3                       # per-entry alignment
        oid    = struct.unpack_from(f'{E}q', data, pos)[0]; pos += 8
        bstart = struct.unpack_from(f'{E}q', data, pos)[0] if fmt >= 22 \
                 else struct.unpack_from(f'{E}I', data, pos)[0]
        pos   += 8 if fmt >= 22 else 4
        bsize  = struct.unpack_from(f'{E}I', data, pos)[0]; pos += 4
        tidx   = struct.unpack_from(f'{E}i', data, pos)[0]; pos += 4
        cid    = cids[tidx] if 0 <= tidx < len(cids) else -1
        all_objects.append((oid, data_off + bstart, bsize, cid))

    # ── Extract target bytes ───────────────────────────────────────────────────
    if path_id is not None:
        for oid, off, sz, _ in all_objects:
            if oid == path_id:
                return data[off:off + sz]
        raise ValueError(f"pathID {path_id} not found in {assets_path}")

    # Auto-detect: try MonoBehaviours first (class_id 114), then all large objects
    candidates = sorted(
        [(oid, off, sz) for oid, off, sz, cid in all_objects
         if cid == MONO and sz >= 10_000],
        key=lambda x: x[2], reverse=True,
    )
    if not candidates:
        candidates = sorted(
            [(oid, off, sz) for oid, off, sz, _ in all_objects if sz >= 10_000],
            key=lambda x: x[2], reverse=True,
        )

    for oid, off, sz in candidates:
        raw = data[off:off + sz]
        if len(raw) < 64:
            continue
        tc2 = struct.unpack_from('<i', raw, _TERMS_COUNT_OFF)[0]
        if not (100 < tc2 < 200_000):
            continue
        try:
            result, _ = _parse_term(raw, _TERMS_DATA_OFF)
            if result is not None and result.key:
                print(
                    f"  Found I2Languages  pathID={oid}  size={sz:,}  terms={tc2}",
                    file=sys.stderr,
                )
                return raw
        except Exception:
            continue

    raise ValueError(
        f"No I2Languages MonoBehaviour found in {assets_path}.\n"
        "Try specifying --path-id <id> if you know the exact pathID."
    )


# ─── public API ───────────────────────────────────────────────────────────────

def parse_dat(path: Union[str, Path], path_id: Optional[int] = None) -> tuple:
    """Parse I2Languages from a UABEA RAW .dat file or a Unity .assets file.

    Automatically detects the file type from the extension:
      - ``.dat``    → read directly as UABEA RAW binary export
      - ``.assets`` → extract I2Languages MonoBehaviour via UnityPy first

    Args:
        path:    Path to a .dat or .assets file.
        path_id: (assets only) Force extraction of a specific pathID.

    Returns:
        (terms: list[I2Term], languages: list[I2Language])
    """
    path = str(path)
    ext = Path(path).suffix.lower()

    if ext == '.assets':
        print(f"Extracting I2Languages from Unity assets file …", file=sys.stderr)
        data = _read_assets_raw(path, path_id=path_id)
    else:
        with open(path, 'rb') as f:
            data = f.read()

    term_count = struct.unpack_from('<i', data, _TERMS_COUNT_OFF)[0]
    off = _TERMS_DATA_OFF

    terms: list[I2Term] = []
    for i in range(term_count):
        result, off = _parse_term(data, off)
        if result is None:
            raise ValueError(
                f"Failed to parse term {i} at offset {off}. "
                f"Bytes: {data[off:off+20].hex()}"
            )
        terms.append(result)

    # Locate mLanguages section (starts with "English" name)
    lang_start = data.find(b'\x07\x00\x00\x00English', off)
    if lang_start == -1:
        # Try to find any language block after terms
        lang_start = off
    languages, _ = _parse_languages(data, lang_start)

    return terms, languages


def export_json(
    terms: list,
    languages: list,
    output_path: str,
    skip_special: bool = True,
    skip_empty: bool = True,
    include_comments: bool = False,
) -> int:
    """Export terms to JSON.

    JSON structure:
      {
        "languages": [{"code": "en", "name": "English"}, ...],
        "terms": {
          "UI/AbilityPoints": {"en": "Divine Inspiration", "ja": "神聖なる啓示", ...},
          ...
        }
      }

    Returns: number of terms written.
    """
    result: dict = {
        "format": "I2Localization",
        "languages": [
            {"code": lang.code, "name": lang.name}
            for lang in languages
        ],
        "terms": {}
    }

    SPECIAL_PREFIXES = ("REFS/", "FONTS/")

    for term in terms:
        key = term.key
        if not key:
            continue
        if skip_special and any(key.startswith(p) for p in SPECIAL_PREFIXES):
            continue

        entry: dict = {}
        for i, lang in enumerate(languages):
            val = term.translation(i)
            if val:
                col_key = lang.code if lang.code else lang.name
                entry[col_key] = val

        if include_comments and term.comments:
            entry["__comments__"] = term.comments
        if include_comments and term.max_char_limit:
            entry["__max_chars__"] = term.max_char_limit

        if skip_empty and not entry:
            continue

        result["terms"][key] = entry

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    return len(result["terms"])


def export_csv(
    terms: list,
    languages: list,
    output_path: str,
    skip_special: bool = True,
) -> int:
    """Export terms to CSV.

    Columns: key, [lang_code, ...]
    Returns: number of rows written.
    """
    SPECIAL_PREFIXES = ("REFS/", "FONTS/")
    lang_keys = [lang.code if lang.code else lang.name for lang in languages]

    rows_written = 0
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["key"] + lang_keys)
        for term in terms:
            key = term.key
            if not key:
                continue
            if skip_special and any(key.startswith(p) for p in SPECIAL_PREFIXES):
                continue
            row = [key] + [term.translation(i) for i in range(len(languages))]
            writer.writerow(row)
            rows_written += 1

    return rows_written


def import_json(
    source_dat: str,
    json_path: str,
    output_dat: str,
    lang_code_override: Optional[str] = None,
) -> tuple:
    """Patch a .dat file using translations from a JSON export.

    The JSON should have the same structure produced by export_json().
    Only non-empty strings in the JSON overwrite existing translations.
    Strings not present in the JSON are left unchanged.

    Returns: (patched_count, term_count)
    """
    terms, languages = parse_dat(source_dat)

    with open(json_path, 'r', encoding='utf-8') as f:
        patch = json.load(f)

    # Build code -> lang_index map
    code_to_idx: dict = {}
    for i, lang in enumerate(languages):
        k = lang.code if lang.code else lang.name
        code_to_idx[k] = i

    patch_terms: dict = patch.get("terms", {})
    patched = 0

    # Build key -> term index for fast lookup
    key_to_term: dict = {t.key: t for t in terms}

    for key, translations in patch_terms.items():
        term = key_to_term.get(key)
        if term is None:
            continue
        changed = False
        for code, text in translations.items():
            if code.startswith("__"):
                continue  # skip __comments__ etc.
            idx = code_to_idx.get(code)
            if idx is None:
                continue
            col = idx + _HEADER_COLS
            # Extend languages list if needed
            while len(term.languages) <= col:
                term.languages.append("")
            if term.languages[col] != text:
                term.languages[col] = text
                changed = True
        if changed:
            patched += 1

    # Rebuild binary
    with open(source_dat, 'rb') as f:
        original = f.read()

    # Replace mTerms block: keep header (first 56 bytes), write new term count + terms
    header = original[:_TERMS_COUNT_OFF]
    new_terms_bin = bytearray()
    new_terms_bin += _w_int(len(terms))
    for t in terms:
        new_terms_bin += _serialise_term(t)

    # Keep everything after old mTerms block (mLanguages etc.)
    # We need to find where old mTerms ended; use the offset we computed during parse
    _, old_terms_end = _find_terms_end(original)
    footer = original[old_terms_end:]

    output = header + bytes(new_terms_bin) + footer

    Path(output_dat).parent.mkdir(parents=True, exist_ok=True)
    with open(output_dat, 'wb') as f:
        f.write(output)

    return patched, len(terms)


def _find_terms_end(data: bytes) -> tuple:
    """Return (terms_list, offset_after_last_term)."""
    term_count = struct.unpack_from('<i', data, _TERMS_COUNT_OFF)[0]
    off = _TERMS_DATA_OFF
    terms = []
    for i in range(term_count):
        result, off = _parse_term(data, off)
        if result is None:
            raise ValueError(f"Parse failed at term {i}")
        terms.append(result)
    return terms, off


def print_stats(terms: list, languages: list) -> None:
    """Print summary statistics."""
    print(f"Terms total       : {len(terms)}")
    print(f"Languages         : {len(languages)}")
    print()
    print(f"{'#':>3}  {'Code':<8}  {'Name':<25}  {'Translations':>12}")
    print("─" * 55)
    for i, lang in enumerate(languages):
        count = sum(1 for t in terms if t.translation(i))
        code = lang.code if lang.code else "—"
        print(f"{i:>3}  {code:<8}  {lang.name:<25}  {count:>12}")

    real = sum(1 for t in terms
               if t.key and not t.key.startswith(("REFS/", "FONTS/"))
               and any(t.translation(i) for i in range(len(languages))))
    print()
    print(f"Translatable terms: {real}")


def find_terms(terms: list, languages: list, query: str) -> list:
    """Search terms whose key or any translation contains query (case-insensitive)."""
    q = query.lower()
    results = []
    for t in terms:
        if q in t.key.lower():
            results.append(t)
            continue
        if any(q in s.lower() for s in t.languages if s):
            results.append(t)
    return results


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _cli():
    import argparse

    p = argparse.ArgumentParser(
        description=(
            "I2Localization parser — reads UABEA RAW .dat exports "
            "or Unity .assets files directly (pure Python, no external tools needed)"
        )
    )
    p.add_argument(
        "dat_file",
        help="Input file: UABEA RAW export (.dat) or Unity assets file (.assets)",
    )
    p.add_argument("--export-json",  metavar="OUT",  help="Export all terms to JSON")
    p.add_argument("--export-csv",   metavar="OUT",  help="Export all terms to CSV")
    p.add_argument("--import-json",  metavar="IN",   help="JSON file with translations to patch in")
    p.add_argument("--output",       metavar="OUT",  help="Output .dat for --import-json")
    p.add_argument("--stats",        action="store_true", help="Show statistics")
    p.add_argument("--find",         metavar="QUERY", help="Search terms by key or translation")
    p.add_argument("--include-special", action="store_true",
                   help="Include REFS/ and FONTS/ metadata terms in export")
    p.add_argument("--include-comments", action="store_true",
                   help="Include __comments__ and __max_chars__ in JSON export")
    p.add_argument("--lang",         metavar="CODE",
                   help="Filter --find results to a specific language code")
    p.add_argument("--path-id",      metavar="ID",   type=int,
                   help="(assets only) Force extraction of a specific MonoBehaviour pathID")

    args = p.parse_args()

    print(f"Parsing {args.dat_file} …", file=sys.stderr)
    terms, languages = parse_dat(args.dat_file, path_id=args.path_id)
    print(f"Parsed {len(terms)} terms, {len(languages)} languages.", file=sys.stderr)

    if args.stats:
        print_stats(terms, languages)

    if args.export_json:
        n = export_json(
            terms, languages, args.export_json,
            skip_special=not args.include_special,
            include_comments=args.include_comments,
        )
        print(f"Exported {n} terms → {args.export_json}")

    if args.export_csv:
        n = export_csv(terms, languages, args.export_csv,
                       skip_special=not args.include_special)
        print(f"Exported {n} rows → {args.export_csv}")

    if args.import_json:
        out = args.output or args.dat_file.replace(".dat", "_patched.dat")
        patched, total = import_json(args.dat_file, args.import_json, out)
        print(f"Patched {patched} / {total} terms → {out}")

    if args.find:
        results = find_terms(terms, languages, args.find)
        print(f"\nSearch '{args.find}' — {len(results)} result(s)\n")
        for t in results[:50]:
            print(f"  [{t.key}]")
            lang_filter = args.lang
            for i, lang in enumerate(languages):
                if lang_filter and lang.code != lang_filter:
                    continue
                val = t.translation(i)
                if val:
                    code = lang.code if lang.code else lang.name
                    print(f"    {code:<8} {val[:80]}")
        if len(results) > 50:
            print(f"  … and {len(results)-50} more")


if __name__ == "__main__":
    _cli()
