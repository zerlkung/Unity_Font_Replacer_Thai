[> for English version of README.md](README_EN.md)

# Unity Font Replacer

Unity 게임의 폰트를 한글 폰트로 교체하는 도구입니다. TTF 폰트와 TextMeshPro SDF 폰트를 모두 지원합니다.

## 빠른 시작 (EXE 기준)

릴리즈 ZIP을 풀면 보통 아래처럼 구성됩니다.

```
release/
├── unity_font_replacer_ko.exe
├── export_fonts_ko.exe
├── KR_ASSETS/
├── Il2CppDumper/
└── README.md
```

`make_sdf.exe`는 별도 단독 ZIP(`make_sdf_vX.Y.Z.zip`)으로 제공합니다.

권장 실행 방식:

```bat
cd release
unity_font_replacer_ko.exe
```

| 실행 파일 | 설명 |
|-----------|------|
| `unity_font_replacer_ko.exe` | 폰트 교체 도구 (한국어 UI) |
| `unity_font_replacer_en.exe` | 폰트 교체 도구 (영문 UI) |
| `export_fonts_ko.exe` | TMP SDF 폰트 추출 도구 (한국어 UI) |
| `export_fonts_en.exe` | TMP SDF 폰트 추출 도구 (영문 UI) |
| `make_sdf.exe` | TTF -> TMP SDF JSON/Atlas 생성 도구 (별도 단독 ZIP) |

---

## 폰트 교체 (unity_font_replacer_ko.exe)

### 기본 사용법

```bat
:: 대화형 모드 (게임 경로 입력)
unity_font_replacer_ko.exe

:: 게임 경로 지정 + Mulmaru 일괄 교체
unity_font_replacer_ko.exe --gamepath "C:/path/to/game" --mulmaru
```

### 명령줄 옵션

#### 기본

| 옵션 | 설명 |
|------|------|
| `--gamepath <경로>` | 게임 루트 경로 또는 `_Data` 폴더 경로 |
| `--parse` | 게임 폰트 정보를 JSON 파일로 출력 |
| `--list <JSON파일>` | JSON 파일 기준 개별 폰트 교체 |
| `--verbose` | 콘솔은 현재 수준으로 유지하고, `verbose.txt`에 상세 DEBUG 로그(경로/Unity 버전/파일별/폰트별)를 저장 |

- `--verbose`를 사용하면 실행 파일(또는 스크립트) 기준 폴더에 `verbose.txt`가 생성되며, 타임스탬프/로그 레벨이 포함된 상세 추적 로그가 기록됩니다.

#### 교체 대상

| 옵션 | 설명 |
|------|------|
| `--mulmaru` | 모든 폰트를 Mulmaru로 일괄 교체 |
| `--nanumgothic` | 모든 폰트를 NanumGothic으로 일괄 교체 |
| `--sdfonly` | SDF 폰트만 교체 |
| `--ttfonly` | TTF 폰트만 교체 |
| `--target-file <파일명>` | 지정한 파일명만 교체 대상에 포함 (여러 번/콤마로 지정 가능) |

#### SDF 교체 옵션

| 옵션 | 설명 |
|------|------|
| `--use-game-material` | 게임 원본 Material 파라미터 유지 (기본: 교체 Material 보정 적용) |
| `--force-raster` | SDF 교체를 Raster 기준으로 강제 (렌더 모드 + Material 효과값 Raster 보정) |
| `--use-game-line-metrics` | 게임 원본 줄 간격 메트릭 사용 (pointSize는 교체값 유지) |

#### 저장

| 옵션 | 설명 |
|------|------|
| `--original-compress` | 저장 시 원본 압축 모드를 우선 사용 (기본: 무압축 계열 우선) |
| `--temp-dir <경로>` | 임시 저장 폴더 루트 경로 지정 (빠른 SSD/NVMe 권장) |
| `--output-only <경로>` | 원본은 유지하고, 수정된 파일만 지정 폴더에 저장 (상대 경로 유지) |
| `--split-save-force` | one-shot을 건너뛰고 SDF 1개씩 강제 분할 저장 |
| `--oneshot-save-force` | 분할 저장 폴백 없이 one-shot만 시도 |

#### PS5 / 스캔

| 옵션 | 설명 |
|------|------|
| `--ps5-swizzle` | PS5 Atlas swizzle 자동 판별/변환 (텍스쳐 크기별 mask 자동 계산, `rotate=90`) |
| `--preview-export` | `preview/`에 SDF Atlas + 글리프 crop PNG 저장 (`--ps5-swizzle`와 함께 사용 시 unswizzle 기준) |
| `--scan-jobs <N>`, `--max-workers <N>` | 폰트 스캔 병렬 워커 수 (기본: `1`) |

### 사용 예시

**기본 교체:**

```bat
:: Mulmaru로 전체 교체
unity_font_replacer_ko.exe --gamepath "C:/path/to/game" --mulmaru

:: NanumGothic으로 SDF만 교체
unity_font_replacer_ko.exe --gamepath "C:/path/to/game" --nanumgothic --sdfonly

:: JSON 기반 개별 교체
unity_font_replacer_ko.exe --gamepath "C:/path/to/game" --list font_map.json
```

**파싱 / 스캔:**

```bat
:: 폰트 정보 파싱 (font_map.json 생성)
unity_font_replacer_ko.exe --gamepath "C:/path/to/game" --parse

:: 병렬 워커 + PS5 swizzle 판별 필드 포함 (alias: --max-workers)
unity_font_replacer_ko.exe --gamepath "C:/path/to/game" --parse --max-workers 10 --ps5-swizzle
```

**SDF 옵션:**

```bat
:: 게임 원본 Material 파라미터 유지
unity_font_replacer_ko.exe --gamepath "C:/path/to/game" --nanumgothic --use-game-material

:: 게임 원본 줄 간격 메트릭 유지
unity_font_replacer_ko.exe --gamepath "C:/path/to/game" --nanumgothic --use-game-line-metrics

:: SDF 교체를 Raster 기준으로 강제
unity_font_replacer_ko.exe --gamepath "C:/path/to/game" --nanumgothic --force-raster
```

**저장 / 출력:**

```bat
:: 특정 파일만 대상으로 교체
unity_font_replacer_ko.exe --gamepath "C:/path/to/game" --nanumgothic --target-file "sharedassets0.assets"

:: 원본 유지 + 수정 파일만 별도 폴더 출력
unity_font_replacer_ko.exe --gamepath "C:/path/to/game" --nanumgothic --output-only "D:\output"

:: 저장 시 원본 압축 우선
unity_font_replacer_ko.exe --gamepath "C:/path/to/game" --nanumgothic --original-compress

:: 임시 저장 폴더를 빠른 SSD/NVMe 경로로 지정
unity_font_replacer_ko.exe --gamepath "C:/path/to/game" --nanumgothic --temp-dir "E:\UFR_TEMP"
```

**PS5 미리보기:**

```bat
:: 일반(PC) 미리보기 export
unity_font_replacer_ko.exe --gamepath "C:/path/to/game" --preview-export --sdfonly

:: PS5 unswizzle 기준 미리보기 export
unity_font_replacer_ko.exe --gamepath "C:/path/to/game" --preview-export --ps5-swizzle --sdfonly
```

---

## 개별 폰트 교체 (--list)

1. `--parse`로 폰트 정보 JSON 생성
2. JSON의 `Replace_to` 필드에 원하는 폰트 이름 입력
3. `--list`로 교체 실행

JSON 예시 (`--ps5-swizzle` 미사용):

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
        "force_raster": "False",
        "Replace_to": ""
    }
}
```

### force_raster 필드

`--parse` JSON에서는 **SDF 항목에만** `force_raster` 필드가 포함되며 기본값은 `"False"`입니다.

| 필드 | 설명 |
|------|------|
| `force_raster` | 해당 항목만 Raster 방식으로 강제 처리 (`"True"` / `"False"`, 기본 `"False"`) |

- `force_raster: "True"`이면 해당 SDF 교체 항목에서 렌더 모드를 Raster 기준으로 강제하고 Material 효과값 보정을 적용합니다.
- `--force-raster`를 함께 쓰면 JSON 값과 무관하게 모든 SDF 교체 항목에 Raster 강제가 적용됩니다.

### PS5 swizzle 필드

`--ps5-swizzle`를 함께 사용해 `--parse`하면, SDF 항목에 아래 2개 필드가 추가됩니다.

| 필드 | 설명 |
|------|------|
| `swizzle` | 원본 대상 Atlas의 자동 판별 결과 (`"True"` / `"False"`) |
| `process_swizzle` | 교체 Atlas를 swizzle 상태로 강제 변환할지 여부 (기본 `"False"`) |

- 구버전 JSON(해당 키 없음)도 그대로 호환됩니다.

JSON 예시 (`--ps5-swizzle` 사용, SDF):

```json
{
    "sharedassets0.assets|sharedassets0.assets|Arial SDF|SDF|456": {
        "File": "sharedassets0.assets",
        "assets_name": "sharedassets0.assets",
        "Path_ID": 456,
        "Type": "SDF",
        "Name": "Arial SDF",
        "force_raster": "False",
        "swizzle": "True",
        "process_swizzle": "False",
        "Replace_to": ""
    }
}
```

### Replace_to 지정 방법

`Replace_to`가 비어 있으면 해당 항목은 교체하지 않습니다.

| 입력 | 예시 |
|------|------|
| 폰트 이름 | `Mulmaru`, `NanumGothic` |
| TTF 파일 | `Mulmaru.ttf` |
| SDF JSON | `Mulmaru SDF.json`, `Mulmaru Raster.json` |
| SDF Atlas | `Mulmaru SDF Atlas.png`, `Mulmaru Raster Atlas.png` |
| Material | `NGothic Material.json` |

---

## PS5 검증 워크플로 (--preview-export)

원본/수정본을 같은 방법으로 비교하려면 아래 순서를 권장합니다.

1. 대상 파일만 스캔 JSON 생성
2. 원본 상태에서 `--list + --ps5-swizzle + --preview-export` 실행 (원본 crop 추출)
3. `--nanumgothic --ps5-swizzle`로 교체
4. 다시 `--list + --ps5-swizzle + --preview-export` 실행 (수정본 crop 추출)
5. 두 결과를 비교

예시 (PS5 번들 1개만 검증):

```bat
:: 1) 대상 파일 JSON 생성
unity_font_replacer_ko.exe --gamepath "C:\Game\Game_Data" ^
    --parse --target-file "38871756d6e98b9e67fb2e7a61dbb88e.bundle" --ps5-swizzle

:: 2) 원본 crop 추출
unity_font_replacer_ko.exe --gamepath "C:\Game\Game_Data" ^
    --list "Game.json" --target-file "38871756d6e98b9e67fb2e7a61dbb88e.bundle" ^
    --ps5-swizzle --preview-export --sdfonly

:: 3) NanumGothic 교체 (원본 보호 필요 시 --output-only 사용)
unity_font_replacer_ko.exe --gamepath "C:\Game\Game_Data" ^
    --nanumgothic --sdfonly --target-file "38871756d6e98b9e67fb2e7a61dbb88e.bundle" ^
    --ps5-swizzle --output-only "D:\output"

:: 4) 수정본 crop 추출
unity_font_replacer_ko.exe --gamepath "C:\Game\Game_Data" ^
    --list "Game.json" --target-file "38871756d6e98b9e67fb2e7a61dbb88e.bundle" ^
    --ps5-swizzle --preview-export --sdfonly
```

출력 경로:

| 종류 | 경로 |
|------|------|
| Atlas preview | `preview/<파일명>/<assets_name>__<atlas_pathid>__<font>__unswizzled__*.png` |
| Glyph crop | `preview/<파일명>/<assets_name>__<atlas_pathid>__<font>/U+XXXX*.png` |

---

## 폰트 추출 (export_fonts_ko.exe)

TextMeshPro SDF 폰트를 추출하는 도구입니다.

```bat
:: 경로 인자 방식 (권장)
export_fonts_ko.exe "D:\MyGame"

:: 또는 _Data 직접 지정
export_fonts_ko.exe "D:\MyGame\MyGame_Data"

:: 인자 생략 시 대화형 프롬프트
export_fonts_ko.exe
```

실행 후 현재 작업 디렉터리에 다음 파일이 생성됩니다.

| 출력 파일 | 설명 |
|-----------|------|
| `FontAsset이름.json` | TMP 폰트 데이터 |
| `FontAsset이름 SDF Atlas.png` | SDF Atlas 이미지 |
| `Material_*.json` | Material 데이터 (있는 경우) |

---

## 지원 폰트

| 폰트 이름 | 설명 |
|-----------|------|
| Mulmaru | 물마루 |
| NanumGothic | 나눔고딕 |

## 커스텀 폰트 추가

`KR_ASSETS` 폴더에 아래 파일을 추가하면 됩니다.

| 파일 | 필수 여부 |
|------|-----------|
| `폰트이름.ttf` 또는 `.otf` | 필수 |
| `폰트이름 SDF.json` / `Raster.json` / `.json` | SDF 교체 시 필요 |
| `폰트이름 SDF Atlas.png` / `Raster Atlas.png` / `Atlas.png` | SDF 교체 시 필요 |
| `폰트이름 SDF Material.json` / `Raster Material.json` / `Material.json` | 선택 |

SDF 데이터가 없으면 아래 `make_sdf.exe`로 먼저 생성하거나 `export_fonts_ko.exe`로 추출할 수 있습니다.

---

## SDF 생성 도구 (make_sdf.exe)

TTF에서 TMP 호환 JSON/Atlas를 직접 생성할 수 있습니다.

```bat
make_sdf.exe --ttf Mulmaru.ttf
```

| 인자 | 설명 | 기본값 |
|------|------|--------|
| `--ttf <ttfname>` | TTF 파일 경로/이름 | (필수) |
| `--atlas-size <w,h>` | 아틀라스 해상도 | `4096,4096` |
| `--point-size <int or auto>` | 샘플링 포인트 크기 | `auto` |
| `--padding <int>` | 아틀라스 패딩 | `7` |
| `--charset <txtpath or characters>` | 문자셋 파일 경로 또는 직접 문자열 | `./CharList_3911.txt` |
| `--rendermode <sdf,raster>` | 출력 렌더 모드 | `sdf` |

---

## 소스 실행 (선택)

EXE 대신 Python 소스로 실행하려면:

### 요구 사항

- Python 3.12 권장
- 패키지: `UnityPy(포크)`, `TypeTreeGeneratorAPI`, `Pillow`, `numpy`, `scipy`

```bash
pip install TypeTreeGeneratorAPI Pillow numpy scipy
pip install --upgrade git+https://github.com/snowyegret23/UnityPy.git
```

### 실행 예시

```bash
python unity_font_replacer_ko.py --gamepath "C:/path/to/game" --mulmaru
python export_fonts_ko.py "D:\MyGame"
```

---

## 주의 사항

### 저장

- 저장 기본 모드는 무압축 계열 우선(`safe-none -> legacy-none`)이며, 실패 시 `original -> lz4` 순으로 폴백합니다.
- 저장 시 원본 압축 우선이 필요하면 `--original-compress`를 사용하세요.
- 저장 속도가 느리면 `--temp-dir`로 빠른 SSD/NVMe 경로를 지정해 보세요.
- 프로그램 종료 시 임시 폴더는 자동 정리됩니다.
- 대형 SDF 다건 교체에서는 기본적으로 one-shot 실패 시 적응형 분할 저장(배치 크기 자동 조절)으로 폴백합니다.

### 스캔

- `--parse`는 파일 단위 워커 프로세스로 스캔해 단일 파일 크래시가 전체 작업 중단으로 이어지지 않도록 격리합니다.
- 스캔 속도를 높이려면 `--scan-jobs`(별칭: `--max-workers`)로 워커 수를 늘릴 수 있습니다.
- 스캔은 블랙리스트 기반 제외를 사용합니다 (`*.bak`, `.info`, `.config` 등 제외).
- 파일 단위로 제한하려면 `--target-file`을 사용하세요.

### SDF 교체

- 기본 줄 간격 메트릭 모드는 게임 원본 비율을 기준으로 교체 폰트 pointSize에 맞게 보정 적용합니다.
- 게임 원본 줄 간격 메트릭을 그대로 쓰려면 `--use-game-line-metrics`를 사용하세요. (pointSize는 항상 교체 폰트 값)
- SDF 교체 시 기본은 `KR_ASSETS/* SDF Material.json` 머티리얼 float를 적용하며, padding 비율 기준 보정도 함께 적용합니다.
- 원본 게임 머티리얼 스타일을 유지하려면 `--use-game-material`을 사용하세요.
- JSON 항목별 `force_raster: "True"`로 개별 Raster 강제를 지정할 수 있습니다. (`--parse` 기본값: `"False"`)
- 전체 SDF 교체 항목을 Raster 방식으로 강제하려면 `--force-raster`를 사용하세요.
- Raster 방식으로 처리되는 SDF 교체(개별 `force_raster` 또는 `--force-raster`)에서는 SDF 머티리얼 효과값(Outline/Underlay/Glow 등)을 0으로 보정하고 `m_AtlasRenderMode`의 SDF 플래그(0x1000)를 해제해 Raster 경로로 처리합니다.
- Material 참조가 외부 assets 파일(`m_FileID != 0`)이어도 동일하게 보정 대상에 포함됩니다.

### Preview Export

- `--preview-export`는 SDF Atlas/글리프 crop 미리보기를 `preview/`에 저장합니다.
- `--preview-export --ps5-swizzle` 조합이면 unswizzle 기준으로 preview를 저장합니다.

### PS5 Swizzle

- `--ps5-swizzle`는 메타데이터 기반 판정(우선) + raw-data 판정을 이용해 SDF Atlas swizzle 상태를 자동 판별합니다.
- Swizzle mask는 텍스쳐 크기(2의 거듭제곱)에 맞게 자동으로 계산됩니다.
- `swizzle`/`process_swizzle` 필드는 `--ps5-swizzle` 모드에서만 `--parse` JSON에 추가됩니다.
- `process_swizzle: "True"`를 JSON에 지정하면 자동 판정과 무관하게 교체 Atlas를 swizzle 상태로 변환합니다.

### 일반

- `TypeTreeGeneratorAPI`가 TMP(FontAsset) 파싱/교체에 필요합니다.
- 대화형 입력에서 경로 앞뒤 따옴표가 중복되어도 자동으로 정리해 처리합니다.
- 게임 파일 수정 전 백업을 권장합니다.
- 일부 게임은 무결성 검사로 수정 파일이 원복될 수 있습니다.
- 온라인 게임 사용 시 이용 약관을 확인하세요.

---

## Special Thanks

- [UnityPy](https://github.com/K0lb3/UnityPy) by K0lb3
- [Il2CppDumper](https://github.com/Perfare/Il2CppDumper) by Perfare
- [나눔고딕](https://hangeul.naver.com/font) by NAVER | [License](https://help.naver.com/service/30016/contents/18088?osType=PC&lang=ko)
- [물마루](https://github.com/mushsooni/mulmaru) by mushsooni | [License](https://github.com/mushsooni/mulmaru/blob/main/LICENSE_ko)

## 라이선스

MIT License

