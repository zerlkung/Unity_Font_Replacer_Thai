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
- [วิธีใช้งาน — PC](#วิธีใช้งาน--pc-usage)
- [วิธีใช้งาน — PS4](#วิธีใช้งาน--ps4-usage)
- [ตัวเลือก / Options](#ตัวเลือก--options)
- [I2Localization Parser](#i2localization-parser)
- [Addressables Catalog](#addressables-catalog)
- [เครดิต / Credits](#เครดิต--credits)

---

## รองรับ Platform

| Platform | --parse (สแกนฟอนต์) | เปลี่ยนฟอนต์ | หมายเหตุ |
|----------|---------------------|--------------|----------|
| **PC (IL2CPP)** | TTF + SDF | TTF + SDF | ต้องการ Il2CppDumper + TypeTreeGeneratorAPI |
| **PC (Mono)** | TTF + SDF | TTF + SDF | ใช้ `Managed/` folder โดยตรง |
| **PS4** | TTF + SDF (built-in trees) | TTF + SDF ✅ | ใช้ `--ps4-swizzle` สำหรับ SDF atlas |
| **PS5** | TTF + SDF | TTF + SDF | ใช้ `--ps5-swizzle` |

> **PS4 — SDF/TMP:** Il2CppDumper ไม่รองรับ PS4 ELF binary → ไม่มี custom type tree
> อย่างไรก็ตาม Unity built-in type trees ยังพบ SDF fonts ได้ในหลายเกม
> เมื่อใช้ `--list` + `--ps4-swizzle` สามารถแทนที่ทั้ง TTF และ SDF atlas ได้สมบูรณ์
>
> **Smart scan:** catalog-based scan (`.assets` root + font bundles จาก `catalog.json`)
> เปิดใช้งานอัตโนมัติ ไม่ต้องตั้งค่าพิเศษ (เร็วกว่า ~750× ลดจาก ~15,000 ไฟล์ เหลือ ~20 ไฟล์)

---

## ความต้องการของระบบ / Requirements

- **Python** 3.12 ขึ้นไป
- **OS:** Windows (Linux/macOS รองรับบางส่วน)

### โครงสร้างไฟล์ตาม Platform / File Structure by Platform

**PC (IL2CPP):**
```
<game_root>/
  GameAssembly.dll
  <GameName>_Data/
    il2cpp_data/
      Metadata/
        global-metadata.dat
```

**PC (Mono):**
```
<game_root>/
  <GameName>_Data/
    Managed/              ← DLL folder (ตรวจพบอัตโนมัติ)
```

**PS4:**
```
Image0/
  eboot.bin               ← PS4 executable (ตรวจพบอัตโนมัติ)
  Media/                  ← หรือ Media_Data/ — ตรวจพบอัตโนมัติ
    Metadata/
      global-metadata.dat
    StreamingAssets/
      aa/
        catalog.json      ← Addressables catalog (smart scan)
```

> **Auto-detection:** `--gamepath` รับได้ทั้ง game root, `_Data` folder, หรือ `Media` folder
> ระบบจะหา data folder ให้อัตโนมัติ

---

## การติดตั้ง / Installation

### 1. Clone โปรเจกต์

```bash
git clone https://github.com/zerlkung/Unity_Font_Replacer_Thai.git
cd Unity_Font_Replacer_Thai
```

### 2. ติดตั้ง Python packages

**PC (ต้องการ SDF/TMP support เต็มรูปแบบ):**
```bash
pip install UnityPy TypeTreeGeneratorAPI Pillow scipy
```

**PS4 หรือ PC (Mono):**
```bash
pip install UnityPy Pillow scipy
```

> `TypeTreeGeneratorAPI` จำเป็นสำหรับ SDF/TMP บนเกม IL2CPP (PC)
> ถ้าไม่ติดตั้ง ยังใช้งานได้แต่จะสแกนได้เฉพาะ TTF และ SDF ที่มี built-in type tree

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

### 3. สร้าง SDF atlas (สำหรับเกมที่ใช้ TextMeshPro)

```bash
python make_sdf.py --ttf TH_ASSETS/Sarabun.ttf
```

ย้ายไฟล์ output ไปไว้ใน `TH_ASSETS/`:

```
TH_ASSETS/
  Sarabun SDF.json
  Sarabun SDF Atlas.png
  Sarabun SDF Material.json
```

> PS4 ต้องการ SDF atlas ด้วย เพราะใช้ `--list` + `--ps4-swizzle` แทนที่ทั้ง TTF และ SDF

---

## วิธีใช้งาน — PC / PC Usage

### ขั้นตอนที่ 1 — สแกนฟอนต์

```bash
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --parse
```

สร้าง `<game_name>.json` รายชื่อฟอนต์ทั้งหมดในเกม

### ขั้นตอนที่ 2 — เปลี่ยนฟอนต์

**เปลี่ยนทั้งหมดในครั้งเดียว (Bulk replace):**

```bash
# Sarabun (TTF + SDF)
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --sarabun

# Noto Sans Thai (TTF + SDF)
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --notosansthai
```

**เปลี่ยนเฉพาะบางประเภท:**

```bash
# เฉพาะ SDF/TMP fonts
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --sarabun --sdfonly

# เฉพาะ TTF fonts
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --sarabun --ttfonly
```

**เปลี่ยนรายตัวผ่าน JSON:**

แก้ไข JSON จาก `--parse` ระบุ `Replace_to` สำหรับฟอนต์ที่ต้องการ แล้วรัน:

```bash
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --list font_map.json
```

**บันทึกเฉพาะไฟล์ที่เปลี่ยน (ไม่แตะไฟล์เดิม):**

```bash
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --sarabun --output-only output/
```

---

## วิธีใช้งาน — PS4 / PS4 Usage

PS4 มีขั้นตอนแตกต่างจาก PC เนื่องจาก Il2CppDumper ไม่รองรับ PS4 ELF binary
จึงใช้ `--parse` → `--list` เป็นหลัก และเพิ่ม `--ps4-swizzle` สำหรับ SDF atlas

> PS4 stores Alpha8 SDF textures in Morton 8×8 pixel-swizzled format
> `--ps4-swizzle` จะ swizzle atlas ที่เราสร้างขึ้นให้ถูก format ก่อนบันทึก

### ขั้นตอนที่ 1 — สแกนฟอนต์

```bash
python unity_font_replacer_th.py --gamepath "C:/path/to/Image0" --parse
```

หรือสแกนเฉพาะ bundle ที่ต้องการ:

```bash
python unity_font_replacer_th.py --gamepath "C:/path/to/Image0" --parse --target-file "abc123.bundle"
```

> **หมายเหตุ:** `--parse` จะพบ TTF fonts เสมอ
> SDF fonts จะพบได้ถ้าเกมใช้ Unity built-in type trees (พบได้ในหลายเกม)
> ถ้าไม่พบ SDF ให้ดู path_id จาก PC version ของเกมแล้ว ใส่ `Replace_to` ใน JSON ด้วยตนเอง

### ขั้นตอนที่ 2 — เตรียม JSON mapping

แก้ไข JSON ที่ได้ เพิ่ม `Replace_to` สำหรับฟอนต์ที่ต้องการ:

```json
{
  "bundle.bundle|CAB-xxx|FiraSans-Regular|TTF|-707454088402410502": {
    "File": "bundle.bundle",
    "assets_name": "CAB-xxx",
    "Path_ID": -707454088402410502,
    "Type": "TTF",
    "Name": "FiraSans-Regular",
    "Replace_to": "Sarabun.ttf"
  },
  "bundle.bundle|CAB-xxx|FiraSans-Regular SDF|SDF|7230518206509157603": {
    "File": "bundle.bundle",
    "assets_name": "CAB-xxx",
    "Path_ID": 7230518206509157603,
    "Type": "SDF",
    "Name": "FiraSans-Regular SDF",
    "Replace_to": "Sarabun SDF.json"
  }
}
```

> **`Replace_to`:** ระบุแค่ชื่อฟอนต์ เช่น `"Sarabun.ttf"` หรือ `"Sarabun SDF.json"`
> ไม่ต้องใส่ path เต็ม — tool จะค้นหาใน `TH_ASSETS/` ให้อัตโนมัติ

### ขั้นตอนที่ 3 — เปลี่ยนฟอนต์

**TTF + SDF พร้อม PS4 swizzle:**

```bash
python unity_font_replacer_th.py \
  --gamepath "C:/path/to/Image0" \
  --list font_map.json \
  --ps4-swizzle \
  --output-only output/
```

**สแกนเฉพาะ bundle ที่ต้องการ:**

```bash
python unity_font_replacer_th.py \
  --gamepath "C:/path/to/Image0" \
  --list font_map.json \
  --target-file "abc123.bundle" \
  --ps4-swizzle \
  --output-only output/
```

### ขั้นตอนที่ 4 — Copy ไฟล์กลับเข้าเกม

```
output/
  abc123.bundle   ← copy ไฟล์นี้กลับเข้า Image0/Media/
```

> ไฟล์อื่นใน `output/` (เช่น `globalgamemanagers`) คือ dependency ที่ tool ใช้ระหว่าง process
> **ไม่ต้อง** copy กลับเข้าเกม — copy เฉพาะ `.bundle` ที่ถูกแก้ไข

---

### สรุปขั้นตอน PS4 / PS4 Quick Reference

```
1. สแกน:    --parse
2. แก้ JSON: ระบุ Replace_to สำหรับ TTF → "Sarabun.ttf"
                              SDF → "Sarabun SDF.json"
3. เปลี่ยน:  --list font_map.json --ps4-swizzle --output-only output/
4. Copy:    output/<bundle>.bundle → Image0/Media/
```

---

## ตัวเลือก / Options

| Flag | รายละเอียด | PC | PS4 |
|------|------------|:--:|:---:|
| `--gamepath <path>` | Path ของโฟลเดอร์เกม (รับ game root / _Data / Media / Image0) | ✓ | ✓ |
| `--parse` | สแกนฟอนต์ → บันทึก JSON (smart scan อัตโนมัติ) | ✓ | ✓ |
| `--sarabun` | เปลี่ยนทุกฟอนต์เป็น Sarabun | ✓ | TTF only |
| `--notosansthai` | เปลี่ยนทุกฟอนต์เป็น Noto Sans Thai | ✓ | TTF only |
| `--list <file>` | เปลี่ยนตาม JSON mapping (รองรับทั้ง TTF และ SDF) | ✓ | ✓ |
| `--sdfonly` | เปลี่ยนเฉพาะ SDF/TMP | ✓ | ✓ |
| `--ttfonly` | เปลี่ยนเฉพาะ TTF | ✓ | ✓ |
| `--ps5-swizzle` | เปิด PS5 texture swizzle auto-detect/transform | ✓ | ✗ |
| `--ps4-swizzle` | เปิด PS4 Morton 8×8 pixel swizzle สำหรับ SDF atlas | ✗ | ✓ |
| `--target-file <name>` | สแกน/เปลี่ยนเฉพาะไฟล์ที่ระบุ (คั่นด้วย `,` สำหรับหลายไฟล์) | ✓ | ✓ |
| `--output-only <dir>` | บันทึกเฉพาะไฟล์ที่เปลี่ยนแปลงไปยัง folder ที่ระบุ | ✓ | ✓ |
| `--preview-export` | Export Atlas/Glyph preview PNG ก่อนเปลี่ยน | ✓ | - |
| `--scan-jobs <n>` | จำนวน parallel scan worker (default: 1) | ✓ | ✓ |
| `--force-raster` | บังคับ SDF → Raster mode | ✓ | - |
| `--verbose` | บันทึก DEBUG log ละเอียดไปยัง `verbose.txt` | ✓ | ✓ |

> **Smart scan (ค่าเริ่มต้น):** `--parse` สแกนเฉพาะ `.assets` ใน data root และ `.bundle` ที่ระบุใน `catalog.json`
> เร็วกว่าการ scan ทุกไฟล์อย่างมาก (~15,000 ไฟล์ → ~20 ไฟล์)

---

## I2Localization Parser

`i2_localization.py` — อ่านและแก้ไขไฟล์ localization จาก [I2Localization](https://inter-illusion.com/assets/I2-Localization)

รองรับทั้ง UABEA RAW export (`.dat`) และไฟล์ Unity assets (`.assets`) โดยตรง
**ไม่ต้องการ:** UABEA, UnityPy, GameAssembly.dll, global-metadata.dat
**รองรับทุก platform:** PC, PS4, Switch และอื่นๆ รวมถึงเกม IL2CPP ที่มี stripped type tree

### วิธีใช้งาน

**อ่านจาก UABEA RAW export (.dat):**

```bash
python i2_localization.py I2Languages.dat --stats
python i2_localization.py I2Languages.dat --export-json terms.json
```

**อ่านจากไฟล์ .assets โดยตรง:**

```bash
# PC
python i2_localization.py "<GameName>_Data/resources.assets" --export-json terms.json

# PS4
python i2_localization.py "Image0/Media/resources.assets" --export-json terms.json

# ระบุ pathID เพื่อความเร็ว
python i2_localization.py resources.assets --path-id 27659 --export-json terms.json
```

### คำสั่งทั้งหมด / All Commands

| คำสั่ง | รายละเอียด |
|--------|------------|
| `--stats` | แสดงสถิติจำนวน term และ translation ต่อภาษา |
| `--export-json <out>` | Export ทุก term พร้อม translation ทุกภาษาเป็น JSON |
| `--export-csv <out>` | Export เป็น CSV |
| `--import-json <in> --output <out>` | นำ JSON ที่แก้ไขแล้ว import กลับเป็น .dat |
| `--find <query>` | ค้นหา term จาก key หรือ translation |
| `--lang <code>` | กรองผลลัพธ์ --find ตาม language code เช่น `en`, `ko` |
| `--include-special` | รวม REFS/ และ FONTS/ metadata terms ใน export |
| `--include-comments` | รวม `__comments__` และ `__max_chars__` ใน JSON |
| `--path-id <id>` | (`.assets` เท่านั้น) ระบุ pathID ของ MonoBehaviour โดยตรง |

```bash
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

terms, languages = parse_dat("I2Languages.dat")
terms, languages = parse_dat("resources.assets", path_id=27659)

export_json(terms, languages, "out.json")
results = find_terms(terms, languages, "ability")
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
python addressables_catalog.py catalog.json "\.otf|\.ttf"

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

n = patch_crc(cat)
write_catalog_json(cat, "catalog_patched.json")
```

---

## เครดิต / Credits

### โปรเจกต์ต้นแบบ / Source Projects

| โปรเจกต์ | ผู้สร้าง | การใช้งาน |
|----------|----------|-----------|
| [Unity_Font_Replacer](https://github.com/snowyegret23/Unity_Font_Replacer) | snowyegret23 | ต้นฉบับของ fork นี้ |
| [AddressablesTools](https://github.com/nesrak1/AddressablesTools) | nesrak1 | C# library ต้นแบบของ `addressables_catalog.py` |

### เครื่องมือภายนอก / External Tools

| เครื่องมือ | ผู้สร้าง | การใช้งาน |
|-----------|----------|-----------|
| [Il2CppDumper](https://github.com/Perfare/Il2CppDumper) | Perfare | สร้าง dummy DLL จาก IL2CPP binary (PC) |
| [Console-Swizzler](https://github.com/matyamod/Console-Swizzler) | matyamod | อ้างอิง PS4 Morton 8×8 BC block swizzle algorithm |
| [GFD-Studio](https://github.com/tge-was-taken/GFD-Studio) | tge-was-taken | อ้างอิง PS4 Morton 8×8 BC block swizzle algorithm |
| [UABEA](https://github.com/nesrak1/UABEA) | nesrak1 | อ้างอิง PS4 uncompressed texture swizzle research ([#297](https://github.com/nesrak1/UABEA/issues/297)) |

### Python Libraries

| Library | ผู้สร้าง | การใช้งาน |
|---------|----------|-----------|
| [UnityPy](https://github.com/K0lb3/UnityPy) | K0lb3 | อ่าน/เขียน Unity assets และ bundle files |
| [TypeTreeGeneratorAPI](https://github.com/nicoco007/TypeTreeGeneratorAPI) | nicoco007 | สร้าง type tree จาก IL2CPP สำหรับ SDF/TMP parsing |
| [Pillow](https://github.com/python-pillow/Pillow) | python-pillow | Image processing สำหรับ SDF atlas |
| [scipy](https://github.com/scipy/scipy) | SciPy team | Euclidean Distance Transform สำหรับ SDF generation |
| [fontTools](https://github.com/fonttools/fonttools) | fonttools | อ่านข้อมูล TTF font (glyph metrics, charset) |
| [texture2ddecoder](https://github.com/K0lb3/texture2ddecoder) | K0lb3 | Decode compressed texture formats (optional) |
| [numpy](https://github.com/numpy/numpy) | NumPy team | Array operations สำหรับ SDF pipeline (optional) |

---

## สัญญาอนุญาต / License

ดู [LICENSE](LICENSE) — เหมือนกับโปรเจกต์ต้นฉบับ / Same as the original project.
