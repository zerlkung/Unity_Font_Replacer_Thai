# Unity Font Replacer — Thai Edition

> เครื่องมือเปลี่ยนฟอนต์ในเกม Unity ให้รองรับภาษาไทย โดยไม่ต้องมี source code ของเกม
>
> Replace fonts in compiled Unity games with Thai fonts — no source code required.

Fork จาก / Forked from: [snowyegret23/Unity_Font_Replacer](https://github.com/snowyegret23/Unity_Font_Replacer)

---

## สารบัญ / Table of Contents

- [รองรับ Platform](#รองรับ-platform)
- [ความต้องการของระบบ / Requirements](#ความต้องการของระบบ--requirements)
- [การติดตั้ง / Installation](#การติดตั้ง--installation)
- [เตรียมฟอนต์ไทย / Thai Font Setup](#เตรียมฟอนต์ไทย--thai-font-setup)
- [วิธีใช้งาน / Usage](#วิธีใช้งาน--usage)
- [ตัวเลือก / Options](#ตัวเลือก--options)
- [I2Localization Parser](#i2localization-parser)
- [Addressables Catalog](#addressables-catalog)
- [เครดิต / Credits](#เครดิต--credits)

---

## รองรับ Platform

| Platform | --parse (สแกนฟอนต์) | --sarabun / --notosansthai (เปลี่ยนฟอนต์) | หมายเหตุ |
|----------|---------------------|---------------------------------------------|----------|
| **PC (Windows, IL2CPP)** | TTF + SDF | TTF + SDF | ต้องการ Il2CppDumper + TypeTreeGeneratorAPI |
| **PC (Windows, Mono)** | TTF + SDF | TTF + SDF | ใช้ Managed/ folder โดยตรง |
| **PS4** | TTF เท่านั้น | TTF เท่านั้น | รองรับ BC texture swizzle — SDF/TMP ยังไม่รองรับ |
| **PS5** | TTF + SDF | TTF + SDF | ต้องใช้ `--ps5-swizzle` |

> **PS4 — SDF/TMP:** Il2CppDumper ยังไม่รองรับ PS4 ELF binary format
> จึงไม่สามารถสร้าง type tree สำหรับ TMP_FontAsset ได้ — SDF fonts ถูก skip อัตโนมัติ
>
> **Smart scan:** การสแกนแบบ catalog-based (เฉพาะ `.assets` root + font bundles ที่ระบุใน `catalog.json`)
> เปิดใช้งานโดยอัตโนมัติเสมอ ไม่จำเป็นต้องใส่ flag พิเศษ

---

## ความต้องการของระบบ / Requirements

- **Python** 3.12 ขึ้นไป
- **OS**: Windows (รองรับ Linux/macOS บางส่วน)

### โครงสร้างไฟล์ที่ต้องการตาม Platform

**PC (IL2CPP):**
```
<game_root>/
  GameAssembly.dll               ← IL2CPP binary
  <GameName>_Data/
    il2cpp_data/
      Metadata/
        global-metadata.dat      ← IL2CPP metadata
```

**PS4:**
```
Image0/
  eboot.bin                      ← PS4 executable (ตรวจพบอัตโนมัติ)
  Media/                         ← หรือ Media_Data/ — ตรวจพบอัตโนมัติทั้งคู่
    Metadata/
      global-metadata.dat        ← IL2CPP metadata
    StreamingAssets/
      aa/
        catalog.json             ← Addressables catalog (ใช้สำหรับ smart scan)
```

**PC (Mono):**
```
<game_root>/
  <GameName>_Data/
    Managed/                     ← DLL folder (ไม่ต้องตั้งค่าเพิ่ม)
```

> **Auto-detection:** `--gamepath` รับได้ทั้ง game root, `_Data` folder, หรือ `Media` folder โดยตรง
> ระบบจะหา data folder ให้อัตโนมัติ

---

## การติดตั้ง / Installation

### 1. Clone โปรเจกต์

```bash
git clone https://github.com/zerlkung/Unity_Font_Replacer_Thai.git
cd Unity_Font_Replacer_Thai
```

### 2. ติดตั้ง Python packages

**PC (ต้องการ SDF / TMP support):**
```bash
pip install UnityPy TypeTreeGeneratorAPI Pillow scipy
```

**PS4 / Mono (TTF only หรือ Mono):**
```bash
pip install UnityPy Pillow scipy
```

> `TypeTreeGeneratorAPI` ต้องการสำหรับการ parse SDF/TMP fonts บนเกม IL2CPP (PC)
> ถ้าไม่ติดตั้งจะยังใช้งานได้ แต่จะสแกนได้เฉพาะ TTF fonts เท่านั้น

### 3. ติดตั้ง Il2CppDumper (PC IL2CPP เท่านั้น)

> ข้ามขั้นตอนนี้ถ้าเกมเป็น Mono หรือ PS4

วาง `Il2CppDumper.exe` ไว้ที่:
```
Il2CppDumper/
  Il2CppDumper.exe
```

ดาวน์โหลดได้จาก: [Perfare/Il2CppDumper](https://github.com/Perfare/Il2CppDumper/releases)

---

## เตรียมฟอนต์ไทย / Thai Font Setup

### 1. ดาวน์โหลดฟอนต์ไทย

| ฟอนต์ | ลิงก์ | ใช้กับ |
|-------|-------|--------|
| Sarabun | [Google Fonts](https://fonts.google.com/specimen/Sarabun) | TTF + SDF |
| Noto Sans Thai | [Google Fonts](https://fonts.google.com/noto/specimen/Noto+Sans+Thai) | TTF + SDF |

### 2. วางไฟล์ TTF ใน `TH_ASSETS/`

```
TH_ASSETS/
  Sarabun.ttf
  NotoSansThai.ttf
```

### 3. สร้าง SDF atlas (เฉพาะเกม PC ที่ใช้ TextMeshPro)

> ข้ามขั้นตอนนี้ถ้าเป็น PS4 หรือเกมที่ไม่ใช้ TextMeshPro

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

### ขั้นตอนที่ 1 — สแกนฟอนต์ในเกม

สร้าง JSON map ของฟอนต์ทั้งหมดในเกม:

**PC:**
```bash
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --parse
```

**PS4:**
```bash
python unity_font_replacer_th.py --gamepath "C:/path/to/Image0" --parse
```

ผลลัพธ์จะบันทึกเป็นไฟล์ `<game_name>.json` ในโฟลเดอร์เดียวกับ script

> **PS4:** จะพบเฉพาะ TTF fonts เท่านั้น SDF/TMP fonts ถูก skip โดยอัตโนมัติ
> ถ้าต้องการ SDF fonts ต้องใช้ PC version ของเกมแทน

---

### ขั้นตอนที่ 2 — เปลี่ยนฟอนต์

#### เปลี่ยนแบบเหมารวม / Bulk replace

เปลี่ยนทุกฟอนต์ในเกมเป็นฟอนต์ไทยในครั้งเดียว:

```bash
# Sarabun
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --sarabun

# Noto Sans Thai
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --notosansthai
```

เปลี่ยนเฉพาะบางประเภท:
```bash
# เฉพาะ SDF (TextMeshPro) — PC เท่านั้น
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --sarabun --sdfonly

# เฉพาะ TTF — ใช้ได้ทั้ง PC และ PS4
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --sarabun --ttfonly
```

#### เปลี่ยนรายตัวผ่าน JSON / Per-font via JSON

แก้ไขไฟล์ JSON ที่ได้จาก `--parse` ระบุว่าฟอนต์ไหนจะเปลี่ยนเป็นอะไร แล้วรัน:

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

| Flag | รายละเอียด | PC | PS4 |
|------|------------|:--:|:---:|
| `--gamepath <path>` | Path ของโฟลเดอร์เกม (รับ game root / _Data / Media) | ✓ | ✓ |
| `--parse` | สแกนฟอนต์ → บันทึกเป็น JSON (ใช้ smart scan อัตโนมัติ) | ✓ | ✓ (TTF only) |
| `--sarabun` | เปลี่ยนทุกฟอนต์เป็น Sarabun | ✓ | ✓ (TTF only) |
| `--notosansthai` | เปลี่ยนทุกฟอนต์เป็น Noto Sans Thai | ✓ | ✓ (TTF only) |
| `--list <file>` | เปลี่ยนตาม JSON mapping | ✓ | ✓ |
| `--sdfonly` | เปลี่ยนเฉพาะ SDF (TextMeshPro) | ✓ | ✗ |
| `--ttfonly` | เปลี่ยนเฉพาะ TTF | ✓ | ✓ |
| `--ps5-swizzle` | เปิด PS5 texture swizzle/unswizzle | ✓ | ✗ |
| `--preview-export` | Export preview PNG ก่อนเปลี่ยน | ✓ | - |
| `--scan-jobs <n>` | จำนวน parallel worker (default: 1) | ✓ | ✓ |
| `--output-only <dir>` | บันทึกเฉพาะไฟล์ที่เปลี่ยน | ✓ | ✓ |
| `--target-file <name>` | สแกนเฉพาะไฟล์ที่ระบุ (ชื่อไฟล์ หรือคั่นด้วย `,`) | ✓ | ✓ |

> **Smart scan (ค่าเริ่มต้น):** `--parse` จะสแกนเฉพาะ `.assets` ใน data root และ `.bundle` ที่ระบุใน `catalog.json`
> ทำให้เร็วกว่าการ scan ทุกไฟล์อย่างมาก (จาก ~15,000 ไฟล์ เหลือ ~20 ไฟล์)

---

## I2Localization Parser

`i2_localization.py` — อ่านและแก้ไขไฟล์ localization จาก [I2Localization](https://inter-illusion.com/assets/I2-Localization)

รองรับทั้ง UABEA RAW export (`.dat`) และไฟล์ Unity assets (`.assets`) โดยตรง
**ไม่ต้องการ:** UABEA, UnityPy, GameAssembly.dll, global-metadata.dat
**รองรับทุก platform:** PC, PS4, Switch ฯลฯ ใช้ได้กับเกม IL2CPP ที่มี stripped type tree

---

### วิธีใช้งาน

#### อ่านจากไฟล์ .dat (UABEA RAW export)

```bash
python i2_localization.py I2Languages.dat --stats
python i2_localization.py I2Languages.dat --export-json terms.json
```

#### อ่านจากไฟล์ .assets โดยตรง (ไม่ต้องผ่าน UABEA)

**PC:**
```bash
python i2_localization.py "<GameName>_Data/resources.assets" --export-json terms.json
```

**PS4:**
```bash
python i2_localization.py "Image0/Media_Data/resources.assets" --export-json terms.json
```

ถ้ารู้ pathID ของ I2Languages ให้ระบุตรงๆ เพื่อความเร็ว:
```bash
python i2_localization.py resources.assets --path-id 27659 --export-json terms.json
```

---

### คำสั่งทั้งหมด / All Commands

| คำสั่ง | รายละเอียด |
|--------|------------|
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
# ดูสถิติ
python i2_localization.py resources.assets --stats

# ค้นหาคำ
python i2_localization.py resources.assets --find "Crusade" --lang en

# แปลใหม่แล้ว import กลับ
python i2_localization.py I2Languages.dat --import-json my_thai.json --output patched.dat
```

---

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

---

### Python API

```python
from i2_localization import parse_dat, export_json, import_json, find_terms

# จาก UABEA RAW export
terms, languages = parse_dat("I2Languages.dat")

# จาก Unity assets file โดยตรง (PC หรือ PS4)
terms, languages = parse_dat("resources.assets")
terms, languages = parse_dat("resources.assets", path_id=27659)

export_json(terms, languages, "out.json")

# ค้นหา
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
|--------|---------|
| `python addressables_catalog.py catalog.json` | แสดง summary |
| `python addressables_catalog.py catalog.json --fonts` | แสดง font ทั้งหมด |
| `python addressables_catalog.py catalog.json <pattern>` | ค้นหาด้วย regex |
| `python addressables_catalog.py catalog.json --patch-crc out.json` | Patch CRC แล้วบันทึก |

```bash
# แสดง font ทั้งหมดพร้อม bundle ที่อยู่
python addressables_catalog.py catalog.json --fonts --output result/fonts.txt

# ค้นหาไฟล์ .otf และ .ttf ทั้งหมด
python addressables_catalog.py catalog.json "\.otf|\.ttf" --output result/fonts.txt

# Patch CRC หลังแก้ไข bundle
python addressables_catalog.py catalog.json --patch-crc catalog_patched.json
```

**ตัวอย่าง output:**
```
[Assets/Resources_moved/Fonts/Headings/6092-Reg.otf]  →  000f31824b70d0c577402a06d3c2cb8c.bundle
[Assets/Resources_moved/Fonts/Body/NotoSans.ttf]       →  0a1d5db632cad408c6acb9f588cfc39c.bundle
```

### Python API

```python
from addressables_catalog import (
    read_catalog, patch_crc, find_font_resources,
    find_resources, get_bundle_for_location, write_catalog_json
)

cat = read_catalog("catalog.json")   # รองรับ .json / .bin / .bundle

fonts = find_font_resources(cat)
for loc in fonts:
    bundle = get_bundle_for_location(loc)
    print(f"{loc.primary_key}  →  {bundle}")

results = find_resources(cat, r"\.otf|\.ttf")

n = patch_crc(cat)
write_catalog_json(cat, "catalog_patched.json")
print(f"Patched {n} bundle CRC(s)")
```

---

## เครดิต / Credits

### โปรเจกต์ต้นแบบ / Source Projects

| โปรเจกต์ | ผู้สร้าง | การใช้งาน |
|----------|----------|-----------|
| [Unity_Font_Replacer](https://github.com/snowyegret23/Unity_Font_Replacer) | snowyegret23 | ต้นฉบับของ fork นี้ / Original project this is forked from |
| [AddressablesTools](https://github.com/nesrak1/AddressablesTools) | nesrak1 | C# library ต้นแบบของ `addressables_catalog.py` |

### เครื่องมือภายนอก / External Tools

| เครื่องมือ | ผู้สร้าง | การใช้งาน |
|-----------|----------|-----------|
| [Il2CppDumper](https://github.com/Perfare/Il2CppDumper) | Perfare | สร้าง dummy DLL จาก IL2CPP games (PC) |
| [Console-Swizzler](https://github.com/nicoco007/Console-Swizzler) | nicoco007 | อ้างอิง PS4 BC swizzle algorithm (Morton 8×8 order) |
| [GFD-Studio](https://github.com/Joschuka/GFD-Studio) | Joschuka | อ้างอิง PS4 BC swizzle algorithm (Morton 8×8 order) |

### Python Libraries

| Library | ลิงก์ | การใช้งาน |
|---------|-------|-----------|
| [UnityPy](https://github.com/K0lb3/UnityPy) | K0lb3 | อ่าน/เขียน Unity assets files |
| [TypeTreeGeneratorAPI](https://github.com/nicoco007/TypeTreeGeneratorAPI) | nicoco007 | สร้าง type tree จาก IL2CPP สำหรับ SDF parsing |
| [Pillow](https://github.com/python-pillow/Pillow) | python-pillow | Image processing สำหรับ SDF atlas |
| [scipy](https://github.com/scipy/scipy) | SciPy team | Euclidean Distance Transform สำหรับ SDF generation |
| [fontTools](https://github.com/fonttools/fonttools) | fonttools | อ่านข้อมูล TTF font (glyph metrics, charset) |
| [texture2ddecoder](https://github.com/K0lb3/texture2ddecoder) | K0lb3 | Decode compressed texture formats (optional) |
| [numpy](https://github.com/numpy/numpy) | NumPy team | Array operations สำหรับ SDF pipeline (optional) |

---

## สัญญาอนุญาต / License

ดู [LICENSE](LICENSE) — เหมือนกับโปรเจกต์ต้นฉบับ / Same as the original project.
