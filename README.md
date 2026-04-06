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

## Addressables Catalog

`addressables_catalog.py` — Python port ของ [nesrak1/AddressablesTools](https://github.com/nesrak1/AddressablesTools)

อ่านและแก้ไข Unity Addressables catalog files (`catalog.json`, `catalog.bin`, `catalog.bundle`)

### ตัวอย่างการใช้งาน / Examples

**ดู summary ของ catalog:**
```bash
python addressables_catalog.py catalog.json
```

**ค้นหา asset:**
```bash
python addressables_catalog.py catalog.json font
```

**Patch CRC** (จำเป็นหลังแก้ไข bundle เพื่อป้องกัน integrity check พัง):
```bash
python addressables_catalog.py catalog.json --patch-crc catalog_patched.json
```

**ใช้ใน Python script:**
```python
from addressables_catalog import read_catalog, patch_crc, find_font_resources, write_catalog_json

cat = read_catalog("catalog.json")   # รองรับ .json / .bin / .bundle

fonts = find_font_resources(cat)
for loc in fonts:
    print(loc.primary_key, "→", loc.internal_id)

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
