"""KR: TTF 폰트로부터 TextMeshPro 호환 SDF/Raster 아틀라스 에셋을 생성한다.

생성되는 산출물:
  - JSON: TMP FontAsset 직렬화 구조 (m_FaceInfo, m_GlyphTable, m_CharacterTable 등)
  - PNG : 알파 채널에 SDF 또는 래스터 비트맵이 기록된 RGBA 아틀라스
  - Material JSON: _GradientScale 등 셰이더 파라미터

SDF 생성 원리:
  Pillow로 글리프별 8비트 그레이스케일 비트맵을 렌더링한 뒤,
  scipy EDT(Euclidean Distance Transform)로 안팎 거리장을 구하고
  spread 범위 내에서 [0, 1]로 정규화한다(edge = 0.5, inside > 0.5).
  TMP 셰이더는 이 0.5 경계를 기준으로 글리프 외곽선을 재구성한다.

좌표계 규약:
  TMP 신형(>= 1.4.0) GlyphRect.y 는 bottom-origin(Y=0이 아틀라스 하단).
  PIL Image 는 top-origin(Y=0이 상단)이므로 아틀라스에 배치한 뒤
  `glyph_y = atlas_h - glyph_y_top - glyph_h` 로 좌표를 변환하여 저장한다.

EN: Generate TextMeshPro-compatible SDF/Raster atlas assets from TTF fonts.

Generated outputs:
  - JSON: TMP FontAsset serialization structure (m_FaceInfo, m_GlyphTable, m_CharacterTable, etc.)
  - PNG : RGBA atlas with SDF or raster bitmap in alpha channel
  - Material JSON: shader parameters such as _GradientScale

SDF generation principle:
  Render 8-bit greyscale glyph bitmaps with Pillow, compute inside/outside
  distance fields via scipy EDT, and normalize to [0,1] within spread range
  (edge=0.5, inside>0.5). The TMP shader reconstructs glyph outlines at 0.5.

Coordinate convention:
  New TMP (>=1.4.0) GlyphRect.y is bottom-origin (Y=0 at atlas bottom).
  PIL Image is top-origin, so after placement we convert coordinates via
  `glyph_y = atlas_h - glyph_y_top - glyph_h`.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Literal

from PIL import Image, ImageFont

try:
    import numpy as np
except Exception:  # pragma: no cover - 선택 의존성 폴백 / optional dependency fallback
    np = None  # type: ignore[assignment]

try:
    from scipy import ndimage as scipy_ndimage
except Exception:  # pragma: no cover - 선택 의존성 폴백 / optional dependency fallback
    scipy_ndimage = None  # type: ignore[assignment]

try:
    from fontTools.ttLib import TTFont
except Exception:  # pragma: no cover - 선택 의존성 폴백 / optional dependency fallback
    TTFont = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
#  KR: 타입 별칭 & 로거
#  EN: Type aliases & logger
# --------------------------------------------------------------------------- #

RenderMode = Literal["sdf", "raster"]
JsonDict = dict[str, Any]
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  KR: 로깅 설정
#  EN: Logging configuration
# --------------------------------------------------------------------------- #

def configure_logging(level: int = logging.INFO) -> None:
    """KR: CLI 모드 전용 로깅을 구성한다. 이미 핸들러가 있으면 레벨만 변경.
    EN: Configure CLI-only logging. If handlers already exist, only change the level.
    """
    if logging.getLogger().handlers:
        logging.getLogger().setLevel(level)
        return
    logging.basicConfig(level=level, format="%(message)s")


# --------------------------------------------------------------------------- #
#  KR: 문자열·숫자 유틸리티
#  EN: String & number utilities
# --------------------------------------------------------------------------- #

def normalize_font_name(name: str) -> str:
    """KR: 파일명에서 확장자와 TMP 접미사(' SDF', ' Raster' 등)를 제거하여
    순수 폰트 패밀리명을 반환한다.
    EN: Strip file extension and TMP suffixes (' SDF', ' Raster', etc.)
    to return the pure font family name.
    """
    for ext in [".ttf", ".json", ".png"]:
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
    if name.endswith(" SDF Atlas"):
        name = name[: -len(" SDF Atlas")]
    elif name.endswith(" SDF"):
        name = name[: -len(" SDF")]
    elif name.endswith(" Raster Atlas"):
        name = name[: -len(" Raster Atlas")]
    elif name.endswith(" Raster"):
        name = name[: -len(" Raster")]
    return name


def _safe_int(value: Any, default: int = 0) -> int:
    """KR: 임의 값을 반올림 후 정수로 변환한다. 실패 시 default를 반환.
    EN: Round and convert an arbitrary value to int. Return default on failure.
    """
    try:
        return int(round(float(value)))
    except Exception:
        return default


# --------------------------------------------------------------------------- #
#  KR: CLI 인자 파서 헬퍼
#  EN: CLI argument parser helpers
# --------------------------------------------------------------------------- #

def _parse_atlas_size(value: str) -> tuple[int, int]:
    """KR: 'W,H' 형식 문자열을 (width, height) 정수 튜플로 파싱한다.
    EN: Parse a 'W,H' format string into a (width, height) integer tuple.
    """
    text = value.strip().replace(" ", "")
    parts = text.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("atlas size must be in 'W,H' format.")
    try:
        width = int(parts[0])
        height = int(parts[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("atlas size must contain integers.") from exc
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("atlas size must be positive.")
    return width, height


def _parse_point_size(value: str) -> int:
    """KR: 포인트 크기 문자열을 정수로 변환한다. 'auto'이면 0을 반환(자동 탐색).
    EN: Convert a point size string to int. Return 0 for 'auto' (auto-search).
    """
    text = value.strip().lower()
    if text == "auto":
        return 0
    try:
        point_size = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "point size must be an integer or 'auto'."
        ) from exc
    if point_size <= 0:
        raise argparse.ArgumentTypeError("point size must be positive or 'auto'.")
    return point_size


# --------------------------------------------------------------------------- #
#  KR: 파일 탐색
#  EN: File discovery
# --------------------------------------------------------------------------- #

def _resolve_ttf_path(ttf_arg: str) -> Path:
    """KR: TTF 파일 경로를 해석한다.
    직접 경로 -> CWD -> 스크립트 디렉토리 -> KR_ASSETS 하위 순서로 탐색.
    EN: Resolve a TTF file path.
    Search order: direct path -> CWD -> script directory -> KR_ASSETS subdirectory.
    """
    raw = Path(ttf_arg)
    if raw.is_file():
        return raw.resolve()

    candidate_names = [ttf_arg]
    if not ttf_arg.lower().endswith(".ttf"):
        candidate_names.append(f"{ttf_arg}.ttf")

    base_dirs = [
        Path.cwd(),
        Path(__file__).resolve().parent,
        Path.cwd() / "KR_ASSETS",
        Path(__file__).resolve().parent / "KR_ASSETS",
    ]
    for base_dir in base_dirs:
        for candidate_name in candidate_names:
            candidate = base_dir / candidate_name
            if candidate.is_file():
                return candidate.resolve()

    raise FileNotFoundError(f"TTF file not found: {ttf_arg}")


def _load_charset_text(charset_arg: str) -> str:
    """KR: 문자셋 인자를 텍스트로 변환한다.
    파일 경로이면 읽고, 아니면 리터럴 문자열로 취급.
    EN: Convert a charset argument to text.
    If it is a file path, read it; otherwise treat it as a literal string.
    """
    path = Path(charset_arg)
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="replace")

    looks_like_path = any(
        sep in charset_arg for sep in ["/", "\\"]
    ) or charset_arg.lower().endswith(".txt")
    if looks_like_path:
        raise FileNotFoundError(f"Charset file not found: {charset_arg}")

    return charset_arg


def _text_to_unicodes(text: str) -> list[int]:
    """KR: 텍스트에서 유니크한 유니코드 코드포인트 목록을 추출한다.
    NUL(0) 과 서로게이트 쌍(U+D800~U+DFFF)은 제외.
    EN: Extract a list of unique Unicode code points from text.
    Excludes NUL(0) and surrogate pairs (U+D800~U+DFFF).
    """
    seen: set[int] = set()
    unicodes: list[int] = []
    for char in text:
        code = ord(char)
        if code == 0:
            continue
        if 0xD800 <= code <= 0xDFFF:
            continue
        if code in seen:
            continue
        seen.add(code)
        unicodes.append(code)
    return unicodes


# --------------------------------------------------------------------------- #
#  KR: TTF 메타데이터 추출
#  EN: TTF metadata extraction
# --------------------------------------------------------------------------- #

def _get_ttf_name_info(ttf_data: bytes, fallback_name: str) -> tuple[str, str, int]:
    """KR: fontTools로 TTF의 패밀리명, 스타일명, unitsPerEm을 읽는다.
    fontTools가 없으면 fallback_name과 기본값을 반환.
    EN: Read family name, style name, and unitsPerEm from TTF via fontTools.
    Returns fallback_name and defaults if fontTools is unavailable.
    """
    family_name = normalize_font_name(fallback_name)
    style_name = "Regular"
    units_per_em = 1000

    if TTFont is None:
        return family_name, style_name, units_per_em

    try:
        with TTFont(io.BytesIO(ttf_data), lazy=True) as font:
            head = font.get("head")
            if head is not None:
                units_per_em = max(1, int(getattr(head, "unitsPerEm", units_per_em)))
            name_table = font.get("name")
            if name_table is not None:
                family_name = str(name_table.getBestFamilyName() or family_name)
                style_name = str(name_table.getBestSubFamilyName() or style_name)
    except Exception:
        pass

    return family_name, style_name, units_per_em


# --------------------------------------------------------------------------- #
#  KR: 글리프 메트릭 측정
#  EN: Glyph metric measurement
# --------------------------------------------------------------------------- #

def _measure_glyph_metrics(
    font: ImageFont.FreeTypeFont,
    unicode_value: int,
    ascent: int,
) -> tuple[int, int, JsonDict]:
    """KR: 단일 글리프의 바운딩 박스와 TMP m_Metrics 구조를 계산한다.
    반환값: (width, height, metrics_dict)
    TMP m_Metrics 필드 의미:
      m_Width / m_Height        : 글리프 비트맵 픽셀 크기
      m_HorizontalBearingX      : 원점->글리프 좌측 간격 (bbox x0)
      m_HorizontalBearingY      : 베이스라인->글리프 상단 거리 (ascent - y0)
      m_HorizontalAdvance       : 다음 글리프까지의 수평 이동량

    EN: Compute the bounding box and TMP m_Metrics structure for a single glyph.
    Returns: (width, height, metrics_dict)
    TMP m_Metrics field meanings:
      m_Width / m_Height        : glyph bitmap pixel size
      m_HorizontalBearingX      : origin-to-glyph left offset (bbox x0)
      m_HorizontalBearingY      : baseline-to-glyph top distance (ascent - y0)
      m_HorizontalAdvance       : horizontal advance to next glyph
    """
    character = chr(unicode_value)
    bbox = font.getbbox(character)
    if bbox is None:
        bbox = (0, 0, 0, 0)

    x0, y0, x1, y1 = bbox
    width = max(0, int(x1 - x0))
    height = max(0, int(y1 - y0))

    try:
        advance = float(font.getlength(character))
    except Exception:
        advance = float(width)

    metrics = {
        "m_Width": float(width),
        "m_Height": float(height),
        "m_HorizontalBearingX": float(x0),
        # KR: TMP 규약: 베이스라인에서 글리프 상단까지의 거리(양수).
        #     Pillow의 ascent는 베이스라인~폰트 상단이고, y0은 상단 원점에서
        #     글리프 상단까지의 오프셋이므로 ascent - y0 으로 계산.
        # EN: TMP convention: distance from baseline to glyph top (positive).
        #     Pillow ascent is baseline-to-font-top, y0 is the offset from top
        #     origin to glyph top, so compute as ascent - y0.
        "m_HorizontalBearingY": float(ascent - y0),
        "m_HorizontalAdvance": float(advance),
    }
    return width, height, metrics


# --------------------------------------------------------------------------- #
#  KR: 글리프 비트맵 렌더링
#  EN: Glyph bitmap rendering
# --------------------------------------------------------------------------- #

def _render_glyph_bitmap(font: ImageFont.FreeTypeFont, unicode_value: int) -> Any:
    """KR: FreeType으로 단일 글리프의 8비트 그레이스케일 비트맵을 렌더링한다.
    아웃라인이 없는 코드포인트는 빈(0x0) 배열을 반환.
    EN: Render a single glyph's 8-bit greyscale bitmap via FreeType.
    Code points without outlines return an empty (0x0) array.
    """
    if np is None:
        raise RuntimeError("numpy is required for SDF generation.")

    try:
        mask = font.getmask(chr(unicode_value), mode="L")
    except OSError:
        # KR: 메트릭은 있지만 비트맵이 없는 글리프(일부 CJK 폰트의 누락 아웃라인).
        #     빈 타일로 처리하여 아틀라스 배치를 유지한다.
        # EN: Glyph has metrics but no bitmap (missing outlines in some CJK fonts).
        #     Treat as empty tile to preserve atlas layout.
        return np.zeros((0, 0), dtype=np.uint8)

    mask_w, mask_h = mask.size
    if mask_w <= 0 or mask_h <= 0:
        return np.zeros((0, 0), dtype=np.uint8)

    mask_bytes = bytes(mask)
    bitmap = np.frombuffer(mask_bytes, dtype=np.uint8)
    if bitmap.size != mask_w * mask_h:
        bitmap = np.array(mask, dtype=np.uint8)
    return bitmap.reshape((mask_h, mask_w))


# --------------------------------------------------------------------------- #
#  KR: Shelf 방식 사각형 패킹
#  EN: Shelf-based rectangle packing
# --------------------------------------------------------------------------- #

def _pack_rectangles_shelf(
    rectangles: list[tuple[int, int, int]],
    atlas_width: int,
    atlas_height: int,
) -> tuple[dict[int, tuple[int, int, int, int]], list[JsonDict]] | None:
    """KR: Shelf(선반) 알고리즘으로 글리프 사각형을 아틀라스에 배치한다.
    각 사각형은 (glyph_index, rect_w, rect_h).
    면적/높이 내림차순으로 정렬한 뒤, 좌->우로 채우다가 행이 넘치면
    다음 선반(행)으로 내린다.
    반환: (placements, used_rects) 또는 초과 시 None.
      placements : glyph_index -> (x, y, w, h)
      used_rects : TMP m_UsedGlyphRects 형식 딕셔너리 리스트

    EN: Pack glyph rectangles into the atlas using a shelf algorithm.
    Each rectangle is (glyph_index, rect_w, rect_h).
    Sorted by area/height descending, filled left-to-right; when a row
    overflows, move down to the next shelf (row).
    Returns: (placements, used_rects) or None if overflow.
      placements : glyph_index -> (x, y, w, h)
      used_rects : list of dicts in TMP m_UsedGlyphRects format
    """
    placements: dict[int, tuple[int, int, int, int]] = {}
    used_rects: list[JsonDict] = []

    x = 0
    y = 0
    row_height = 0
    ordered = sorted(
        rectangles, key=lambda item: (item[1] * item[2], item[2], item[1]), reverse=True
    )

    for glyph_index, rect_w, rect_h in ordered:
        if rect_w > atlas_width or rect_h > atlas_height:
            return None

        if x + rect_w > atlas_width:
            x = 0
            y += row_height
            row_height = 0

        if y + rect_h > atlas_height:
            return None

        placements[glyph_index] = (x, y, rect_w, rect_h)
        used_rects.append({"m_X": x, "m_Y": y, "m_Width": rect_w, "m_Height": rect_h})
        x += rect_w
        row_height = max(row_height, rect_h)

    return placements, used_rects


# --------------------------------------------------------------------------- #
#  KR: 레이아웃 검증
#  EN: Layout validation
# --------------------------------------------------------------------------- #

def _validate_layout_rectangles(
    placements: dict[int, tuple[int, int, int, int]],
    used_rects: list[JsonDict],
    glyph_indices: set[int],
    atlas_width: int,
    atlas_height: int,
) -> tuple[bool, str]:
    """KR: 패킹 결과가 유효한지 검증한다.
    확인 항목:
      - placement 수와 glyph 수 일치
      - 모든 사각형이 아틀라스 범위 내
      - 좌표/크기가 양수
      - 중복 할당 없음

    EN: Validate whether the packing result is valid.
    Checks:
      - placement count matches glyph count
      - all rectangles within atlas bounds
      - coordinates/sizes are positive
      - no duplicate allocations
    """
    if len(placements) != len(glyph_indices):
        return False, "placement count mismatch"
    if set(placements.keys()) != glyph_indices:
        return False, "placement glyph index mismatch"
    if len(used_rects) != len(placements):
        return False, "used rect count mismatch"

    occupied: set[tuple[int, int, int, int]] = set()
    for glyph_index, rect in placements.items():
        px, py, pw, ph = rect
        if px < 0 or py < 0 or pw <= 0 or ph <= 0:
            return (
                False,
                f"invalid rectangle for glyph {glyph_index}: ({px}, {py}, {pw}, {ph})",
            )
        if px + pw > atlas_width or py + ph > atlas_height:
            return (
                False,
                f"out-of-bounds rectangle for glyph {glyph_index}: ({px}, {py}, {pw}, {ph})",
            )
        key = (px, py, pw, ph)
        if key in occupied:
            return False, f"duplicate rectangle allocation: ({px}, {py}, {pw}, {ph})"
        occupied.add(key)
    return True, ""


# --------------------------------------------------------------------------- #
#  KR: SDF 타일 계산
#  EN: SDF tile computation
# --------------------------------------------------------------------------- #

def _compute_sdf_tile(alpha_tile: Any, spread: int) -> Any:
    """KR: 알파 타일에서 SDF(Signed Distance Field) 값을 계산한다.
    알고리즘:
      1. 127 임계값으로 inside/outside 이진 마스크 생성
      2. scipy EDT로 각 픽셀의 최근접 경계까지 유클리드 거리 계산
      3. signed_distance = dist_inside - dist_outside
      4. spread 범위로 정규화: 0.5 + (sd / (2 * spread))
         -> edge ~ 0.5, inside > 0.5, outside < 0.5
    TMP SDF 셰이더가 이 0.5 경계를 기준으로 글리프 윤곽을 판단하고,
    _GradientScale(= padding + 1) 값으로 안티앨리어싱 폭을 결정한다.

    EN: Compute SDF (Signed Distance Field) values from an alpha tile.
    Algorithm:
      1. Create inside/outside binary mask at threshold 127
      2. Compute Euclidean distance to nearest boundary via scipy EDT
      3. signed_distance = dist_inside - dist_outside
      4. Normalize within spread range: 0.5 + (sd / (2 * spread))
         -> edge ~ 0.5, inside > 0.5, outside < 0.5
    The TMP SDF shader uses this 0.5 boundary to determine glyph outlines,
    and _GradientScale (= padding + 1) controls the anti-aliasing width.
    """
    if np is None or scipy_ndimage is None:
        raise RuntimeError("numpy and scipy are required for SDF generation.")

    inside = alpha_tile > 127
    if not inside.any():
        return np.zeros_like(alpha_tile, dtype=np.uint8)
    if inside.all():
        return np.full_like(alpha_tile, 255, dtype=np.uint8)

    outside = ~inside
    dist_outside = scipy_ndimage.distance_transform_edt(outside)
    dist_inside = scipy_ndimage.distance_transform_edt(inside)
    # KR: TMP / SDF 셰이더 규약: edge ~ 0.5, inside > 0.5, outside < 0.5.
    # EN: TMP / SDF shader convention: edge ~ 0.5, inside > 0.5, outside < 0.5.
    signed_distance = dist_inside - dist_outside

    spread_value = float(max(1, spread))
    normalized = np.clip(0.5 + (signed_distance / (2.0 * spread_value)), 0.0, 1.0)
    return (normalized * 255.0).astype(np.uint8)


# --------------------------------------------------------------------------- #
#  KR: SDF 페이로드 정규화
#  EN: SDF payload normalization
# --------------------------------------------------------------------------- #

def _normalize_sdf_payload(data: JsonDict) -> JsonDict:
    """KR: SDF 데이터를 독립 실행 시에도 유효하도록 최소 필드를 보장한다.
    딥카피 후 누락된 필수 키(m_AtlasTextures, m_UsedGlyphRects 등)를 채운다.
    m_AtlasTextures의 FileID/PathID는 정수로 정규화한다.
    EN: Ensure minimum required fields so SDF data is valid even standalone.
    Deep-copy then fill missing required keys (m_AtlasTextures, m_UsedGlyphRects, etc.).
    Normalize m_AtlasTextures FileID/PathID to integers.
    """
    data = json.loads(json.dumps(data))
    data.setdefault("m_AtlasTextures", [{"m_FileID": 0, "m_PathID": 0}])
    data.setdefault("m_UsedGlyphRects", [])
    data.setdefault("m_FreeGlyphRects", [])
    data.setdefault("m_FontWeightTable", [])
    if isinstance(data.get("m_AtlasTextures"), list):
        normalized = []
        for tex in data["m_AtlasTextures"]:
            if isinstance(tex, dict):
                normalized.append(
                    {
                        "m_FileID": int(tex.get("m_FileID", 0) or 0),
                        "m_PathID": int(tex.get("m_PathID", 0) or 0),
                    }
                )
        data["m_AtlasTextures"] = normalized
    return data


# --------------------------------------------------------------------------- #
#  KR: 메인 SDF 에셋 생성 파이프라인
#  EN: Main SDF asset generation pipeline
# --------------------------------------------------------------------------- #

def generate_sdf_assets_from_ttf(
    ttf_data: bytes,
    font_name: str,
    unicodes: list[int],
    point_size: int,
    atlas_padding: int,
    atlas_width: int,
    atlas_height: int,
    render_mode: RenderMode = "sdf",
    log_fn: Callable[[str], None] | None = None,
) -> JsonDict | None:
    """KR: TTF 바이너리로부터 TMP 호환 SDF/Raster 에셋 세트를 생성한다.
    처리 흐름:
      1. 포인트 크기 결정
         - point_size > 0: 고정 크기. 초과 시 점진 축소 후보 탐색
         - point_size == 0 ('auto'): 이진 탐색으로 아틀라스에 수용 가능한 최대 크기 결정
      2. 글리프 메트릭 측정 및 사각형 패킹(Shelf 알고리즘)
      3. 글리프 비트맵 렌더링 -> SDF 변환(또는 래스터 직접 사용)
      4. RGBA 아틀라스 이미지 생성 (알파 채널에 SDF/래스터 기록)
      5. TMP 직렬화 구조(m_FaceInfo, m_GlyphTable, m_CharacterTable) 조립
    인자:
      ttf_data      : TTF 파일 바이너리
      font_name     : 폰트 이름 (확장자 포함 가능, 내부에서 정규화)
      unicodes      : 생성할 글리프의 유니코드 코드포인트 목록
      point_size    : 샘플링 포인트 크기 (0이면 자동)
      atlas_padding : 글리프 간 패딩 (SDF spread 범위와 직결)
      atlas_width/height : 아틀라스 픽셀 크기
      render_mode   : 'sdf' 또는 'raster'
      log_fn        : 진행 로그 콜백
    반환:
      성공 시 딕셔너리:
        ttf_data, sdf_data, sdf_data_normalized, sdf_atlas (PIL Image), sdf_materials
      실패 시 None

    EN: Generate a TMP-compatible SDF/Raster asset set from TTF binary data.
    Processing flow:
      1. Determine point size
         - point_size > 0: fixed size; progressively smaller candidates on overflow
         - point_size == 0 ('auto'): binary search for max size that fits the atlas
      2. Measure glyph metrics and pack rectangles (Shelf algorithm)
      3. Render glyph bitmaps -> SDF transform (or use raster directly)
      4. Create RGBA atlas image (SDF/raster written to alpha channel)
      5. Assemble TMP serialization structures (m_FaceInfo, m_GlyphTable, m_CharacterTable)
    Args:
      ttf_data      : TTF file binary
      font_name     : font name (may include extension; normalized internally)
      unicodes      : list of Unicode code points to generate glyphs for
      point_size    : sampling point size (0 for auto)
      atlas_padding : inter-glyph padding (directly tied to SDF spread range)
      atlas_width/height : atlas pixel dimensions
      render_mode   : 'sdf' or 'raster'
      log_fn        : progress log callback
    Returns:
      On success, a dict:
        ttf_data, sdf_data, sdf_data_normalized, sdf_atlas (PIL Image), sdf_materials
      On failure, None
    """
    def _emit(message: str) -> None:
        if callable(log_fn):
            try:
                log_fn(message)
            except Exception:
                pass

    if np is None or scipy_ndimage is None:
        logger.error("Error: numpy/scipy are required.")
        return None
    if not unicodes:
        return None

    # KR: 렌더 모드 정규화
    # EN: Normalize render mode
    normalized_render_mode = str(render_mode).strip().lower()
    if normalized_render_mode not in {"sdf", "raster"}:
        normalized_render_mode = "sdf"

    # KR: 포인트 크기 클램핑 (8~512 범위)
    # EN: Clamp point size (range 8~512)
    requested_point_size = _safe_int(point_size, 0)
    if requested_point_size > 0:
        requested_point_size = max(8, min(requested_point_size, 512))

    # KR: 패딩 및 아틀라스 크기 클램핑
    # EN: Clamp padding and atlas dimensions
    atlas_padding = max(1, min(int(atlas_padding), 64))
    atlas_width = max(64, min(int(atlas_width), 8192))
    atlas_height = max(64, min(int(atlas_height), 8192))

    # KR: TTF 메타데이터(패밀리명, 스타일, unitsPerEm) 추출
    # EN: Extract TTF metadata (family name, style, unitsPerEm)
    family_name, style_name, units_per_em = _get_ttf_name_info(ttf_data, font_name)

    # KR: 레이아웃 캐시: 동일 포인트 크기 재계산 방지
    # EN: Layout cache: avoid recomputation for same point size
    layout_cache: dict[int, JsonDict | None] = {}

    def _build_layout(candidate_point_size: int) -> JsonDict | None:
        """KR: 주어진 포인트 크기로 글리프 메트릭 측정 -> 사각형 패킹을 시도한다.
        아틀라스에 모두 배치 가능하면 레이아웃 딕셔너리를, 초과 시 None을 반환.
        EN: Measure glyph metrics -> attempt rectangle packing at the given point size.
        Return a layout dict if all glyphs fit in the atlas, or None on overflow.
        """
        if candidate_point_size in layout_cache:
            return layout_cache[candidate_point_size]

        try:
            font = ImageFont.truetype(io.BytesIO(ttf_data), size=candidate_point_size)
        except Exception:
            layout_cache[candidate_point_size] = None
            return None

        ascent, descent = font.getmetrics()
        glyph_entries: list[JsonDict] = []
        rectangles: list[tuple[int, int, int]] = []

        for code in unicodes:
            glyph_w, glyph_h, metrics = _measure_glyph_metrics(
                font, int(code), int(ascent)
            )
            # KR: 유효한 글리프는 양쪽에 padding을 더한 타일 크기로 패킹.
            #     빈 글리프(공백 등)는 1x1 최소 슬롯 할당.
            # EN: Valid glyphs are packed with padding added on both sides.
            #     Empty glyphs (spaces, etc.) get a minimum 1x1 slot.
            if glyph_w > 0 and glyph_h > 0:
                rect_w = glyph_w + atlas_padding * 2
                rect_h = glyph_h + atlas_padding * 2
            else:
                rect_w = 1
                rect_h = 1

            rectangles.append((int(code), rect_w, rect_h))
            glyph_entries.append(
                {
                    "unicode": int(code),
                    "glyph_index": int(code),
                    "width": glyph_w,
                    "height": glyph_h,
                    "metrics": metrics,
                }
            )

        packed_result = _pack_rectangles_shelf(rectangles, atlas_width, atlas_height)

        if packed_result is None:
            layout_cache[candidate_point_size] = None
            return None

        placements, used_rects = packed_result
        valid_layout, reason = _validate_layout_rectangles(
            placements=placements,
            used_rects=used_rects,
            glyph_indices={int(code) for code in unicodes},
            atlas_width=atlas_width,
            atlas_height=atlas_height,
        )
        if not valid_layout:
            _emit(
                f"[layout] point-size {candidate_point_size}: invalid layout ({reason})"
            )
            layout_cache[candidate_point_size] = None
            return None

        layout: JsonDict = {
            "font": font,
            "point_size": int(candidate_point_size),
            "ascent": int(ascent),
            "descent": int(descent),
            "glyph_entries": glyph_entries,
            "placements": placements,
            "used_rects": used_rects,
            "atlas_w": int(atlas_width),
            "atlas_h": int(atlas_height),
        }
        layout_cache[candidate_point_size] = layout
        return layout

    # ------------------------------------------------------------------ #
    #  KR: 포인트 크기 결정
    #  EN: Determine point size
    # ------------------------------------------------------------------ #

    selected_layout: JsonDict | None = None
    if requested_point_size <= 0:
        # KR: 자동 모드: 이진 탐색으로 아틀라스에 수용 가능한 최대 포인트 크기 탐색
        # EN: Auto mode: binary search for the max point size that fits the atlas
        low = 8
        high = 512
        best_size = 0
        best_layout: JsonDict | None = None
        _emit(f"[auto] point-size search range: {low}-{high}")
        while low <= high:
            mid = (low + high) // 2
            layout = _build_layout(mid)
            if layout is not None:
                _emit(f"[auto] point-size {mid}: fit")
                best_size = mid
                best_layout = layout
                low = mid + 1
            else:
                _emit(f"[auto] point-size {mid}: overflow")
                high = mid - 1
        selected_layout = best_layout
        if best_size > 0:
            _emit(f"[auto] selected point-size: {best_size}")
    else:
        # KR: 고정 모드: 지정 크기부터 시작, 초과 시 점진 축소 후보 순회
        # EN: Fixed mode: start from specified size, iterate progressively smaller candidates on overflow
        candidates = [requested_point_size]
        for step in [4, 8, 12, 16, 24, 32, 48, 64, 96, 128]:
            candidate = requested_point_size - step
            if candidate >= 8 and candidate not in candidates:
                candidates.append(candidate)
        if 8 not in candidates:
            candidates.append(8)
        for candidate in candidates:
            layout = _build_layout(candidate)
            _emit(
                f"[fixed] point-size {candidate}: {'fit' if layout is not None else 'overflow'}"
            )
            if layout is not None:
                selected_layout = layout
                break

    if selected_layout is None:
        return None

    # KR: 선택된 레이아웃 언패킹
    # EN: Unpack the selected layout
    selected_font = selected_layout["font"]
    selected_entries = selected_layout["glyph_entries"]
    selected_placements = selected_layout["placements"]
    selected_used_rects = selected_layout["used_rects"]
    selected_atlas_w = int(selected_layout["atlas_w"])
    selected_atlas_h = int(selected_layout["atlas_h"])
    selected_ascent = int(selected_layout["ascent"])
    selected_descent = int(selected_layout["descent"])
    selected_point_size = int(selected_layout["point_size"])

    # KR: 최종 정합성 검증: 글리프 인덱스와 placement 키가 정확히 일치해야 함
    # EN: Final consistency check: glyph indices and placement keys must match exactly
    expected_glyph_indices = {
        int(entry.get("glyph_index", -1)) for entry in selected_entries
    }
    if set(int(key) for key in selected_placements.keys()) != expected_glyph_indices:
        _emit("[layout] selected placements do not match glyph entries.")
        return None
    if len(selected_used_rects) != len(selected_placements):
        _emit("[layout] selected used rect count mismatch.")
        return None

    # ------------------------------------------------------------------ #
    #  KR: 아틀라스 렌더링 (비트맵 -> SDF/래스터 -> 아틀라스 합성)
    #  EN: Atlas rendering (bitmap -> SDF/raster -> atlas compositing)
    # ------------------------------------------------------------------ #

    atlas_alpha = np.zeros((selected_atlas_h, selected_atlas_w), dtype=np.uint8)
    glyph_table: list[JsonDict] = []
    char_table: list[JsonDict] = []
    total_entries = len(selected_entries)
    progress_step = max(200, total_entries // 10 if total_entries > 0 else 1)

    for idx, entry in enumerate(selected_entries, start=1):
        glyph_index = int(entry["glyph_index"])
        unicode_value = int(entry["unicode"])
        placement = selected_placements.get(glyph_index)
        if placement is None:
            continue
        px, py, pw, ph = placement

        # KR: 배치 범위 검증
        # EN: Validate placement bounds
        if (
            px < 0
            or py < 0
            or pw <= 0
            or ph <= 0
            or (px + pw) > selected_atlas_w
            or (py + ph) > selected_atlas_h
        ):
            _emit(
                f"[layout] invalid placement for glyph={glyph_index}: ({px}, {py}, {pw}, {ph})"
            )
            return None
        glyph_w = int(entry["width"])
        glyph_h = int(entry["height"])

        if glyph_w > 0 and glyph_h > 0:
            # KR: 글리프 비트맵 렌더링
            # EN: Render glyph bitmap
            bitmap = _render_glyph_bitmap(selected_font, unicode_value)
            bitmap_h = int(bitmap.shape[0]) if bitmap.ndim == 2 else 0
            bitmap_w = int(bitmap.shape[1]) if bitmap.ndim == 2 else 0
            copy_w = min(glyph_w, bitmap_w)
            copy_h = min(glyph_h, bitmap_h)

            # KR: 패딩을 포함한 타일 생성 후 비트맵 삽입.
            #     패딩 영역은 0으로 유지되어 SDF 계산 시 outside로 처리됨.
            # EN: Create tile with padding then insert bitmap.
            #     Padding area stays 0, treated as outside during SDF computation.
            tile = np.zeros((ph, pw), dtype=np.uint8)
            offset_x = min(atlas_padding, max(0, pw - glyph_w))
            offset_y = min(atlas_padding, max(0, ph - glyph_h))
            if copy_w > 0 and copy_h > 0:
                tile[offset_y : offset_y + copy_h, offset_x : offset_x + copy_w] = (
                    bitmap[:copy_h, :copy_w]
                )

            # KR: SDF 모드: EDT 기반 거리장 변환 / 래스터 모드: 비트맵 그대로 사용
            # EN: SDF mode: EDT-based distance field transform / Raster mode: use bitmap as-is
            if normalized_render_mode == "sdf":
                mode_tile = _compute_sdf_tile(tile, atlas_padding)
            else:
                mode_tile = tile
            # KR: max 합성으로 겹침 방지 (이론상 shelf 패킹에선 겹치지 않지만 안전장치)
            # EN: Max-composite to prevent overlap (shelf packing shouldn't overlap, but safeguard)
            atlas_alpha[py : py + ph, px : px + pw] = np.maximum(
                atlas_alpha[py : py + ph, px : px + pw], mode_tile
            )

            # KR: 글리프 실제 위치 (패딩 오프셋 적용)
            # EN: Actual glyph position (with padding offset applied)
            glyph_x = px + offset_x
            glyph_y_top = py + offset_y
            glyph_rect_w = glyph_w
            glyph_rect_h = glyph_h
        else:
            # KR: 빈 글리프 (공백, 제어문자 등)
            # EN: Empty glyph (spaces, control characters, etc.)
            glyph_x = px
            glyph_y_top = py
            glyph_rect_w = 1
            glyph_rect_h = 1

        # KR: 음수 좌표 검증
        # EN: Validate for negative coordinates
        if glyph_x < 0 or glyph_y_top < 0:
            _emit(
                f"[layout] negative glyph origin for glyph={glyph_index}: ({glyph_x}, {glyph_y_top})"
            )
            return None
        if (
            glyph_x + glyph_rect_w > selected_atlas_w
            or glyph_y_top + glyph_rect_h > selected_atlas_h
        ):
            _emit(
                "[layout] glyph rect exceeds atlas for glyph="
                f"{glyph_index}: ({glyph_x}, {glyph_y_top}, {glyph_rect_w}, {glyph_rect_h})"
            )
            return None

        # KR: 좌표 변환: PIL top-origin -> TMP bottom-origin
        #     PIL 이미지에서 glyph_y_top은 상단 원점 기준이므로,
        #     TMP 신형 GlyphRect.y(하단 원점)로 변환한다.
        #     공식: y_bottom = atlasHeight - y_top - height
        # EN: Coordinate conversion: PIL top-origin -> TMP bottom-origin
        #     In PIL images glyph_y_top is top-origin based, so convert
        #     to new TMP GlyphRect.y (bottom-origin).
        #     Formula: y_bottom = atlasHeight - y_top - height
        glyph_y = selected_atlas_h - glyph_y_top - glyph_rect_h
        glyph_table.append(
            {
                "m_Index": glyph_index,
                "m_Metrics": entry["metrics"],
                "m_GlyphRect": {
                    "m_X": int(glyph_x),
                    "m_Y": int(glyph_y),
                    "m_Width": int(glyph_rect_w),
                    "m_Height": int(glyph_rect_h),
                },
                "m_Scale": 1.0,
                "m_AtlasIndex": 0,
                "m_ClassDefinitionType": 0,
            }
        )
        char_table.append(
            {
                "m_ElementType": 1,
                "m_Unicode": unicode_value,
                "m_GlyphIndex": glyph_index,
                "m_Scale": 1.0,
            }
        )

        if idx == 1 or idx == total_entries or idx % progress_step == 0:
            _emit(f"[render] glyph {idx}/{total_entries}")

    # ------------------------------------------------------------------ #
    #  KR: 중복 제거 및 정렬
    #  EN: Deduplication and sorting
    # ------------------------------------------------------------------ #

    # KR: 동일 glyph index가 여러 번 생성된 경우 첫 번째만 유지
    # EN: If the same glyph index was generated multiple times, keep only the first
    unique_glyph_table: list[JsonDict] = []
    glyph_index_seen: set[int] = set()
    duplicate_glyph_indices: list[int] = []
    for glyph in glyph_table:
        glyph_index = _safe_int(glyph.get("m_Index", -1), -1)
        if glyph_index < 0:
            continue
        if glyph_index in glyph_index_seen:
            duplicate_glyph_indices.append(glyph_index)
            continue
        glyph_index_seen.add(glyph_index)
        unique_glyph_table.append(glyph)
    if duplicate_glyph_indices:
        _emit(
            "[render] duplicate glyph indices removed: "
            + ", ".join(str(x) for x in sorted(set(duplicate_glyph_indices)))
        )
    glyph_table = unique_glyph_table

    # KR: 동일 (unicode, glyph_index) 쌍 중복 제거 + 글리프 테이블에 없는 항목 제외
    # EN: Deduplicate (unicode, glyph_index) pairs + exclude entries not in glyph table
    unique_char_table: list[JsonDict] = []
    char_pair_seen: set[tuple[int, int]] = set()
    duplicate_char_pairs = 0
    for ch in char_table:
        unicode_value = _safe_int(ch.get("m_Unicode", -1), -1)
        glyph_index = _safe_int(ch.get("m_GlyphIndex", -1), -1)
        if unicode_value < 0 or glyph_index < 0:
            continue
        if glyph_index not in glyph_index_seen:
            continue
        key = (unicode_value, glyph_index)
        if key in char_pair_seen:
            duplicate_char_pairs += 1
            continue
        char_pair_seen.add(key)
        unique_char_table.append(ch)
    if duplicate_char_pairs:
        _emit(f"[render] duplicate character entries removed: {duplicate_char_pairs}")
    char_table = unique_char_table

    if len(glyph_table) != len(char_table):
        _emit(
            f"[render] glyph/char count mismatch after dedupe: glyphs={len(glyph_table)}, chars={len(char_table)}"
        )

    # KR: TMP 규약: 인덱스/유니코드 오름차순 정렬
    # EN: TMP convention: sort by index/unicode ascending
    glyph_table.sort(key=lambda item: int(item.get("m_Index", 0)))
    char_table.sort(key=lambda item: int(item.get("m_Unicode", 0)))

    # ------------------------------------------------------------------ #
    #  KR: FaceInfo 조립
    #  EN: FaceInfo assembly
    # ------------------------------------------------------------------ #

    # KR: 대문자 높이(Cap Height)와 x-height(Mean Line)는 각각 'H', 'x' bbox로 측정
    # EN: Cap Height and x-height (Mean Line) are measured from 'H' and 'x' bboxes respectively
    cap_bbox = selected_font.getbbox("H")
    mean_bbox = selected_font.getbbox("x")
    cap_line = (
        float(selected_ascent - cap_bbox[1])
        if cap_bbox is not None
        else float(selected_ascent)
    )
    mean_line = (
        float(selected_ascent - mean_bbox[1])
        if mean_bbox is not None
        else float(selected_ascent * 0.5)
    )
    line_height = float(selected_ascent + selected_descent)
    descent_line = float(-selected_descent)
    underline_thickness = max(1.0, float(selected_point_size) * 0.06)
    strikethrough_offset = (
        cap_line / 2.5 if cap_line != 0 else float(selected_ascent) * 0.4
    )

    face_info: JsonDict = {
        "m_FaceIndex": 0,
        "m_FamilyName": family_name,
        "m_StyleName": style_name,
        "m_PointSize": int(selected_point_size),
        "m_Scale": 1.0,
        "m_UnitsPerEM": int(units_per_em),
        "m_LineHeight": line_height,
        "m_AscentLine": float(selected_ascent),
        "m_CapLine": cap_line,
        "m_MeanLine": mean_line,
        "m_Baseline": 0.0,
        "m_DescentLine": descent_line,
        "m_SuperscriptOffset": float(selected_ascent) * 0.5,
        "m_SuperscriptSize": 0.5,
        "m_SubscriptOffset": descent_line * 0.5,
        "m_SubscriptSize": 0.5,
        "m_UnderlineOffset": descent_line * 0.5,
        "m_UnderlineThickness": underline_thickness,
        "m_StrikethroughOffset": strikethrough_offset,
        "m_StrikethroughThickness": underline_thickness,
        "m_TabWidth": float(selected_point_size) * 0.5,
    }

    # ------------------------------------------------------------------ #
    #  KR: 아틀라스 이미지 생성 (RGBA, 알파 채널에 SDF/래스터 기록)
    #  EN: Create atlas image (RGBA, SDF/raster written to alpha channel)
    # ------------------------------------------------------------------ #

    atlas_rgba = np.zeros((selected_atlas_h, selected_atlas_w, 4), dtype=np.uint8)
    atlas_rgba[:, :, 3] = atlas_alpha
    atlas_image = Image.fromarray(atlas_rgba, mode="RGBA")

    # KR: TMP m_AtlasRenderMode 값:
    #     4165 (0x1045) = SDFAA       -- SDF + 안티앨리어싱 (게임 기본값)
    #     4118 (0x1016) = SDFAA_HINTED -- SDF + 안티앨리어싱 + 힌팅
    #     4    (0x0004) = Raster_Hinted -- 비트맵 래스터 + 힌팅
    # EN: TMP m_AtlasRenderMode values:
    #     4165 (0x1045) = SDFAA       -- SDF + anti-aliasing (game default)
    #     4118 (0x1016) = SDFAA_HINTED -- SDF + anti-aliasing + hinting
    #     4    (0x0004) = Raster_Hinted -- bitmap raster + hinting
    atlas_render_mode_value = 4165 if normalized_render_mode == "sdf" else 4
    generated_sdf_data: JsonDict = {
        "m_FaceInfo": face_info,
        "m_GlyphTable": glyph_table,
        "m_CharacterTable": char_table,
        "m_AtlasTextures": [{"m_FileID": 0, "m_PathID": 0}],
        "m_AtlasWidth": int(selected_atlas_w),
        "m_AtlasHeight": int(selected_atlas_h),
        "m_AtlasPadding": int(atlas_padding),
        "m_AtlasRenderMode": atlas_render_mode_value,
        "m_UsedGlyphRects": selected_used_rects,
        "m_FreeGlyphRects": [],
        "m_FontWeightTable": [],
    }

    # ------------------------------------------------------------------ #
    #  KR: Material 셰이더 파라미터
    #  EN: Material shader parameters
    # ------------------------------------------------------------------ #

    # KR: _GradientScale = padding + 1 은 TMP SDF 셰이더가 안티앨리어싱 폭을
    #     계산할 때 사용하는 핵심 값. 래스터 모드에서는 1.0 고정.
    # EN: _GradientScale = padding + 1 is the key value used by the TMP SDF shader
    #     to compute anti-aliasing width. Fixed at 1.0 in raster mode.
    gradient_scale = (
        float(atlas_padding + 1) if normalized_render_mode == "sdf" else 1.0
    )
    generated_materials: JsonDict = {
        "m_SavedProperties": {
            "m_Floats": [
                ["_GradientScale", gradient_scale],
                ["_TextureWidth", float(selected_atlas_w)],
                ["_TextureHeight", float(selected_atlas_h)],
            ]
        }
    }
    _emit(
        f"[done] point-size={selected_point_size}, atlas={selected_atlas_w}x{selected_atlas_h}, "
        f"glyphs={len(glyph_table)}, rendermode={normalized_render_mode}"
    )
    return {
        "ttf_data": ttf_data,
        "sdf_data": generated_sdf_data,
        "sdf_data_normalized": _normalize_sdf_payload(generated_sdf_data),
        "sdf_atlas": atlas_image,
        "sdf_materials": generated_materials,
    }


# --------------------------------------------------------------------------- #
#  KR: CLI 인자 파서
#  EN: CLI argument parser
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    """KR: make_sdf 명령줄 인자 파서를 생성한다.
    EN: Create the make_sdf command-line argument parser.
    """
    parser = argparse.ArgumentParser(
        description="Generate TMP-compatible atlas/json from TTF.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--ttf", required=True, help="TTF file path or name.")
    parser.add_argument(
        "--atlas-size",
        default="4096,4096",
        help="Atlas size in 'W,H' format. Default: 4096,4096",
    )
    parser.add_argument(
        "--point-size",
        default="auto",
        help="Sampling point size or 'auto'. Default: auto",
    )
    parser.add_argument(
        "--padding", type=int, default=7, help="Atlas padding. Default: 7"
    )
    parser.add_argument(
        "--charset",
        default="./CharList_Thai.txt",
        help="Charset file path or literal characters (default: CharList_Thai.txt for Thai support).",
    )
    parser.add_argument(
        "--rendermode",
        choices=["sdf", "raster"],
        default="sdf",
        help="Render mode. Default: sdf",
    )
    return parser


# --------------------------------------------------------------------------- #
#  KR: CLI 엔트리포인트
#  EN: CLI entry point
# --------------------------------------------------------------------------- #

def run_make_sdf(argv: list[str] | None = None) -> int:
    """KR: make_sdf CLI를 실행한다.
    종료 코드:
      0 : 성공
      1 : 런타임 오류 (파일 미발견, 생성 실패 등)
      2 : 인자 파싱 오류

    EN: Run the make_sdf CLI.
    Exit codes:
      0 : success
      1 : runtime error (file not found, generation failure, etc.)
      2 : argument parsing error
    """
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        atlas_width, atlas_height = _parse_atlas_size(args.atlas_size)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
        return 2

    try:
        point_size = _parse_point_size(args.point_size)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
        return 2

    if args.padding <= 0:
        parser.error("--padding must be a positive integer.")
        return 2

    try:
        ttf_path = _resolve_ttf_path(args.ttf)
    except FileNotFoundError as exc:
        logger.error("Error: %s", exc)
        return 1

    try:
        charset_text = _load_charset_text(args.charset)
    except FileNotFoundError as exc:
        logger.error("Error: %s", exc)
        return 1

    unicodes = _text_to_unicodes(charset_text)
    if not unicodes:
        logger.error("Error: charset is empty.")
        return 1

    ttf_data = ttf_path.read_bytes()
    render_mode = args.rendermode.lower()
    mode_suffix = "SDF" if render_mode == "sdf" else "Raster"

    generated = generate_sdf_assets_from_ttf(
        ttf_data=ttf_data,
        font_name=ttf_path.stem,
        unicodes=unicodes,
        point_size=point_size,
        atlas_padding=int(args.padding),
        atlas_width=atlas_width,
        atlas_height=atlas_height,
        render_mode=render_mode,
        log_fn=logger.info,
    )
    if generated is None:
        logger.error("Error: failed to generate atlas/json from TTF.")
        return 1

    # KR: 출력 파일 경로: TTF와 같은 디렉토리에 "{FontName} {SDF|Raster}.*" 형식
    # EN: Output file paths: "{FontName} {SDF|Raster}.*" format in the same directory as TTF
    normalized_name = normalize_font_name(ttf_path.stem)
    output_dir = ttf_path.parent
    json_path = output_dir / f"{normalized_name} {mode_suffix}.json"
    atlas_path = output_dir / f"{normalized_name} {mode_suffix} Atlas.png"
    material_path = output_dir / f"{normalized_name} {mode_suffix} Material.json"

    sdf_data = generated.get("sdf_data")
    atlas_image = generated.get("sdf_atlas")
    material_data = generated.get("sdf_materials")
    if not isinstance(sdf_data, dict) or atlas_image is None:
        logger.error("Error: generator returned invalid payload.")
        return 1

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(sdf_data, f, indent=4, ensure_ascii=False)
    atlas_image.save(atlas_path)

    if isinstance(material_data, dict):
        with open(material_path, "w", encoding="utf-8") as f:
            json.dump(material_data, f, indent=4, ensure_ascii=False)

    logger.info("Generated: %s", json_path)
    logger.info("Generated: %s", atlas_path)
    if material_path.exists():
        logger.info("Generated: %s", material_path)
    logger.info(
        "Summary: "
        f"glyphs={len(sdf_data.get('m_GlyphTable', []))}, "
        f"chars={len(sdf_data.get('m_CharacterTable', []))}, "
        f"atlas={int(sdf_data.get('m_AtlasWidth', 0))}x{int(sdf_data.get('m_AtlasHeight', 0))}, "
        f"padding={int(sdf_data.get('m_AtlasPadding', 0))}, "
        f"rendermode={render_mode}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(run_make_sdf())
