# Unity Font Replacer — Thai Edition

Fork จาก [snowyegret23/Unity_Font_Replacer](https://github.com/snowyegret23/Unity_Font_Replacer) ดัดแปลงเพื่อรองรับ **การเปลี่ยนฟอนต์ภาษาไทย** ในเกม Unity

เปลี่ยนฟอนต์ในไฟล์ asset ของเกม Unity ที่คอมไพล์แล้วเป็นฟอนต์ภาษาไทย (Sarabun, Noto Sans Thai หรือ TTF ไทยอื่นๆ) — โดยไม่ต้องมี source code ของเกม

---

## เครดิต

ต้นฉบับโดย **snowyegret23**
→ https://github.com/snowyegret23/Unity_Font_Replacer

Fork นี้เพิ่มการรองรับภาษาไทย:
- โหมด `--sarabun` / `--notosansthai` สำหรับเปลี่ยนฟอนต์แบบเหมารวม
- `CharList_Thai.txt` — ชุดอักขระภาษาไทย (U+0E00–U+0E7F + ASCII)
- โฟลเดอร์ `TH_ASSETS/` — สำหรับวางไฟล์ฟอนต์ไทย
- โหลด asset จาก `TH_ASSETS/` ก่อน `KR_ASSETS/`
- ปรับ default charset ของ `make_sdf.py` เป็น `CharList_Thai.txt`

---

## ความต้องการของระบบ

- Python 3.12 ขึ้นไป
- `pip install UnityPy TypeTreeGeneratorAPI Pillow scipy`

---

## เตรียมฟอนต์ไทย

วางไฟล์ฟอนต์ไทยไว้ในโฟลเดอร์ `TH_ASSETS/`:

```
TH_ASSETS/
  Sarabun.ttf
  NotoSansThai.ttf
  Sarabun SDF.json          ← สร้างโดย make_sdf.py
  Sarabun SDF Atlas.png     ← สร้างโดย make_sdf.py
  Sarabun SDF Material.json ← สร้างโดย make_sdf.py
```

ฟอนต์ไทยที่แนะนำ (ฟรี):
- [Sarabun](https://fonts.google.com/specimen/Sarabun) — Google Fonts
- [Noto Sans Thai](https://fonts.google.com/noto/specimen/Noto+Sans+Thai) — Google Fonts

---

## วิธีใช้งาน

### 1. สร้าง SDF atlas จาก TTF (สำหรับเกมที่ใช้ TextMeshPro)

```bash
python make_sdf.py --ttf TH_ASSETS/Sarabun.ttf
```

ไฟล์ output (`Sarabun SDF.json`, `Sarabun SDF Atlas.png`, `Sarabun SDF Material.json`) จะถูกสร้างในโฟลเดอร์ปัจจุบัน — ย้ายไปไว้ใน `TH_ASSETS/`

### 2. สแกนฟอนต์ในเกม (สร้าง JSON map)

```bash
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --parse
```

### 3. เปลี่ยนฟอนต์ทั้งหมดเป็น Sarabun

```bash
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --sarabun
```

### 4. เปลี่ยนฟอนต์ทั้งหมดเป็น Noto Sans Thai

```bash
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --notosansthai
```

### 5. เปลี่ยนฟอนต์รายตัวด้วย JSON map

แก้ไข JSON ที่ได้จาก `--parse` เพื่อกำหนดว่าจะเปลี่ยนฟอนต์ไหนเป็นอะไร แล้วรัน:

```bash
python unity_font_replacer_th.py --gamepath "C:/path/to/game" --list font_map.json
```

### ตัวเลือกทั้งหมด

| Flag | คำอธิบาย |
|---|---|
| `--sarabun` | เปลี่ยนฟอนต์ทั้งหมดเป็น Sarabun |
| `--notosansthai` | เปลี่ยนฟอนต์ทั้งหมดเป็น Noto Sans Thai |
| `--sdfonly` | เปลี่ยนเฉพาะ SDF (TextMeshPro) |
| `--ttfonly` | เปลี่ยนเฉพาะ TTF |
| `--parse` | ส่งออกข้อมูลฟอนต์เป็น JSON |
| `--list <file>` | เปลี่ยนฟอนต์ตาม JSON mapping file |
| `--ps5-swizzle` | เปิดโหมด PS5 texture swizzle/unswizzle |
| `--preview-export` | ส่งออก preview PNG ก่อนเปลี่ยน |
| `--scan-jobs <n>` | จำนวน worker สำหรับสแกนแบบขนาน |

---

## ดึงฟอนต์ออกจากเกม

```bash
python export_fonts_th.py --gamepath "C:/path/to/game"
```

ดึง TMP SDF font assets (JSON + PNG atlas) ออกจากเกม

---

## โหมด Interactive

รันโดยไม่ใส่ flag เพื่อใช้เมนูแบบโต้ตอบ:

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

## สัญญาอนุญาต

ดู [LICENSE](LICENSE) — เหมือนกับโปรเจกต์ต้นฉบับ
