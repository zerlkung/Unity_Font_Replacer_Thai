# Unity Font Replacer — Thai Edition

> เครื่องมือเปลี่ยนฟอนต์ในเกม Unity ให้รองรับภาษาไทย โดยไม่ต้องมี source code ของเกม
>
> A tool to replace fonts in compiled Unity games with Thai fonts — no source code required.

Fork จาก / Forked from: [snowyegret23/Unity_Font_Replacer](https://github.com/snowyegret23/Unity_Font_Replacer)

---

## สารบัญ / Table of Contents

- [ความสามารถ / Features](#ความสามารถ--features)
- [ความต้องการของระบบ / Requirements](#ความต้องการของระบบ--requirements)
- [การติดตั้ง / Installation](#การติดตั้ง--installation)
- [เตรียมฟอนต์ไทย / Thai Font Setup](#เตรียมฟอนต์ไทย--thai-font-setup)
- [วิธีใช้งาน / Usage](#วิธีใช้งาน--usage)
- [ตัวเลือก / Options](#ตัวเลือก--options)
- [Addressables Catalog](#addressables-catalog)
- [เครดิต / Credits](#เครดิต--credits)
- [สัญญาอนุญาต / License](#สัญญาอนุญาต--license)

---

## ความสามารถ / Features

| ฟีเจอร์ | รายละเอียด |
|---|---|
| TTF replacement | เปลี่ยนฟอนต์ TTF ใน asset bundle โดยตรง |
| TMP SDF replacement | รองรับ TextMeshPro ทั้ง schema เก่า (Unity ≤ 2018.3) และใหม่ (Unity ≥ 2018.4) |
| Thai bulk modes | `--sarabun` และ `--notosansthai` เปลี่ยนทุกฟอนต์ในครั้งเดียว |
| SDF atlas generator | สร้าง SDF atlas จากไฟล์ TTF ไทย พร้อม charset ภาษาไทยในตัว |
| PS5 swizzle | รองรับ texture memory layout ของ PlayStation 5 |
| Addressables catalog | อ่าน / แก้ไข / Patch CRC ใน `catalog.json`, `catalog.bin`, `catalog.bundle` |

---

## ความต้องการของระบบ / Requirements

- **Python** 3.12 ขึ้นไป
- **OS**: Windows (รองรับ Linux/macOS บางส่วน)

---

## การติดตั้ง / Installation

```bash
pip install UnityPy TypeTreeGeneratorAPI Pillow scipy
```

Clone โปรเจกต์:

```bash
git clone https://github.com/zerlkung/Unity_Font_Replacer_Thai.git
cd Unity_Font_Replacer_Thai
```

---

## เตรียมฟอนต์ไทย / Thai Font Setup

### 1. ดาวน์โหลดฟอนต์ไทย

ฟอนต์ที่แนะนำ (ฟรี / Free):

| ฟอนต์ | ลิงก์ |
|---|---|
| Sarabun | [Google Fonts](https://fonts.google.com/specimen/Sarabun) |
| Noto Sans Thai | [Google Fonts](https://fonts.google.com/noto/specimen/Noto+Sans+Thai) |

### 2. วางไฟล์ใน `TH_ASSETS/`

```
TH_ASSETS/
  Sarabun.ttf
  NotoSansThai.ttf
```

### 3. สร้าง SDF atlas (เฉพาะเกมที่ใช้ TextMeshPro)

```bash
python make_sdf.py --ttf TH_ASSETS/Sarabun.ttf
```

ย้ายไฟล์ output ที่ได้ไปไว้ใน `TH_ASSETS/`:

```
TH_ASSETS/
  Sarabun SDF.json
  Sarabun SDF Atlas.png
  Sarabun SDF Material.json
```

---

## วิธีใช้งาน / Usage

### สแกนฟอนต์ในเกม / Scan fonts

สร้าง JSON map ของฟอนต์ทั้งหมดในเกม:

```bash
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --parse
```

---

### เปลี่ยนฟอนต์แบบเหมารวม / Bulk replace

**Sarabun:**
```bash
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --sarabun
```

**Noto Sans Thai:**
```bash
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --notosansthai
```

เปลี่ยนเฉพาะ SDF (TextMeshPro) หรือ TTF:
```bash
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --sarabun --sdfonly
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --sarabun --ttfonly
```

---

### เปลี่ยนฟอนต์รายตัว / Per-font replace

แก้ไขไฟล์ JSON ที่ได้จาก `--parse` เพื่อกำหนดว่าจะเปลี่ยนฟอนต์ไหนเป็นอะไร:

```bash
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --list font_map.json
```

---

### ดึงฟอนต์ออกจากเกม / Extract fonts

ดึง TMP SDF font assets (JSON + PNG atlas) จากเกม:

```bash
python export_fonts_th.py --gamepath "C:/path/to/game"
```

---

### โหมด Interactive / Interactive mode

รันโดยไม่ใส่ flag เพื่อเลือกจากเมนู:

```bash
python unity_font_replacer_th.py --gamepath "C:/path/to/game"
```

```
Select a task:
  1. Export font info (create JSON)
  2. Replace fonts using JSON
  3. Bulk replace with Sarabun (Thai)
  4. Bulk replace with Noto Sans Thai
  5. Bulk replace with Mulmaru
  6. Bulk replace with NanumGothic
  7. Preview export (Atlas/Glyph crops)
```

---

## ตัวเลือก / Options

| Flag | ภาษาไทย | English |
|---|---|---|
| `--sarabun` | เปลี่ยนทุกฟอนต์เป็น Sarabun | Bulk replace with Sarabun |
| `--notosansthai` | เปลี่ยนทุกฟอนต์เป็น Noto Sans Thai | Bulk replace with Noto Sans Thai |
| `--sdfonly` | เปลี่ยนเฉพาะ SDF (TextMeshPro) | SDF fonts only |
| `--ttfonly` | เปลี่ยนเฉพาะ TTF | TTF fonts only |
| `--parse` | ส่งออกข้อมูลฟอนต์เป็น JSON | Export font info to JSON |
| `--list <file>` | เปลี่ยนตาม JSON mapping | Replace via JSON mapping |
| `--ps5-swizzle` | เปิด PS5 swizzle/unswizzle | Enable PS5 swizzle mode |
| `--preview-export` | Export preview ก่อนเปลี่ยน | Export preview PNGs |
| `--scan-jobs <n>` | จำนวน parallel worker | Parallel scan workers |
| `--output-only <dir>` | บันทึกเฉพาะไฟล์ที่เปลี่ยน | Save only modified files |

---

## I2Localization Parser

`i2_localization.py` — อ่านและแก้ไขไฟล์ localization จาก [I2Localization](https://inter-illusion.com/assets/I2-Localization)
รองรับทั้ง UABEA RAW export (`.dat`) และไฟล์ Unity assets (`.assets`) โดยตรง โดยไม่ต้องผ่าน UABEA

> **หมายเหตุ**: ใช้ได้กับเกมที่ build แบบ IL2CPP (stripped type tree) ซึ่ง UABEA export JSON ได้แค่ header — parser นี้อ่าน binary ได้โดยตรง
> รองรับหลาย platform: PC, PS4, Switch ฯลฯ — ไม่ต้องการ `GameAssembly.dll`, `global-metadata.dat` หรือ `UnityPy`

### โครงสร้างข้อมูล / Data Layout

Binary format ที่ถอดรหัสได้:
- `TermData` = `Term(str)` + `TermType(int)` + `Languages[21](str[])` + `DescBlob(str)` + `Trailing(int)`
- `Languages[21]` ประกอบด้วย 3 metadata columns + 18 ภาษา (EN/JA/RU/FR/DE/ES/PT/ZH-CN/ZH-TW/KO/IT/NL/TR/FR-CA/AR ฯลฯ)

### คำสั่ง / CLI

| คำสั่ง | รายละเอียด / Description |
|--------|--------------------------|
| `--stats` | แสดงสถิติจำนวน term และ translation ต่อภาษา |
| `--export-json <out>` | Export ทุก term พร้อม key และ translation ทุกภาษา เป็น JSON |
| `--export-csv <out>` | Export เป็น CSV (เปิดใน Excel ได้) |
| `--import-json <in> --output <out>` | นำ JSON ที่แก้ไขแล้ว import กลับเป็น .dat |
| `--find <query>` | ค้นหา term จาก key หรือ translation |
| `--lang <code>` | กรองผลลัพธ์ --find ตาม language code เช่น `en`, `ko` |
| `--include-special` | รวม REFS/ และ FONTS/ metadata terms ใน export |
| `--include-comments` | รวม `__comments__` และ `__max_chars__` ใน JSON |
| `--path-id <id>` | (`.assets` เท่านั้น) ระบุ pathID ของ MonoBehaviour โดยตรง |

```bash
# Export จาก UABEA RAW export (.dat)
python i2_localization.py I2Languages.dat --export-json terms.json

# Export โดยตรงจาก Unity assets file (ไม่ต้องผ่าน UABEA)
python i2_localization.py resources.assets --export-json terms.json

# ระบุ pathID โดยตรง (ถ้ารู้)
python i2_localization.py resources.assets --path-id 27659 --export-json terms.json

# ดูสถิติ
python i2_localization.py I2Languages.dat --stats

# ค้นหาคำ
python i2_localization.py I2Languages.dat --find "Crusade" --lang en

# แปลใหม่แล้ว import กลับ
python i2_localization.py I2Languages.dat --import-json my_thai.json --output patched.dat
```

### รูปแบบ JSON / JSON Format

```json
{
  "languages": [{"code": "en", "name": "English"}, ...],
  "terms": {
    "UI/AbilityPoints": {
      "en": "Divine Inspiration",
      "ja": "神聖なる啓示",
      "ko": "종교적 영감"
    }
  }
}
```

### Python API

```python
from i2_localization import parse_dat, export_json, import_json, find_terms

# From UABEA RAW export
terms, languages = parse_dat("I2Languages.dat")

# Or directly from Unity assets file (requires UnityPy)
terms, languages = parse_dat("resources.assets")
terms, languages = parse_dat("resources.assets", path_id=27659)  # specific pathID

export_json(terms, languages, "out.json")

# Search
results = find_terms(terms, languages, "ability")
for t in results:
    print(t.key, "→", t.english)
```

---

## Addressables Catalog

`addressables_catalog.py` — Python port ของ [nesrak1/AddressablesTools](https://github.com/nesrak1/AddressablesTools)

อ่านและแก้ไข Unity Addressables catalog files (`catalog.json`, `catalog.bin`, `catalog.bundle`)

### CLI

| คำสั่ง | ผลลัพธ์ |
|---|---|
| `python addressables_catalog.py catalog.json` | แสดง summary |
| `python addressables_catalog.py catalog.json --fonts` | แสดง font ทั้งหมด |
| `python addressables_catalog.py catalog.json <pattern>` | ค้นหาด้วย regex |
| `python addressables_catalog.py catalog.json --patch-crc out.json` | Patch CRC แล้วบันทึก |

เพิ่ม `--output <file>` เพื่อเซฟผลลัพธ์เป็นไฟล์ (สร้างโฟลเดอร์อัตโนมัติ):

```bash
# แสดง font ทั้งหมด พร้อมชื่อ bundle ที่อยู่ แล้วเซฟเป็นไฟล์
python addressables_catalog.py catalog.json --fonts --output result/fonts.txt

# ค้นหาไฟล์ .otf และ .ttf ทั้งหมด
python addressables_catalog.py catalog.json "\.otf|\.ttf" --output result/fonts.txt

# Patch CRC หลังแก้ไข bundle
python addressables_catalog.py catalog.json --patch-crc catalog_patched.json
```

**ตัวอย่าง output:**
```
[Assets/Resources_moved/Fonts/Headings/6092-Reg.otf]  Assets/.../6092-Reg.otf  →  000f31824b70d0c577402a06d3c2cb8c.bundle
[Assets/Resources_moved/Fonts/Body/NotoSans.ttf]  Assets/.../NotoSans.ttf  →  0a1d5db632cad408c6acb9f588cfc39c.bundle
```

### ใช้ใน Python script

```python
from addressables_catalog import (
    read_catalog, patch_crc, find_font_resources,
    find_resources, get_bundle_for_location, write_catalog_json
)

cat = read_catalog("catalog.json")   # รองรับ .json / .bin / .bundle

# หา font ทั้งหมดพร้อม bundle ที่อยู่
fonts = find_font_resources(cat)
for loc in fonts:
    bundle = get_bundle_for_location(loc)
    print(f"{loc.primary_key}  →  {bundle}")

# ค้นหาด้วย pattern
results = find_resources(cat, r"\.otf|\.ttf")

# Patch CRC แล้วบันทึก
n = patch_crc(cat)
write_catalog_json(cat, "catalog_patched.json")
print(f"Patched {n} bundle CRC(s)")
```

---

## เครดิต / Credits

| โปรเจกต์ | ผู้สร้าง | หมายเหตุ |
|---|---|---|
| [Unity_Font_Replacer](https://github.com/snowyegret23/Unity_Font_Replacer) | snowyegret23 | ต้นฉบับของ fork นี้ |
| [AddressablesTools](https://github.com/nesrak1/AddressablesTools) | nesrak1 | C# library ต้นแบบของ `addressables_catalog.py` |

---

## สัญญาอนุญาต / License

ดู [LICENSE](LICENSE) — เหมือนกับโปรเจกต์ต้นฉบับ / Same as the original project.
