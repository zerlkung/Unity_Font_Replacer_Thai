[> for Korean version of README.md](README.md)

# Unity Font Replacer

A tool to replace Unity game fonts with Korean/custom fonts. Supports both TTF and TextMeshPro SDF fonts.

## Quick Start (EXE-first)

After extracting a release ZIP, the folder typically looks like this:

```
release_en/
├── unity_font_replacer_en.exe
├── export_fonts_en.exe
├── KR_ASSETS/
├── Il2CppDumper/
└── README_EN.md
```

`make_sdf.exe` and `ps5_swizzler.exe` are distributed as standalone ZIPs (`make_sdf_vX.Y.Z.zip`, `ps5_swizzler_vX.Y.Z.zip`).

Recommended run:

```bat
cd release_en
unity_font_replacer_en.exe
```

| Executable | Description |
|-----------|------|
| `unity_font_replacer.exe` | Font replacement tool (Korean UI) |
| `unity_font_replacer_en.exe` | Font replacement tool (English UI) |
| `export_fonts.exe` | TMP SDF font exporter (Korean UI) |
| `export_fonts_en.exe` | TMP SDF font exporter (English UI) |
| `make_sdf.exe` | TTF -> TMP SDF JSON/Atlas generator (standalone ZIP) |
| `ps5_swizzler.exe` | Standalone PS5 swizzle/unswizzle/detect tool (standalone ZIP) |

---

## Font Replacement (unity_font_replacer_en.exe)

### Basic Usage

```bat
:: Interactive mode (asks for game path)
unity_font_replacer_en.exe

:: Set game path + bulk replace with Mulmaru
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --mulmaru
```

### Command Line Options

#### General

| Option | Description |
|------|------|
| `--gamepath <path>` | Game root path or `_Data` folder path |
| `--parse` | Export font info to JSON |
| `--list <JSON>` | Replace fonts from a JSON mapping |
| `--verbose` | Save full logs to `verbose.txt` |

#### Replacement Targets

| Option | Description |
|------|------|
| `--mulmaru` | Bulk replace all fonts with Mulmaru |
| `--nanumgothic` | Bulk replace all fonts with NanumGothic |
| `--sdfonly` | Replace SDF fonts only |
| `--ttfonly` | Replace TTF fonts only |
| `--target-file <name>` | Limit replacement to specific file name(s) (repeatable/comma-separated) |

#### SDF Options

| Option | Description |
|------|------|
| `--use-game-material` | Keep original in-game Material parameters (default: apply replacement Material) |
| `--use-game-line-metrics` | Keep in-game line metrics (pointSize still follows replacement font) |

#### Save / Output

| Option | Description |
|------|------|
| `--original-compress` | Prefer original compression mode on save (default: uncompressed-family first) |
| `--temp-dir <path>` | Set root path for temporary save files (fast SSD/NVMe recommended) |
| `--output-only <path>` | Keep originals untouched; write modified files only to this folder (preserve relative paths) |
| `--split-save-force` | Skip one-shot and force one-by-one SDF split save |
| `--oneshot-save-force` | Force one-shot only (disable split-save fallback) |

#### PS5 / Scan

| Option | Description |
|------|------|
| `--ps5-swizzle` | PS5 atlas swizzle detect/transform (masks auto-computed per texture size, `rotate=90`) |
| `--preview-export` | Save SDF atlas + glyph crop PNGs into `preview/` (unswizzled view when used with `--ps5-swizzle`) |
| `--scan-jobs <N>` | Number of parallel scan workers (default: `1`) |

### Examples

**Basic replacement:**

```bat
:: Replace all fonts with Mulmaru
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --mulmaru

:: Replace SDF only with NanumGothic
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --nanumgothic --sdfonly

:: Replace using JSON mapping
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --list Muck.json
```

**Parsing / Scan:**

```bat
:: Export font info (creates Muck.json)
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --parse

:: Parallel workers + PS5 swizzle detection fields
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --parse --scan-jobs 10 --ps5-swizzle
```

**SDF options:**

```bat
:: Keep original in-game material parameters
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --nanumgothic --use-game-material

:: Keep in-game line metrics
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --nanumgothic --use-game-line-metrics
```

**Save / Output:**

```bat
:: Limit replacement to a specific file
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --nanumgothic --target-file "sharedassets0.assets"

:: Keep originals and write modified files to a separate folder
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --nanumgothic --output-only "D:\output"

:: Prefer original compression on save
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --nanumgothic --original-compress

:: Use a fast SSD/NVMe path for temporary save files
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --nanumgothic --temp-dir "E:\UFR_TEMP"
```

**PS5 preview:**

```bat
:: Export normal (PC) previews
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --preview-export --sdfonly

:: Export PS5 previews in unswizzled view
unity_font_replacer_en.exe --gamepath "D:\Games\Muck" --preview-export --ps5-swizzle --sdfonly
```

---

## Per-Font Replacement (--list)

1. Run `--parse` to generate font info JSON.
2. Fill `Replace_to` for entries you want to replace.
3. Run with `--list`.

JSON example (without `--ps5-swizzle`):

```json
{
    "sharedassets0.assets|sharedassets0.assets|Arial|TTF|123": {
        "File": "sharedassets0.assets",
        "assets_name": "sharedassets0.assets",
        "Path_ID": 123,
        "Type": "TTF",
        "Name": "Arial",
        "Replace_to": "Mulmaru"
    },
    "sharedassets0.assets|sharedassets0.assets|Arial SDF|SDF|456": {
        "File": "sharedassets0.assets",
        "assets_name": "sharedassets0.assets",
        "Path_ID": 456,
        "Type": "SDF",
        "Name": "Arial SDF",
        "Replace_to": ""
    }
}
```

### PS5 swizzle fields

If you run `--parse` with `--ps5-swizzle`, SDF entries include two additional fields:

| Field | Description |
|------|------|
| `swizzle` | Auto-detected target atlas state (`"True"` / `"False"`) |
| `process_swizzle` | Force replacement atlas into swizzled state (default `"False"`) |

- Old JSON files (without these keys) remain compatible.

JSON example (with `--ps5-swizzle`, SDF):

```json
{
    "sharedassets0.assets|sharedassets0.assets|Arial SDF|SDF|456": {
        "File": "sharedassets0.assets",
        "assets_name": "sharedassets0.assets",
        "Path_ID": 456,
        "Type": "SDF",
        "Name": "Arial SDF",
        "swizzle": "True",
        "process_swizzle": "False",
        "Replace_to": ""
    }
}
```

### Replace_to formats

If `Replace_to` is empty, that font is skipped.

| Input | Examples |
|------|------|
| Font name | `Mulmaru`, `NanumGothic` |
| TTF file | `Mulmaru.ttf` |
| SDF JSON | `Mulmaru SDF.json`, `Mulmaru Raster.json` |
| SDF Atlas | `Mulmaru SDF Atlas.png`, `Mulmaru Raster Atlas.png` |
| Material | `NGothic Material.json` |

---

## PS5 Validation Workflow (--preview-export)

To compare original vs replaced with the exact same extraction pipeline:

1. Create JSON for the target file only.
2. Run `--list + --ps5-swizzle + --preview-export` on original data (extract original crops).
3. Replace with `--nanumgothic --ps5-swizzle`.
4. Run `--list + --ps5-swizzle + --preview-export` again (extract replaced crops).
5. Compare the two preview outputs.

Example (single PS5 bundle target):

```bat
:: 1) Create JSON for a single target file
unity_font_replacer_en.exe --gamepath "C:\Game\Game_Data" ^
    --parse --target-file "38871756d6e98b9e67fb2e7a61dbb88e.bundle" --ps5-swizzle

:: 2) Extract original crops
unity_font_replacer_en.exe --gamepath "C:\Game\Game_Data" ^
    --list "Game.json" --target-file "38871756d6e98b9e67fb2e7a61dbb88e.bundle" ^
    --ps5-swizzle --preview-export --sdfonly

:: 3) Replace with NanumGothic (use --output-only to keep originals intact)
unity_font_replacer_en.exe --gamepath "C:\Game\Game_Data" ^
    --nanumgothic --sdfonly --target-file "38871756d6e98b9e67fb2e7a61dbb88e.bundle" ^
    --ps5-swizzle --output-only "D:\output"

:: 4) Extract replaced crops
unity_font_replacer_en.exe --gamepath "C:\Game\Game_Data" ^
    --list "Game.json" --target-file "38871756d6e98b9e67fb2e7a61dbb88e.bundle" ^
    --ps5-swizzle --preview-export --sdfonly
```

Output locations:

| Type | Path |
|------|------|
| Atlas preview | `preview/<file>/<assets_name>__<atlas_pathid>__<font>__unswizzled__*.png` |
| Glyph crops | `preview/<file>/<assets_name>__<atlas_pathid>__<font>/U+XXXX*.png` |

---

## Font Export (export_fonts_en.exe)

Exports TMP SDF font assets.

```bat
:: Positional path argument (recommended)
export_fonts_en.exe "D:\MyGame"

:: You can also pass _Data directly
export_fonts_en.exe "D:\MyGame\MyGame_Data"

:: If omitted, it prompts for the game path
export_fonts_en.exe
```

Output files are created in the current working directory:

| Output file | Description |
|-------------|------|
| `FontAssetName.json` | TMP font data |
| `FontAssetName SDF Atlas.png` | SDF Atlas image |
| `Material_*.json` | Material data (if present) |

---

## Supported Fonts

| Font | Description |
|-----------|------|
| Mulmaru | Mulmaru Korean font |
| NanumGothic | NanumGothic Korean font |

## Adding Custom Fonts

Add these files under `KR_ASSETS`:

| File | Required |
|------|----------|
| `FontName.ttf` or `.otf` | Required |
| `FontName SDF.json` / `Raster.json` / `.json` | Required for SDF replacement |
| `FontName SDF Atlas.png` / `Raster Atlas.png` / `Atlas.png` | Required for SDF replacement |
| `FontName SDF Material.json` / `Raster Material.json` / `Material.json` | Optional |

If SDF data is missing, generate it first with `make_sdf.exe` below or extract it with `export_fonts_en.exe`.

---

## SDF Generator (make_sdf.exe)

You can generate TMP-compatible JSON/atlas directly from a TTF:

```bat
make_sdf.exe --ttf Mulmaru.ttf
```

| Option | Description | Default |
|--------|-------------|---------|
| `--ttf <ttfname>` | TTF file path/name | (required) |
| `--atlas-size <w,h>` | Atlas resolution | `4096,4096` |
| `--point-size <int or auto>` | Sampling point size | `auto` |
| `--padding <int>` | Atlas padding | `7` |
| `--charset <txtpath or characters>` | Charset file path or literal characters | `./CharList_3911.txt` |
| `--rendermode <sdf,raster>` | Output render mode | `sdf` |

---

## Run from Source (Optional)

If you prefer Python scripts instead of EXEs:

### Requirements

- Python 3.12 recommended
- Packages: `UnityPy (fork)`, `TypeTreeGeneratorAPI`, `Pillow`, `numpy`, `scipy`

```bash
pip install TypeTreeGeneratorAPI Pillow numpy scipy
pip install --upgrade git+https://github.com/snowyegret23/UnityPy.git
```

### Examples

```bash
python unity_font_replacer_en.py --gamepath "D:\Games\Muck" --mulmaru
python export_fonts_en.py "D:\MyGame"
```

---

## Notes

### Save

- Default save order prefers uncompressed-family modes (`safe-none -> legacy-none`), then falls back to `original -> lz4`.
- Use `--original-compress` to prefer original compression mode first.
- If save is slow, try `--temp-dir` and point it to a fast SSD/NVMe path.
- For large multi-SDF replacements, split-save fallback is enabled by default when one-shot fails (adaptive batch size).

### Scan

- `--parse` scans via per-file worker processes so a crash in one file does not terminate the whole scan.
- You can increase scan throughput with `--scan-jobs`.
- Scanning uses blacklist-based exclusion (`*.bak`, `.info`, `.config`, etc.).
- Use `--target-file` to restrict replacements to specific files.

### SDF Replacement

- Default line metrics mode scales original proportions to the replacement font's pointSize.
- Use `--use-game-line-metrics` to keep original in-game line metrics. (pointSize still follows replacement font.)
- Default behavior applies material floats from `KR_ASSETS/* SDF Material.json` with padding-ratio correction.
  Use `--use-game-material` to preserve original in-game material style.
- When a Raster asset is injected into an SDF slot, SDF material effect floats (outline/underlay/glow) are automatically neutralized to reduce box artifacts.

### Preview Export

- `--preview-export` writes SDF atlas previews and glyph crops into `preview/`.
- `--preview-export --ps5-swizzle` writes previews in unswizzled view.

### PS5 Swizzle

- `--ps5-swizzle` uses metadata-first detection (with raw-data fallback) to decide target SDF atlas swizzle state.
- Swizzle masks are auto-computed from texture dimensions (power-of-two).
- `swizzle` and `process_swizzle` are added to `--parse` JSON only when `--ps5-swizzle` is used.
- Set `process_swizzle: "True"` in JSON to force replacement atlas swizzle conversion regardless of auto detection.

### General

- `TypeTreeGeneratorAPI` is required for TMP(FontAsset) parsing/replacement.
- Interactive path input strips repeated wrapping quotes automatically.
- Back up game files before modification.
- Some games may restore modified files by integrity checks.
- Check Terms of Service before using in online games.

---

## Standalone PS5 Texture Tool (ps5_swizzler.py / ps5_swizzler.exe)

`ps5_swizzler.py` is a standalone CLI that mirrors the core PS5 swizzle/unswizzle conversion logic
used in `unity_font_replacer_core.py`. The name is unified to `swizzler` because it supports
swizzle, unswizzle, and detect modes in one command.

Supported conversion paths:

- BIN -> BIN
- BIN -> PNG
- PNG -> BIN
- PNG -> PNG

Main modes:

- `--mode detect`: heuristically detect whether input is swizzled or linear
- `--mode unswizzle`: swizzled -> linear
- `--mode swizzle`: linear -> swizzled

### Key CLI options

| Option | Description |
|------|------|
| `--mode {unswizzle,swizzle,detect}` | Processing mode (default: `unswizzle`) |
| `--input <path>` | Input file path (`.bin` or `.png`) |
| `--input-format {auto,bin,png}` | Force input format (default: `auto`) |
| `--width`, `--height` | Texture dimensions for BIN input |
| `--bytes-per-element <N>` | Bytes per pixel element (e.g. Alpha8=1) |
| `--mask-x`, `--mask-y` | Manual swizzle masks (default: auto from dimensions) |
| `--axis-swap {auto,off}` | For non-square unswizzle, compare `(w,h)` and `(h,w)` candidates and keep the more coherent result |
| `--output-bin <path>` | Binary output path |
| `--output-png <path>` | PNG output path |
| `--skip-bin`, `--skip-png` | Skip writing selected output type |
| `--rotate {0,90,180,270}` | Output image rotation |
| `--hflip`, `--vflip` | Output image horizontal/vertical flip |

### Conversion examples (4-way)

```bat
:: 1) BIN -> BIN (unswizzle)
ps5_swizzler.exe --mode unswizzle --input atlas_swizzled.bin ^
    --width 2048 --height 2048 --bytes-per-element 1 ^
    --output-bin atlas_linear.bin --skip-png

:: 2) BIN -> PNG (unswizzle)
ps5_swizzler.exe --mode unswizzle --input atlas_swizzled.bin ^
    --width 2048 --height 2048 --bytes-per-element 1 ^
    --output-png atlas_linear.png --skip-bin

:: 3) PNG -> BIN (swizzle)
ps5_swizzler.exe --mode swizzle --input atlas_linear.png --input-format png ^
    --bytes-per-element 1 --output-bin atlas_swizzled.bin --skip-png

:: 4) PNG -> PNG (swizzle preview)
ps5_swizzler.exe --mode swizzle --input atlas_linear.png --input-format png ^
    --bytes-per-element 1 --output-png atlas_swizzled.png --skip-bin
```

Additional detect example:

```bat
ps5_swizzler.exe --mode detect --input atlas.bin --width 2048 --height 2048 --bytes-per-element 1
```

Run from Python source:

```bash
python ps5_swizzler.py --mode detect --input atlas.bin --width 2048 --height 2048 --bytes-per-element 1
```

Note:

- `--axis-swap auto` automatically handles blocky/axis-mismatch artifacts seen on some non-square PS5 atlases.

---

## Special Thanks

- [UnityPy](https://github.com/K0lb3/UnityPy) by K0lb3
- [Il2CppDumper](https://github.com/Perfare/Il2CppDumper) by Perfare
- [NanumGothic](https://hangeul.naver.com/font) by NAVER | [License](https://help.naver.com/service/30016/contents/18088?osType=PC&lang=ko)
- [Mulmaru](https://github.com/mushsooni/mulmaru) by mushsooni | [License](https://github.com/mushsooni/mulmaru/blob/main/LICENSE_ko)

## License

MIT License
