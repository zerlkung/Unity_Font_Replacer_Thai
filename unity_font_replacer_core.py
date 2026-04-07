"""KR: Unity 폰트 교체를 위한 핵심 CLI 및 처리 파이프라인.
이 모듈은 Unity 폰트 에셋의 스캔, 파싱, 교체, 프리뷰 내보내기,
    PS5 swizzle/unswizzle 지원 기능을 포함합니다.
주요 기능:
      - TTF 바이너리 교체: 기존 폰트 파일의 바이너리 데이터를 새 폰트로 대체
      - TMP SDF 폰트 데이터 변환: 구 스키마(old)와 신 스키마(new) 간 양방향 변환
      - 아틀라스 텍스처 교체: SDF 아틀라스 이미지 데이터 교체
      - 머티리얼 패칭: TMP 머티리얼의 셰이더 프로퍼티 패딩/스타일 보정
      - PS5 swizzle/unswizzle: PlayStation 5 텍스처 메모리 레이아웃 처리
      - 프리뷰 내보내기: 교체 전후 미리보기 이미지 출력
TMP 스키마 경계:
      - 구 스키마 (old): Unity <=2018.3.14, m_glyphInfoList 사용, top-origin Y 좌표계
      - 신 스키마 (new): Unity >=2018.4.2, m_GlyphTable 사용, bottom-origin Y 좌표계

EN: Core CLI and processing pipeline for Unity font replacement.
This module includes scanning, parsing, replacement, preview export,
    and PS5 swizzle/unswizzle support for Unity font assets.
Key features:
      - TTF binary replacement: replace binary data of existing font files with new fonts
      - TMP SDF font data conversion: bidirectional conversion between old and new schemas
      - Atlas texture replacement: replace SDF atlas image data
      - Material patching: padding/style correction of TMP material shader properties
      - PS5 swizzle/unswizzle: PlayStation 5 texture memory layout handling
      - Preview export: output before/after preview images
TMP schema boundaries:
      - Old schema: Unity <=2018.3.14, uses m_glyphInfoList, top-origin Y coordinates
      - New schema: Unity >=2018.4.2, uses m_GlyphTable, bottom-origin Y coordinates
"""

from __future__ import annotations

import argparse
import atexit
import gc
import inspect
import json
import logging
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import traceback as tb_module
import copy
import struct as struct_module
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Literal, NoReturn, cast

import UnityPy
from PIL import Image, ImageOps
from UnityPy.enums.BundleFile import CompressionFlags
from UnityPy.files.SerializedFile import SerializedType
from UnityPy.helpers import CompressionHelper
from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator
try:
    from UnityPy.enums import TextureFormat as _UnityTextureFormatEnum
except Exception:  # pragma: no cover - KR: 런타임에서 선택적으로 사용 / EN: optionally used at runtime
    _UnityTextureFormatEnum = None

try:
    import texture2ddecoder
except Exception:  # pragma: no cover - KR: 선택적 의존성 / EN: optional dependency
    texture2ddecoder = None

logger = logging.getLogger(__name__)


Language = Literal["ko", "en"]
JsonDict = dict[str, Any]
# KR: 등록된 임시 디렉토리 집합 (프로세스 종료 시 정리용)
# EN: Set of registered temp directories (cleaned up on process exit)
_REGISTERED_TEMP_DIRS: set[str] = set()
# KR: 텍스처 자동 분할 기준값: 단일 텍스처가 이 바이트 수를 초과하면 원샷 분할 적용
# EN: Auto-split threshold: apply one-shot split when a single texture exceeds this byte count
_AUTO_SPLIT_ONESHOT_TEXTURE_BYTES = 1536 * 1024 * 1024
# KR: 텍스처 배치 분할 목표 바이트 수
# EN: Texture batch split target byte count
_AUTO_SPLIT_TEXTURE_BATCH_TARGET_BYTES = 768 * 1024 * 1024
# KR: PS5 swizzle 비트 마스크: X축 인터리브 패턴
# EN: PS5 swizzle bit mask: X-axis interleave pattern
PS5_SWIZZLE_MASK_X = 0x385F0
# KR: PS5 swizzle 비트 마스크: Y축 인터리브 패턴
# EN: PS5 swizzle bit mask: Y-axis interleave pattern
PS5_SWIZZLE_MASK_Y = 0x07A0F
# KR: PS5 텍스처 회전 각도 (도)
# EN: PS5 texture rotation angle (degrees)
PS5_SWIZZLE_ROTATE = 90

# KR: PS5 텍스처 레이아웃 메타데이터.
#     word0: 첫 번째 qword 필드 (예: DXT1/BC1의 경우 0x1d0000000a).
#     block_pack: 블록당 바이트 수, 너비, 높이, 깊이를 하나의 정수로 패킹한 값.
# EN: PS5 texture layout metadata.
#     word0: first qword field (e.g. 0x1d0000000a for DXT1/BC1).
#     block_pack: packed integer of bytes per block, width, height, depth.
_PS5_LAYOUT_FORMAT_META: dict[int, dict[str, Any]] = {
    4: {"label": "R8B8G8A8", "word0": 0x00000004, "block_pack": 0x1010104},
    10: {"label": "DXT1|BC1", "word0": 0x1D0000000A, "block_pack": 0x1040408},
    12: {"label": "DXT5|BC3", "word0": 0x1D0000000C, "block_pack": 0x1040410},
    24: {"label": "BC6H", "word0": 0x1D00000018, "block_pack": 0x1040410},
    25: {"label": "BC7", "word0": 0x1D00000019, "block_pack": 0x1040410},
    26: {"label": "BC4", "word0": 0x1D0000001A, "block_pack": 0x1040408},
    27: {"label": "BC5", "word0": 0x1D0000001B, "block_pack": 0x1040410},
}

# KR: 런타임 레이아웃 선택 로직에서 사용하는 추가 포맷 플래그.
#     `layout_shift`는 다음과 같이 계산됨: ((flags & 0x6) * 2) + 8.
# EN: Additional format flags used in runtime layout selection logic.
#     `layout_shift` is computed as: ((flags & 0x6) * 2) + 8.
_PS5_LAYOUT_FORMAT_FLAGS: dict[int, int] = {
    10: 0x0024,  # DXT1|BC1
    12: 0x0000,  # DXT5|BC3
    24: 0x008C,  # BC6H
    25: 0x0094,  # BC7
    26: 0x00A4,  # BC4
    27: 0x0084,  # BC5
}
# KR: 각 텍스처 포맷에 대응하는 BC 디코더 함수명 매핑
# EN: BC decoder function name mapping for each texture format
_PS5_BC_DECODER_BY_FORMAT: dict[int, str] = {
    10: "decode_bc1",
    12: "decode_bc3",
    24: "decode_bc6",
    25: "decode_bc7",
    26: "decode_bc4",
    27: "decode_bc5",
}


def _ps5_unpack_block_pack(block_pack: int) -> tuple[int, int, int, int]:
    """KR: block_pack 정수에서 블록당 바이트 수, 블록 너비, 블록 높이, 깊이를 추출한다.
    매개변수:
        block_pack: 4바이트로 패킹된 정수 (하위부터 bytes_per_block, block_w, block_h, depth)
    반환값:
        (bytes_per_block, block_w, block_h, depth) 튜플

    EN: Extract bytes per block, block width, block height, and depth from a block_pack integer.
    Args:
        block_pack: 4바이트로 패킹된 정수 (하위부터 bytes_per_block, block_w, block_h, depth)
    Returns:
        (bytes_per_block, block_w, block_h, depth) 튜플
    """
    packed = int(block_pack) & 0xFFFFFFFF
    bytes_per_block = packed & 0xFF
    block_w = (packed >> 8) & 0xFF
    block_h = (packed >> 16) & 0xFF
    depth = (packed >> 24) & 0xFF
    return bytes_per_block, block_w, block_h, depth


def _ps5_build_bc_formats_from_layout_meta() -> dict[int, tuple[int, int, int, str]]:
    """KR: 레이아웃 메타데이터로부터 BC 포맷 정보 딕셔너리를 구성한다.
    반환값:
        {텍스처포맷ID: (블록너비, 블록높이, 블록당바이트, 디코더함수명)} 딕셔너리

    EN: Build a BC format info dictionary from layout metadata.
    Returns:
        {텍스처포맷ID: (블록너비, 블록높이, 블록당바이트, 디코더함수명)} 딕셔너리
    """
    out: dict[int, tuple[int, int, int, str]] = {}
    for texture_format, decoder_name in _PS5_BC_DECODER_BY_FORMAT.items():
        meta = _PS5_LAYOUT_FORMAT_META.get(int(texture_format))
        if not meta:
            continue
        bpb, bw, bh, depth = _ps5_unpack_block_pack(int(meta["block_pack"]))
        if depth != 1 or bpb <= 0 or bw <= 0 or bh <= 0:
            continue
        out[int(texture_format)] = (bw, bh, bpb, decoder_name)
    return out


# KR: BC 포맷 테이블: 레이아웃 메타에서 빌드된 {포맷ID: (블록W, 블록H, 블록바이트, 디코더명)}
# EN: BC format table: built from layout meta {formatID: (blockW, blockH, blockBytes, decoderName)}
_PS5_BC_FORMATS: dict[int, tuple[int, int, int, str]] = _ps5_build_bc_formats_from_layout_meta()

# KR: Addrlib v2 (GFX10+) swizzle 모드 상수 (PS5에서 사용)
# EN: Addrlib v2 (GFX10+) swizzle mode constants (used on PS5)
_PS5_ADDR_SW_256B_S = 1    # KR: 256바이트 표준 / EN: 256-byte standard
_PS5_ADDR_SW_256B_D = 2    # KR: 256바이트 디스플레이 / EN: 256-byte display
_PS5_ADDR_SW_4KB_S = 5     # KR: 4KB 표준 / EN: 4KB standard
_PS5_ADDR_SW_4KB_D = 6     # KR: 4KB 디스플레이 / EN: 4KB display
_PS5_ADDR_SW_64KB_S = 9    # KR: 64KB 표준 / EN: 64KB standard
_PS5_ADDR_SW_64KB_D = 10   # KR: 64KB 디스플레이 / EN: 64KB display
_PS5_ADDR_SW_4KB_S_X = 21  # KR: 4KB 표준 확장 / EN: 4KB standard extended
_PS5_ADDR_SW_4KB_D_X = 22  # KR: 4KB 디스플레이 확장 / EN: 4KB display extended
_PS5_ADDR_SW_64KB_S_X = 25 # KR: 64KB 표준 확장 / EN: 64KB standard extended
_PS5_ADDR_SW_64KB_D_X = 26 # KR: 64KB 디스플레이 확장 / EN: 64KB display extended

# KR: BC 모드 정보: {모드명: (swizzle모드ID, 패턴정보테이블명, 로그2페이지크기, X확장여부)}
# EN: BC mode info: {modeName: (swizzleModeID, patternInfoTableName, log2PageSize, isXorMode)}
_PS5_BC_MODE_INFO: dict[str, tuple[int, str, int, bool]] = {
    "256B_S": (_PS5_ADDR_SW_256B_S, "GFX10_SW_256_S_PATINFO", 8, False),
    "256B_D": (_PS5_ADDR_SW_256B_D, "GFX10_SW_256_D_PATINFO", 8, False),
    "4KB_S": (_PS5_ADDR_SW_4KB_S, "GFX10_SW_4K_S_PATINFO", 12, False),
    "4KB_D": (_PS5_ADDR_SW_4KB_D, "GFX10_SW_4K_D_PATINFO", 12, False),
    "4KB_S_X": (_PS5_ADDR_SW_4KB_S_X, "GFX10_SW_4K_S_X_PATINFO", 12, True),
    "4KB_D_X": (_PS5_ADDR_SW_4KB_D_X, "GFX10_SW_4K_D_X_PATINFO", 12, True),
    "64KB_S": (_PS5_ADDR_SW_64KB_S, "GFX10_SW_64K_S_PATINFO", 16, False),
    "64KB_D": (_PS5_ADDR_SW_64KB_D, "GFX10_SW_64K_D_PATINFO", 16, False),
    "64KB_S_X": (_PS5_ADDR_SW_64KB_S_X, "GFX10_SW_64K_S_X_PATINFO", 16, True),
    "64KB_D_X": (_PS5_ADDR_SW_64KB_D_X, "GFX10_SW_64K_D_X_PATINFO", 16, True),
}
# KR: 빠른 모드 탐색 순서 (자주 사용되는 모드 우선)
# EN: Fast mode search order (frequently used modes first)
_PS5_BC_FAST_MODE_NAMES = ["4KB_S", "64KB_S", "4KB_D", "256B_S", "64KB_D", "256B_D"]

# KR: Thin 2D 타일 차원 (페이지 클래스별): {블록당바이트: (x비트수, y비트수)}
#     256바이트 페이지 클래스
# EN: Thin 2D tile dimensions (by page class): {bytesPerBlock: (xBits, yBits)}
#     256-byte page class
_PS5_LAYOUT_BLOCK256_2D_BITS: dict[int, tuple[int, int]] = {
    1: (4, 4),
    2: (4, 3),
    4: (3, 3),
    8: (3, 2),
    16: (2, 2),
}
# KR: 4KB 페이지 클래스
# EN: 4KB page class
_PS5_LAYOUT_BLOCK4K_2D_BITS: dict[int, tuple[int, int]] = {
    1: (6, 6),
    2: (6, 5),
    4: (5, 5),
    8: (5, 4),
    16: (4, 4),
}
# KR: 64KB 페이지 클래스
# EN: 64KB page class
_PS5_LAYOUT_BLOCK64K_2D_BITS: dict[int, tuple[int, int]] = {
    1: (8, 8),
    2: (8, 7),
    4: (7, 7),
    8: (7, 6),
    16: (6, 6),
}

# KR: 4KB_S 트리플릿: log2(블록당바이트)별 인덱싱 {블록바이트: (깊이비트, x비트, y비트)}
# EN: 4KB_S triplets: indexed by log2(bytesPerBlock) {blockBytes: (depthBits, xBits, yBits)}
_PS5_4KB_S_TRIPLETS_BY_BLOCK_BYTES: dict[int, tuple[int, int, int]] = {
    1: (0, 6, 6),
    2: (0, 6, 5),
    4: (0, 5, 5),
    8: (0, 5, 4),
    16: (0, 4, 4),
}

# KR: 포맷별 마이크로타일 차원: {블록당바이트: (x비트수, y비트수)}
# EN: Micro-tile dimensions per format: {bytesPerBlock: (xBits, yBits)}
_PS5_MICRO_TILE_BITS: dict[int, tuple[int, int]] = {
    1: (5, 4),  # KR: 32x16 픽셀 / EN: 32x16 pixels
    2: (4, 3),  # KR: 16x8 픽셀 / EN: 16x8 pixels
    3: (4, 3),  # KR: 16x8 픽셀 / EN: 16x8 pixels
    4: (3, 2),  # KR: 8x4 픽셀 / EN: 8x4 pixels
}
_PS5_MICRO_X_BITS_DEFAULT = 5  # KR: 8bpp 기본 X 비트 수 / EN: 8bpp default X bit count
_PS5_MICRO_Y_BITS_DEFAULT = 4  # KR: 8bpp 기본 Y 비트 수 / EN: 8bpp default Y bit count

# KR: 비정사각형 텍스처의 축 전치 여부: {블록당바이트: 전치여부}
# EN: Axis transpose for non-square textures: {bytesPerBlock: shouldTranspose}
_PS5_AXIS_TRANSPOSE: dict[int, bool] = {
    1: True,   # KR: Alpha8: 항상 전치 / EN: Alpha8: always transpose
    2: False,
    3: False,
    4: False,  # KR: RGBA32: 전치 안 함 / EN: RGBA32: no transpose
}


def _ps5_get_micro_tile_bits(bytes_per_element: int = 1) -> tuple[int, int]:
    """KR: 주어진 요소당 바이트 수에 대한 마이크로타일 비트 수를 반환한다.
    매개변수:
        bytes_per_element: 픽셀당 바이트 수 (bpe)
    반환값:
        (x_bits, y_bits) 튜플

    EN: Return micro-tile bit counts for the given bytes per element.
    Args:
        bytes_per_element: 픽셀당 바이트 수 (bpe)
    Returns:
        (x_bits, y_bits) 튜플
    """
    return _PS5_MICRO_TILE_BITS.get(
        bytes_per_element,
        (_PS5_MICRO_X_BITS_DEFAULT, _PS5_MICRO_Y_BITS_DEFAULT),
    )


def _emit_phase_callback(
    phase_callback: Callable[[str, JsonDict], None] | None,
    phase: str,
    **payload: Any,
) -> None:
    """KR: 진행 단계 콜백을 안전하게 호출한다.
    매개변수:
        phase_callback: 호출할 콜백 함수 (None이면 무시)
        phase: 현재 처리 단계 이름
        **payload: 콜백에 전달할 추가 데이터

    EN: Safely invoke a progress phase callback.
    Args:
        phase_callback: 호출할 콜백 함수 (None이면 무시)
        phase: 현재 처리 단계 이름
        **payload: 콜백에 전달할 추가 데이터
    """
    if phase_callback is None:
        return
    try:
        phase_callback(phase, cast(JsonDict, payload))
    except Exception:
        logger.debug("단계 콜백 실패: %s", phase, exc_info=True)
# KR: Unity-Runtime-Libraries reports/sdf_font 분석 기준 경계 버전
#     구 스키마(old)만 지원하는 마지막 버전
# EN: Unity-Runtime-Libraries reports/sdf_font analysis boundary versions
#     Last version supporting only old schema
_TMP_OLD_ONLY_LAST = (2018, 3, 14)
# KR: 신 스키마(new)가 처음 도입된 버전
# EN: First version introducing new schema
_TMP_NEW_SCHEMA_FIRST = (2018, 4, 2)
# KR: TMP 폰트 에셋 생성 설정 키 (버전별로 다른 이름 사용)
# EN: TMP font asset creation settings key (different names per version)
_TMP_CREATION_SETTINGS_KEYS = (
    "m_CreationSettings",
    "m_FontAssetCreationSettings",
    "m_fontAssetCreationEditorSettings",
)
# KR: TMP 더티 플래그 키 (룩업 테이블 재빌드 필요 여부)
# EN: TMP dirty flag key (whether lookup table rebuild is needed)
_TMP_DIRTY_FLAG_KEYS = (
    "m_IsFontAssetLookupTablesDirty",
    "IsFontAssetLookupTablesDirty",
)
# KR: TMP 글리프 인덱스 목록 키
# EN: TMP glyph index list keys
_TMP_GLYPH_INDEX_LIST_KEYS = (
    "m_GlyphIndexList",
    "m_GlyphIndexes",
)
# KR: Unity 에셋 번들 시그니처 문자열 집합
# EN: Unity asset bundle signature string set
BUNDLE_SIGNATURES = {"UnityFS", "UnityWeb", "UnityRaw"}
# KR: 구 스키마 라인 메트릭 키 목록 (TMP <=2018.3.14)
# EN: Old schema line metric key list (TMP <=2018.3.14)
_OLD_LINE_METRIC_KEYS = (
    "LineHeight",
    "Baseline",
    "Ascender",
    "CapHeight",
    "Descender",
    "CenterLine",
    "Scale",
    "SuperscriptOffset",
    "SubscriptOffset",
    "SubSize",
    "Underline",
    "UnderlineThickness",
    "strikethrough",
    "strikethroughThickness",
    "TabWidth",
)
# KR: 구 스키마에서 스케일 보정 대상이 되는 라인 메트릭 키
# EN: Old schema line metric keys subject to scale correction
_OLD_LINE_METRIC_SCALE_KEYS = (
    "LineHeight",
    "Baseline",
    "Ascender",
    "CapHeight",
    "Descender",
    "CenterLine",
    "SuperscriptOffset",
    "SubscriptOffset",
    "Underline",
    "UnderlineThickness",
    "strikethrough",
    "strikethroughThickness",
    "TabWidth",
)
# KR: 신 스키마 라인 메트릭 키 목록 (TMP >=2018.4.2)
# EN: New schema line metric key list (TMP >=2018.4.2)
_NEW_LINE_METRIC_KEYS = (
    "m_LineHeight",
    "m_AscentLine",
    "m_CapLine",
    "m_MeanLine",
    "m_Baseline",
    "m_DescentLine",
    "m_Scale",
    "m_SuperscriptOffset",
    "m_SuperscriptSize",
    "m_SubscriptOffset",
    "m_SubscriptSize",
    "m_UnderlineOffset",
    "m_UnderlineThickness",
    "m_StrikethroughOffset",
    "m_StrikethroughThickness",
    "m_TabWidth",
)
# KR: 신 스키마에서 스케일 보정 대상이 되는 라인 메트릭 키
# EN: New schema line metric keys subject to scale correction
_NEW_LINE_METRIC_SCALE_KEYS = (
    "m_LineHeight",
    "m_AscentLine",
    "m_CapLine",
    "m_MeanLine",
    "m_Baseline",
    "m_DescentLine",
    "m_SuperscriptOffset",
    "m_SubscriptOffset",
    "m_UnderlineOffset",
    "m_UnderlineThickness",
    "m_StrikethroughOffset",
    "m_StrikethroughThickness",
    "m_TabWidth",
)
# KR: 머티리얼 패딩 스케일 키: 아틀라스 크기 변경 시 비례 보정이 필요한 셰이더 프로퍼티
# EN: Material padding scale keys: shader properties needing proportional correction on atlas size change
_MATERIAL_PADDING_SCALE_KEYS = (
    "_GradientScale",
    "_FaceDilate",
    "_OutlineWidth",
    "_OutlineSoftness",
    "_UnderlayDilate",
    "_UnderlaySoftness",
    "_UnderlayOffsetX",
    "_UnderlayOffsetY",
    "_GlowOffset",
    "_GlowInner",
    "_GlowOuter",
)
# KR: 머티리얼 스타일 float 키: 원본 머티리얼의 시각적 스타일을 보존해야 하는 프로퍼티
# EN: Material style float keys: properties that must preserve the original material visual style
_MATERIAL_STYLE_FLOAT_KEYS = (
    "_FaceDilate",
    "_OutlineWidth",
    "_OutlineSoftness",
    "_UnderlayDilate",
    "_UnderlaySoftness",
    "_UnderlayOffsetX",
    "_UnderlayOffsetY",
    "_GlowOffset",
    "_GlowInner",
    "_GlowOuter",
    "_ScaleRatioA",
    "_ScaleRatioB",
    "_ScaleRatioC",
)
# KR: 머티리얼 스타일에서 패딩 스케일 보정이 필요한 키
# EN: Material style keys needing padding scale correction
_MATERIAL_STYLE_PADDING_SCALE_KEYS = (
    "_FaceDilate",
    "_OutlineWidth",
    "_OutlineSoftness",
    "_UnderlayDilate",
    "_UnderlaySoftness",
    "_UnderlayOffsetX",
    "_UnderlayOffsetY",
    "_GlowOffset",
    "_GlowInner",
    "_GlowOuter",
)
# KR: 머티리얼 스타일 색상 키: 원본에서 보존해야 하는 색상 프로퍼티
# EN: Material style color keys: color properties to preserve from original
_MATERIAL_STYLE_COLOR_KEYS = (
    "_FaceColor",
    "_OutlineColor",
    "_UnderlayColor",
    "_GlowColor",
)
# KR: 외곽선 비율 보정 대상 키
# EN: Outline ratio correction target keys
_MATERIAL_OUTLINE_RATIO_KEYS = (
    "_OutlineWidth",
    "_OutlineSoftness",
)
# KR: 로그 포맷 상수
# EN: Log format constants
LOG_CONSOLE_FORMAT = "%(message)s"  # KR: 콘솔 출력 포맷 (메시지만) / EN: Console output format (message only)
LOG_FILE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"  # KR: 파일 로그 포맷 / EN: File log format
LOG_FILE_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"  # KR: 파일 로그 날짜 포맷 / EN: File log date format
VERBOSE_LOG_FILENAME = "verbose.txt"  # KR: 상세 로그 파일명 / EN: Verbose log filename


def _compose_log_message(*parts: object, sep: str = " ") -> str:
    """KR: 로그 파트들을 하나의 문자열로 합친다.
    매개변수:
        *parts: 로그 메시지를 구성하는 각 부분
        sep: 구분자 (기본: 공백)
    반환값:
        합쳐진 로그 메시지 문자열

    EN: Combine log parts into a single string.
    Args:
        *parts: 로그 메시지를 구성하는 각 부분
        sep: 구분자 (기본: 공백)
    Returns:
        합쳐진 로그 메시지 문자열
    """
    return sep.join(str(part) for part in parts)


def _configure_logging(
    console_level: int = logging.INFO,
    verbose_log_path: str | None = None,
) -> None:
    """KR: 콘솔 및 선택적 파일 로그 핸들러를 구성한다.
    매개변수:
        console_level: 콘솔 출력 로그 레벨 (기본: INFO)
        verbose_log_path: 상세 로그 파일 경로 (None이면 파일 로그 비활성화)

    EN: Configure console and optional file log handlers.
    Args:
        console_level: 콘솔 출력 로그 레벨 (기본: INFO)
        verbose_log_path: 상세 로그 파일 경로 (None이면 파일 로그 비활성화)
    """
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.DEBUG if verbose_log_path else console_level)

    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(logging.Formatter(LOG_CONSOLE_FORMAT))
    root_logger.addHandler(console_handler)

    if verbose_log_path:
        file_handler = logging.FileHandler(
            verbose_log_path,
            mode="w",
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(LOG_FILE_FORMAT, datefmt=LOG_FILE_DATE_FORMAT)
        )
        root_logger.addHandler(file_handler)


def _coerce_log_level(message: str, default_level: int = logging.INFO) -> int:
    """KR: 지역화된 메시지 접두사로부터 로그 레벨을 추론한다.
    매개변수:
        message: 로그 메시지 문자열
        default_level: 추론 실패 시 기본 레벨 (기본: INFO)
    반환값:
        추론된 로그 레벨 정수

    EN: Infer log level from localized message prefix.
    Args:
        message: 로그 메시지 문자열
        default_level: 추론 실패 시 기본 레벨 (기본: INFO)
    Returns:
        추론된 로그 레벨 정수
    """
    lowered = message.lower()
    if "경고" in message or "warning" in lowered:
        return logging.WARNING
    if (
        "오류" in message
        or "error" in lowered
        or "failed" in lowered
        or "실패" in message
    ):
        return logging.ERROR
    return default_level


def _log_console(
    *parts: object,
    sep: str = " ",
    level: int | None = None,
    include_traceback: bool = False,
) -> None:
    """KR: 레거시 호출 지점에서 사용하는 print 호환 로깅 브리지.
    매개변수:
        *parts: 로그 메시지 부분들
        sep: 구분자
        level: 로그 레벨 (None이면 메시지 내용에서 자동 추론)
        include_traceback: True이면 예외 Traceback 포함

    EN: Print-compatible logging bridge used at legacy call sites.
    Args:
        *parts: 로그 메시지 부분들
        sep: 구분자
        level: 로그 레벨 (None이면 메시지 내용에서 자동 추론)
        include_traceback: True이면 예외 Traceback 포함
    """
    message = _compose_log_message(*parts, sep=sep)
    resolved_level = _coerce_log_level(message) if level is None else level
    if include_traceback:
        logger.log(resolved_level, message, exc_info=True)
        return
    logger.log(resolved_level, message)


def _log_debug(*parts: object, sep: str = " ") -> None:
    """KR: 디버그 레벨 로그를 기록한다.
    EN: Record a debug-level log entry.
    """
    logger.debug(_compose_log_message(*parts, sep=sep))


def _log_info(*parts: object, sep: str = " ") -> None:
    """KR: 정보 레벨 로그를 기록한다.
    EN: Record an info-level log entry.
    """
    logger.info(_compose_log_message(*parts, sep=sep))


def _log_warning(*parts: object, sep: str = " ") -> None:
    """KR: 경고 레벨 로그를 기록한다.
    EN: Record a warning-level log entry.
    """
    logger.warning(_compose_log_message(*parts, sep=sep))


def _log_error(*parts: object, sep: str = " ") -> None:
    """KR: 오류 레벨 로그를 기록한다.
    EN: Record an error-level log entry.
    """
    logger.error(_compose_log_message(*parts, sep=sep))


def _log_exception(*parts: object, sep: str = " ") -> None:
    """KR: 예외 Traceback을 포함한 에러 로그를 기록한다.
    EN: Record an error log entry including exception traceback.
    """
    logger.exception(_compose_log_message(*parts, sep=sep))


@lru_cache(maxsize=64)
def compute_ps5_swizzle_masks(
    width: int, height: int, bytes_per_element: int = 1,
) -> tuple[int, int]:
    """KR: 텍스처 크기에 맞는 PS5 swizzle 비트 마스크를 계산한다.
    PS5 텍스처 메모리는 타일 기반 swizzle 레이아웃을 사용한다.
    마이크로타일 크기는 요소당 바이트 수(bpe)에 따라 결정된다:
      bpe=1 -> 32x16, bpe=4 -> 8x4 등.
    매크로타일 비트는 마이크로타일 비트 위에 다음 순서로 인터리브된다:
      첫 번째 Y, 첫 번째 X, 나머지 Y..., 나머지 X...
    매개변수:
        width: 텍스처 너비 (2의 거듭제곱이어야 함)
        height: 텍스처 높이 (2의 거듭제곱이어야 함)
        bytes_per_element: 픽셀당 바이트 수 (기본: 1)
    반환값:
        (mask_x, mask_y) swizzle 비트 마스크 튜플
    예외:
        ValueError: 유효하지 않은 차원이거나 2의 거듭제곱이 아닌 경우

    EN: Compute PS5 swizzle bit masks for the given texture dimensions.
    PS5 텍스처 메모리는 타일 기반 swizzle 레이아웃을 사용한다.
    마이크로타일 크기는 요소당 바이트 수(bpe)에 따라 결정된다:
      bpe=1 -> 32x16, bpe=4 -> 8x4 등.
    매크로타일 비트는 마이크로타일 비트 위에 다음 순서로 인터리브된다:
      첫 번째 Y, 첫 번째 X, 나머지 Y..., 나머지 X...
    Args:
        width: 텍스처 너비 (2의 거듭제곱이어야 함)
        height: 텍스처 높이 (2의 거듭제곱이어야 함)
        bytes_per_element: 픽셀당 바이트 수 (기본: 1)
    Returns:
        (mask_x, mask_y) swizzle 비트 마스크 튜플
    Raises:
        ValueError: 유효하지 않은 차원이거나 2의 거듭제곱이 아닌 경우
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"PS5 swizzle 마스크에 유효하지 않은 크기: {width}x{height}")
    if width & (width - 1) or height & (height - 1):
        raise ValueError(
            f"PS5 swizzle에는 2의 거듭제곱 크기가 필요합니다: {width}x{height}"
        )
    micro_x_bits, micro_y_bits = _ps5_get_micro_tile_bits(bytes_per_element)
    micro_w = 1 << micro_x_bits
    micro_h = 1 << micro_y_bits
    if width < micro_w or height < micro_h:
        raise ValueError(
            f"PS5 swizzle 마이크로타일({micro_w}x{micro_h})보다 작은 텍스처: "
            f"{width}x{height}"
        )
    total_x = width.bit_length() - 1   # log2(width)
    total_y = height.bit_length() - 1   # log2(height)
    macro_x = total_x - micro_x_bits    # KR: 매크로타일 X 비트 수 / EN: Number of macro-tile X bits
    macro_y = total_y - micro_y_bits     # KR: 매크로타일 Y 비트 수 / EN: Number of macro-tile Y bits

    mask_x = 0
    mask_y = 0
    pos = 0
    # KR: 마이크로타일 Y 비트 (최하위 비트부터 배치)
    # EN: Micro-tile Y bits (placed from least significant bit)
    for _ in range(micro_y_bits):
        mask_y |= 1 << pos
        pos += 1
    # KR: 마이크로타일 X 비트
    # EN: Micro-tile X bits
    for _ in range(micro_x_bits):
        mask_x |= 1 << pos
        pos += 1
    # KR: 매크로: 첫 번째 Y 비트
    # EN: Macro: first Y bit
    mx_rem = macro_x
    my_rem = macro_y
    if my_rem > 0:
        mask_y |= 1 << pos
        pos += 1
        my_rem -= 1
    # KR: 매크로: 첫 번째 X 비트
    # EN: Macro: first X bit
    if mx_rem > 0:
        mask_x |= 1 << pos
        pos += 1
        mx_rem -= 1
    # KR: 매크로: 나머지 Y 비트
    # EN: Macro: remaining Y bits
    for _ in range(my_rem):
        mask_y |= 1 << pos
        pos += 1
    # KR: 매크로: 나머지 X 비트
    # EN: Macro: remaining X bits
    for _ in range(mx_rem):
        mask_x |= 1 << pos
        pos += 1
    return mask_x, mask_y


def _ps5_dimensions_supported(width: int, height: int, bytes_per_element: int = 1) -> bool:
    """KR: 주어진 텍스처 크기가 PS5 swizzle을 지원하는지 확인한다.
    매개변수:
        width: 텍스처 너비
        height: 텍스처 높이
        bytes_per_element: 픽셀당 바이트 수
    반환값:
        지원 가능하면 True, 아니면 False

    EN: Check whether the given texture dimensions support PS5 swizzle.
    Args:
        width: 텍스처 너비
        height: 텍스처 높이
        bytes_per_element: 픽셀당 바이트 수
    Returns:
        지원 가능하면 True, 아니면 False
    """
    if width <= 0 or height <= 0:
        return False
    if width & (width - 1) or height & (height - 1):
        return False
    xbits, ybits = _ps5_get_micro_tile_bits(bytes_per_element)
    micro_w = 1 << xbits
    micro_h = 1 << ybits
    return width >= micro_w and height >= micro_h


def _ps5_is_power_of_two(value: int) -> bool:
    """KR: 값이 2의 거듭제곱인지 확인한다.
    EN: Check if a value is a power of two.
    """
    return value > 0 and (value & (value - 1)) == 0


def _ps5_iter_divisor_pairs(total: int) -> Iterable[tuple[int, int]]:
    """KR: 주어진 정수의 모든 약수 쌍 (d, total/d)을 반복한다.
    매개변수:
        total: 약수를 구할 양의 정수
    반환값:
        (약수, 몲) 튜플의 반복자

    EN: Iterate over all divisor pairs (d, total/d) of a given integer.
    Args:
        total: 약수를 구할 양의 정수
    Returns:
        (약수, 몲) 튜플의 반복자
    """
    if total <= 0:
        return
    root = int(math.isqrt(total))
    for d in range(1, root + 1):
        if (total % d) != 0:
            continue
        q = total // d
        yield d, q
        if d != q:
            yield q, d


def _ps5_infer_physical_grid(
    total_elements: int,
    logical_width: int,
    logical_height: int,
    *,
    align_width: int,
    align_height: int,
) -> tuple[int, int]:
    """KR: 원시 요소 수로부터 유력한 물리적 그리드 크기를 추론한다.
    PS5 런타임은 표면(특히 BC 텍스처)을 논리적 크기 이상으로 패딩하는 경우가 많다.
    약수 쌍을 탐색하고 정렬/패딩 점수를 매겨 가장 적합한 물리적 WxH를 추론한다.
    매개변수:
        total_elements: 총 요소(픽셀/블록) 수
        logical_width: 논리적 텍스처 너비
        logical_height: 논리적 텍스처 높이
        align_width: 너비 정렬 단위
        align_height: 높이 정렬 단위
    반환값:
        (물리적너비, 물리적높이) 튜플

    EN: Infer the likely physical grid size from the raw element count.
    PS5 런타임은 표면(특히 BC 텍스처)을 논리적 크기 이상으로 패딩하는 경우가 많다.
    약수 쌍을 탐색하고 정렬/패딩 점수를 매겨 가장 적합한 물리적 WxH를 추론한다.
    Args:
        total_elements: 총 요소(픽셀/블록) 수
        logical_width: 논리적 텍스처 너비
        logical_height: 논리적 텍스처 높이
        align_width: 너비 정렬 단위
        align_height: 높이 정렬 단위
    Returns:
        (물리적너비, 물리적높이) 튜플
    """
    logical_total = max(0, logical_width) * max(0, logical_height)
    if (
        total_elements <= 0
        or logical_width <= 0
        or logical_height <= 0
        or total_elements < logical_total
    ):
        return logical_width, logical_height
    if total_elements == logical_total:
        return logical_width, logical_height

    best_pair: tuple[int, int] | None = None
    best_score: int | None = None

    for cand_w, cand_h in _ps5_iter_divisor_pairs(total_elements):
        if cand_w < logical_width or cand_h < logical_height:
            continue

        pad_w = cand_w - logical_width
        pad_h = cand_h - logical_height
        pad_area = (cand_w * cand_h) - logical_total

        # KR: 최소 여분 면적을 선호하고, 그 다음 높이 패딩보다 너비 패딩을 선호
        # EN: Prefer minimal excess area, then prefer width padding over height padding
        score = pad_area * 1000 + pad_h * 32 + pad_w * 4
        if align_width > 1 and (cand_w % align_width) != 0:
            score += 250  # KR: 너비 정렬 미달 페널티 / EN: Width alignment miss penalty
        if align_height > 1 and (cand_h % align_height) != 0:
            score += 250  # KR: 높이 정렬 미달 페널티 / EN: Height alignment miss penalty
        if _ps5_is_power_of_two(cand_w):
            score -= 32   # KR: 2의 거듭제곱 너비 보너스 / EN: Power-of-two width bonus
        if _ps5_is_power_of_two(cand_h):
            score -= 16   # KR: 2의 거듭제곱 높이 보너스 / EN: Power-of-two height bonus

        if best_score is None or score < best_score:
            best_score = score
            best_pair = (cand_w, cand_h)

    return best_pair if best_pair is not None else (logical_width, logical_height)


def _ps5_align_up(value: int, align: int) -> int:
    """KR: 값을 지정된 정렬 단위로 올림 정렬한다.
    EN: Align a value up to the specified alignment unit.
    """
    if align <= 1:
        return int(value)
    return ((int(value) + int(align) - 1) // int(align)) * int(align)


def _ps5_physical_grid_candidates_for_mode(
    total_elements: int,
    logical_width: int,
    logical_height: int,
    *,
    bytes_per_block: int,
    mode_name: str,
    align_width: int,
    align_height: int,
) -> list[tuple[int, int]]:
    """KR: 주어진 BC 스위즐 모드에 대한 물리 그리드 후보를 우선순위 순서로 반환한다.
    EN: Return physical grid candidates in priority order for a given BC swizzle mode.

    순서:
    1) 레이아웃 정렬된 후보,
    2) 약수 기반 추론 후보,
    3) 논리 그리드.
    """
    out: list[tuple[int, int]] = []

    def _push(pair: tuple[int, int]) -> None:
        # KR: 유효하지 않은 후보 필터링: 양수, 논리 크기 이상, 총 요소 수 이하
        # EN: Filter out invalid candidates: positive, at least logical size, within total elements
        if pair[0] <= 0 or pair[1] <= 0:
            return
        if pair[0] < logical_width or pair[1] < logical_height:
            return
        if pair[0] * pair[1] > total_elements:
            return
        if pair not in out:
            out.append(pair)

    # KR: 타일 비트 차원에서 정렬된 후보 계산
    # EN: Compute aligned candidates from tile bit dimensions
    bits = _ps5_tile_bit_dimensions_for_mode(mode_name, bytes_per_block)
    if bits is not None:
        tile_w = 1 << bits[0]  # KR: 타일 너비 (2의 거듭제곱) / EN: Tile width (power of two)
        tile_h = 1 << bits[1]  # KR: 타일 높이 (2의 거듭제곱) / EN: Tile height (power of two)
        aligned_w = _ps5_align_up(logical_width, tile_w)
        aligned_h = _ps5_align_up(logical_height, tile_h)
        _push((aligned_w, aligned_h))
        # KR: 정렬된 너비로 총 요소 수를 나누어 높이 후보 추론
        # EN: Infer height candidate by dividing total elements by aligned width
        if aligned_w > 0 and (total_elements % aligned_w) == 0:
            aligned_h_from_total = total_elements // aligned_w
            if (
                aligned_h_from_total >= aligned_h
                and (aligned_h_from_total % tile_h) == 0
            ):
                _push((aligned_w, aligned_h_from_total))
        # KR: 정렬된 높이로 총 요소 수를 나누어 너비 후보 추론
        # EN: Infer width candidate by dividing total elements by aligned height
        if aligned_h > 0 and (total_elements % aligned_h) == 0:
            aligned_w_from_total = total_elements // aligned_h
            if (
                aligned_w_from_total >= aligned_w
                and (aligned_w_from_total % tile_w) == 0
            ):
                _push((aligned_w_from_total, aligned_h))

    # KR: 약수 기반 물리 그리드 추론 결과 추가
    # EN: Add divisor-based physical grid inference result
    inferred = _ps5_infer_physical_grid(
        total_elements,
        logical_width,
        logical_height,
        align_width=align_width,
        align_height=align_height,
    )
    _push(inferred)
    # KR: 최종 폴백: 논리 그리드 자체
    # EN: Final fallback: the logical grid itself
    _push((logical_width, logical_height))
    return out


def _ps5_read_lines(path: Path) -> list[str]:
    """KR: 파일을 UTF-8로 읽어 줄 단위 리스트로 반환한다.
    EN: Read a file as UTF-8 and return a list of lines.
    """
    return path.read_text(encoding="utf-8").splitlines()


def _ps5_extract_block(lines: list[str], decl_prefix: str) -> list[str]:
    """KR: C 헤더 파일에서 선언 접두사로 시작하는 블록의 본문 줄들을 추출한다.
    EN: Extract body lines of a block starting with a declaration prefix from a C header file.
    """
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith(decl_prefix):
            # KR: 선언부 다음 줄부터 본문 시작
            # EN: Body starts from the line after the declaration
            start = i + 1
            break
    if start is None:
        raise RuntimeError(f"Declaration not found: {decl_prefix}")
    out: list[str] = []
    for line in lines[start:]:
        # KR: 닫는 중괄호를 만나면 블록 종료
        # EN: Block ends at closing brace
        if line.strip().startswith("};"):
            break
        out.append(line)
    return out


def _ps5_expr_to_mask(expr: str) -> int:
    """KR: 스위즐 패턴 수식 문자열(예: "X0^Y1^Z2")을 64비트 마스크 정수로 변환한다.
    EN: Convert a swizzle pattern expression string (e.g. "X0^Y1^Z2") to a 64-bit mask integer.

    각 채널(X/Y/Z/S)은 16비트 영역을 차지하며, XOR로 결합된다.
    """
    expr = expr.strip()
    if expr == "0":
        return 0
    total = 0
    # KR: "^"로 분리된 각 토큰을 파싱하여 비트 마스크에 XOR 누적
    # EN: Parse each token separated by '^' and XOR-accumulate into the bit mask
    for part in expr.split("^"):
        token = part.strip()
        if not token:
            continue
        ch = token[0]  # KR: 채널 문자: X, Y, Z, S / EN: Channel character: X, Y, Z, S
        if ch not in "XYZS" or not token[1:].isdigit():
            raise RuntimeError(f"Unexpected token in swizzle expression: {token}")
        idx = int(token[1:])  # KR: 비트 인덱스 (0~15) / EN: Bit index (0~15)
        if idx < 0 or idx > 15:
            raise RuntimeError(f"Token bit out of range: {token}")
        chan = "XYZS".index(ch)  # KR: 채널 오프셋 (X=0, Y=1, Z=2, S=3) / EN: Channel offset (X=0, Y=1, Z=2, S=3)
        # KR: 채널별 16비트 슬롯 내의 해당 비트를 설정
        # EN: Set the corresponding bit within the channel's 16-bit slot
        total ^= 1 << (chan * 16 + idx)
    return total


def _ps5_parse_nibble_array(
    lines: list[str], name: str, row_width: int
) -> list[list[int]]:
    """KR: gfx10SwizzlePattern.h에서 니블(nibble) 배열을 파싱하여 마스크 행렬로 반환한다.
    EN: Parse a nibble array from gfx10SwizzlePattern.h and return it as a mask matrix.
    """
    block = _ps5_extract_block(lines, f"const UINT_64 {name}")
    rows: list[list[int]] = []
    for line in block:
        if "{" not in line:
            continue
        # KR: 중괄호 내부의 수식 항목들을 추출
        # EN: Extract expression items inside braces
        body = line.split("{", 1)[1].split("}", 1)[0]
        items = [x.strip() for x in body.split(",") if x.strip()]
        if len(items) < row_width:
            continue
        # KR: 각 수식을 64비트 마스크로 변환
        # EN: Convert each expression to a 64-bit mask
        rows.append([_ps5_expr_to_mask(items[i]) for i in range(row_width)])
    return rows


def _ps5_parse_patinfo_array(
    lines: list[str], name: str
) -> list[tuple[int, int, int, int, int]]:
    """KR: ADDR_SW_PATINFO 배열을 파싱하여 (maxItemCount, idx01, idx2, idx3, idx4) 튜플 리스트로 반환한다.
    EN: Parse an ADDR_SW_PATINFO array and return a list of (maxItemCount, idx01, idx2, idx3, idx4) tuples.
    """
    block = _ps5_extract_block(lines, f"const ADDR_SW_PATINFO {name}")
    rows: list[tuple[int, int, int, int, int]] = []
    # KR: 5개 정수 필드를 가진 구조체 초기화 패턴 매칭
    # EN: Pattern matching for struct initializer with 5 integer fields
    pat = re.compile(
        r"\{\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,?\s*\}"
    )
    for line in block:
        m = pat.search(line)
        if m:
            rows.append(tuple(int(m.group(i)) for i in range(1, 6)))
    return rows


@lru_cache(maxsize=1)
def _ps5_resolve_swizzle_pattern_path() -> str | None:
    """KR: gfx10SwizzlePattern.h 파일 경로를 환경변수 또는 기본 위치에서 탐색하여 반환한다.
    EN: Search for the gfx10SwizzlePattern.h file path from environment variable or default locations.
    """
    # KR: 환경변수 PS5_SWIZZLE_PATTERN_H를 우선 확인
    # EN: Check PS5_SWIZZLE_PATTERN_H environment variable first
    env_path = os.environ.get("PS5_SWIZZLE_PATTERN_H")
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path))
    # KR: 리포지토리 내부의 기본 Addrlib 헤더 경로
    # EN: Default Addrlib header path inside the repository
    repo_root = Path(__file__).resolve().parent
    candidates.append(
        repo_root
        / "TMP_Info"
        / "method1"
        / "pal"
        / "src"
        / "core"
        / "imported"
        / "addrlib"
        / "src"
        / "gfx10"
        / "gfx10SwizzlePattern.h"
    )
    # KR: 후보 경로 중 존재하는 첫 번째 파일 반환
    # EN: Return the first existing file among candidates
    for path in candidates:
        if path.exists():
            return str(path)
    return None


@lru_cache(maxsize=1)
def _ps5_load_bc_pattern_tables() -> dict[str, Any] | None:
    """KR: gfx10SwizzlePattern.h에서 모든 니블 및 패턴 정보 테이블을 로드한다.
    EN: Load all nibble and pattern info tables from gfx10SwizzlePattern.h.

    파일이 없거나 파싱 실패 시 None을 반환한다.
    """
    pattern_path = _ps5_resolve_swizzle_pattern_path()
    if not pattern_path:
        return None
    try:
        lines = _ps5_read_lines(Path(pattern_path))
        # KR: 니블 배열 파싱 (nib01은 8열, nib2/3/4는 4열)
        # EN: Parse nibble arrays (nib01 has 8 columns, nib2/3/4 have 4 columns)
        nib01 = _ps5_parse_nibble_array(lines, "GFX10_SW_PATTERN_NIBBLE01", 8)
        nib2 = _ps5_parse_nibble_array(lines, "GFX10_SW_PATTERN_NIBBLE2", 4)
        nib3 = _ps5_parse_nibble_array(lines, "GFX10_SW_PATTERN_NIBBLE3", 4)
        nib4 = _ps5_parse_nibble_array(lines, "GFX10_SW_PATTERN_NIBBLE4", 4)
        # KR: 각 스위즐 모드별 PATINFO 배열 파싱
        # EN: Parse PATINFO array for each swizzle mode
        patinfo_tables = {
            mode_name: _ps5_parse_patinfo_array(lines, info[1])
            for mode_name, info in _PS5_BC_MODE_INFO.items()
        }
        return {
            "nib01": nib01,
            "nib2": nib2,
            "nib3": nib3,
            "nib4": nib4,
            "patinfo_tables": patinfo_tables,
        }
    except Exception:
        return None


def _ps5_compute_thin_block_dim(block_bits: int, bytes_per_block: int) -> tuple[int, int]:
    """KR: Thin 2D 블록의 너비/높이 차원을 계산한다 (addrlib2.cpp::ComputeThinBlockDimension, numSamples=1).
    EN: Compute thin 2D block width/height dimensions (addrlib2.cpp::ComputeThinBlockDimension, numSamples=1).
    """
    log2_ele = int(math.log2(bytes_per_block))  # KR: 요소당 바이트의 log2 / EN: log2 of bytes per element
    log2_num_ele = block_bits - log2_ele  # KR: 블록 내 요소 수의 log2 / EN: log2 of number of elements in block
    log2_w = (log2_num_ele + 1) // 2  # KR: 너비 비트 수 (높이보다 1비트 많거나 같음) / EN: Width bits (equal to or 1 more than height bits)
    w = 1 << log2_w
    h = 1 << (log2_num_ele - log2_w)
    return w, h


def _ps5_parity(value: int) -> int:
    """KR: 정수의 설정된 비트 수의 패리티(홀짝)를 반환한다 (0 또는 1).
    EN: Return the parity (even/odd) of set bits in an integer (0 or 1).
    """
    return value.bit_count() & 1


def _ps5_tile_bit_dimensions_for_mode(
    mode_name: str,
    bytes_per_block: int,
) -> tuple[int, int] | None:
    """KR: 레이아웃 테이블에서 thin-2D 타일의 비트 차원(x_bits, y_bits)을 조회한다.
    EN: Look up thin-2D tile bit dimensions (x_bits, y_bits) from the layout table.
    """
    if mode_name.endswith("_X"):
        # KR: XOR 스위즐 변형은 여기서 재구성할 수 없는 추가 방정식 비트가 필요하다.
        # EN: XOR swizzle variants require additional equation bits that cannot be reconstructed here.
        return None
    table: dict[int, tuple[int, int]] | None = None
    # KR: 페이지 크기별 레이아웃 테이블 선택
    # EN: Select layout table by page size
    if mode_name.startswith("256B_"):
        table = _PS5_LAYOUT_BLOCK256_2D_BITS
    elif mode_name.startswith("4KB_"):
        table = _PS5_LAYOUT_BLOCK4K_2D_BITS
    elif mode_name.startswith("64KB_"):
        table = _PS5_LAYOUT_BLOCK64K_2D_BITS
    if table is None:
        return None
    return table.get(int(bytes_per_block))


def _ps5_tile_bit_order_for_mode(mode_name: str, bytes_per_block: int) -> str:
    """KR: BC 레이아웃 폴백용 타일 내부 비트 인터리빙 순서를 선택한다.
    EN: Select the in-tile bit interleaving order for BC layout fallback.
    """
    if mode_name.startswith("4KB_") or mode_name.startswith("64KB_"):
        if int(bytes_per_block) >= 16:
            return "yxyx"  # KR: Y/X 비트 교차 인터리빙 / EN: Y/X bit interleaved alternation
        if int(bytes_per_block) == 8:
            return "x0_yxyx"  # KR: X의 최하위 비트 선행 후 Y/X 교차 / EN: X LSB precedes, then Y/X alternation
    return "yx"  # KR: 기본: Y 하위, X 상위 / EN: Default: Y lower, X upper


def _ps5_local_swizzle_index(
    local_x: int,
    local_y: int,
    x_bits: int,
    y_bits: int,
    order: str,
) -> int:
    """KR: 타일 내부 (local_x, local_y) 좌표를 지정된 비트 순서에 따라 선형 인덱스로 변환한다.
    EN: Convert tile-local (local_x, local_y) coordinates to a linear index according to the specified bit order.
    """
    if order == "yx":
        # KR: 단순 순서: Y가 하위 비트, X가 상위 비트
        # EN: Simple order: Y is lower bits, X is upper bits
        return local_y + (local_x << y_bits)
    if order == "yxyx":
        # KR: Y/X 비트를 번갈아 인터리빙
        # EN: Y/X bits interleaved alternately
        out = 0
        bit_pos = 0
        for bit in range(max(x_bits, y_bits)):
            if bit < y_bits:
                # KR: Y의 bit번째 비트를 출력 위치에 배치
                # EN: Place Y's bit-th bit at output position
                out |= ((local_y >> bit) & 1) << bit_pos
                bit_pos += 1
            if bit < x_bits:
                # KR: X의 bit번째 비트를 출력 위치에 배치
                # EN: Place X's bit-th bit at output position
                out |= ((local_x >> bit) & 1) << bit_pos
                bit_pos += 1
        return out
    if order == "x0_yxyx":
        # KR: X의 최하위 비트가 먼저 오고, 나머지는 Y/X 교차 인터리빙
        # EN: X's least significant bit comes first, rest are Y/X alternating interleave
        if x_bits <= 0:
            return local_y
        out = local_x & 1  # KR: X의 bit0을 최하위에 배치 / EN: Place X's bit0 at LSB
        bit_pos = 1
        for bit in range(max(x_bits - 1, y_bits)):
            if bit < y_bits:
                out |= ((local_y >> bit) & 1) << bit_pos
                bit_pos += 1
            if bit < (x_bits - 1):
                # KR: X의 bit1 이상을 인터리빙
                # EN: Interleave X's bit1 and above
                out |= ((local_x >> (bit + 1)) & 1) << bit_pos
                bit_pos += 1
        return out
    # KR: 기본 폴백: yx 순서
    # EN: Default fallback: yx order
    return local_y + (local_x << y_bits)


def _ps5_4kb_s_scalar_mix_bytes1(value: int) -> int:
    """KR: 4KB_S 경로에서 1바이트 블록용 스칼라 믹스 헬퍼.
    EN: Scalar mix helper for 1-byte blocks in 4KB_S path.
    """
    v = int(value)
    # KR: 비트 시프트 후 마스크로 특정 비트 위치만 추출하여 XOR 결합
    # EN: Extract specific bit positions via shift and mask, then XOR combine
    return ((v << 4) & 0x1F0) ^ ((v << 5) & 0x400)


def _ps5_4kb_s_scalar_mix_bytes2_4(value: int) -> int:
    """KR: 4KB_S 경로에서 2바이트 또는 4바이트 블록용 스칼라 믹스 헬퍼.
    EN: Scalar mix helper for 2-byte or 4-byte blocks in 4KB_S path.
    """
    v = int(value)
    return ((v << 4) & 0x70) ^ ((v << 5) & 0x100) ^ ((v << 6) & 0x400)


def _ps5_4kb_s_scalar_mix_bytes8_16(value: int) -> int:
    """KR: 4KB_S 경로에서 8바이트 또는 16바이트 블록용 스칼라 믹스 헬퍼.
    EN: Scalar mix helper for 8-byte or 16-byte blocks in 4KB_S path.
    """
    v = int(value)
    return ((v << 4) & 0x30) ^ ((v << 6) & 0x100) ^ ((v << 7) & 0x400)


def _ps5_4kb_s_vector_mix_bytes1(value: int) -> int:
    """KR: 4KB_S 경로에서 1바이트 블록용 벡터 믹스 헬퍼.
    EN: Vector mix helper for 1-byte blocks in 4KB_S path.
    """
    v = int(value)
    # KR: 하위 4비트 직접 사용 + 상위 비트를 시프트하여 XOR 결합
    # EN: Use lower 4 bits directly + shift upper bits for XOR combination
    return (v & 0x0F) ^ ((v << 5) & 0x200) ^ ((v << 6) & 0x800)


def _ps5_4kb_s_vector_mix_bytes2(value: int) -> int:
    """KR: 4KB_S 경로에서 2바이트 블록용 벡터 믹스 헬퍼.
    EN: Vector mix helper for 2-byte blocks in 4KB_S path.
    """
    v = int(value)
    return ((v << 1) & 0x0E) ^ ((v << 4) & 0x80) ^ ((v << 5) & 0x200) ^ ((v << 6) & 0x800)


def _ps5_4kb_s_vector_mix_bytes4(value: int) -> int:
    """KR: 4KB_S 경로에서 4바이트 블록용 벡터 믹스 헬퍼.
    EN: Vector mix helper for 4-byte blocks in 4KB_S path.
    """
    v = int(value)
    return ((v << 2) & 0x0C) ^ ((v << 5) & 0x80) ^ ((v << 6) & 0x200) ^ ((v << 7) & 0x800)


def _ps5_4kb_s_vector_mix_bytes8(value: int) -> int:
    """KR: 4KB_S 경로에서 8바이트 블록용 벡터 믹스 헬퍼.
    EN: Vector mix helper for 8-byte blocks in 4KB_S path.
    """
    v = int(value)
    return (
        ((v << 3) & 0x08)
        ^ ((v << 5) & 0xC0)
        ^ ((v << 6) & 0x200)
        ^ ((v << 7) & 0x800)
    )


def _ps5_4kb_s_vector_mix_bytes16(value: int) -> int:
    """KR: 4KB_S 경로에서 16바이트 블록용 벡터 믹스 헬퍼.
    EN: Vector mix helper for 16-byte blocks in 4KB_S path.
    """
    v = int(value)
    return ((v << 6) & 0xC0) ^ ((v << 7) & 0x200) ^ ((v << 8) & 0x800)


def _ps5_4kb_s_tile_index(
    local_x: int,
    local_y: int,
    bytes_per_block: int,
) -> int | None:
    """KR: 4KB_S 경로에서 타일 내부 좌표를 스위즐된 인덱스로 변환한다.
    EN: Convert tile-local coordinates to a swizzled index in 4KB_S path.
    """
    bpb = int(bytes_per_block)
    # KR: 블록 크기별로 적절한 스칼라(Y)/벡터(X) 믹스 함수 선택
    # EN: Select appropriate scalar(Y)/vector(X) mix function by block size
    if bpb == 1:
        base = _ps5_4kb_s_scalar_mix_bytes1(local_y)
        mixed = base ^ _ps5_4kb_s_vector_mix_bytes1(local_x)
    elif bpb == 2:
        base = _ps5_4kb_s_scalar_mix_bytes2_4(local_y)
        mixed = base ^ _ps5_4kb_s_vector_mix_bytes2(local_x)
    elif bpb == 4:
        base = _ps5_4kb_s_scalar_mix_bytes2_4(local_y)
        mixed = base ^ _ps5_4kb_s_vector_mix_bytes4(local_x)
    elif bpb == 8:
        base = _ps5_4kb_s_scalar_mix_bytes8_16(local_y)
        mixed = base ^ _ps5_4kb_s_vector_mix_bytes8(local_x)
    elif bpb == 16:
        base = _ps5_4kb_s_scalar_mix_bytes8_16(local_y)
        mixed = base ^ _ps5_4kb_s_vector_mix_bytes16(local_x)
    else:
        return None
    # KR: 바이트 오프셋을 요소 인덱스로 변환 (블록 크기만큼 우측 시프트)
    # EN: Convert byte offset to element index (right shift by block size)
    return mixed >> int(math.log2(bpb))


def _ps5_build_bc_lut_from_layout_rules(
    block_w: int,
    block_h: int,
    bytes_per_block: int,
    mode_name: str,
    pipe_bank_xor: int,
) -> tuple[int, ...] | None:
    """KR: 외부 패턴 헤더 없이 레이아웃 규칙만으로 BC LUT를 구축한다.
    EN: Build a BC LUT using only layout rules without external pattern headers.

    pipe_bank_xor가 0이 아니면 이 경로는 지원하지 않으므로 None을 반환한다.
    """
    if pipe_bank_xor != 0:
        return None
    bits = _ps5_tile_bit_dimensions_for_mode(mode_name, bytes_per_block)
    if bits is None:
        return None
    x_bits, y_bits = bits
    if x_bits <= 0 or y_bits <= 0:
        return None

    tile_w = 1 << x_bits  # KR: 타일 너비 (블록 단위) / EN: Tile width (in blocks)
    tile_h = 1 << y_bits  # KR: 타일 높이 (블록 단위) / EN: Tile height (in blocks)
    if tile_w <= 0 or tile_h <= 0 or block_w <= 0 or block_h <= 0:
        return None

    local_order = _ps5_tile_bit_order_for_mode(mode_name, bytes_per_block)
    use_4kb_s_helper_formula = mode_name == "4KB_S"
    macro_cols = (block_w + tile_w - 1) // tile_w  # KR: 매크로 타일 열 수 / EN: Number of macro tile columns
    tile_elements = tile_w * tile_h  # KR: 타일 하나의 총 요소 수 / EN: Total elements per tile
    total = block_w * block_h

    lut: list[int] = [0] * total
    for y in range(block_h):
        macro_y = y // tile_h  # KR: 매크로 타일 행 인덱스 / EN: Macro tile row index
        local_y = y & (tile_h - 1)  # KR: 타일 내부 Y 좌표 / EN: Tile-local Y coordinate
        row_base = y * block_w
        macro_row_base = macro_y * macro_cols * tile_elements
        for x in range(block_w):
            macro_x = x // tile_w  # KR: 매크로 타일 열 인덱스 / EN: Macro tile column index
            local_x = x & (tile_w - 1)  # KR: 타일 내부 X 좌표 / EN: Tile-local X coordinate
            if use_4kb_s_helper_formula:
                # KR: 4KB_S 모드: 전용 비트 믹스 공식 사용
                # EN: 4KB_S mode: use dedicated bit mix formula
                local_off = _ps5_4kb_s_tile_index(
                    local_x,
                    local_y,
                    bytes_per_block,
                )
                if local_off is None:
                    return None
            else:
                # KR: 일반 모드: 비트 인터리빙 순서에 따라 인덱스 계산
                # EN: General mode: compute index according to bit interleaving order
                local_off = _ps5_local_swizzle_index(
                    local_x,
                    local_y,
                    x_bits,
                    y_bits,
                    local_order,
                )
            if local_off < 0 or local_off >= tile_elements:
                return None
            # KR: 매크로 타일 기준 오프셋 + 타일 내 로컬 오프셋 = 스위즐된 인덱스
            # EN: Macro tile base offset + tile-local offset = swizzled index
            swizzled_idx = macro_row_base + macro_x * tile_elements + local_off
            if swizzled_idx >= total:
                return None
            lut[row_base + x] = swizzled_idx
    return tuple(lut)


def _ps5_compute_offset(
    pattern_bits: list[int],
    block_bits: int,
    x: int,
    y: int,
    z: int = 0,
    s: int = 0,
) -> int:
    """KR: Addrlib 패턴 비트 배열로부터 (x, y, z, s) 좌표의 블록 내 오프셋을 계산한다.
    EN: Compute the in-block offset for (x, y, z, s) coordinates from an Addrlib pattern bit array.

    각 출력 비트는 X/Y/Z/S 채널 마스크의 패리티를 XOR 결합하여 결정된다.
    """
    out = 0
    for i in range(block_bits):
        m = pattern_bits[i]
        if m == 0:
            continue
        # KR: 64비트 마스크에서 각 채널(X/Y/Z/S)의 16비트 슬롯 추출
        # EN: Extract 16-bit slot for each channel (X/Y/Z/S) from 64-bit mask
        xmask = m & 0xFFFF
        ymask = (m >> 16) & 0xFFFF
        zmask = (m >> 32) & 0xFFFF
        smask = (m >> 48) & 0xFFFF
        # KR: 각 채널의 좌표와 마스크를 AND한 뒤 패리티를 XOR 결합
        # EN: AND coordinate with mask for each channel, then XOR combine parities
        bit = (
            _ps5_parity(x & xmask)
            ^ _ps5_parity(y & ymask)
            ^ _ps5_parity(z & zmask)
            ^ _ps5_parity(s & smask)
        )
        out |= bit << i  # KR: 결과 비트를 출력 오프셋의 i번째 위치에 배치 / EN: Place result bit at i-th position of output offset
    return out


def _ps5_build_full_pattern(
    nib01: list[list[int]],
    nib2: list[list[int]],
    nib3: list[list[int]],
    nib4: list[list[int]],
    patinfo: tuple[int, int, int, int, int],
) -> list[int]:
    """KR: PATINFO 인덱스를 사용하여 니블 테이블 4개를 연결한 완전한 패턴 비트 배열을 구축한다.
    EN: Build a complete pattern bit array by concatenating 4 nibble tables using PATINFO indices.
    """
    _, idx01, idx2, idx3, idx4 = patinfo
    # KR: 각 니블 테이블에 대한 인덱스 범위 검증
    # EN: Validate index range for each nibble table
    if (
        idx01 >= len(nib01)
        or idx2 >= len(nib2)
        or idx3 >= len(nib3)
        or idx4 >= len(nib4)
    ):
        raise RuntimeError(f"Nibble index out of range: {patinfo}")
    # KR: nib01(8개) + nib2(4개) + nib3(4개) + nib4(4개) = 20비트 패턴
    # EN: nib01(8) + nib2(4) + nib3(4) + nib4(4) = 20-bit pattern
    return list(nib01[idx01]) + list(nib2[idx2]) + list(nib3[idx3]) + list(nib4[idx4])


@lru_cache(maxsize=2048)
def _ps5_build_bc_lut_cached(
    block_w: int,
    block_h: int,
    bytes_per_block: int,
    mode_name: str,
    pipe_log2: int,
    pipe_bank_xor: int,
) -> tuple[int, ...] | None:
    """KR: 캐시된 BC 스위즐 LUT 구축. 패턴 헤더 파일이 있으면 사용하고, 없으면 레이아웃 규칙으로 폴백한다.
    EN: Build a cached BC swizzle LUT. Uses pattern header file if available, falls back to layout rules otherwise.
    """
    tables = _ps5_load_bc_pattern_tables()
    if tables is None:
        # KR: 패턴 테이블 없음: 레이아웃 규칙 기반 폴백
        # EN: No pattern tables: fallback to layout-rule based approach
        return _ps5_build_bc_lut_from_layout_rules(
            block_w,
            block_h,
            bytes_per_block,
            mode_name,
            pipe_bank_xor,
        )
    mode_info = _PS5_BC_MODE_INFO.get(mode_name)
    if mode_info is None:
        return None
    _, _, block_bits, is_xor_mode = mode_info
    patinfo_rows = tables["patinfo_tables"].get(mode_name, [])
    pat_index = int(math.log2(bytes_per_block))  # KR: 블록 크기의 log2를 패턴 인덱스로 사용 / EN: Use log2 of block size as pattern index
    if pat_index < 0 or pat_index >= len(patinfo_rows):
        # KR: 해당 블록 크기에 대한 패턴 정보가 없으면 레이아웃 규칙으로 폴백
        # EN: No pattern info for this block size; fallback to layout rules
        return _ps5_build_bc_lut_from_layout_rules(
            block_w,
            block_h,
            bytes_per_block,
            mode_name,
            pipe_bank_xor,
        )
    # KR: 니블 테이블 4개를 결합하여 완전한 패턴 비트 배열 생성
    # EN: Combine 4 nibble tables into a complete pattern bit array
    pattern_bits = _ps5_build_full_pattern(
        tables["nib01"],
        tables["nib2"],
        tables["nib3"],
        tables["nib4"],
        patinfo_rows[pat_index],
    )

    total = block_w * block_h
    lut: list[int] = [0] * total

    # KR: thin 블록 차원 계산 및 피치 정렬
    # EN: Compute thin block dimensions and pitch alignment
    blk_w, blk_h = _ps5_compute_thin_block_dim(block_bits, bytes_per_block)
    pitch_aligned = ((block_w + blk_w - 1) // blk_w) * blk_w
    pitch_blocks = pitch_aligned // blk_w

    # KR: pipe/bank XOR 오프셋 계산을 위한 비트 마스크 준비
    # EN: Prepare bit masks for pipe/bank XOR offset calculation
    blk_mask = (1 << block_bits) - 1
    pipe_interleave_log2 = 8  # KR: Addrlib 파이프 인터리브 기본값: 256바이트 / EN: Addrlib pipe interleave default: 256 bytes
    column_bits = 2
    bank_bits_cap = 4
    bank_xor_bits = max(
        0,
        min(
            block_bits - pipe_interleave_log2 - pipe_log2 - column_bits,
            bank_bits_cap,
        ),
    )
    pipe_mask = (1 << pipe_log2) - 1 if pipe_log2 > 0 else 0
    bank_mask = (
        ((1 << bank_xor_bits) - 1) << (pipe_log2 + column_bits)
        if bank_xor_bits > 0
        else 0
    )
    # KR: XOR 모드인 경우 pipe_bank_xor를 블록 내 오프셋으로 변환
    # EN: For XOR mode, convert pipe_bank_xor to in-block offset
    pb_xor_off = 0
    if is_xor_mode:
        pb_xor_off = (
            (pipe_bank_xor & (pipe_mask | bank_mask)) << pipe_interleave_log2
        ) & blk_mask

    elem_log2 = int(math.log2(bytes_per_block))  # KR: 요소 크기의 log2 / EN: log2 of element size
    for y in range(block_h):
        yb = y // blk_h  # KR: 블록 행 인덱스 / EN: Block row index
        row_base = y * block_w
        for x in range(block_w):
            xb = x // blk_w  # KR: 블록 열 인덱스 / EN: Block column index
            blk_idx = yb * pitch_blocks + xb  # KR: 선형 블록 인덱스 / EN: Linear block index
            # KR: 패턴 비트에서 블록 내 오프셋 계산
            # EN: Compute in-block offset from pattern bits
            blk_off = _ps5_compute_offset(pattern_bits, block_bits, x, y, 0, 0)
            # KR: 블록 인덱스와 XOR 오프셋을 결합하여 최종 주소 생성
            # EN: Combine block index and XOR offset to generate final address
            addr = (blk_idx << block_bits) + (blk_off ^ pb_xor_off)
            swizzled_idx = addr >> elem_log2  # KR: 바이트 주소를 요소 인덱스로 변환 / EN: Convert byte address to element index
            linear_idx = row_base + x
            lut[linear_idx] = swizzled_idx % total

    return tuple(lut)


def _ps5_unswizzle_bc_blocks(
    raw: bytes,
    block_w: int,
    block_h: int,
    bytes_per_block: int,
    lut: tuple[int, ...],
) -> bytes:
    """KR: LUT를 사용하여 스위즐된 BC 블록 데이터를 선형 순서로 역스위즐한다.
    EN: Unswizzle swizzled BC block data to linear order using a LUT.
    """
    total = block_w * block_h
    src = memoryview(raw[: total * bytes_per_block])
    dst = bytearray(total * bytes_per_block)
    # KR: LUT의 각 항목: linear_idx -> swizzled_idx 매핑
    # EN: Each LUT entry: linear_idx -> swizzled_idx mapping
    for linear_idx, swizzled_idx in enumerate(lut):
        src_off = swizzled_idx * bytes_per_block  # KR: 스위즐된 소스 오프셋 / EN: Swizzled source offset
        dst_off = linear_idx * bytes_per_block  # KR: 선형 대상 오프셋 / EN: Linear destination offset
        dst[dst_off : dst_off + bytes_per_block] = src[
            src_off : src_off + bytes_per_block
        ]
    return bytes(dst)


def _ps5_decode_bc_to_rgba(
    raw_bytes: bytes,
    pixel_width: int,
    pixel_height: int,
    texture_format: int,
) -> bytes | None:
    """KR: BC 압축 텍스처 데이터를 RGBA 픽셀 데이터로 디코딩한다.
    EN: Decode BC-compressed texture data to RGBA pixel data.
    """
    if texture2ddecoder is None:
        return None
    bc_info = _PS5_BC_FORMATS.get(texture_format)
    if bc_info is None:
        return None
    _, _, _, decoder_name = bc_info
    # KR: 텍스처 포맷에 해당하는 디코더 함수 조회
    # EN: Look up decoder function for the texture format
    decoder = getattr(texture2ddecoder, decoder_name, None)
    if not callable(decoder):
        return None
    try:
        return bytes(decoder(raw_bytes, pixel_width, pixel_height))
    except Exception:
        return None


def _ps5_swap_rb_image(image: Image.Image) -> Image.Image:
    """KR: 이미지의 R(적색)과 B(청색) 채널을 교환한다 (BGR <-> RGB 변환).
    EN: Swap R (red) and B (blue) channels of an image (BGR <-> RGB conversion).
    """
    rgba = image.convert("RGBA")
    r, g, b, a = rgba.split()
    return Image.merge("RGBA", (b, g, r, a))


def _ps5_should_swap_rb_for_bc_preview(texture_format: int) -> bool:
    """KR: PS5 BC 텍스처 프리뷰 시 R/B 채널 교환이 필요한지 판별한다.
    EN: Determine whether R/B channel swap is needed for PS5 BC texture preview.

    PS5 BC 표면은 이 경로에서 BGR 성분 순서로 디코딩되므로 R/B 교환이 필요하다.
    """
    return int(texture_format) in _PS5_BC_FORMATS


def _ps5_crop_blocks_top_left(
    block_data: bytes,
    physical_block_w: int,
    logical_block_w: int,
    logical_block_h: int,
    bytes_per_block: int,
) -> bytes:
    """KR: 물리 블록 그리드에서 논리 블록 영역(좌상단)만 잘라낸다.
    EN: Crop only the logical block region (top-left) from the physical block grid.

    BC 블록 압축 시 물리 그리드는 타일 정렬 요건에 따라 논리 크기보다 클 수 있다.
    각 행에서 논리 너비만큼만 복사하여 패딩 블록을 제거한다.
    """
    if (
        physical_block_w <= 0
        or logical_block_w <= 0
        or logical_block_h <= 0
        or bytes_per_block <= 0
    ):
        return block_data
    logical_size = logical_block_w * logical_block_h * bytes_per_block
    if physical_block_w == logical_block_w:
        return block_data[:logical_size]
    src = memoryview(block_data)
    out = bytearray(logical_size)
    for y in range(logical_block_h):
        src_off = (y * physical_block_w) * bytes_per_block
        dst_off = (y * logical_block_w) * bytes_per_block
        row_bytes = logical_block_w * bytes_per_block
        out[dst_off : dst_off + row_bytes] = src[src_off : src_off + row_bytes]
    return bytes(out)


def _ps5_unswizzle_addrlib_uncompressed_candidate(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
) -> tuple[bytes, float] | None:
    """KR: 비압축 텍스처에 대해 Addrlib 4KB_S 역스위즐을 시도한다.
    EN: Attempt Addrlib 4KB_S unswizzle for uncompressed textures.

    물리 그리드 크기를 추론하여 LUT 기반 역스위즐 후 논리 영역만 잘라 반환한다.
    """
    if bytes_per_element not in {2, 4}:
        return None
    total_elements = width * height
    if total_elements <= 0 or total_elements > 2_000_000:
        return None

    logical_bytes = total_elements * bytes_per_element
    usable = data[: (len(data) // bytes_per_element) * bytes_per_element]
    if len(usable) < logical_bytes:
        return None

    physical_total = len(usable) // bytes_per_element
    inferred_w, inferred_h = _ps5_infer_physical_grid(
        physical_total,
        width,
        height,
        align_width=8,
        align_height=8,
    )
    candidates: list[tuple[int, int]] = [(width, height)]
    if (
        inferred_w * inferred_h == physical_total
        and (inferred_w, inferred_h) not in candidates
    ):
        candidates.append((inferred_w, inferred_h))

    for physical_w, physical_h in candidates:
        physical_bytes = physical_w * physical_h * bytes_per_element
        if physical_bytes > len(usable):
            continue
        lut = _ps5_build_bc_lut_cached(
            physical_w,
            physical_h,
            bytes_per_element,
            "4KB_S",
            2,
            0,
        )
        if lut is None:
            continue
        unsw_full = _ps5_unswizzle_bc_blocks(
            usable[:physical_bytes],
            physical_w,
            physical_h,
            bytes_per_element,
            lut,
        )
        unsw_logical = _ps5_crop_blocks_top_left(
            unsw_full,
            physical_w,
            width,
            height,
            bytes_per_element,
        )
        return unsw_logical, 0.0

    return None


def _ps5_pipe_bank_xor_span(
    mode_name: str, bytes_per_block: int, pipe_log2: int
) -> int:
    """KR: 주어진 모드의 pipe/bank XOR 값 범위를 계산한다.
    EN: Compute the pipe/bank XOR value range for the given mode.

    XOR 모드가 아니면 1을 반환한다. XOR 모드인 경우 pipe 비트와 bank 비트의
    합으로 가능한 조합 수(2^total_bits)를 계산하여 반환한다.
    """
    mode_info = _PS5_BC_MODE_INFO.get(mode_name)
    if mode_info is None:
        return 0
    _, _, block_bits, is_xor_mode = mode_info
    if not is_xor_mode:
        return 1
    pipe_log2 = max(0, int(pipe_log2))
    pipe_mask_bits = pipe_log2
    bank_xor_bits = max(
        0,
        min(
            block_bits - 8 - pipe_log2 - 2,
            4,
        ),
    )
    total_bits = pipe_mask_bits + bank_xor_bits
    if total_bits <= 0:
        return 1
    return 1 << total_bits


def _ps5_iter_pipe_bank_xor_values(
    mode_name: str,
    bytes_per_block: int,
    pipe_log2: int,
    *,
    exhaustive: bool = False,
) -> tuple[int, ...]:
    """KR: 역스위즐 후보 탐색용 pipe/bank XOR 값 목록을 생성한다.
    EN: Generate a list of pipe/bank XOR values for unswizzle candidate search.

    exhaustive=True이면 전체 범위를 반환하고, 아니면 빠른 탐색용 축약 목록을 반환한다.
    """
    span = _ps5_pipe_bank_xor_span(mode_name, bytes_per_block, pipe_log2)
    if span <= 1:
        return (0,)
    if exhaustive:
        return tuple(range(span))
    # KR: 기본 경로는 빠르게 처리하고, 필요 시에만 전수 탐색으로 전환
    # EN: Default path handles quickly; switch to exhaustive search only when needed
    quick = tuple(v for v in (0, 1, 2, 3, 4, 7) if v < span)
    return quick if quick else (0,)


def _ps5_unswizzle_bc_best_candidate(
    raw: bytes,
    pixel_width: int,
    pixel_height: int,
    texture_format: int,
    *,
    mode_candidates: Iterable[str] | None = None,
    pipe_log2_candidates: Iterable[int] | None = None,
    exhaustive: bool = False,
    exhaustive_xor: bool = False,
) -> tuple[bytes, str | None, float | None, tuple[int, int], tuple[int, int]] | None:
    """KR: BC 블록 압축 텍스처의 최적 역스위즐 후보를 점수 기반으로 선택한다.
    EN: Select the best unswizzle candidate for BC block-compressed textures based on scoring.

    모드/pipe/XOR 조합별로 역스위즐한 뒤 RGBA 디코딩하여 거칠기 점수를 비교한다.
    가장 낮은 거칠기 점수를 가진 후보의 블록 데이터와 모드 정보를 반환한다.
    """
    bc_info = _PS5_BC_FORMATS.get(texture_format)
    if bc_info is None:
        return None
    block_w_px, block_h_px, bytes_per_block, _ = bc_info
    logical_block_w = (pixel_width + block_w_px - 1) // block_w_px
    logical_block_h = (pixel_height + block_h_px - 1) // block_h_px
    logical_block_total = logical_block_w * logical_block_h
    logical_bytes = logical_block_total * bytes_per_block

    usable = raw[: (len(raw) // bytes_per_block) * bytes_per_block]
    if len(usable) < logical_bytes:
        return None

    physical_total_blocks = len(usable) // bytes_per_block
    align = 16 if bytes_per_block >= 16 else 8

    raw_logical = usable[:logical_bytes]
    raw_rgba = _ps5_decode_bc_to_rgba(
        raw_logical, pixel_width, pixel_height, texture_format
    )
    raw_score = (
        _ps5_roughness_score(raw_rgba, pixel_width, pixel_height, 4)
        if raw_rgba is not None
        else None
    )

    modes = (
        list(mode_candidates)
        if mode_candidates is not None
        else (
            list(_PS5_BC_MODE_INFO.keys())
            if exhaustive
            else list(_PS5_BC_FAST_MODE_NAMES)
        )
    )
    pipe_candidates = (
        tuple(pipe_log2_candidates)
        if pipe_log2_candidates is not None
        else ((0, 1, 2, 3) if exhaustive else (2, 1, 3))
    )

    best_raw = raw_logical
    best_mode: str | None = None
    best_ratio: float | None = None
    best_score: float | None = None

    best_physical = (logical_block_w, logical_block_h)

    for mode_name in modes:
        physical_candidates = _ps5_physical_grid_candidates_for_mode(
            physical_total_blocks,
            logical_block_w,
            logical_block_h,
            bytes_per_block=bytes_per_block,
            mode_name=mode_name,
            align_width=align,
            align_height=align,
        )
        for physical_block_w, physical_block_h in physical_candidates:
            physical_bytes = physical_block_w * physical_block_h * bytes_per_block
            if physical_bytes > len(usable):
                continue
            source_for_layout = usable[:physical_bytes]

            for pipe_log2 in pipe_candidates:
                pipe_bank_xor_values = _ps5_iter_pipe_bank_xor_values(
                    mode_name,
                    bytes_per_block,
                    pipe_log2,
                    exhaustive=exhaustive_xor,
                )
                for pipe_bank_xor in pipe_bank_xor_values:
                    lut = _ps5_build_bc_lut_cached(
                        physical_block_w,
                        physical_block_h,
                        bytes_per_block,
                        mode_name,
                        pipe_log2,
                        pipe_bank_xor,
                    )
                    if lut is None:
                        continue
                    unsw_full = _ps5_unswizzle_bc_blocks(
                        source_for_layout,
                        physical_block_w,
                        physical_block_h,
                        bytes_per_block,
                        lut,
                    )
                    unsw_logical = _ps5_crop_blocks_top_left(
                        unsw_full,
                        physical_block_w,
                        logical_block_w,
                        logical_block_h,
                        bytes_per_block,
                    )

                    if raw_score is None:
                        if best_mode is None:
                            best_raw = unsw_logical
                            best_mode = (
                                f"{mode_name}:p{pipe_log2}:x{pipe_bank_xor}"
                            )
                            best_physical = (physical_block_w, physical_block_h)
                        continue

                    rgba = _ps5_decode_bc_to_rgba(
                        unsw_logical, pixel_width, pixel_height, texture_format
                    )
                    if rgba is None:
                        continue
                    score = _ps5_roughness_score(rgba, pixel_width, pixel_height, 4)
                    ratio = (score / raw_score) if raw_score > 0 else None
                    if best_score is None or score < best_score:
                        best_score = score
                        best_ratio = ratio
                        best_mode = f"{mode_name}:p{pipe_log2}:x{pipe_bank_xor}"
                        best_raw = unsw_logical
                        best_physical = (physical_block_w, physical_block_h)

    return (
        best_raw,
        best_mode,
        best_ratio,
        (logical_block_w, logical_block_h),
        best_physical,
    )


def _ps5_try_end_aligned_4kb_s_candidate(
    usable: bytes,
    logical_block_w: int,
    logical_block_h: int,
    bytes_per_block: int,
) -> tuple[bytes, str, tuple[int, int]] | None:
    """KR: 스트림 끝 정렬 기반 4KB_S 역스위즐 후보를 시도한다.
    EN: Try a stream end-aligned 4KB_S unswizzle candidate.

    mip 레벨이 여러 개인 경우 mip0 데이터가 스트림 끝에 위치할 수 있으므로,
    끝 기준 오프셋에서 물리 블록 크기만큼 잘라 4KB_S LUT로 역스위즐한다.
    """
    bits = _ps5_tile_bit_dimensions_for_mode("4KB_S", bytes_per_block)
    if bits is None:
        return None
    tile_w = 1 << bits[0]
    tile_h = 1 << bits[1]
    if tile_w <= 0 or tile_h <= 0:
        return None

    physical_block_w = _ps5_align_up(logical_block_w, tile_w)
    physical_block_h = _ps5_align_up(logical_block_h, tile_h)
    physical_bytes = physical_block_w * physical_block_h * bytes_per_block
    if physical_bytes <= 0 or physical_bytes > len(usable):
        return None

    offset_bytes = len(usable) - physical_bytes
    source_for_layout = usable[offset_bytes : offset_bytes + physical_bytes]
    lut = _ps5_build_bc_lut_cached(
        physical_block_w,
        physical_block_h,
        bytes_per_block,
        "4KB_S",
        2,
        0,
    )
    if lut is None:
        return None
    unsw_full = _ps5_unswizzle_bc_blocks(
        source_for_layout,
        physical_block_w,
        physical_block_h,
        bytes_per_block,
        lut,
    )
    unsw_logical = _ps5_crop_blocks_top_left(
        unsw_full,
        physical_block_w,
        logical_block_w,
        logical_block_h,
        bytes_per_block,
    )
    return (
        unsw_logical,
        f"4KB_S:p2:x0:o{offset_bytes}",
        (physical_block_w, physical_block_h),
    )


def _ps5_unswizzle_bc_best_layout_match(
    raw: bytes,
    pixel_width: int,
    pixel_height: int,
    texture_format: int,
    *,
    mip_count: int | None = None,
) -> tuple[bytes, str | None, float | None, tuple[int, int], tuple[int, int]] | None:
    """KR: 고정 우선순위로 첫 번째 유효한 BC 레이아웃 변형을 선택한다.
    EN: Select the first valid BC layout variant using fixed priority.

    mip 레벨 오프셋을 계산하여 mip0 시작 위치를 결정한 뒤, 모드/pipe/XOR
    조합을 순회하며 첫 번째 유효한 역스위즐 결과를 반환한다.
    비정방 텍스처의 경우 end-aligned 4KB_S 후보도 추가로 확인한다.
    """
    bc_info = _PS5_BC_FORMATS.get(texture_format)
    if bc_info is None:
        return None
    block_w_px, block_h_px, bytes_per_block, _ = bc_info
    logical_block_w = (pixel_width + block_w_px - 1) // block_w_px
    logical_block_h = (pixel_height + block_h_px - 1) // block_h_px
    logical_block_total = logical_block_w * logical_block_h
    logical_bytes = logical_block_total * bytes_per_block

    usable = raw[: (len(raw) // bytes_per_block) * bytes_per_block]
    if len(usable) < logical_bytes:
        return None
    source_window = usable
    mip0_offset_bytes = 0
    if mip_count is not None and int(mip_count) > 1:
        # KR: 높은 mip부터 offset이 누적되므로 mip0가 tail 뒤에서 시작할 수 있다
        # EN: Offset accumulates from higher mips, so mip0 may start after the tail
        lower_tail_sum = 0
        w = max(1, int(pixel_width))
        h = max(1, int(pixel_height))
        levels: list[int] = []
        level_count = max(1, int(mip_count))
        for _ in range(level_count):
            bw = max(1, (w + block_w_px - 1) // block_w_px)
            bh = max(1, (h + block_h_px - 1) // block_h_px)
            levels.append(bw * bh * bytes_per_block)
            w = max(1, w >> 1)
            h = max(1, h >> 1)
        if len(levels) > 1:
            # KR: tail packing은 mip별 256B, tail 2KB 정렬을 사용한다
            # EN: Tail packing uses 256B per mip, 2KB tail alignment
            for level_bytes in levels[1:]:
                lower_tail_sum += _ps5_align_up(level_bytes, 0x100)
            mip0_offset_bytes = _ps5_align_up(lower_tail_sum, 0x800)
            base_alloc = _ps5_align_up(levels[0], 0x800)
            modeled_total = mip0_offset_bytes + base_alloc
            if modeled_total < len(usable):
                # KR: 비정방 케이스에서는 stream 끝 기준으로 mip0를 다시 맞춘다
                # EN: For non-square cases, re-align mip0 from stream end
                mip0_offset_bytes += len(usable) - modeled_total
            if mip0_offset_bytes + base_alloc <= len(usable):
                base_end = mip0_offset_bytes + base_alloc
                source_window = usable[mip0_offset_bytes:base_end]
            elif mip0_offset_bytes + levels[0] <= len(usable):
                base_end = mip0_offset_bytes + levels[0]
                source_window = usable[mip0_offset_bytes:base_end]
            else:
                mip0_offset_bytes = 0
                source_window = usable

    if len(source_window) < logical_bytes:
        return None
    raw_logical = source_window[:logical_bytes]

    physical_total_blocks = len(source_window) // bytes_per_block
    align = 16 if bytes_per_block >= 16 else 8

    # KR: 4KB_S 모드를 최우선으로 시도한다
    # EN: Try 4KB_S mode with highest priority
    mode_order: list[str] = ["4KB_S"]
    for mode_name in _PS5_BC_FAST_MODE_NAMES:
        if mode_name not in mode_order:
            mode_order.append(mode_name)
    for mode_name in _PS5_BC_MODE_INFO.keys():
        if mode_name not in mode_order:
            mode_order.append(mode_name)
    pipe_order = (2, 1, 3, 0)

    for mode_name in mode_order:
        physical_candidates = _ps5_physical_grid_candidates_for_mode(
            physical_total_blocks,
            logical_block_w,
            logical_block_h,
            bytes_per_block=bytes_per_block,
            mode_name=mode_name,
            align_width=align,
            align_height=align,
        )
        for physical_block_w, physical_block_h in physical_candidates:
            physical_bytes = physical_block_w * physical_block_h * bytes_per_block
            if physical_bytes > len(source_window):
                continue
            source_for_layout = source_window[:physical_bytes]
            for pipe_log2 in pipe_order:
                for pipe_bank_xor in _ps5_iter_pipe_bank_xor_values(
                    mode_name,
                    bytes_per_block,
                    pipe_log2,
                    exhaustive=True,
                ):
                    lut = _ps5_build_bc_lut_cached(
                        physical_block_w,
                        physical_block_h,
                        bytes_per_block,
                        mode_name,
                        pipe_log2,
                        pipe_bank_xor,
                    )
                    if lut is None:
                        continue
                    unsw_full = _ps5_unswizzle_bc_blocks(
                        source_for_layout,
                        physical_block_w,
                        physical_block_h,
                        bytes_per_block,
                        lut,
                    )
                    unsw_logical = _ps5_crop_blocks_top_left(
                        unsw_full,
                        physical_block_w,
                        logical_block_w,
                        logical_block_h,
                        bytes_per_block,
                    )
                    if (
                        mip_count is not None
                        and int(mip_count) > 1
                        and mode_name.startswith("256B_")
                    ):
                        # KR: 일부 비정방 케이스는 end-aligned 4KB_S 레이아웃으로 재확인한다
                        # EN: Some non-square cases need re-verification with end-aligned 4KB_S layout
                        alt = _ps5_try_end_aligned_4kb_s_candidate(
                            usable,
                            logical_block_w,
                            logical_block_h,
                            bytes_per_block,
                        )
                        if alt is not None:
                            alt_raw, alt_mode, alt_physical = alt
                            return (
                                alt_raw,
                                alt_mode,
                                None,
                                (logical_block_w, logical_block_h),
                                alt_physical,
                            )
                    return (
                        unsw_logical,
                        (
                            f"{mode_name}:p{pipe_log2}:x{pipe_bank_xor}:o{mip0_offset_bytes}"
                            if mip0_offset_bytes > 0
                            else f"{mode_name}:p{pipe_log2}:x{pipe_bank_xor}"
                        ),
                        None,
                        (logical_block_w, logical_block_h),
                        (physical_block_w, physical_block_h),
                    )

    return (
        raw_logical,
        None,
        None,
        (logical_block_w, logical_block_h),
        (logical_block_w, logical_block_h),
    )


def find_ggm_file(data_path: str) -> str | None:
    """KR: 데이터 폴더에서 globalgamemanagers 계열 파일 경로를 찾는다.
    EN: Find the globalgamemanagers family file path in the data folder.
    """
    candidates = ["globalgamemanagers", "globalgamemanagers.assets", "data.unity3d"]
    candidates_resources = ["unity default resources", "unity_builtin_extra"]
    fls: list[str] = []
    # KR: globalgamemanagers 핵심 파일을 우선 탐색한다
    # EN: Search for globalgamemanagers core files first
    for candidate in candidates:
        ggm_path = os.path.join(data_path, candidate)
        if os.path.exists(ggm_path):
            fls.append(ggm_path)
    for candidate in candidates_resources:
        ggm_path = os.path.join(data_path, "Resources", candidate)
        if os.path.exists(ggm_path):
            fls.append(ggm_path)
    if fls:
        return fls[0]
    return None


def resolve_game_path(path: str, lang: Language = "ko") -> tuple[str, str]:
    """KR: 입력 경로를 게임 루트와 _Data 경로로 정규화한다.
    EN: Normalize the input path to game root and _Data path.
    """
    path = os.path.normpath(os.path.abspath(path))

    if path.lower().endswith("_data"):
        data_path = path
        game_path = os.path.dirname(path)
    else:
        game_path = path
        data_folders = [
            d
            for d in os.listdir(path)
            if d.lower().endswith("_data") and os.path.isdir(os.path.join(path, d))
        ]

        if not data_folders:
            if lang == "ko":
                raise FileNotFoundError(f"'{path}'에서 _Data 폴더를 찾을 수 없습니다.")
            raise FileNotFoundError(f"Could not find _Data folder in '{path}'.")

        data_path = os.path.join(game_path, data_folders[0])

    ggm_path = find_ggm_file(data_path)
    if not ggm_path:
        if lang == "ko":
            raise FileNotFoundError(
                f"'{data_path}'에서 globalgamemanagers 파일을 찾을 수 없습니다.\n올바른 Unity 게임 폴더인지 확인해주세요."
            )
        raise FileNotFoundError(
            f"Could not find a globalgamemanagers file in '{data_path}'.\nPlease verify this is a valid Unity game folder."
        )

    return game_path, data_path


def get_data_path(game_path: str, lang: Language = "ko") -> str:
    """KR: 게임 루트에서 _Data 폴더 경로를 반환한다.
    EN: Return the _Data folder path from the game root.
    """
    data_folders = [i for i in os.listdir(game_path) if i.lower().endswith("_data")]
    if not data_folders:
        if lang == "ko":
            raise FileNotFoundError(f"'{game_path}'에서 _Data 폴더를 찾을 수 없습니다.")
        raise FileNotFoundError(f"Could not find _Data folder in '{game_path}'.")
    return os.path.join(game_path, data_folders[0])


def get_unity_version(game_path: str, lang: Language = "ko") -> str:
    """KR: 게임 경로에서 Unity 버전을 읽어 반환한다.
    EN: Read and return the Unity version from the game path.
    """
    data_path = get_data_path(game_path, lang=lang)
    candidates = [
        os.path.join(data_path, "globalgamemanagers"),
        os.path.join(data_path, "globalgamemanagers.assets"),
        os.path.join(data_path, "data.unity3d"),
    ]
    existing_candidates = [p for p in candidates if os.path.exists(p)]
    if not existing_candidates:
        if lang == "ko":
            raise FileNotFoundError(
                f"'{data_path}'에서 globalgamemanagers 파일을 찾을 수 없습니다.\n올바른 Unity 게임 폴더인지 확인해주세요."
            )
        raise FileNotFoundError(
            f"Could not find a globalgamemanagers file in '{data_path}'.\nPlease verify this is a valid Unity game folder."
        )

    for candidate in existing_candidates:
        env = None
        try:
            env = UnityPy.load(candidate)

            # KR: 1) 빠른 경로: 최상위 파일에서 unity_version을 바로 확인한다
            # EN: 1) Fast path: check unity_version directly on top-level file
            top_file = getattr(env, "file", None)
            top_version = getattr(top_file, "unity_version", None)
            if top_version:
                return str(top_version)

            # KR: 2) 로드된 파일들을 확인한다
            # EN: 2) Check loaded files
            env_files = getattr(env, "files", None)
            if isinstance(env_files, dict):
                for loaded in env_files.values():
                    uv = getattr(loaded, "unity_version", None)
                    if uv:
                        return str(uv)

            # KR: 3) 폴백: 파싱된 오브젝트가 있을 때만 검사한다
            # EN: 3) Fallback: inspect only when parsed objects exist
            objs = getattr(env, "objects", None)
            if objs:
                first_obj = objs[0]
                assets_file = getattr(first_obj, "assets_file", None)
                uv = getattr(assets_file, "unity_version", None)
                if uv:
                    return str(uv)
        except Exception:
            continue
        finally:
            close_unitypy_env(env)
            env = None
            gc.collect()

    tried = ", ".join(os.path.basename(p) for p in existing_candidates)
    if lang == "ko":
        raise RuntimeError(f"Unity 버전 감지에 실패했습니다. 시도한 파일: {tried}")
    raise RuntimeError(f"Failed to detect Unity version. Tried files: {tried}")


def get_script_dir() -> str:
    """KR: 실행 기준 디렉터리(스크립트/배포 바이너리)를 반환한다.
    EN: Return the execution base directory (script/distribution binary).
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def parse_target_files_arg(target_file_args: list[str] | None) -> set[str]:
    """KR: --target-file 인자(반복/콤마 구분)를 파일명 집합으로 정규화한다.
    EN: Normalize --target-file arguments (repeated/comma-separated) into a filename set.
    """
    selected_files: set[str] = set()
    if not target_file_args:
        return selected_files
    for entry in target_file_args:
        for token in str(entry).split(","):
            name = os.path.basename(token.strip())
            if name:
                selected_files.add(name)
    return selected_files


def parse_exclude_exts_arg(exclude_ext_args: list[str] | None) -> set[str]:
    """KR: --exclude-ext 인자(반복/콤마 구분)를 확장자 집합으로 정규화한다.
    EN: Normalize --exclude-ext arguments (repeated/comma-separated) into an extension set.
    """
    normalized_exts: set[str] = set()
    if not exclude_ext_args:
        return normalized_exts
    for entry in exclude_ext_args:
        for token in str(entry).split(","):
            raw = token.strip().lower()
            if not raw:
                continue
            if raw.startswith("*"):
                raw = raw.lstrip("*")
            if not raw:
                continue
            if not raw.startswith("."):
                raw = f".{raw}"
            normalized_exts.add(raw)
    return normalized_exts


_PRIMARY_MODE_ARGS: tuple[tuple[str, str], ...] = (
    ("parse", "--parse"),
    ("mulmaru", "--mulmaru"),
    ("nanumgothic", "--nanumgothic"),
    ("sarabun", "--sarabun"),
    ("notosansthai", "--notosansthai"),
    ("list", "--list"),
    ("preview_export", "--preview-export"),
)


def _selected_primary_modes(args: Any) -> list[str]:
    """KR: CLI 인자에서 활성화된 주요 모드 목록을 반환한다.
    EN: Return the list of active primary modes from CLI arguments.
    """
    selected: list[str] = []
    for attr_name, cli_name in _PRIMARY_MODE_ARGS:
        value = getattr(args, attr_name, None)
        if isinstance(value, str):
            if value.strip():
                selected.append(cli_name)
        elif value:
            selected.append(cli_name)
    return selected


def _mode_uses_scan_jobs(mode: str | None) -> bool:
    """KR: 해당 모드가 스캔 작업을 사용하는지 여부를 반환한다.
    EN: Return whether the given mode uses scan jobs.
    """
    return mode in {"parse", "mulmaru", "nanumgothic", "sarabun", "notosansthai", "preview_export"}


def _should_pause_before_exit(*, interactive_session: bool = False) -> bool:
    """KR: 종료 전 일시정지가 필요한지 판별한다.
    EN: Determine whether a pause is needed before exit.
    """
    return bool(interactive_session or getattr(sys, "frozen", False))


def _pause_before_exit(
    lang: Language = "ko",
    *,
    interactive_session: bool = False,
) -> None:
    """KR: 대화형 세션 또는 배포 바이너리 실행 시 종료 전 사용자 입력을 대기한다.
    EN: Wait for user input before exit in interactive session or distribution binary execution.
    """
    if not _should_pause_before_exit(interactive_session=interactive_session):
        return
    if lang == "ko":
        input("\n엔터를 눌러 종료...")
    else:
        input("\nPress Enter to exit...")


def strip_wrapping_quotes_repeated(value: str) -> str:
    """KR: 앞뒤 따옴표(' 또는 ")를 반복 제거한다.
    EN: Repeatedly strip leading/trailing quotes (' or ").
    """
    text = str(value).strip()
    while True:
        updated = text.strip().strip('"').strip("'")
        if updated == text:
            return updated
        text = updated


def sanitize_filename_component(
    value: str, fallback: str = "unnamed", max_len: int = 96
) -> str:
    """KR: 파일명 구성요소에서 경로/예약 문자를 안전한 문자로 치환한다.
    EN: Replace path/reserved characters with safe characters in a filename component.
    """
    text = str(value or "").strip()
    invalid_chars = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in invalid_chars else ch for ch in text)
    cleaned = cleaned.strip().strip(".")
    if not cleaned:
        cleaned = fallback
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    return cleaned


def resolve_output_only_path(source_file: str, data_path: str, output_root: str) -> str:
    """KR: output-only 저장 시 원본 data_path 기준 상대 경로를 유지한 출력 경로를 계산한다.
    EN: Compute the output path preserving relative path from original data_path for output-only saves.
    """
    source_abs = os.path.abspath(source_file)
    data_abs = os.path.abspath(data_path)
    output_abs = os.path.abspath(output_root)
    try:
        rel_path = os.path.relpath(source_abs, data_abs)
    except ValueError:
        rel_path = os.path.basename(source_abs)
    if rel_path.startswith("..") or os.path.isabs(rel_path):
        rel_path = os.path.basename(source_abs)
    return os.path.join(output_abs, rel_path)


def prepare_output_only_dependencies(
    data_path: str,
    output_root: str,
    lang: Language = "ko",
) -> None:
    """KR: output-only 모드에서 핵심 의존 파일을 출력 루트에 미리 복사한다.
    EN: Pre-copy essential dependency files to the output root in output-only mode.
    """
    candidate_rel_paths = [
        "globalgamemanagers",
        "globalgamemanagers.assets",
        "data.unity3d",
        os.path.join("Resources", "unity default resources"),
        os.path.join("Resources", "unity_builtin_extra"),
    ]
    copied: list[str] = []
    for rel_path in candidate_rel_paths:
        source_path = os.path.join(data_path, rel_path)
        if not os.path.isfile(source_path):
            continue
        output_path = os.path.join(output_root, rel_path)
        if os.path.exists(output_path):
            continue
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)
        shutil.copy2(source_path, output_path)
        copied.append(rel_path)

    if copied:
        if lang == "ko":
            _log_console(
                f"출력 전용 의존 파일 준비: {len(copied)}개 ({', '.join(copied)})"
            )
        else:
            _log_console(
                f"Prepared output-only dependencies: {len(copied)} ({', '.join(copied)})"
            )


def register_temp_dir_for_cleanup(path: str) -> str:
    """KR: 종료 시 삭제할 임시 디렉터리를 등록하고 정규화 경로를 반환한다.
    EN: Register a temp directory for cleanup on exit and return the normalized path.
    """
    normalized = os.path.abspath(path)
    _REGISTERED_TEMP_DIRS.add(normalized)
    return normalized


def cleanup_registered_temp_dirs() -> None:
    """KR: 등록된 임시 디렉터리를 깊은 경로부터 안전하게 삭제한다.
    EN: Safely delete registered temp directories starting from deepest paths.
    """
    if not _REGISTERED_TEMP_DIRS:
        return
    for temp_dir in sorted(_REGISTERED_TEMP_DIRS, key=len, reverse=True):
        try:
            if os.path.isdir(temp_dir):
                shutil.rmtree(temp_dir)
        except Exception:
            pass
    _REGISTERED_TEMP_DIRS.clear()


atexit.register(cleanup_registered_temp_dirs)


def _close_unitypy_reader(obj: Any) -> None:
    """KR: UnityPy 내부 reader/object를 안전하게 dispose한다.
    EN: Safely dispose UnityPy internal reader/object.

    BundleFile의 mmap/temp 파일과 SerializedFile의 spill store도 정리한다.
    """
    if obj is None:
        return
    # KR: BundleFile의 mmap/temp 블록 저장소 정리
    # EN: Clean up BundleFile's mmap/temp block storage
    cleanup_fn = getattr(obj, "_cleanup_temp_blocks_storage", None)
    if callable(cleanup_fn):
        try:
            cleanup_fn()
        except Exception:
            pass
    # KR: SerializedFile의 spill store (temp 파일) 정리
    # EN: Clean up SerializedFile's spill store (temp file)
    close_fn = getattr(obj, "close", None)
    if callable(close_fn):
        try:
            close_fn()
        except Exception:
            pass
    reader = getattr(obj, "reader", None)
    if reader is not None and hasattr(reader, "dispose"):
        try:
            reader.dispose()
        except Exception:
            pass
    if hasattr(obj, "dispose"):
        try:
            obj.dispose()
        except Exception:
            pass


def close_unitypy_env(environment: Any) -> None:
    """KR: Environment에 연결된 UnityPy 파일 리소스를 순회하며 종료한다.
    EN: Traverse and close UnityPy file resources connected to the Environment.
    """
    if environment is None:
        return
    stack: list[Any] = []
    files = getattr(environment, "files", None)
    if isinstance(files, dict):
        stack.extend(files.values())
    while stack:
        item = stack.pop()
        _close_unitypy_reader(item)
        sub_files = getattr(item, "files", None)
        if isinstance(sub_files, dict):
            stack.extend(sub_files.values())


def normalize_font_name(name: str) -> str:
    """KR: 확장자/SDF 접미사를 제거해 폰트 기본 이름으로 정규화한다.
    EN: Normalize to the base font name by removing extensions/SDF suffixes.
    """
    for ext in [".ttf", ".otf", ".json", ".png"]:
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
    for suffix in (
        " SDF Atlas",
        " Raster Atlas",
        " Atlas",
        " SDF Material",
        " Raster Material",
        " Material",
        " SDF",
        " Raster",
    ):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def parse_bool_flag(value: Any) -> bool:
    """KR: 문자열/숫자/불리언 입력을 안전하게 bool로 해석한다.
    EN: Safely interpret string/number/boolean input as bool.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def _read_bundle_signature(
    path: str, bundle_signatures: set[str] | None = None
) -> str | None:
    """KR: 파일 헤더에서 Unity 번들 시그니처를 읽는다.
    EN: Read a Unity bundle signature from the file header.
    """
    signatures = bundle_signatures or BUNDLE_SIGNATURES
    try:
        with open(path, "rb") as f:
            header = f.read(16)
    except Exception:
        return None

    for sig in signatures:
        token = (sig + "\x00").encode("ascii")
        if header.startswith(token):
            return sig
    return None


def _safe_metric_scale(game_point_size: Any, replacement_point_size: Any) -> float:
    """KR: 게임 pointSize 대비 교체 pointSize 비율을 계산한다.
    EN: Compute the ratio of replacement pointSize to game pointSize.
    """
    try:
        game_ps = float(game_point_size)
        repl_ps = float(replacement_point_size)
        if game_ps > 0 and repl_ps > 0:
            return repl_ps / game_ps
    except Exception:
        pass
    return 1.0


def _detect_target_texture_swizzle(
    texture_object_lookup: dict[tuple[str, int], Any],
    texture_swizzle_state_cache: dict[str, tuple[str | None, str | None]],
    assets_name: str,
    path_id: int,
) -> tuple[str | None, str | None]:
    """KR: 타겟 Texture2D의 swizzle 판정 결과를 캐시와 함께 반환합니다.
    EN: Return the swizzle detection result for the target Texture2D, with caching.
    """
    cache_key = f"{assets_name}|{path_id}"
    if cache_key in texture_swizzle_state_cache:
        return texture_swizzle_state_cache[cache_key]
    texture_obj = texture_object_lookup.get((assets_name, int(path_id)))
    verdict, source = (
        detect_texture_object_ps5_swizzle_detail(texture_obj)
        if texture_obj is not None
        else (None, None)
    )
    texture_swizzle_state_cache[cache_key] = (verdict, source)
    return verdict, source


def _preview_visible_image(image: Image.Image) -> Image.Image:
    """KR: RGBA/LA Atlas를 사람이 보기 쉬운 단일 채널 이미지로 정규화합니다.
    EN: Normalize an RGBA/LA atlas to a human-viewable single-channel image.
    """
    try:
        if image.mode == "RGBA":
            alpha = image.getchannel("A")
            rgb = image.convert("RGB")
            rgb_bbox = rgb.getbbox()
            alpha_bbox = alpha.getbbox()
            if alpha_bbox and not rgb_bbox:
                return alpha
            return alpha if alpha_bbox else image.convert("L")
        if image.mode == "LA":
            alpha = image.getchannel("A")
            return alpha if alpha.getbbox() else image.getchannel("L")
        if image.mode == "P":
            return image.convert("L")
        if image.mode not in {"L", "RGB"}:
            return image.convert("L")
        return image
    except Exception:
        return image.convert("L")


def _load_target_unswizzled_preview_image(
    texture_object_lookup: dict[tuple[str, int], Any],
    assets_name: str,
    atlas_path_id: int,
    swizzle_verdict: str | None,
    preview_rotate: int = PS5_SWIZZLE_ROTATE,
) -> Image.Image | None:
    """KR: 대상 게임 Atlas(Texture2D)에서 검증용 unswizzle preview 이미지를 생성합니다.
    EN: Generate an unswizzled preview image from the target game atlas (Texture2D) for verification.
    """
    texture_obj = texture_object_lookup.get((assets_name, int(atlas_path_id)))
    if texture_obj is None:
        return None
    try:
        texture = texture_obj.parse_as_object()
        width = int(getattr(texture, "m_Width", 0) or 0)
        height = int(getattr(texture, "m_Height", 0) or 0)
        raw_data: bytes | None = None

        get_image_data = getattr(texture, "get_image_data", None)
        if callable(get_image_data):
            try:
                candidate = get_image_data()
                if isinstance(candidate, (bytes, bytearray)):
                    raw_data = bytes(candidate)
            except Exception:
                raw_data = None
        if raw_data is None:
            image_data = getattr(texture, "image_data", None)
            if isinstance(image_data, (bytes, bytearray)):
                raw_data = bytes(image_data)

        if width > 0 and height > 0 and raw_data:
            total_elements = width * height
            bpe: int | None = None
            try:
                texture_format = int(getattr(texture, "m_TextureFormat", -1) or -1)
            except Exception:
                texture_format = -1

            if _texture_format_is_bc(texture_format):
                bc_info = _PS5_BC_FORMATS.get(texture_format)
                if bc_info is not None:
                    block_w_px, block_h_px, bytes_per_block, _ = bc_info
                    logical_block_w = (width + block_w_px - 1) // block_w_px
                    logical_block_h = (height + block_h_px - 1) // block_h_px
                    logical_bytes = (
                        logical_block_w * logical_block_h * bytes_per_block
                    )
                    candidate_raw = raw_data[:logical_bytes]
                    best = None
                    if swizzle_verdict != "likely_linear_input":
                        mip_count = int(getattr(texture, "m_MipCount", 1) or 1)
                        best = _ps5_unswizzle_bc_best_layout_match(
                            raw_data,
                            width,
                            height,
                            texture_format,
                            mip_count=mip_count,
                        )
                    if best is not None:
                        best_raw, _, _, _, _ = best
                        if swizzle_verdict == "likely_swizzled_input":
                            candidate_raw = best_raw
                    rgba = _ps5_decode_bc_to_rgba(
                        candidate_raw, width, height, texture_format
                    )
                    if rgba is not None:
                        preview_rgba = Image.frombytes("RGBA", (width, height), rgba)
                        if _ps5_should_swap_rb_for_bc_preview(texture_format):
                            preview_rgba = _ps5_swap_rb_image(preview_rgba)
                        # KR: BC preview는 Unity 좌표계와 일치하도록 상하 반전
                        # EN: Flip vertically to match Unity coordinate system for BC preview
                        return ImageOps.flip(preview_rgba)

            bpe_hint = _texture_format_bytes_per_element(texture_format)
            if bpe_hint is not None:
                bpe = bpe_hint
            elif total_elements > 0 and (len(raw_data) % total_elements) == 0:
                derived_bpe = len(raw_data) // total_elements
                if derived_bpe in {1, 2, 3, 4}:
                    bpe = derived_bpe

            if bpe in {1, 2, 3, 4}:
                logical_bytes = width * height * int(bpe)
                usable_data = raw_data[: (len(raw_data) // int(bpe)) * int(bpe)]
                base_data = usable_data[:logical_bytes]
                processed = base_data
                preview_width = width
                preview_height = height
                unsw_variant = "normal"
                if swizzle_verdict == "likely_swizzled_input":
                    try:
                        processed, preview_width, preview_height, unsw_variant, _ = (
                            _ps5_unswizzle_best_variant(
                                usable_data,
                                width,
                                height,
                                int(bpe),
                                allow_axis_swap=True,
                                roughness_guard=True,
                            )
                        )
                    except Exception:
                        processed = base_data
                        preview_width = width
                        preview_height = height
                        unsw_variant = "normal"
                mode_map = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}
                preview_image = Image.frombytes(
                    mode_map[int(bpe)],
                    (preview_width, preview_height),
                    processed,
                )
                if (
                    swizzle_verdict == "likely_swizzled_input"
                    and unsw_variant != "already_linear"
                ):
                    # KR: 축-스왑(전치)된 경우에만 회전 적용 (예: Alpha8, bpe=1)
                    # EN: Apply rotation only for axis-swapped (transposed) case (e.g. Alpha8, bpe=1)
                    if unsw_variant == "swapped_axes" and preview_rotate % 360 != 0:
                        preview_image = preview_image.rotate(
                            preview_rotate % 360, expand=True
                        )
                else:
                    # KR: linear(비-swizzle) 텍스쳐는 Unity 좌표계(Y=0 하단)로 저장되므로 상하 반전 보정
                    # EN: Linear (non-swizzle) textures are stored in Unity coordinates (Y=0 bottom), so flip vertically
                    preview_image = ImageOps.flip(preview_image)
                if unsw_variant == "addrlib_4KB_S":
                    # KR: addrlib 비압축 복원 경로는 Y축이 뒤집힌 사례(ui_button)가 있어 보정
                    # EN: Addrlib uncompressed restore path has cases with flipped Y-axis (ui_button), so correct it
                    preview_image = ImageOps.flip(preview_image)
                return preview_image

        image = getattr(texture, "image", None)
        if isinstance(image, Image.Image):
            preview_image = image
            if swizzle_verdict == "likely_swizzled_input":
                try:
                    preview_image = apply_ps5_unswizzle_to_image(
                        preview_image,
                        rotate=preview_rotate,
                        allow_axis_swap=True,
                        roughness_guard=True,
                    )
                except Exception:
                    pass
            return preview_image
    except Exception:
        return None
    return None


def _save_swizzle_preview(
    image: Image.Image,
    *,
    preview_enabled: bool,
    preview_root: str | None,
    assets_file_name: str,
    assets_name: str,
    atlas_path_id: int,
    font_name: str,
    target_swizzled: bool,
    lang: Language,
) -> None:
    """KR: swizzle 상태 확인용 preview 이미지를 PNG로 저장합니다.
    EN: Save a preview image for swizzle state verification as PNG.
    """
    if not (preview_enabled and preview_root):
        return
    try:
        visible = _preview_visible_image(image)
        file_dir = sanitize_filename_component(assets_file_name, fallback="assets_file")
        out_dir = os.path.join(preview_root, file_dir)
        os.makedirs(out_dir, exist_ok=True)
        safe_assets = sanitize_filename_component(assets_name, fallback="assets")
        safe_font = sanitize_filename_component(font_name, fallback="font")
        state_label = "target_swizzled" if target_swizzled else "target_linear"
        out_name = f"{safe_assets}__{atlas_path_id}__{safe_font}__unswizzled__{state_label}.png"
        out_path = os.path.join(out_dir, out_name)
        visible.save(out_path, format="PNG")
        if lang == "ko":
            _log_console(f"  Preview 저장: {out_path}")
        else:
            _log_console(f"  Preview saved: {out_path}")
    except Exception as preview_error:
        if lang == "ko":
            _log_console(f"  경고: preview 저장 실패 ({preview_error})")
        else:
            _log_console(f"  Warning: failed to save preview ({preview_error})")


def _save_glyph_crop_previews(
    image: Image.Image,
    *,
    preview_enabled: bool,
    preview_root: str | None,
    assets_file_name: str,
    assets_name: str,
    atlas_path_id: int,
    font_name: str,
    sdf_data: JsonDict,
    lang: Language,
) -> None:
    """KR: 글리프 테이블에서 개별 문자 crop preview를 PNG로 저장합니다.
    EN: Save individual character crop previews from the glyph table as PNG.
    """
    if not (preview_enabled and preview_root):
        return
    glyph_table = sdf_data.get("m_GlyphTable")
    char_table = sdf_data.get("m_CharacterTable")
    if not isinstance(glyph_table, list) or not isinstance(char_table, list):
        return
    try:
        visible = _preview_visible_image(image)
        file_dir = sanitize_filename_component(assets_file_name, fallback="assets_file")
        safe_assets = sanitize_filename_component(assets_name, fallback="assets")
        safe_font = sanitize_filename_component(font_name, fallback="font")
        glyph_dir = os.path.join(
            preview_root,
            file_dir,
            f"{safe_assets}__{atlas_path_id}__{safe_font}",
        )
        os.makedirs(glyph_dir, exist_ok=True)

        glyph_rect_by_index: dict[int, tuple[int, int, int, int]] = {}
        for glyph in glyph_table:
            if not isinstance(glyph, dict):
                continue
            try:
                glyph_index = int(glyph.get("m_Index", -1))
            except Exception:
                continue
            rect_raw = glyph.get("m_GlyphRect", {})
            if not isinstance(rect_raw, dict):
                continue
            try:
                gx = int(rect_raw.get("m_X", 0))
                gy = int(rect_raw.get("m_Y", 0))
                gw = int(rect_raw.get("m_Width", 0))
                gh = int(rect_raw.get("m_Height", 0))
            except Exception:
                continue
            if gw <= 0 or gh <= 0:
                continue
            glyph_rect_by_index[glyph_index] = (gx, gy, gw, gh)

        if not glyph_rect_by_index:
            return

        saved = 0
        used_names: set[str] = set()
        for ch in char_table:
            if not isinstance(ch, dict):
                continue
            try:
                codepoint = int(ch.get("m_Unicode", -1))
                glyph_index = int(ch.get("m_GlyphIndex", -1))
            except Exception:
                continue
            if codepoint < 0:
                continue
            rect = glyph_rect_by_index.get(glyph_index)
            if rect is None:
                continue

            x, y, w, h = rect
            # KR: TMP new GlyphRect.y는 bottom-origin이므로 PIL(top-origin) crop 좌표로 변환
            # EN: TMP new GlyphRect.y is bottom-origin, so convert to PIL (top-origin) crop coordinates
            y = int(round(_tmp_flip_y_between_old_new(y, h, visible.height)))
            x0 = max(0, min(visible.width, x))
            y0 = max(0, min(visible.height, y))
            x1 = max(0, min(visible.width, x + w))
            y1 = max(0, min(visible.height, y + h))
            if x1 <= x0 or y1 <= y0:
                continue

            base = f"U+{codepoint:04X}"
            try:
                ch_text = chr(codepoint)
                if ch_text.isprintable() and not ch_text.isspace():
                    safe_char = sanitize_filename_component(
                        ch_text, fallback="", max_len=8
                    )
                    if safe_char and safe_char != "unnamed":
                        base = f"{base}_{safe_char}"
            except Exception:
                pass

            name = base
            if name in used_names:
                name = f"{name}_g{glyph_index}"
            used_names.add(name)
            out_path = os.path.join(glyph_dir, f"{name}.png")
            visible.crop((x0, y0, x1, y1)).save(out_path, format="PNG")
            saved += 1

        if saved > 0:
            if lang == "ko":
                _log_console(f"  Glyph preview 저장: {saved}개 -> {glyph_dir}")
            else:
                _log_console(f"  Glyph previews saved: {saved} -> {glyph_dir}")
    except Exception as preview_error:
        if lang == "ko":
            _log_console(f"  경고: glyph preview 저장 실패 ({preview_error})")
        else:
            _log_console(f"  Warning: failed to save glyph previews ({preview_error})")


def _prepare_texture_replacement_for_target(
    texture_plan: JsonDict,
    *,
    assets_file_name: str,
    target_assets_name: str,
    target_path_id: int,
    texture_object_lookup: dict[tuple[str, int], Any],
    texture_swizzle_state_cache: dict[str, tuple[str | None, str | None]],
    ps5_swizzle: bool,
    preview_export: bool,
    preview_root: str | None,
    lang: Language,
) -> JsonDict | None:
    """KR: 교체 Atlas의 swizzle 상태를 타겟에 맞추고 preview를 생성합니다.
    EN: Match the replacement atlas swizzle state to the target and generate previews.
    """
    source_atlas = _load_spilled_plan_image(
        texture_plan,
        image_key="source_atlas",
        path_key="source_atlas_path",
    )
    if not isinstance(source_atlas, Image.Image):
        return None

    alpha8_linear_source = _load_spilled_plan_image(
        texture_plan,
        image_key="alpha8_linear_source",
        path_key="alpha8_linear_source_path",
    )
    atlas_linear_for_alpha8 = (
        alpha8_linear_source
        if isinstance(alpha8_linear_source, Image.Image)
        else source_atlas
    )
    source_swizzled = parse_bool_flag(texture_plan.get("source_swizzled"))
    replacement_swizzle_hint = parse_bool_flag(
        texture_plan.get("replacement_swizzle_hint")
    )
    replacement_process_swizzle = parse_bool_flag(
        texture_plan.get("replacement_process_swizzle")
    )
    asset_process_swizzle = parse_bool_flag(texture_plan.get("asset_process_swizzle"))
    font_name = str(
        texture_plan.get("font_name")
        or texture_plan.get("replacement_font")
        or f"Texture_{target_path_id}"
    )
    try:
        atlas_metadata_width = int(
            texture_plan.get("metadata_width", source_atlas.width) or source_atlas.width
        )
    except Exception:
        atlas_metadata_width = int(source_atlas.width)
    try:
        atlas_metadata_height = int(
            texture_plan.get("metadata_height", source_atlas.height)
            or source_atlas.height
        )
    except Exception:
        atlas_metadata_height = int(source_atlas.height)

    target_swizzle_verdict: str | None = None
    target_swizzle_source: str | None = None
    target_is_swizzled: bool | None = None
    desired_swizzle_state = source_swizzled

    if ps5_swizzle:
        target_swizzle_verdict, target_swizzle_source = _detect_target_texture_swizzle(
            texture_object_lookup,
            texture_swizzle_state_cache,
            target_assets_name,
            int(target_path_id),
        )
        if target_swizzle_verdict == "likely_swizzled_input":
            target_is_swizzled = True
        elif target_swizzle_verdict == "likely_linear_input":
            target_is_swizzled = False
        elif replacement_swizzle_hint:
            target_is_swizzled = True

        if target_is_swizzled is not None:
            desired_swizzle_state = target_is_swizzled

    if replacement_process_swizzle or asset_process_swizzle:
        desired_swizzle_state = True

    if ps5_swizzle:
        if target_swizzle_verdict == "likely_swizzled_input":
            reason = (
                f" (근거: {target_swizzle_source})"
                if lang == "ko" and target_swizzle_source
                else (
                    f" (source: {target_swizzle_source})"
                    if target_swizzle_source
                    else ""
                )
            )
            if lang == "ko":
                _log_console(
                    f"  PS5 swizzle 감지: 대상 Atlas가 swizzled 상태로 판별되었습니다.{reason}"
                )
            else:
                _log_console(
                    f"  PS5 swizzle detect: target atlas is likely swizzled.{reason}"
                )
        elif target_swizzle_verdict == "likely_linear_input":
            reason = (
                f" (근거: {target_swizzle_source})"
                if lang == "ko" and target_swizzle_source
                else (
                    f" (source: {target_swizzle_source})"
                    if target_swizzle_source
                    else ""
                )
            )
            if lang == "ko":
                _log_console(
                    f"  PS5 swizzle 감지: 대상 Atlas가 선형(linear) 상태로 판별되었습니다.{reason}"
                )
            else:
                _log_console(
                    f"  PS5 swizzle detect: target atlas is likely linear.{reason}"
                )
        elif replacement_swizzle_hint:
            if lang == "ko":
                _log_console(
                    "  PS5 swizzle 힌트: JSON swizzle=yes 값을 기준으로 swizzle 적용합니다."
                )
            else:
                _log_console(
                    "  PS5 swizzle hint: applying swizzle based on JSON swizzle=yes."
                )
        elif lang == "ko":
            _log_console(
                "  PS5 swizzle 감지: inconclusive, 교체 Atlas 원본 상태를 유지합니다."
            )
        else:
            _log_console(
                "  PS5 swizzle detect: inconclusive, keeping replacement atlas state."
            )
    elif replacement_process_swizzle:
        if lang == "ko":
            _log_console(
                "  process_swizzle=True: 교체 Atlas를 swizzle 상태로 변환합니다."
            )
        else:
            _log_console(
                "  process_swizzle=True: converting replacement atlas to swizzled state."
            )

    _log_debug(
        f"[replace_texture_plan] file={assets_file_name} assets={target_assets_name} "
        f"path_id={target_path_id} source_swizzled={source_swizzled} "
        f"target_swizzle_verdict={target_swizzle_verdict} "
        f"target_swizzle_source={target_swizzle_source} "
        f"desired_swizzle={desired_swizzle_state}"
    )

    atlas_for_write = source_atlas
    if desired_swizzle_state != source_swizzled:
        try:
            if desired_swizzle_state:
                atlas_for_write = apply_ps5_swizzle_to_image(source_atlas)
            else:
                atlas_for_write = apply_ps5_unswizzle_to_image(source_atlas)
        except Exception as swizzle_error:
            atlas_for_write = source_atlas
            if lang == "ko":
                _log_console(
                    f"  경고: PS5 swizzle 변환 실패, 원본 Atlas를 사용합니다. ({swizzle_error})"
                )
            else:
                _log_console(
                    f"  Warning: PS5 swizzle transform failed; using original atlas. ({swizzle_error})"
                )

    if preview_export:
        preview_image = atlas_for_write
        if ps5_swizzle and desired_swizzle_state:
            try:
                preview_image = apply_ps5_unswizzle_to_image(atlas_for_write)
            except Exception as preview_unswizzle_error:
                preview_image = atlas_for_write
                if lang == "ko":
                    _log_console(
                        "  경고: preview unswizzle 실패, 저장 상태 Atlas 그대로 미리보기를 저장합니다. "
                        f"({preview_unswizzle_error})"
                    )
                else:
                    _log_console(
                        "  Warning: preview unswizzle failed; saving preview from stored atlas state. "
                        f"({preview_unswizzle_error})"
                    )
        _save_swizzle_preview(
            preview_image,
            preview_enabled=preview_export,
            preview_root=preview_root,
            assets_file_name=assets_file_name,
            assets_name=target_assets_name,
            atlas_path_id=int(target_path_id),
            font_name=font_name,
            target_swizzled=bool(desired_swizzle_state),
            lang=lang,
        )
        preview_sdf_data = texture_plan.get("preview_sdf_data")
        if isinstance(preview_sdf_data, dict):
            _save_glyph_crop_previews(
                preview_image,
                preview_enabled=preview_export,
                preview_root=preview_root,
                assets_file_name=assets_file_name,
                assets_name=target_assets_name,
                atlas_path_id=int(target_path_id),
                font_name=font_name,
                sdf_data=preview_sdf_data,
                lang=lang,
            )

    return {
        "replacement_image": atlas_for_write,
        "target_swizzled_state": target_is_swizzled,
        "replacement_linear_source": atlas_linear_for_alpha8,
        "metadata_size": (
            int(atlas_metadata_width),
            int(atlas_metadata_height),
        ),
    }


def _image_to_alpha8_bytes(image: Image.Image) -> tuple[bytes, int, int]:
    """KR: Pillow 이미지를 Alpha8 raw bytes로 변환합니다.
    EN: Convert a Pillow image to Alpha8 raw bytes.
    """
    if image.mode in {"RGBA", "LA"}:
        alpha = image.getchannel("A")
    elif image.mode == "L":
        alpha = image
    else:
        alpha = image.convert("L")
    return alpha.tobytes(), alpha.width, alpha.height


def _encode_alpha8_replacement_bytes(
    alpha_source: Image.Image,
    *,
    ps5_swizzle: bool,
    target_swizzled_state: bool | None,
) -> tuple[bytes, int, int, str]:
    """KR: Alpha8 교체 바이트를 타겟 swizzle 상태에 맞게 인코딩합니다.
    EN: Encode Alpha8 replacement bytes to match the target swizzle state.
    """
    if ps5_swizzle and target_swizzled_state is True:
        alpha_linear, aw, ah = _image_to_alpha8_bytes(alpha_source)
        alpha_linear_img = Image.frombytes("L", (int(aw), int(ah)), alpha_linear)
        alpha_swizzled_img = apply_ps5_swizzle_to_image(alpha_linear_img)
        alpha_raw, aw, ah = _image_to_alpha8_bytes(alpha_swizzled_img)
        return alpha_raw, aw, ah, "swizzled"

    if (not ps5_swizzle) or target_swizzled_state is False:
        alpha_raw, aw, ah = _image_to_alpha8_bytes(ImageOps.flip(alpha_source))
        return alpha_raw, aw, ah, "linear_flipped"

    alpha_raw, aw, ah = _image_to_alpha8_bytes(alpha_source)
    return alpha_raw, aw, ah, "direct"


@lru_cache(maxsize=128)
def _ps5_bit_positions(mask: int) -> tuple[int, ...]:
    """KR: 마스크에서 세트된 비트 위치 목록을 반환합니다.
    EN: Return a list of set bit positions in a mask.
    """
    return tuple(i for i in range(max(mask.bit_length(), 0)) if (mask >> i) & 1)


@lru_cache(maxsize=128)
def _ps5_axis_tile_size(mask: int) -> int:
    """KR: 마스크의 세트된 비트 수로부터 축별 타일 크기를 계산합니다.
    EN: Compute axis tile size from the number of set bits in a mask.
    """
    positions = _ps5_bit_positions(mask)
    return 1 << len(positions) if positions else 1


@lru_cache(maxsize=128)
def _ps5_deposit_table(mask: int) -> tuple[int, ...]:
    """KR: 마스크 비트폭(타일 기준) pdep 유사 배치 테이블을 생성합니다.
    EN: Generate a pdep-like deposit table for the mask bit width (tile-based).
    """
    positions = _ps5_bit_positions(mask)
    axis_size = _ps5_axis_tile_size(mask)
    table: list[int] = [0] * axis_size
    for value in range(axis_size):
        deposited = 0
        for bit_index, dst_bit in enumerate(positions):
            if (value >> bit_index) & 1:
                deposited |= 1 << dst_bit
        table[value] = deposited
    return tuple(table)


def _ps5_validate_texture_shape(
    data: bytes, width: int, height: int, bytes_per_element: int
) -> int:
    """KR: 텍스처 크기/데이터 길이를 검증하고 총 요소 수를 반환합니다.
    EN: Validate texture dimensions/data length and return the total element count.
    """
    if width <= 0 or height <= 0 or bytes_per_element <= 0:
        raise ValueError(
            f"Invalid texture shape for swizzle: width={width}, height={height}, bpe={bytes_per_element}"
        )
    total_elements = width * height
    expected_size = total_elements * bytes_per_element
    if len(data) < expected_size:
        raise ValueError(
            f"Texture data size mismatch: expected_at_least={expected_size}, got={len(data)} "
            f"(w={width}, h={height}, bpe={bytes_per_element})"
        )
    return total_elements


def _ps5_clip_to_base_level(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
) -> tuple[bytes, int]:
    """KR: mip0 기본 레벨만 남기고 초과 바이트를 잘라냅니다.
    EN: Clip to mip0 base level only, trimming excess bytes.
    """
    total_elements = _ps5_validate_texture_shape(data, width, height, bytes_per_element)
    expected_size = total_elements * bytes_per_element
    if len(data) > expected_size:
        return data[:expected_size], len(data) - expected_size
    return data, 0


def _texture_format_enum_name(texture_format: int) -> str:
    """KR: 텍스처 포맷 정수를 UnityPy enum 이름 문자열로 변환합니다.
    EN: Convert a texture format integer to a UnityPy enum name string.
    """
    value = int(texture_format)
    if _UnityTextureFormatEnum is not None:
        try:
            return str(_UnityTextureFormatEnum(value).name)
        except Exception:
            pass
    return f"TextureFormat_{value}"


def _texture_format_layout_details(texture_format: int) -> dict[str, Any] | None:
    """KR: 텍스처 포맷의 PS5 레이아웃 메타데이터(블록 크기, 디코더 등)를 반환합니다.
    EN: Return PS5 layout metadata (block size, decoder, etc.) for a texture format.
    """
    value = int(texture_format)
    meta = _PS5_LAYOUT_FORMAT_META.get(value)
    if meta is None:
        return None
    block_pack = int(meta["block_pack"])
    bytes_per_block, block_w, block_h, depth = _ps5_unpack_block_pack(block_pack)
    flags_word = _PS5_LAYOUT_FORMAT_FLAGS.get(value)
    layout_shift = ((flags_word & 0x6) * 2 + 8) if flags_word is not None else None
    mode_4kb_s_triplet = _PS5_4KB_S_TRIPLETS_BY_BLOCK_BYTES.get(bytes_per_block)
    return {
        "label": str(meta["label"]),
        "word0": int(meta["word0"]),
        "block_pack": block_pack,
        "bytes_per_block": bytes_per_block,
        "block_width": block_w,
        "block_height": block_h,
        "block_depth": depth,
        "decoder": _PS5_BC_DECODER_BY_FORMAT.get(value),
        "flags_word": int(flags_word) if flags_word is not None else None,
        "layout_shift": int(layout_shift) if layout_shift is not None else None,
        "mode_4kb_s_triplet": list(mode_4kb_s_triplet)
        if mode_4kb_s_triplet is not None
        else None,
    }


def _texture_format_bytes_per_element(texture_format: int) -> int | None:
    """KR: 텍스처 포맷에서 픽셀당 바이트 수(BPE)를 반환합니다.
    EN: Return bytes per pixel (BPE) for a texture format.
    """
    # KR: 가능한 경우 UnityPy enum 이름 기준으로 BPE를 해석 (버전별 숫자 변동 방지)
    # EN: Interpret BPE by UnityPy enum name when possible (prevents version-specific number changes)
    bpe_by_name = {
        "Alpha8": 1,
        "ARGB4444": 2,
        "RGB24": 3,
        "RGBA32": 4,
        "ARGB32": 4,
        "RGB565": 2,
        "R16": 2,
        "RG16": 2,
        "R8": 1,
    }
    value: int | None = None
    enum_name = _texture_format_enum_name(texture_format)
    if enum_name.startswith("TextureFormat_"):
        enum_name = ""
    if enum_name:
        value = bpe_by_name.get(enum_name)

    # KR: enum 해석 실패 시 최소 숫자 fallback.
    # EN: Minimal numeric fallback when enum interpretation fails.
    if value is None:
        format_to_bpe = {
            1: 1,  # Alpha8
            2: 2,  # ARGB4444
            3: 3,  # RGB24
            4: 4,  # RGBA32
            5: 4,  # ARGB32
            7: 2,  # RGB565
            9: 2,  # R16
            62: 2,  # RG16
            63: 1,  # R8
        }
        value = format_to_bpe.get(int(texture_format), None)

    if value in {1, 2, 3, 4}:
        return value
    return None


def _texture_format_is_bc(texture_format: int) -> bool:
    """KR: BC(블록 압축) 포맷 여부를 반환합니다.
    EN: Return whether the format is BC (block compressed).
    """
    return int(texture_format) in _PS5_BC_FORMATS


def _texture_format_is_crunched(texture_format: int) -> bool:
    """KR: Crunched(크런치 압축) 포맷 여부를 반환합니다.
    EN: Return whether the format is Crunched (crunch compressed).
    """
    value = int(texture_format)
    if value in {28, 29}:  # DXT1Crunched / DXT5Crunched
        return True
    enum_name = _texture_format_enum_name(value)
    return enum_name in {
        "DXT1Crunched",
        "DXT5Crunched",
        "ETC_RGB4Crunched",
        "ETC2_RGBA8Crunched",
    }


def ps5_unswizzle_bytes(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int | None = None,
    mask_y: int | None = None,
) -> bytes:
    """KR: PS5 swizzled 바이트 배열을 선형(행 우선) 순서로 변환합니다.
    mask_x/mask_y가 None이면 width/height에서 자동 계산합니다.
    EN: Convert a PS5 swizzled byte array to linear (row-major) order.
    Automatically computes mask_x/mask_y from width/height if None.
    """
    if not _ps5_dimensions_supported(width, height, bytes_per_element):
        clipped, _ = _ps5_clip_to_base_level(data, width, height, bytes_per_element)
        return clipped
    if mask_x is None or mask_y is None:
        mask_x, mask_y = compute_ps5_swizzle_masks(width, height, bytes_per_element)
    data, _ = _ps5_clip_to_base_level(data, width, height, bytes_per_element)
    total_elements = _ps5_validate_texture_shape(data, width, height, bytes_per_element)
    src = memoryview(data)
    dst = bytearray(len(data))
    tile_w = _ps5_axis_tile_size(mask_x)
    tile_h = _ps5_axis_tile_size(mask_y)
    xdep = _ps5_deposit_table(mask_x)
    ydep = _ps5_deposit_table(mask_y)
    macro_cols = (width + tile_w - 1) // tile_w
    tile_elements = tile_w * tile_h

    for y in range(height):
        row_start = y * width
        macro_y = y // tile_h
        local_y = y % tile_h
        row_offset = ydep[local_y]
        for x in range(width):
            macro_x = x // tile_w
            local_x = x % tile_w
            tile_base = ((macro_y * macro_cols) + macro_x) * tile_elements
            src_idx = tile_base + row_offset + xdep[local_x]
            if src_idx < 0 or src_idx >= total_elements:
                raise ValueError(
                    f"PS5 unswizzle index out of range: idx={src_idx}, total={total_elements}, "
                    f"w={width}, h={height}, mask_x={mask_x:#x}, mask_y={mask_y:#x}"
                )
            src_off = src_idx * bytes_per_element
            dst_off = (row_start + x) * bytes_per_element
            dst[dst_off : dst_off + bytes_per_element] = src[
                src_off : src_off + bytes_per_element
            ]

    return bytes(dst)


def ps5_swizzle_bytes(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int | None = None,
    mask_y: int | None = None,
) -> bytes:
    """KR: 선형(행 우선) 바이트 배열을 PS5 swizzle 순서로 변환합니다.
    mask_x/mask_y가 None이면 width/height에서 자동 계산합니다.
    EN: Convert a linear (row-major) byte array to PS5 swizzle order.
    Automatically computes mask_x/mask_y from width/height if None.
    """
    if not _ps5_dimensions_supported(width, height, bytes_per_element):
        clipped, _ = _ps5_clip_to_base_level(data, width, height, bytes_per_element)
        return clipped
    if mask_x is None or mask_y is None:
        mask_x, mask_y = compute_ps5_swizzle_masks(width, height, bytes_per_element)
    data, _ = _ps5_clip_to_base_level(data, width, height, bytes_per_element)
    total_elements = _ps5_validate_texture_shape(data, width, height, bytes_per_element)
    src = memoryview(data)
    dst = bytearray(len(data))
    tile_w = _ps5_axis_tile_size(mask_x)
    tile_h = _ps5_axis_tile_size(mask_y)
    xdep = _ps5_deposit_table(mask_x)
    ydep = _ps5_deposit_table(mask_y)
    macro_cols = (width + tile_w - 1) // tile_w
    tile_elements = tile_w * tile_h

    for y in range(height):
        row_start = y * width
        macro_y = y // tile_h
        local_y = y % tile_h
        row_offset = ydep[local_y]
        for x in range(width):
            macro_x = x // tile_w
            local_x = x % tile_w
            tile_base = ((macro_y * macro_cols) + macro_x) * tile_elements
            dst_idx = tile_base + row_offset + xdep[local_x]
            if dst_idx < 0 or dst_idx >= total_elements:
                raise ValueError(
                    f"PS5 swizzle index out of range: idx={dst_idx}, total={total_elements}, "
                    f"w={width}, h={height}, mask_x={mask_x:#x}, mask_y={mask_y:#x}"
                )
            src_off = (row_start + x) * bytes_per_element
            dst_off = dst_idx * bytes_per_element
            dst[dst_off : dst_off + bytes_per_element] = src[
                src_off : src_off + bytes_per_element
            ]

    return bytes(dst)


def _ps5_mode_for_swizzle(image: Image.Image) -> str:
    """KR: swizzle 처리에 적합한 PIL 모드를 결정합니다.
    EN: Determine the PIL mode suitable for swizzle processing.
    """
    mode = image.mode
    if mode in {"L", "LA", "RGB", "RGBA"}:
        return mode
    if mode == "P":
        return "L"
    return "RGBA"


def _ps5_prepare_image(image: Image.Image) -> Image.Image:
    """KR: swizzle 처리 전 이미지를 적합한 모드로 변환합니다.
    EN: Convert image to a suitable mode before swizzle processing.
    """
    mode = _ps5_mode_for_swizzle(image)
    if image.mode == mode:
        return image
    return image.convert(mode)


def _ps5_roughness_score(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
) -> float:
    """KR: 로컬 픽셀 변화량 기반 거칠기 점수를 계산합니다.
    낮을수록 부드러움 = linear 가능성 높음; swizzled 데이터는 노이즈처럼 보입니다.
    항상 인접 픽셀(step=1)을 비교하여 swizzle/linear를 정확히 판별합니다.
    성능을 위해 전체 픽셀 대신 행/열 서브셋을 샘플링합니다.
    EN: Compute a roughness score based on local pixel variation.
    Lower means smoother = likely linear; swizzled data looks like noise.
    Always compares adjacent pixels (step=1) to accurately distinguish swizzle/linear.
    Samples row/column subsets instead of all pixels for performance.
    """
    data, _ = _ps5_clip_to_base_level(data, width, height, bytes_per_element)
    _ps5_validate_texture_shape(data, width, height, bytes_per_element)
    view = memoryview(data)
    bpe = bytes_per_element

    # KR: --- 측정할 채널 결정 ---
    # EN: --- Determine channel to measure ---
    max_sample_lines = 256
    channel_index = 0
    if bpe > 1:
        # KR: 분산이 가장 높은(정보량이 가장 많은) 채널을 선택
        # EN: Select the channel with highest variance (most information)
        row_step = max(1, height // max_sample_lines)
        col_step = max(1, width // max_sample_lines)
        sums = [0.0] * bpe
        sums_sq = [0.0] * bpe
        sample_count = 0
        for y in range(0, height, row_step):
            row_base = y * width * bpe
            for x in range(0, width, col_step):
                base = row_base + x * bpe
                sample_count += 1
                for ch in range(bpe):
                    value = float(view[base + ch])
                    sums[ch] += value
                    sums_sq[ch] += value * value
        if sample_count > 0:
            best_var = -1.0
            for ch in range(bpe):
                mean = sums[ch] / sample_count
                variance = (sums_sq[ch] / sample_count) - (mean * mean)
                if variance > best_var:
                    best_var = variance
                    channel_index = ch

    # KR: --- dx(수평) 측정: 행 샘플링, 항상 인접 픽셀 비교 ---
    # EN: --- dx (horizontal) measurement: row sampling, always comparing adjacent pixels ---
    dx_sum = 0.0
    dx_count = 0
    row_step = max(1, height // max_sample_lines)
    if width > 1:
        for y in range(0, height, row_step):
            row_base = y * width * bpe
            for x in range(width - 1):
                left_idx = row_base + x * bpe + channel_index
                right_idx = left_idx + bpe          # step=1, KR: 항상 인접 / EN: always adjacent
                dx_sum += abs(float(view[right_idx]) - float(view[left_idx]))
                dx_count += 1

    # KR: --- dy(수직) 측정: 열 샘플링, 항상 인접 픽셀 비교 ---
    # EN: --- dy (vertical) measurement: column sampling, always comparing adjacent pixels ---
    dy_sum = 0.0
    dy_count = 0
    col_step = max(1, width // max_sample_lines)
    if height > 1:
        row_stride = width * bpe
        for x in range(0, width, col_step):
            col_base = x * bpe + channel_index
            for y in range(height - 1):
                up_idx = col_base + y * row_stride
                down_idx = up_idx + row_stride      # step=1, KR: 항상 인접 / EN: always adjacent
                dy_sum += abs(float(view[down_idx]) - float(view[up_idx]))
                dy_count += 1

    dx = dx_sum / dx_count if dx_count else 0.0
    dy = dy_sum / dy_count if dy_count else 0.0
    return float(dx + dy)


def detect_ps5_swizzle_state(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int | None = None,
    mask_y: int | None = None,
) -> tuple[str, float, float, float, bytes, bytes]:
    """KR: 입력 바이트가 swizzled인지 휴리스틱으로 판별합니다.
    EN: Heuristically determine whether input bytes are swizzled.
    """
    data, _ = _ps5_clip_to_base_level(data, width, height, bytes_per_element)
    if not _ps5_dimensions_supported(width, height, bytes_per_element):
        raw_score = _ps5_roughness_score(data, width, height, bytes_per_element)
        return "inconclusive", raw_score, raw_score, raw_score, data, data
    if mask_x is None or mask_y is None:
        mask_x, mask_y = compute_ps5_swizzle_masks(width, height, bytes_per_element)
    raw_score = _ps5_roughness_score(data, width, height, bytes_per_element)
    unswizzled = ps5_unswizzle_bytes(
        data, width, height, bytes_per_element, mask_x=mask_x, mask_y=mask_y
    )
    swizzled = ps5_swizzle_bytes(
        data, width, height, bytes_per_element, mask_x=mask_x, mask_y=mask_y
    )
    unsw_score = _ps5_roughness_score(unswizzled, width, height, bytes_per_element)
    swz_score = _ps5_roughness_score(swizzled, width, height, bytes_per_element)

    if unsw_score < raw_score * 0.92 and unsw_score <= swz_score * 0.98:
        verdict = "likely_swizzled_input"
    elif raw_score <= unsw_score * 0.92 and raw_score <= swz_score * 0.92:
        verdict = "likely_linear_input"
    else:
        verdict = "inconclusive"

    return verdict, raw_score, unsw_score, swz_score, unswizzled, swizzled


def _ps5_unswizzle_best_variant(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int | None = None,
    mask_y: int | None = None,
    allow_axis_swap: bool = False,
    roughness_guard: bool = False,
) -> tuple[bytes, int, int, str, float]:
    """KR: bpe별 축-전치 규칙에 따라 unswizzle 후보를 선택합니다.
    roughness_guard=True이면, unswizzle 결과가 원본보다 거칠 경우 원본을 반환합니다.
    축 전치는 bpe에 따라 달라집니다:
      bpe=1 (Alpha8): 항상 전치 -> (H,W)로 unswizzle 후 90도 회전으로 복원.
      bpe=4 (RGBA32): 전치 안 함 -> (W,H)로 직접 unswizzle.
    EN: Select an unswizzle candidate according to per-bpe axis-transpose rules.
    If roughness_guard=True, returns original when unswizzle result is rougher.
    Axis transposition varies by bpe:
      bpe=1 (Alpha8): always transpose -> unswizzle as (H,W) then restore via 90-degree rotation.
      bpe=4 (RGBA32): no transpose -> unswizzle directly as (W,H).
    """
    logical_bytes = width * height * bytes_per_element
    usable = data[: (len(data) // bytes_per_element) * bytes_per_element]
    clipped = usable[:logical_bytes]

    # KR: roughness guard를 위해 원본 roughness를 미리 계산합니다.
    # EN: Pre-compute original roughness for roughness guard.
    raw_score = (
        _ps5_roughness_score(clipped, width, height, bytes_per_element)
        if roughness_guard
        else None
    )

    # KR: bpe별 축 전치 규칙 결정.
    # EN: Determine axis transpose rule by bpe.
    should_transpose = _PS5_AXIS_TRANSPOSE.get(bytes_per_element, False)

    if (
        allow_axis_swap
        and should_transpose
        and mask_x is None
        and mask_y is None
        and _ps5_dimensions_supported(height, width, bytes_per_element)
    ):
        # KR: 전치 bpe (예: Alpha8): 종횡비와 무관하게 (H,W)로 unswizzle 후 회전 후보로 취급합니다
        #     (정사각형 텍스처 포함).
        # EN: Transposed bpe (e.g. Alpha8): unswizzle as (H,W) regardless of aspect ratio, treated as rotation candidate
        #     (including square textures).
        try:
            swapped = ps5_unswizzle_bytes(
                clipped,
                height,
                width,
                bytes_per_element,
                mask_x=None,
                mask_y=None,
            )
            best_data = swapped
            best_width = height
            best_height = width
            best_variant = "swapped_axes"
            best_score = _ps5_roughness_score(
                swapped, height, width, bytes_per_element
            )
        except Exception:
            # KR: 일반 모드로 폴백
            # EN: Fallback to normal mode
            normal = ps5_unswizzle_bytes(
                clipped, width, height, bytes_per_element,
                mask_x=mask_x, mask_y=mask_y,
            )
            best_data = normal
            best_width = width
            best_height = height
            best_variant = "normal"
            best_score = _ps5_roughness_score(normal, width, height, bytes_per_element)
    else:
        # KR: 비전치 bpe (예: RGBA32) 또는 정사각형: (W,H)로 unswizzle합니다.
        # EN: Non-transposed bpe (e.g. RGBA32) or square: unswizzle as (W,H).
        normal = ps5_unswizzle_bytes(
            clipped, width, height, bytes_per_element,
            mask_x=mask_x, mask_y=mask_y,
        )
        best_data = normal
        best_width = width
        best_height = height
        best_variant = "normal"
        best_score = _ps5_roughness_score(normal, width, height, bytes_per_element)

    # KR: 일부 RGBA/LA 텍스처는 addrlib 기반 4KB_S 경로가 더 정확합니다.
    # EN: Some RGBA/LA textures are more accurate with addrlib-based 4KB_S path.
    if (
        best_width == width
        and best_height == height
        and bytes_per_element in {2, 4}
    ):
        addrlib_candidate = _ps5_unswizzle_addrlib_uncompressed_candidate(
            usable, width, height, bytes_per_element
        )
        if addrlib_candidate is not None:
            addrlib_data, addrlib_score = addrlib_candidate
            if addrlib_score < (best_score * 0.98):
                best_data = addrlib_data
                best_width = width
                best_height = height
                best_variant = "addrlib_4KB_S"
                best_score = addrlib_score

    # KR: Roughness guard – unswizzle 결과가 원본보다 거칠면, 원본이 이미 linear입니다.
    # EN: Roughness guard - if unswizzle result is rougher than original, original is already linear.
    if roughness_guard and raw_score is not None and best_score >= raw_score * 0.92:
        return clipped, width, height, "already_linear", raw_score

    return best_data, best_width, best_height, best_variant, best_score


def detect_ps5_swizzle_state_from_image(
    image: Image.Image,
    mask_x: int | None = None,
    mask_y: int | None = None,
    rotate: int = PS5_SWIZZLE_ROTATE,
) -> tuple[str, float, float, float]:
    """KR: Pillow 이미지의 swizzle 상태를 판별합니다.
    EN: Determine the swizzle state of a Pillow image.
    """
    prepared = _ps5_prepare_image(image)

    data = prepared.tobytes()
    bytes_per_element = len(prepared.getbands())
    verdict, raw_score, unsw_score, swz_score, _, _ = detect_ps5_swizzle_state(
        data,
        prepared.width,
        prepared.height,
        bytes_per_element,
        mask_x=mask_x,
        mask_y=mask_y,
    )
    return verdict, raw_score, unsw_score, swz_score


def apply_ps5_swizzle_to_image(
    image: Image.Image,
    mask_x: int | None = None,
    mask_y: int | None = None,
    rotate: int = PS5_SWIZZLE_ROTATE,
) -> Image.Image:
    """KR: 선형 이미지에 PS5 swizzle 변환을 적용합니다.
    EN: Apply PS5 swizzle transform to a linear image.
    """
    prepared = _ps5_prepare_image(image)
    bytes_per_element = len(prepared.getbands())
    if not _ps5_dimensions_supported(prepared.width, prepared.height, bytes_per_element):
        return prepared
    # KR: 전치 bpe (예: Alpha8)에만 역방향 회전을 적용합니다.
    # EN: Apply inverse rotation only for transposed bpe (e.g. Alpha8).
    should_transpose = _PS5_AXIS_TRANSPOSE.get(bytes_per_element, False)
    if should_transpose and rotate % 360 != 0:
        prepared = prepared.rotate((-rotate) % 360, expand=True)
    if not _ps5_dimensions_supported(prepared.width, prepared.height, bytes_per_element):
        return _ps5_prepare_image(image)

    data = prepared.tobytes()
    swizzled = ps5_swizzle_bytes(
        data,
        prepared.width,
        prepared.height,
        bytes_per_element,
        mask_x=mask_x,
        mask_y=mask_y,
    )
    return Image.frombytes(prepared.mode, (prepared.width, prepared.height), swizzled)


def apply_ps5_unswizzle_to_image(
    image: Image.Image,
    mask_x: int | None = None,
    mask_y: int | None = None,
    rotate: int = PS5_SWIZZLE_ROTATE,
    allow_axis_swap: bool = False,
    roughness_guard: bool = False,
) -> Image.Image:
    """KR: swizzled 이미지에 PS5 unswizzle 변환을 적용합니다.
    EN: Apply PS5 unswizzle transform to a swizzled image.
    roughness_guard=True이면, 이미 linear인 입력은 변환하지 않습니다.
    """
    prepared = _ps5_prepare_image(image)
    bytes_per_element = len(prepared.getbands())
    if not _ps5_dimensions_supported(prepared.width, prepared.height, bytes_per_element):
        return prepared
    data = prepared.tobytes()
    unswizzled, out_width, out_height, variant, _ = _ps5_unswizzle_best_variant(
        data,
        prepared.width,
        prepared.height,
        bytes_per_element,
        mask_x=mask_x,
        mask_y=mask_y,
        allow_axis_swap=allow_axis_swap,
        roughness_guard=roughness_guard,
    )
    if variant == "already_linear":
        return prepared
    output = Image.frombytes(prepared.mode, (out_width, out_height), unswizzled)
    # KR: rotate는 축-스왑(전치) 된 경우에만 적용합니다 (예: Alpha8).
    # EN: Rotation is applied only when axis-swap (transpose) occurred (e.g. Alpha8).
    if variant == "swapped_axes" and rotate % 360 != 0:
        output = output.rotate(rotate % 360, expand=True)
    return output


def detect_texture_object_ps5_swizzle(
    texture_obj: Any,
    mask_x: int | None = None,
    mask_y: int | None = None,
    rotate: int = PS5_SWIZZLE_ROTATE,
) -> str | None:
    """KR: Texture2D 오브젝트의 swizzle 상태를 판별합니다.
    EN: Determines the swizzle state of a Texture2D object.
    """
    verdict, _ = detect_texture_object_ps5_swizzle_detail(
        texture_obj,
        mask_x=mask_x,
        mask_y=mask_y,
        rotate=rotate,
    )
    return verdict


def detect_texture_object_ps5_swizzle_detail(
    texture_obj: Any,
    mask_x: int | None = None,
    mask_y: int | None = None,
    rotate: int = PS5_SWIZZLE_ROTATE,
) -> tuple[str | None, str | None]:
    """KR: Texture2D 오브젝트의 swizzle 상태를 판별합니다.
    반환값은 (판정값, 판정근거)입니다.
    EN: Determines the swizzle state of a Texture2D object.
    Returns (verdict, reason).
    """
    try:
        texture = texture_obj.parse_as_object()
        width = int(getattr(texture, "m_Width", 0) or 0)
        height = int(getattr(texture, "m_Height", 0) or 0)
        stream_data = getattr(texture, "m_StreamData", None)
        try:
            stream_size = int(getattr(stream_data, "size", 0) or 0)
        except Exception:
            stream_size = 0
        is_readable = bool(getattr(texture, "m_IsReadable", False))
        try:
            texture_format = int(getattr(texture, "m_TextureFormat", -1) or -1)
        except Exception:
            texture_format = -1

        image_data = getattr(texture, "image_data", None)
        if isinstance(image_data, (bytes, bytearray)):
            image_data_len = len(image_data)
        else:
            image_data_len = 0

        # KR: 포맷/메타데이터 기반 공용 규칙:
        #  - BC: stream+non-readable => swizzled, inline+readable => linear
        #  - Crunched: UnityPy decode 경로 기준 linear 취급
        #  - Uncompressed: stream/inline 메타 + bpe 일치 여부로 판정
        # EN: Common rules based on format/metadata:
        #  - BC: stream+non-readable => swizzled, inline+readable => linear
        #  - Crunched: treated as linear per UnityPy decode path
        #  - Uncompressed: determined by stream/inline meta + bpe match
        meta_hint: str | None = None
        meta_source: str | None = None
        if width > 0 and height > 0:
            if _texture_format_is_crunched(texture_format):
                return "likely_linear_input", "crunched-unitypy-decode"

            expected_alpha8_size = width * height
            if (
                texture_format == 1
                and stream_size > 0
                and not is_readable
                and stream_size == expected_alpha8_size
            ):
                meta_hint = "likely_swizzled_input"
                meta_source = "meta-alpha8-stream"
            elif (
                texture_format == 1
                and stream_size == 0
                and not is_readable
                and image_data_len == expected_alpha8_size
            ):
                meta_hint = "likely_swizzled_input"
                meta_source = "meta-alpha8-inline-nonread"
            elif stream_size > 0 and not is_readable:
                meta_hint = "likely_swizzled_input"
                meta_source = "meta-stream"
            elif stream_size == 0 and is_readable and image_data_len > 0:
                meta_hint = "likely_linear_input"
                meta_source = "meta-inline"

        # KR: 메타 기준이 확실하면 유사도보다 우선합니다.
        # EN: If metadata criteria are definitive, they take priority over similarity.
        if meta_hint is not None:
            return meta_hint, meta_source or "meta"

        if width > 0 and height > 0:
            raw_data: bytes | None = None
            get_image_data = getattr(texture, "get_image_data", None)
            if callable(get_image_data):
                try:
                    candidate = get_image_data()
                    if isinstance(candidate, (bytes, bytearray)):
                        raw_data = bytes(candidate)
                except Exception:
                    raw_data = None
            if raw_data is None:
                image_data = getattr(texture, "image_data", None)
                if isinstance(image_data, (bytes, bytearray)):
                    raw_data = bytes(image_data)

            if raw_data:
                if _texture_format_is_bc(texture_format):
                    # KR: BC 포맷은 descriptor 비트(타일모드/selector)가 핵심이며
                    # 현재 자산 API에서 직접 노출되지 않으므로, 휴리스틱 점수 판별을 피합니다.
                    # EN: BC format relies on descriptor bits (tile mode/selector) which
                    # are not directly exposed by the current asset API, so heuristic scoring is avoided.
                    if stream_size > 0 and not is_readable:
                        return "likely_swizzled_input", "bc-meta-stream"
                    if stream_size == 0 and is_readable and image_data_len > 0:
                        return "likely_linear_input", "bc-meta-inline"
                    return "inconclusive", "bc-descriptor-unavailable"

                total_elements = width * height
                bytes_per_element: int | None = _texture_format_bytes_per_element(
                    texture_format
                )
                if (
                    bytes_per_element is None
                    and total_elements > 0
                    and (len(raw_data) % total_elements) == 0
                ):
                    derived = len(raw_data) // total_elements
                    if derived in {1, 2, 3, 4}:
                        bytes_per_element = int(derived)
                if bytes_per_element in {1, 2, 3, 4}:
                    expected_base = width * height * int(bytes_per_element)
                    if (
                        stream_size > 0
                        and not is_readable
                        and len(raw_data) >= expected_base
                    ):
                        return "likely_swizzled_input", "raw-meta-stream-bpe"
                    if stream_size == 0 and len(raw_data) >= expected_base:
                        return "likely_linear_input", "raw-meta-inline-bpe"

        image = getattr(texture, "image", None)
        if isinstance(image, Image.Image):
            verdict, _, _, _ = detect_ps5_swizzle_state_from_image(
                image,
                mask_x=mask_x,
                mask_y=mask_y,
                rotate=rotate,
            )
            return verdict, "image"
        return None, None
    except Exception:
        return None, None


def build_replacement_lookup(
    replacements: dict[str, JsonDict],
) -> tuple[dict[tuple[str, str, str, int], str], set[str]]:
    """KR: 교체 JSON을 빠른 조회용 룩업 테이블로 변환합니다.
    (Type, File, assets_name, Path_ID) → font_name 매핑을 생성합니다.
    EN: Converts the replacement JSON into a fast-lookup table.
    Builds a (Type, File, assets_name, Path_ID) → font_name mapping.
    """
    lookup: dict[tuple[str, str, str, int], str] = {}
    files_to_process: set[str] = set()

    for info in replacements.values():
        replace_to = info.get("Replace_to")
        if not replace_to:
            continue

        file_name_raw = info.get("File")
        assets_name_raw = info.get("assets_name")
        path_id_raw = info.get("Path_ID")
        type_name_raw = info.get("Type")

        if not isinstance(file_name_raw, str) or not file_name_raw:
            continue
        if not isinstance(assets_name_raw, str) or not assets_name_raw:
            continue
        if not isinstance(type_name_raw, str) or not type_name_raw:
            continue
        if path_id_raw is None:
            continue

        try:
            path_id = int(path_id_raw)
        except (TypeError, ValueError):
            continue

        normalized_target = normalize_font_name(str(replace_to))
        lookup[(type_name_raw, file_name_raw, assets_name_raw, path_id)] = (
            normalized_target
        )
        files_to_process.add(file_name_raw)

    return lookup, files_to_process


def debug_parse_enabled() -> bool:
    """KR: 디버그 파싱 로그 활성화 여부를 반환합니다.
    EN: Returns whether debug parse logging is enabled.
    """
    return os.environ.get("UFR_DEBUG_PARSE", "").strip() == "1"


def debug_parse_log(message: str) -> None:
    """KR: 디버그 모드일 때만 파싱 로그를 출력합니다.
    EN: Outputs parse logs only when debug mode is active.
    """
    if debug_parse_enabled():
        _log_console(message)


def _log_scan_result_details(
    file_name: str, scanned: dict[str, list[JsonDict]]
) -> None:
    """KR: 스캔 결과를 파일/폰트 단위 DEBUG 로그로 남깁니다.
    EN: Logs scan results at file/font level as DEBUG output.
    """
    ttf_entries = list(scanned.get("ttf", []))
    sdf_entries = list(scanned.get("sdf", []))
    _log_debug(
        f"[scan_debug] file={file_name} ttf_count={len(ttf_entries)} sdf_count={len(sdf_entries)}"
    )

    for font_entry in ttf_entries:
        assets_name = str(font_entry.get("assets_name", ""))
        font_name = str(font_entry.get("name", ""))
        path_id = font_entry.get("path_id")
        _log_debug(
            f"[scan_debug] type=TTF file={file_name} assets={assets_name} path_id={path_id} name={font_name}"
        )

    for font_entry in sdf_entries:
        assets_name = str(font_entry.get("assets_name", ""))
        font_name = str(font_entry.get("name", ""))
        path_id = font_entry.get("path_id")
        swizzle = font_entry.get("swizzle")
        swizzle_text = f" swizzle={swizzle}" if swizzle is not None else ""
        _log_debug(
            f"[scan_debug] type=SDF file={file_name} assets={assets_name} path_id={path_id} name={font_name}{swizzle_text}"
        )


def _is_scan_retry_candidate(
    scanned: dict[str, list[JsonDict]],
    worker_error: str | None,
) -> bool:
    """KR: 최종 순차 재시도 대상(실패/빈 결과)을 판정합니다.
    EN: Determines if a scan result qualifies for final sequential retry (failure/empty results).
    """
    if not isinstance(worker_error, str) or not worker_error.strip():
        return False
    if list(scanned.get("ttf", [])) or list(scanned.get("sdf", [])):
        return False
    lowered = worker_error.lower()
    if "scan worker" not in lowered:
        return False
    if "failed" in lowered or "실패" in worker_error or "exit=" in lowered:
        return True
    return False


def _log_replacement_plan_details(
    file_name: str,
    replacement_mapping: dict[str, JsonDict],
) -> None:
    """KR: 파일별 교체 계획을 DEBUG 로그로 기록합니다.
    EN: Records the per-file replacement plan as DEBUG log.
    """
    if not replacement_mapping:
        _log_debug(f"[replace_plan] file={file_name} targets=0")
        return

    ttf_count = sum(
        1 for item in replacement_mapping.values() if item.get("Type") == "TTF"
    )
    sdf_count = sum(
        1 for item in replacement_mapping.values() if item.get("Type") == "SDF"
    )
    _log_debug(
        f"[replace_plan] file={file_name} targets={len(replacement_mapping)} ttf={ttf_count} sdf={sdf_count}"
    )

    for entry_key in sorted(replacement_mapping.keys()):
        entry = replacement_mapping[entry_key]
        type_name = str(entry.get("Type", ""))
        assets_name = str(entry.get("assets_name", ""))
        path_id = entry.get("Path_ID")
        source_name = str(entry.get("Name", ""))
        replace_to = str(entry.get("Replace_to", ""))
        force_raster = entry.get("force_raster")
        swizzle = entry.get("swizzle")
        process_swizzle = entry.get("process_swizzle")
        extra_flags = ""
        if (
            force_raster is not None
            or swizzle is not None
            or process_swizzle is not None
        ):
            extra_flags = (
                f" force_raster={force_raster} swizzle={swizzle} "
                f"process_swizzle={process_swizzle}"
            )
        _log_debug(
            f"[replace_plan] type={type_name} file={file_name} assets={assets_name} path_id={path_id} "
            f"name={source_name} replace_to={replace_to}{extra_flags}"
        )


def ensure_int(data: JsonDict | None, keys: Iterable[str]) -> None:
    """KR: 딕셔너리의 지정 키 값을 int로 강제 변환합니다.
    EN: Force-converts the specified key values in a dictionary to int.
    """
    if not data:
        return
    for key in keys:
        if key in data and data[key] is not None:
            data[key] = int(data[key])


@lru_cache(maxsize=256)
def _parse_unity_version_triplet(version_text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_text or "")
    if not match:
        return None
    try:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    except Exception:
        return None


@lru_cache(maxsize=1)
def _load_tmp_info_unity_field_index() -> dict[tuple[int, int, int], set[str]]:
    """KR: TMP_Info의 Unity 축 스냅샷에서 버전별 최상위 필드 인덱스를 로드합니다.
    EN: Loads per-version top-level field index from TMP_Info Unity axis snapshots.
    """
    try:
        path = os.path.join(
            get_script_dir(), "TMP_Info", "02_unity_version_changes.json"
        )
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        snapshots = obj.get("snapshots", []) if isinstance(obj, dict) else []
        index: dict[tuple[int, int, int], set[str]] = {}
        if not isinstance(snapshots, list):
            return {}
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                continue
            if not bool(snapshot.get("has_type", False)):
                continue
            version_text = str(snapshot.get("version", "") or "")
            triplet = _parse_unity_version_triplet(version_text)
            if triplet is None:
                continue
            declared_fields = snapshot.get("declared_fields", [])
            if not isinstance(declared_fields, list):
                continue
            fields: set[str] = set()
            for field in declared_fields:
                if isinstance(field, str) and field:
                    fields.add(field)
                elif isinstance(field, dict):
                    name = field.get("name")
                    if isinstance(name, str) and name:
                        fields.add(name)
            if fields:
                index[triplet] = fields
        return index
    except Exception:
        return {}


@lru_cache(maxsize=256)
def _get_tmp_info_fields_for_unity(unity_version: str | None) -> set[str]:
    """KR: Unity 버전에 가장 가까운 TMP_Info 스냅샷 필드 집합을 반환합니다.
    EN: Returns the TMP_Info snapshot field set closest to the given Unity version.
    """
    if not unity_version:
        return set()
    triplet = _parse_unity_version_triplet(str(unity_version))
    if triplet is None:
        return set()
    index = _load_tmp_info_unity_field_index()
    if not index:
        return set()
    if triplet in index:
        return set(index[triplet])
    lower_or_equal = [key for key in index.keys() if key <= triplet]
    if lower_or_equal:
        return set(index[max(lower_or_equal)])
    return set(index[min(index.keys())])


def _resolve_creation_settings_key(
    data: JsonDict, unity_version: str | None = None
) -> str | None:
    """KR: 타겟 딕셔너리에서 creation settings 키를 판별합니다.
    EN: Identifies the creation settings key in the target dictionary.
    """
    for key in _TMP_CREATION_SETTINGS_KEYS:
        if isinstance(data.get(key), dict):
            return key
    expected_fields = _get_tmp_info_fields_for_unity(unity_version)
    for key in _TMP_CREATION_SETTINGS_KEYS:
        if key in expected_fields and key in data and isinstance(data.get(key), dict):
            return key
    return None


def _sync_creation_settings_payload(
    creation_settings: JsonDict,
    atlas_width: int,
    atlas_height: int,
    padding: int,
    point_size: int,
) -> None:
    """KR: creation settings 내부 키 패턴을 감지해 atlas/pointSize를 동기화합니다.
    EN: Detects key patterns inside creation settings and syncs atlas/pointSize.
    """
    for key in list(creation_settings.keys()):
        normalized = key.replace("_", "").lower()
        if "atlaswidth" in normalized:
            creation_settings[key] = int(atlas_width)
        elif "atlasheight" in normalized:
            creation_settings[key] = int(atlas_height)
        elif normalized.endswith("padding") or normalized == "padding":
            creation_settings[key] = int(padding)
        elif normalized.endswith("pointsize") or normalized == "pointsize":
            creation_settings[key] = int(point_size)


def _tmp_version_hint(unity_version: str | None) -> Literal["new", "old"] | None:
    if not unity_version:
        return None
    triplet = _parse_unity_version_triplet(str(unity_version))
    if triplet is None:
        return None
    if triplet <= _TMP_OLD_ONLY_LAST:
        return "old"
    if triplet >= _TMP_NEW_SCHEMA_FIRST:
        return "new"
    return None


def _safe_list_len(value: Any) -> int:
    """KR: 리스트이면 길이를 반환하고, 아니면 0을 반환합니다.
    EN: Returns the length if it is a list, otherwise returns 0.
    """
    return len(value) if isinstance(value, list) else 0


def _first_atlas_ref(value: Any) -> JsonDict | None:
    """KR: 아틀라스 텍스처 리스트에서 첫 번째 딕셔너리 참조를 반환합니다.
    EN: Returns the first dictionary reference from the atlas texture list.
    """
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, dict):
            return cast(JsonDict, item)
    return None


def _atlas_ref_ids(ref: Any) -> tuple[int, int]:
    """KR: 아틀라스 참조 딕셔너리에서 (m_FileID, m_PathID) 튜플을 추출합니다.
    EN: Extracts the (m_FileID, m_PathID) tuple from an atlas reference dictionary.
    """
    if not isinstance(ref, dict):
        return 0, 0
    try:
        file_id = int(ref.get("m_FileID", 0) or 0)
    except Exception:
        file_id = 0
    try:
        path_id = int(ref.get("m_PathID", 0) or 0)
    except Exception:
        path_id = 0
    return file_id, path_id


def _normalize_assets_basename(value: Any) -> str | None:
    """KR: 에셋 경로에서 파일명(basename)만 정규화하여 반환합니다.
    EN: Normalizes and returns only the filename (basename) from an asset path.
    """
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    normalized = text.replace("\\", "/")
    name = os.path.basename(normalized)
    return name or None


def _normalize_asset_lookup_path(value: Any) -> str | None:
    """KR: 에셋 조회용 경로를 정규화합니다. archive://, file:// 접두사를 제거하고 소문자로 변환합니다.
    EN: Normalizes the asset lookup path. Strips archive://, file:// prefixes and converts to lowercase.
    """
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    normalized = text.replace("\\", "/")
    lowered = normalized.lower()
    for prefix in ("archive://", "archive:/", "file://"):
        if lowered.startswith(prefix):
            normalized = normalized[len(prefix) :]
            lowered = normalized.lower()
            break
    while normalized.startswith("/"):
        normalized = normalized[1:]
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = re.sub(r"/{2,}", "/", normalized).strip()
    return normalized.lower() if normalized else None


def _normalize_asset_file_key(path: Any) -> str | None:
    """KR: 에셋 파일 경로를 절대경로 기반의 정규화된 키로 변환합니다.
    EN: Converts an asset file path to a normalized key based on its absolute path.
    """
    text = str(path).strip() if path is not None else ""
    if not text:
        return None
    return os.path.normcase(os.path.abspath(text))


def _build_asset_file_index(
    all_assets_files: list[str],
    data_path: str,
) -> dict[str, Any]:
    """KR: 모든 에셋 파일 목록으로부터 상대경로/basename 기반 인덱스를 구축합니다.
    EN: Builds a relative-path/basename-based index from the full list of asset files.
    """
    data_root = os.path.abspath(data_path)
    path_by_key: dict[str, str] = {}
    relpath_to_keys: dict[str, list[str]] = {}
    basename_to_keys: dict[str, list[str]] = {}
    relpath_by_key: dict[str, str] = {}
    basename_by_key: dict[str, str] = {}

    for candidate_path in sorted(all_assets_files):
        key = _normalize_asset_file_key(candidate_path)
        if not key:
            continue
        abs_path = os.path.abspath(candidate_path)
        rel_path = os.path.relpath(abs_path, data_root).replace("\\", "/").lower()
        basename = os.path.basename(abs_path).lower()
        path_by_key[key] = abs_path
        relpath_by_key[key] = rel_path
        basename_by_key[key] = basename
        relpath_to_keys.setdefault(rel_path, []).append(key)
        basename_to_keys.setdefault(basename, []).append(key)

    return {
        "data_root": data_root,
        "path_by_key": path_by_key,
        "relpath_to_keys": relpath_to_keys,
        "basename_to_keys": basename_to_keys,
        "relpath_by_key": relpath_by_key,
        "basename_by_key": basename_by_key,
    }


def _extract_external_assets_name(external_ref: Any) -> str | None:
    """KR: 외부 참조 객체에서 에셋 이름(basename)을 추출합니다.
    EN: Extracts the asset name (basename) from an external reference object.
    """
    if external_ref is None:
        return None

    candidates: list[Any] = []
    if isinstance(external_ref, dict):
        candidates.extend(
            [
                external_ref.get("path"),
                external_ref.get("pathName"),
                external_ref.get("name"),
                external_ref.get("fileName"),
                external_ref.get("asset_name"),
                external_ref.get("assetPath"),
            ]
        )
    else:
        for attr in (
            "path",
            "pathName",
            "name",
            "fileName",
            "asset_name",
            "assetPath",
        ):
            candidates.append(getattr(external_ref, attr, None))

    for candidate in candidates:
        name = _normalize_assets_basename(candidate)
        if name:
            return name
    return None


def _extract_external_assets_candidates(external_ref: Any) -> list[str]:
    """KR: 외부 참조 객체에서 가능한 모든 에셋 경로/이름 후보를 추출합니다.
    EN: Extracts all possible asset path/name candidates from an external reference object.
    """
    if external_ref is None:
        return []

    raw_candidates: list[Any] = []
    if isinstance(external_ref, dict):
        raw_candidates.extend(
            [
                external_ref.get("path"),
                external_ref.get("pathName"),
                external_ref.get("name"),
                external_ref.get("fileName"),
                external_ref.get("asset_name"),
                external_ref.get("assetPath"),
            ]
        )
    else:
        for attr in (
            "path",
            "pathName",
            "name",
            "fileName",
            "asset_name",
            "assetPath",
        ):
            raw_candidates.append(getattr(external_ref, attr, None))

    resolved: list[str] = []
    seen: set[str] = set()
    for candidate in raw_candidates:
        normalized_path = _normalize_asset_lookup_path(candidate)
        if normalized_path and normalized_path not in seen:
            seen.add(normalized_path)
            resolved.append(normalized_path)
        normalized_name = _normalize_assets_basename(candidate)
        if normalized_name:
            lowered_name = normalized_name.lower()
            if lowered_name not in seen:
                seen.add(lowered_name)
                resolved.append(lowered_name)
    return resolved


def _resolve_external_ref(source_assets_file: Any, file_id: int) -> Any:
    """KR: FileID를 사용하여 소스 에셋 파일의 externals 목록에서 외부 참조를 조회합니다. FileID=0은 같은 파일, FileID>0은 externals 리스트의 1-based 인덱스입니다.
    EN: Looks up an external reference from the source asset file's externals list using FileID. FileID=0 means the same file; FileID>0 is a 1-based index into the externals list.
    """
    try:
        resolved_file_id = int(file_id or 0)
    except Exception:
        resolved_file_id = 0

    if resolved_file_id == 0:
        return None

    externals = getattr(source_assets_file, "externals", None)
    if externals is None:
        externals = getattr(source_assets_file, "m_Externals", None)

    if isinstance(externals, dict):
        external_ref = externals.get(resolved_file_id)
        if external_ref is None:
            external_ref = externals.get(resolved_file_id - 1)
        return external_ref

    if isinstance(externals, (list, tuple)):
        ext_index = resolved_file_id - 1
        if 0 <= ext_index < len(externals):
            return externals[ext_index]
    return None


def _resolve_assets_name_from_file_id(source_assets_file: Any, file_id: int) -> str | None:
    """KR: FileID로부터 대상 에셋 파일 이름을 확인합니다. FileID=0이면 현재 파일 이름을 반환합니다.
    EN: Resolves the target asset file name from a FileID. Returns the current file name if FileID=0.
    """
    try:
        resolved_file_id = int(file_id or 0)
    except Exception:
        resolved_file_id = 0

    if resolved_file_id == 0:
        return _normalize_assets_basename(getattr(source_assets_file, "name", ""))

    externals = getattr(source_assets_file, "externals", None)
    if externals is None:
        externals = getattr(source_assets_file, "m_Externals", None)

    external_ref = _resolve_external_ref(source_assets_file, resolved_file_id)
    if externals is None:
        return None
    return _extract_external_assets_name(external_ref)


def _resolve_target_assets_name(
    source_assets_file: Any,
    current_assets_name: str,
    file_id: int,
) -> str | None:
    """KR: FileID 기반으로 대상 에셋 이름을 결정합니다. FileID=0이면 현재 에셋 이름을 그대로 반환합니다.
    EN: Determines the target asset name based on FileID. Returns the current asset name as-is if FileID=0.
    """
    try:
        resolved_file_id = int(file_id or 0)
    except Exception:
        resolved_file_id = 0
    if resolved_file_id == 0:
        return str(current_assets_name)
    return _resolve_assets_name_from_file_id(source_assets_file, resolved_file_id)


def _collect_asset_file_index_matches(
    asset_file_index: dict[str, Any] | None,
    reference: Any,
) -> list[str]:
    """KR: 에셋 파일 인덱스에서 참조 문자열과 일치하는 모든 키를 수집합니다.
    EN: Collects all keys from the asset file index that match the reference string.
    """
    if not isinstance(asset_file_index, dict):
        return []

    normalized_reference = _normalize_asset_lookup_path(reference)
    if not normalized_reference:
        normalized_reference = _normalize_assets_basename(reference)
        if normalized_reference:
            normalized_reference = normalized_reference.lower()
    if not normalized_reference:
        return []

    relpath_to_keys = cast(
        dict[str, list[str]],
        asset_file_index.get("relpath_to_keys", {}),
    )
    basename_to_keys = cast(
        dict[str, list[str]],
        asset_file_index.get("basename_to_keys", {}),
    )
    relpath_by_key = cast(dict[str, str], asset_file_index.get("relpath_by_key", {}))

    matches: list[str] = []
    seen: set[str] = set()

    def _append_match(match_key: str) -> None:
        if match_key and match_key not in seen:
            seen.add(match_key)
            matches.append(match_key)

    for match_key in relpath_to_keys.get(normalized_reference, []):
        _append_match(match_key)

    if not matches and "/" in normalized_reference:
        suffix = "/" + normalized_reference
        for match_key, rel_path in relpath_by_key.items():
            if rel_path == normalized_reference or rel_path.endswith(suffix):
                _append_match(match_key)

    basename = os.path.basename(normalized_reference)
    for match_key in basename_to_keys.get(basename, []):
        _append_match(match_key)

    return matches


def _choose_asset_file_match(
    asset_file_index: dict[str, Any] | None,
    matches: list[str],
    *,
    current_file_key: str | None,
    reference_desc: str,
) -> str | None:
    """KR: 여러 일치 항목 중 하나를 선택합니다. 같은 디렉토리의 형제 파일을 우선하고, 모호하면 정렬 후 첫 번째를 사용합니다.
    EN: Selects one match from multiple candidates. Prefers sibling files in the same directory; if ambiguous, sorts and uses the first.
    """
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    if current_file_key and isinstance(asset_file_index, dict):
        path_by_key = cast(dict[str, str], asset_file_index.get("path_by_key", {}))
        current_path = path_by_key.get(current_file_key)
        if current_path:
            current_dir = os.path.dirname(current_path)
            sibling_matches = [
                match_key
                for match_key in matches
                if os.path.dirname(path_by_key.get(match_key, "")) == current_dir
            ]
            if len(sibling_matches) == 1:
                return sibling_matches[0]
    chosen = sorted(matches)[0]
    _log_warning(
        f"[asset_path_ambiguous] reference={reference_desc} match_count={len(matches)} "
        f"using_first={chosen}"
    )
    return chosen


def _resolve_target_outer_file_key(
    current_file_key: str,
    source_assets_file: Any,
    file_id: int,
    target_assets_name: str | None,
    *,
    source_bundle_signature: str | None,
    asset_file_index: dict[str, Any] | None,
) -> str | None:
    """KR: FileID와 에셋 이름을 조합하여 대상 외부 파일의 정규화된 키를 확인합니다. 번들 서명이 있으면 현재 파일 키를 반환합니다.
    EN: Resolves the normalized key of the target external file by combining FileID and asset name. Returns the current file key if a bundle signature is present.
    """
    if source_bundle_signature in BUNDLE_SIGNATURES:
        return str(current_file_key)
    try:
        resolved_file_id = int(file_id or 0)
    except Exception:
        resolved_file_id = 0
    if resolved_file_id == 0:
        return str(current_file_key)

    external_ref = _resolve_external_ref(source_assets_file, resolved_file_id)
    candidates = _extract_external_assets_candidates(external_ref)
    if target_assets_name:
        normalized_assets_name = _normalize_assets_basename(target_assets_name)
        if normalized_assets_name:
            candidates.append(normalized_assets_name.lower())

    for candidate in candidates:
        matches = _collect_asset_file_index_matches(asset_file_index, candidate)
        chosen = _choose_asset_file_match(
            asset_file_index,
            matches,
            current_file_key=current_file_key,
            reference_desc=str(candidate),
        )
        if chosen:
            return chosen
    return None


def _make_assets_object_key(assets_name: str, path_id: int) -> str:
    """KR: 에셋 이름과 PathID를 결합하여 고유 객체 키 문자열을 생성합니다.
    EN: Creates a unique object key string by combining the asset name and PathID.
    """
    return f"{str(assets_name)}|{int(path_id)}"


def _lookup_patch_value(mapping: dict[str, Any], key: str) -> Any | None:
    """KR: 패치 맵에서 키를 조회합니다. 대소문자 구분 후 소문자 폴백을 시도합니다.
    EN: Looks up a key in the patch map. Tries case-sensitive first, then falls back to lowercase.
    """
    if key in mapping:
        return mapping[key]
    lowered = key.lower()
    if lowered in mapping:
        return mapping[lowered]
    return None


def _store_patch_value(mapping: dict[str, Any], key: str, value: Any) -> None:
    """KR: 패치 맵에 값을 저장합니다. 원본 키와 소문자 키 양쪽에 동시 저장합니다.
    EN: Stores a value in the patch map. Saves to both the original key and its lowercase variant.
    """
    mapping[key] = value
    lowered = key.lower()
    if lowered != key:
        mapping[lowered] = value


def _copy_patch_bucket(
    patch_map: dict[str, dict[str, Any]] | None,
    file_key: str,
) -> dict[str, Any]:
    """KR: 패치 맵에서 파일 키에 해당하는 버킷을 복사하여 반환합니다.
    EN: Copies and returns the bucket corresponding to the file key from the patch map.
    """
    if not isinstance(patch_map, dict):
        return {}
    bucket = patch_map.get(str(file_key), {})
    return dict(bucket) if isinstance(bucket, dict) else {}


def _spill_image_to_temp_file(
    image: Image.Image,
    deferred_dir: str,
    *,
    prefix: str,
) -> str:
    """KR: PIL 이미지를 임시 PNG 파일로 저장하고 경로를 반환합니다. 메모리 절감을 위한 디스크 스필 처리입니다.
    EN: Saves a PIL image to a temporary PNG file and returns the path. This is a disk spill operation for memory savings.
    """
    os.makedirs(deferred_dir, exist_ok=True)
    fd, spill_path = tempfile.mkstemp(
        prefix=prefix,
        suffix=".png",
        dir=deferred_dir,
    )
    os.close(fd)
    image.save(spill_path, format="PNG")
    return spill_path


def _spill_deferred_texture_plan_to_disk(
    texture_plan: JsonDict,
    deferred_dir: str,
) -> JsonDict:
    """KR: 지연 텍스처 계획의 이미지 데이터를 디스크 임시 파일로 스필합니다. 2패스 메커니즘에서 메모리를 절감합니다.
    EN: Spills image data from a deferred texture plan to temporary disk files. Saves memory in the 2-pass mechanism.
    """
    source_atlas = texture_plan.get("source_atlas")
    if not isinstance(source_atlas, Image.Image):
        return texture_plan

    spilled_plan = dict(texture_plan)
    atlas_path = str(spilled_plan.get("source_atlas_path", "")).strip()
    if not atlas_path:
        atlas_path = _spill_image_to_temp_file(
            source_atlas,
            deferred_dir,
            prefix="atlas_",
        )
    spilled_plan.pop("source_atlas", None)
    spilled_plan["source_atlas_path"] = atlas_path

    alpha_image = spilled_plan.get("alpha8_linear_source")
    if isinstance(alpha_image, Image.Image):
        alpha_path = str(spilled_plan.get("alpha8_linear_source_path", "")).strip()
        if not alpha_path:
            if alpha_image is source_atlas:
                alpha_path = atlas_path
            else:
                alpha_path = _spill_image_to_temp_file(
                    alpha_image,
                    deferred_dir,
                    prefix="alpha8_",
                )
        spilled_plan.pop("alpha8_linear_source", None)
        spilled_plan["alpha8_linear_source_path"] = alpha_path
    return spilled_plan


def _load_spilled_plan_image(
    payload: JsonDict,
    *,
    image_key: str,
    path_key: str,
) -> Image.Image | None:
    """KR: 디스크에 스필된 이미지 경로로부터 PIL 이미지를 다시 로드합니다.
    EN: Reloads a PIL image from a spilled image path on disk.
    """
    image = payload.get(image_key)
    if isinstance(image, Image.Image):
        return image
    image_path = str(payload.get(path_key, "")).strip()
    if not image_path or not os.path.exists(image_path):
        return None
    loaded_image = Image.open(image_path)
    loaded_image.load()
    return loaded_image


def _cleanup_deferred_patch_bucket(bucket: dict[str, Any] | None) -> None:
    """KR: 지연 패치 버킷에서 사용된 임시 스필 파일들을 정리합니다.
    EN: Cleans up temporary spill files used in the deferred patch bucket.
    """
    if not isinstance(bucket, dict):
        return
    seen_payloads: set[int] = set()
    seen_paths: set[str] = set()
    for payload in bucket.values():
        if not isinstance(payload, dict):
            continue
        payload_id = id(payload)
        if payload_id in seen_payloads:
            continue
        seen_payloads.add(payload_id)
        for path_key in ("source_atlas_path", "alpha8_linear_source_path"):
            candidate_path = str(payload.get(path_key, "")).strip()
            if candidate_path:
                seen_paths.add(candidate_path)

    for candidate_path in sorted(seen_paths):
        try:
            if os.path.isfile(candidate_path):
                os.remove(candidate_path)
        except Exception:
            pass


def _register_deferred_patch(
    patch_map: dict[str, dict[str, Any]] | None,
    target_file_key: str | None,
    object_key: str,
    payload: Any,
    *,
    pending_files: set[str] | None,
    patch_kind: str,
) -> None:
    """KR: 지연 패치(deferred patch)를 패치 맵에 등록합니다. 1패스에서 변경사항을 수집하고 2패스에서 적용하는 구조입니다. 충돌 시 경고를 기록합니다.
    EN: Registers a deferred patch in the patch map. Changes are collected in pass 1 and applied in pass 2. Logs a warning on conflicts.
    """
    normalized_file = _normalize_asset_file_key(target_file_key)
    if not (isinstance(patch_map, dict) and normalized_file and object_key):
        return
    bucket = patch_map.setdefault(normalized_file, {})
    existing = _lookup_patch_value(bucket, object_key)
    existing_font = (
        str(existing.get("replacement_font", ""))
        if isinstance(existing, dict)
        else ""
    )
    existing_source = (
        str(existing.get("source_entry", ""))
        if isinstance(existing, dict)
        else ""
    )
    new_font = (
        str(payload.get("replacement_font", ""))
        if isinstance(payload, dict)
        else ""
    )
    new_source = (
        str(payload.get("source_entry", ""))
        if isinstance(payload, dict)
        else ""
    )
    if existing is not None and existing_font and new_font and (
        existing_font != new_font or existing_source != new_source
    ):
        _log_warning(
            f"[patch_plan_conflict] kind={patch_kind} file={normalized_file} "
            f"key={object_key} existing={existing_font}@{existing_source} "
            f"new={new_font}@{new_source}"
        )
    _store_patch_value(bucket, object_key, payload)
    if isinstance(pending_files, set) and (
        existing is None
        or existing_font != new_font
        or existing_source != new_source
    ):
        pending_files.add(normalized_file)


def _unitypy_supports_streaming_save() -> bool:
    """KR: 현재 UnityPy가 메모리 절감용 save_to() 스트리밍 저장 API를 지원하는지 확인합니다.
    EN: Checks whether the current UnityPy supports the memory-saving save_to() streaming save API.
    """
    try:
        from UnityPy.files.BundleFile import BundleFile as _BundleFile
        from UnityPy.files.SerializedFile import SerializedFile as _SerializedFile
    except Exception:
        return False
    return callable(getattr(_BundleFile, "save_to", None)) and callable(
        getattr(_SerializedFile, "save_to", None)
    )


def _ensure_custom_unitypy_streaming_save(lang: Language = "ko") -> None:
    """KR: 스트리밍 저장을 지원하지 않으면 RuntimeError를 발생시킵니다.
    EN: Raises RuntimeError if streaming save is not supported.
    """
    if _unitypy_supports_streaming_save():
        return
    unitypy_path = getattr(UnityPy, "__file__", "")
    if lang == "ko":
        raise RuntimeError(
            "현재 UnityPy에는 메모리 절감용 save_to() 구현이 없습니다.\n"
            "커스텀 UnityPy를 다시 설치해 주세요.\n"
            f"현재 로드 경로: {unitypy_path}"
        )
    raise RuntimeError(
        "The currently loaded UnityPy does not provide the memory-saving save_to() APIs.\n"
        "Reinstall the custom UnityPy build.\n"
        f"Loaded from: {unitypy_path}"
    )


def _has_real_atlas_path(ref: Any) -> bool:
    """KR: 아틀라스 참조의 PathID가 0보다 큰지(실제 유효한 경로인지) 확인합니다.
    EN: Checks whether the atlas reference PathID is greater than 0 (i.e. actually valid).
    """
    _, path_id = _atlas_ref_ids(ref)
    return path_id > 0


def _first_valid_atlas_ref(value: Any) -> JsonDict | None:
    """KR: 아틀라스 텍스처 리스트에서 유효한 PathID를 가진 첫 번째 참조를 반환합니다.
    EN: Returns the first reference with a valid PathID from the atlas texture list.
    """
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, dict) and _has_real_atlas_path(item):
            return cast(JsonDict, item)
    return None


def _best_atlas_ref(
    data: JsonDict,
    *,
    prefer_new: bool,
) -> JsonDict | None:
    """KR: 신형/구형 아틀라스 참조 중 가장 적합한 것을 선택합니다. prefer_new에 따라 우선순위가 달라집니다.
    EN: Selects the best atlas reference from new/old variants. Priority changes based on prefer_new.
    """
    new_any = _first_atlas_ref(data.get("m_AtlasTextures"))
    new_valid = _first_valid_atlas_ref(data.get("m_AtlasTextures"))
    old_any = (
        cast(JsonDict | None, data.get("atlas"))
        if isinstance(data.get("atlas"), dict)
        else None
    )
    old_valid = old_any if _has_real_atlas_path(old_any) else None

    ordered = (
        (new_valid, old_valid, new_any, old_any)
        if prefer_new
        else (old_valid, new_valid, old_any, new_any)
    )
    for ref in ordered:
        if isinstance(ref, dict):
            return ref
    return None


def _apply_color_override(current_value: Any, override: JsonDict) -> Any:
    """KR: RGBA 색상 오버라이드를 현재 값에 적용합니다. dict와 객체 속성 모두 처리합니다.
    EN: Applies RGBA color overrides to the current value. Handles both dict and object attributes.
    """
    for attr, key in (("r", "r"), ("g", "g"), ("b", "b"), ("a", "a")):
        if key not in override:
            continue
        try:
            val = float(override[key])
        except Exception:
            continue
        if isinstance(current_value, dict):
            current_value[key] = val
        if hasattr(current_value, attr):
            try:
                setattr(current_value, attr, val)
            except Exception:
                pass
    return current_value


def _texture_ref_to_dict(texture_ref: Any) -> JsonDict:
    """KR: 텍스처 참조를 m_FileID/m_PathID 딕셔너리로 변환합니다.
    EN: Converts a texture reference to an m_FileID/m_PathID dictionary.
    """
    if isinstance(texture_ref, dict):
        file_id = int(texture_ref.get("m_FileID", 0) or 0)
        path_id = int(texture_ref.get("m_PathID", 0) or 0)
        return {"m_FileID": file_id, "m_PathID": path_id}
    file_id = int(getattr(texture_ref, "m_FileID", 0) or 0)
    path_id = int(getattr(texture_ref, "m_PathID", 0) or 0)
    return {"m_FileID": file_id, "m_PathID": path_id}


def _extract_texture_ref_from_tex_env(env_value: Any) -> JsonDict:
    """KR: TexEnv 항목에서 m_Texture 참조를 딕셔너리로 추출합니다.
    EN: Extracts the m_Texture reference as a dictionary from a TexEnv entry.
    """
    if isinstance(env_value, dict):
        return _texture_ref_to_dict(env_value.get("m_Texture"))
    tex = getattr(env_value, "m_Texture", None)
    return _texture_ref_to_dict(tex)


def _color_value_to_dict(value: Any, default: JsonDict) -> JsonDict:
    """KR: 색상 값을 RGBA 딕셔너리로 변환합니다. 누락된 채널은 기본값으로 채웁니다.
    EN: Converts a color value to an RGBA dictionary. Missing channels are filled with defaults.
    """
    if isinstance(value, dict):
        return {
            "r": float(value.get("r", default["r"])),
            "g": float(value.get("g", default["g"])),
            "b": float(value.get("b", default["b"])),
            "a": float(value.get("a", default["a"])),
        }
    out = dict(default)
    for key in ("r", "g", "b", "a"):
        attr = getattr(value, key, None)
        if attr is not None:
            try:
                out[key] = float(attr)
            except Exception:
                pass
    return out


def _build_tex_env_entry(texture_ref: JsonDict) -> JsonDict:
    """KR: 텍스처 참조로부터 TexEnv 항목을 구성합니다. Scale=(1,1), Offset=(0,0) 기본값을 사용합니다.
    EN: Builds a TexEnv entry from a texture reference. Uses defaults Scale=(1,1), Offset=(0,0).
    """
    return {
        "m_Texture": {
            "m_FileID": int(texture_ref.get("m_FileID", 0) or 0),
            "m_PathID": int(texture_ref.get("m_PathID", 0) or 0),
        },
        "m_Scale": {"x": 1.0, "y": 1.0},
        "m_Offset": {"x": 0.0, "y": 0.0},
    }


def _prune_material_saved_properties_for_raster(
    parse_dict: Any,
    color_overrides: dict[str, JsonDict],
) -> bool:
    """KR: 래스터 폰트용으로 머티리얼의 SavedProperties를 최소 속성 세트로 정리합니다.
    EN: Prunes material SavedProperties to a minimal property set for raster fonts.
    """
    saved_props = getattr(parse_dict, "m_SavedProperties", None)
    if saved_props is None:
        return False

    tex_envs = getattr(saved_props, "m_TexEnvs", None)
    main_tex_ref: JsonDict = {"m_FileID": 0, "m_PathID": 0}
    face_tex_ref: JsonDict = {"m_FileID": 0, "m_PathID": 0}
    if isinstance(tex_envs, list):
        for entry in tex_envs:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            prop_name = str(entry[0])
            env_value = entry[1]
            if prop_name == "_MainTex":
                main_tex_ref = _extract_texture_ref_from_tex_env(env_value)
            elif prop_name == "_FaceTex":
                face_tex_ref = _extract_texture_ref_from_tex_env(env_value)

    new_tex_envs: list[tuple[str, JsonDict]] = [
        ("_FaceTex", _build_tex_env_entry(face_tex_ref)),
        ("_MainTex", _build_tex_env_entry(main_tex_ref)),
    ]
    new_floats: list[tuple[str, float]] = [
        ("_ColorMask", 15.0),
        ("_CullMode", 0.0),
        ("_MaskSoftnessX", 0.0),
        ("_MaskSoftnessY", 0.0),
        ("_Stencil", 0.0),
        ("_StencilComp", 8.0),
        ("_StencilOp", 0.0),
        ("_StencilReadMask", 255.0),
        ("_StencilWriteMask", 255.0),
        ("_VertexOffsetX", 0.0),
        ("_VertexOffsetY", 0.0),
    ]

    color_map: dict[str, Any] = {}
    old_colors = getattr(saved_props, "m_Colors", None)
    if isinstance(old_colors, list):
        for entry in old_colors:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            color_map[str(entry[0])] = entry[1]

    clip_rect = _color_value_to_dict(
        color_map.get("_ClipRect"),
        {"r": -32767.0, "g": -32767.0, "b": 32767.0, "a": 32767.0},
    )
    face_color_value = _color_value_to_dict(
        color_map.get("_FaceColor"),
        {"r": 1.0, "g": 1.0, "b": 1.0, "a": 1.0},
    )
    face_override = color_overrides.get("_FaceColor")
    if isinstance(face_override, dict):
        face_color_value = _apply_color_override(face_color_value, face_override)

    new_colors: list[tuple[str, JsonDict]] = [
        ("_ClipRect", clip_rect),
        ("_FaceColor", face_color_value),
    ]

    saved_props.m_TexEnvs = new_tex_envs
    if hasattr(saved_props, "m_Ints"):
        try:
            saved_props.m_Ints = []
        except Exception:
            pass
    saved_props.m_Floats = new_floats
    saved_props.m_Colors = new_colors
    return True


def _apply_material_replacement_to_object(parse_dict: Any, mat_info: JsonDict) -> bool:
    """KR: 머티리얼 교체 정보를 파싱된 객체에 적용합니다. float/color 오버라이드, 외곽선 비율, 스타일 보존 등을 처리합니다.
    EN: Applies material replacement info to the parsed object. Handles float/color overrides, outline ratio, style preservation, etc.
    """
    changed = False
    float_overrides_raw = mat_info.get("float_overrides", {})
    float_overrides = (
        float_overrides_raw if isinstance(float_overrides_raw, dict) else {}
    )
    color_overrides_raw = mat_info.get("color_overrides", {})
    color_overrides = (
        color_overrides_raw if isinstance(color_overrides_raw, dict) else {}
    )
    try:
        outline_ratio = float(mat_info.get("outline_ratio", 1.0))
    except Exception:
        outline_ratio = 1.0
    if outline_ratio <= 0:
        outline_ratio = 1.0
    outline_fallback_used = False
    preserve_game_style = bool(mat_info.get("preserve_game_style", False))
    try:
        style_padding_scale_ratio = float(mat_info.get("style_padding_scale_ratio", 1.0))
    except Exception:
        style_padding_scale_ratio = 1.0
    if style_padding_scale_ratio <= 0:
        style_padding_scale_ratio = 1.0
    prune_raster_material = bool(mat_info.get("prune_raster_material", False))
    preserve_gradient_floor = bool(mat_info.get("preserve_gradient_floor", False))
    replacement_padding = float(mat_info.get("replacement_padding", 0) or 0)
    gradient_scale = mat_info.get("gs")
    texture_h_raw = mat_info.get("h")
    texture_w_raw = mat_info.get("w")
    try:
        texture_h = float(texture_h_raw) if texture_h_raw is not None else None
    except Exception:
        texture_h = None
    try:
        texture_w = float(texture_w_raw) if texture_w_raw is not None else None
    except Exception:
        texture_w = None

    saved_props = getattr(parse_dict, "m_SavedProperties", None)
    if saved_props is None:
        return False

    if prune_raster_material:
        if _prune_material_saved_properties_for_raster(parse_dict, color_overrides):
            changed = True
    else:
        float_props = getattr(saved_props, "m_Floats", None)
        if isinstance(float_props, list):
            existing_float_map: dict[str, float] = {}
            for entry in float_props:
                if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                    continue
                try:
                    existing_float_map[str(entry[0])] = float(entry[1])
                except Exception:
                    continue

            has_texture_height = False
            has_texture_width = False
            has_gradient_scale = False
            for i in range(len(float_props)):
                entry = float_props[i]
                if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                    continue
                prop_name = str(entry[0])
                if prop_name == "_GradientScale":
                    candidate: float | None = None
                    if prop_name in float_overrides:
                        try:
                            candidate = float(float_overrides[prop_name])
                        except Exception:
                            candidate = None
                    elif gradient_scale is not None:
                        try:
                            candidate = float(gradient_scale)
                        except Exception:
                            candidate = None
                    if candidate is not None:
                        # KR: _GradientScale은 교체 아틀라스의 padding 기반 값을 강제 적용합니다.
                        # preserve_gradient_floor 로직은 교체 아틀라스와 불일치를 유발하므로 제거되었습니다.
                        # EN: _GradientScale is force-set to the padding-based value of the replacement atlas.
                        # The preserve_gradient_floor logic was removed as it caused mismatch with the replacement atlas.
                        float_props[i] = ("_GradientScale", candidate)
                        has_gradient_scale = True
                        changed = True
                elif preserve_game_style and prop_name in _MATERIAL_STYLE_FLOAT_KEYS:
                    candidate = existing_float_map.get(prop_name)
                    if candidate is None:
                        continue
                    if prop_name in _MATERIAL_STYLE_PADDING_SCALE_KEYS:
                        candidate = float(candidate * style_padding_scale_ratio)
                    if prop_name in _MATERIAL_OUTLINE_RATIO_KEYS:
                        candidate = float(candidate * outline_ratio)
                    float_props[i] = (prop_name, float(candidate))
                    changed = True
                elif prop_name in _MATERIAL_OUTLINE_RATIO_KEYS:
                    candidate: float | None = None
                    existing_value: float | None = None
                    try:
                        existing_value = float(entry[1])
                    except Exception:
                        existing_value = None
                    if prop_name in float_overrides:
                        try:
                            candidate = float(float_overrides[prop_name])
                        except Exception:
                            candidate = None
                        if (
                            outline_ratio != 1.0
                            and candidate is not None
                            and abs(candidate) <= 1e-9
                        ):
                            if existing_value is not None and abs(existing_value) > 1e-9:
                                candidate = existing_value
                                outline_fallback_used = True
                            elif prop_name == "_OutlineWidth":
                                baseline_gradient_scale = None
                                try:
                                    if "_GradientScale" in float_overrides:
                                        baseline_gradient_scale = float(
                                            float_overrides["_GradientScale"]
                                        )
                                    elif gradient_scale is not None:
                                        baseline_gradient_scale = float(gradient_scale)
                                    else:
                                        baseline_gradient_scale = existing_float_map.get(
                                            "_GradientScale"
                                        )
                                except Exception:
                                    baseline_gradient_scale = None
                                if (
                                    baseline_gradient_scale is not None
                                    and baseline_gradient_scale > 0
                                ):
                                    candidate = 1.0 / baseline_gradient_scale
                                    outline_fallback_used = True
                    elif outline_ratio != 1.0:
                        candidate = existing_value
                        if (
                            prop_name == "_OutlineWidth"
                            and candidate is not None
                            and abs(candidate) <= 1e-9
                        ):
                            baseline_gradient_scale = None
                            try:
                                if "_GradientScale" in float_overrides:
                                    baseline_gradient_scale = float(
                                        float_overrides["_GradientScale"]
                                    )
                                elif gradient_scale is not None:
                                    baseline_gradient_scale = float(gradient_scale)
                                else:
                                    baseline_gradient_scale = existing_float_map.get(
                                        "_GradientScale"
                                    )
                            except Exception:
                                baseline_gradient_scale = None
                            if (
                                baseline_gradient_scale is not None
                                and baseline_gradient_scale > 0
                            ):
                                candidate = 1.0 / baseline_gradient_scale
                                outline_fallback_used = True
                    if candidate is not None:
                        float_props[i] = (prop_name, float(candidate * outline_ratio))
                        changed = True
                elif prop_name == "_TextureHeight" and texture_h is not None:
                    # KR: _TextureHeight는 실제 아틀라스 크기가 float_overrides보다 우선합니다.
                    # EN: For _TextureHeight, the actual atlas size takes priority over float_overrides.
                    float_props[i] = ("_TextureHeight", texture_h)
                    has_texture_height = True
                    changed = True
                elif prop_name == "_TextureWidth" and texture_w is not None:
                    float_props[i] = ("_TextureWidth", texture_w)
                    has_texture_width = True
                    changed = True
                elif prop_name in float_overrides:
                    float_props[i] = (prop_name, float(float_overrides[prop_name]))
                    changed = True
                if prop_name == "_TextureHeight":
                    has_texture_height = True
                elif prop_name == "_TextureWidth":
                    has_texture_width = True
                elif prop_name == "_GradientScale":
                    has_gradient_scale = True
            if texture_h is not None and not has_texture_height:
                float_props.append(("_TextureHeight", texture_h))
                changed = True
            if texture_w is not None and not has_texture_width:
                float_props.append(("_TextureWidth", texture_w))
                changed = True
            if gradient_scale is not None and not has_gradient_scale:
                float_props.append(("_GradientScale", float(gradient_scale)))
                changed = True

            # KR: _ScaleRatioA를 교체 아틀라스의 padding/GradientScale로 재계산합니다.
            # TMP에서 ScaleRatioA = padding / GradientScale이며, 이 값이 불일치하면 외곽선/그림자 크기가 틀어집니다.
            # EN: Recalculates _ScaleRatioA using the replacement atlas padding/GradientScale.
            # In TMP, ScaleRatioA = padding / GradientScale; mismatch causes incorrect outline/shadow sizes.
            if replacement_padding > 0:
                final_gs = None
                for _fp in float_props:
                    if isinstance(_fp, (list, tuple)) and len(_fp) >= 2 and _fp[0] == "_GradientScale":
                        try:
                            final_gs = float(_fp[1])
                        except Exception:
                            pass
                        break
                if final_gs and final_gs > 0:
                    new_scale_ratio_a = replacement_padding / final_gs
                    for k, fp in enumerate(float_props):
                        if isinstance(fp, (list, tuple)) and len(fp) >= 2 and fp[0] == "_ScaleRatioA":
                            float_props[k] = ("_ScaleRatioA", float(new_scale_ratio_a))
                            changed = True
                            break

            if outline_fallback_used:
                logger.debug(
                    "outline_ratio used original material baseline because replacement outline values were zero: %s",
                    mat_info.get("source_entry", ""),
                )

        color_props = getattr(saved_props, "m_Colors", None)
        if isinstance(color_props, list) and color_overrides:
            for i in range(len(color_props)):
                color_name = color_props[i][0]
                if preserve_game_style and str(color_name) in _MATERIAL_STYLE_COLOR_KEYS:
                    continue
                override = color_overrides.get(color_name)
                if not isinstance(override, dict):
                    continue
                current_value = color_props[i][1]
                color_props[i] = (
                    color_name,
                    _apply_color_override(current_value, override),
                )
                changed = True

    if bool(mat_info.get("reset_keywords", False)):
        if hasattr(parse_dict, "m_ShaderKeywords"):
            try:
                parse_dict.m_ShaderKeywords = ""
                changed = True
            except Exception:
                pass
        if hasattr(parse_dict, "m_ValidKeywords"):
            try:
                parse_dict.m_ValidKeywords = []
                changed = True
            except Exception:
                pass
        if hasattr(parse_dict, "m_InvalidKeywords"):
            try:
                parse_dict.m_InvalidKeywords = []
                changed = True
            except Exception:
                pass
    return changed


def detect_tmp_version(
    data: JsonDict, unity_version: str | None = None
) -> Literal["new", "old"]:
    """KR: SDF TMP 데이터가 신형/구형 포맷인지 판별합니다.
    EN: Determines whether SDF TMP data uses the new or old format.
    """
    new_glyph_count = _safe_list_len(data.get("m_GlyphTable"))
    old_glyph_count = _safe_list_len(data.get("m_glyphInfoList"))
    has_new_glyphs = new_glyph_count > 0
    has_old_glyphs = old_glyph_count > 0

    has_new_face = isinstance(data.get("m_FaceInfo"), dict)
    has_old_face = isinstance(data.get("m_fontInfo"), dict)
    has_new_atlas = _first_atlas_ref(data.get("m_AtlasTextures")) is not None
    has_old_atlas = isinstance(data.get("atlas"), dict)

    # KR: 두 포맷 키가 동시에 있어도 실제 글리프가 있는 쪽을 우선합니다.
    # EN: Even if both format keys exist, the side with actual glyphs takes priority.
    if has_new_glyphs != has_old_glyphs:
        return "new" if has_new_glyphs else "old"
    if new_glyph_count != old_glyph_count:
        return "new" if new_glyph_count > old_glyph_count else "old"

    # KR: 글리프가 비슷하면 face/atlas 신호를 비교합니다.
    # EN: If glyph counts are similar, compare face/atlas signals.
    if has_new_face != has_old_face:
        return "new" if has_new_face else "old"
    if has_new_atlas != has_old_atlas:
        return "new" if has_new_atlas else "old"

    # KR: Unity-Runtime-Libraries 기준 버전 힌트(2018.3.14 / 2018.4.2)를 사용합니다.
    # EN: Uses version hints based on Unity-Runtime-Libraries (2018.3.14 / 2018.4.2).
    hint = _tmp_version_hint(unity_version)
    if hint is not None:
        return hint

    # KR: 최종 폴백은 신형 우선입니다.
    # EN: Final fallback prefers the new format.
    if has_new_face or has_new_atlas or "m_CharacterTable" in data:
        return "new"
    if has_old_face or has_old_atlas:
        return "old"

    return "new"


def inspect_tmp_font_schema(
    data: JsonDict,
    unity_version: str | None = None,
) -> dict[str, Any]:
    """KR: TMP 스키마 판별과 glyph/atlas 핵심 메타를 공통 형태로 반환합니다.
    EN: Returns TMP schema detection and core glyph/atlas metadata in a common format.
    """
    target_version = detect_tmp_version(data, unity_version=unity_version)

    new_glyph_count = _safe_list_len(data.get("m_GlyphTable"))
    old_glyph_count = _safe_list_len(data.get("m_glyphInfoList"))
    has_new_face = isinstance(data.get("m_FaceInfo"), dict)
    has_old_face = isinstance(data.get("m_fontInfo"), dict)
    new_atlas_ref = _first_atlas_ref(data.get("m_AtlasTextures"))
    old_atlas_ref = (
        cast(JsonDict | None, data.get("atlas"))
        if isinstance(data.get("atlas"), dict)
        else None
    )

    if target_version == "new":
        glyph_count = new_glyph_count if new_glyph_count > 0 else old_glyph_count
        atlas_ref = _best_atlas_ref(data, prefer_new=True)
    else:
        glyph_count = old_glyph_count if old_glyph_count > 0 else new_glyph_count
        atlas_ref = _best_atlas_ref(data, prefer_new=False)

    atlas_file_id, atlas_path_id = _atlas_ref_ids(atlas_ref)

    is_tmp = bool(
        new_glyph_count > 0
        or old_glyph_count > 0
        or has_new_face
        or has_old_face
        or new_atlas_ref is not None
        or old_atlas_ref is not None
    )

    return {
        "version": target_version,
        "is_tmp": is_tmp,
        "glyph_count": int(glyph_count),
        "atlas_file_id": int(atlas_file_id),
        "atlas_path_id": int(atlas_path_id),
    }


def extract_tmp_atlas_padding(
    data: JsonDict,
    unity_version: str | None = None,
) -> float:
    """KR: TMP 에셋 데이터에서 아틀라스 패딩 값을 추출합니다. m_AtlasPadding, CreationSettings, m_fontInfo 순으로 탐색합니다.
    EN: Extracts the atlas padding value from TMP asset data. Searches m_AtlasPadding, CreationSettings, m_fontInfo in order.
    """
    candidates: list[Any] = [data.get("m_AtlasPadding")]
    creation_settings_key = _resolve_creation_settings_key(
        data,
        unity_version=unity_version,
    )
    if creation_settings_key and isinstance(data.get(creation_settings_key), dict):
        candidates.append(cast(JsonDict, data[creation_settings_key]).get("padding"))
    if isinstance(data.get("m_fontInfo"), dict):
        candidates.append(cast(JsonDict, data["m_fontInfo"]).get("Padding"))

    for candidate in candidates:
        try:
            numeric = float(candidate)
        except Exception:
            continue
        if numeric > 0:
            return numeric
    return 0.0


def convert_face_info_new_to_old(
    face_info: JsonDict,
    atlas_padding: int = 0,
    atlas_width: int = 0,
    atlas_height: int = 0,
) -> JsonDict:
    """KR: 신형 m_FaceInfo를 구형 m_fontInfo 구조로 변환합니다.
    EN: Converts new-format m_FaceInfo to old-format m_fontInfo structure.
    """
    return {
        "Name": face_info.get("m_FamilyName", ""),
        "PointSize": face_info.get("m_PointSize", 0),
        "Scale": face_info.get("m_Scale", 1.0),
        "CharacterCount": 0,
        "LineHeight": face_info.get("m_LineHeight", 0),
        "Baseline": face_info.get("m_Baseline", 0),
        "Ascender": face_info.get("m_AscentLine", 0),
        "CapHeight": face_info.get("m_CapLine", 0),
        "Descender": face_info.get("m_DescentLine", 0),
        "CenterLine": face_info.get("m_MeanLine", 0),
        "SuperscriptOffset": face_info.get("m_SuperscriptOffset", 0),
        "SubscriptOffset": face_info.get("m_SubscriptOffset", 0),
        "SubSize": face_info.get("m_SubscriptSize", 0.5),
        "Underline": face_info.get("m_UnderlineOffset", 0),
        "UnderlineThickness": face_info.get("m_UnderlineThickness", 0),
        "strikethrough": face_info.get("m_StrikethroughOffset", 0),
        "strikethroughThickness": face_info.get("m_StrikethroughThickness", 0),
        "TabWidth": face_info.get("m_TabWidth", 0),
        "Padding": atlas_padding,
        "AtlasWidth": atlas_width,
        "AtlasHeight": atlas_height,
    }


def convert_face_info_old_to_new(font_info: JsonDict) -> JsonDict:
    """KR: 구형 m_fontInfo를 신형 m_FaceInfo 구조로 변환합니다.
    EN: Converts old-format m_fontInfo to new-format m_FaceInfo structure.
    """
    return {
        "m_FaceIndex": 0,
        "m_FamilyName": font_info.get("Name", ""),
        "m_StyleName": "regular",
        "m_PointSize": font_info.get("PointSize", 0),
        "m_Scale": font_info.get("Scale", 1.0),
        "m_UnitsPerEM": 0,
        "m_LineHeight": font_info.get("LineHeight", 0),
        "m_AscentLine": font_info.get("Ascender", 0),
        "m_CapLine": font_info.get("CapHeight", 0),
        "m_MeanLine": font_info.get("CenterLine", 0),
        "m_Baseline": font_info.get("Baseline", 0),
        "m_DescentLine": font_info.get("Descender", 0),
        "m_SuperscriptOffset": font_info.get("SuperscriptOffset", 0),
        "m_SuperscriptSize": 0.5,
        "m_SubscriptOffset": font_info.get("SubscriptOffset", 0),
        "m_SubscriptSize": font_info.get("SubSize", 0.5),
        "m_UnderlineOffset": font_info.get("Underline", 0),
        "m_UnderlineThickness": font_info.get("UnderlineThickness", 0),
        "m_StrikethroughOffset": font_info.get("strikethrough", 0),
        "m_StrikethroughThickness": font_info.get("strikethroughThickness", 0),
        "m_TabWidth": font_info.get("TabWidth", 0),
    }


def _new_glyph_rect_to_int(rect: JsonDict) -> tuple[int, int, int, int]:
    """KR: 신형 TMP glyph rect를 정수 좌표/크기로 정규화합니다.
    EN: Normalizes a new-format TMP glyph rect to integer coordinates/dimensions.
    """
    x = int(round(float(rect.get("m_X", 0))))
    y = int(round(float(rect.get("m_Y", 0))))
    w = max(1, int(round(float(rect.get("m_Width", 0)))))
    h = max(1, int(round(float(rect.get("m_Height", 0)))))
    return x, y, w, h


def _tmp_flip_y_between_old_new(
    y_value: float, glyph_height: float, atlas_height: int | float | None
) -> float:
    """KR: TMP old(top-origin) <-> new(bottom-origin) Y 변환 공식을 적용합니다.
    EN: Applies the TMP old(top-origin) <-> new(bottom-origin) Y conversion formula.
    """
    if atlas_height is None:
        return float(y_value)
    try:
        atlas_h = float(atlas_height)
    except Exception:
        return float(y_value)
    if atlas_h <= 0:
        return float(y_value)
    return atlas_h - float(y_value) - float(glyph_height)


def convert_glyphs_new_to_old(
    glyph_table: list[JsonDict],
    char_table: list[JsonDict],
    atlas_height: int | None = None,
) -> list[JsonDict]:
    """KR: 신형 글리프/문자 테이블을 구형 m_glyphInfoList로 변환합니다.
    EN: Converts new-format glyph/character tables to old-format m_glyphInfoList.
    """
    glyph_by_index: dict[int, JsonDict] = {}
    for g in glyph_table:
        glyph_by_index[int(g.get("m_Index", 0))] = g
    result: list[JsonDict] = []
    for char in char_table:
        unicode_val = char.get("m_Unicode", 0)
        glyph_idx = char.get("m_GlyphIndex", 0)
        g = glyph_by_index.get(glyph_idx, {})
        metrics = g.get("m_Metrics", {})
        rect = g.get("m_GlyphRect", {})
        rect_h = float(rect.get("m_Height", 0))
        rect_y = _tmp_flip_y_between_old_new(
            float(rect.get("m_Y", 0)),
            rect_h,
            atlas_height,
        )
        result.append(
            {
                "id": int(unicode_val),
                "x": float(rect.get("m_X", 0)),
                "y": rect_y,
                "width": float(metrics.get("m_Width", 0)),
                "height": float(metrics.get("m_Height", 0)),
                "xOffset": float(metrics.get("m_HorizontalBearingX", 0)),
                "yOffset": float(metrics.get("m_HorizontalBearingY", 0)),
                "xAdvance": float(metrics.get("m_HorizontalAdvance", 0)),
                "scale": float(g.get("m_Scale", 1.0)),
            }
        )
    return result


def convert_glyphs_old_to_new(
    glyph_info_list: list[JsonDict],
    atlas_height: int | None = None,
) -> tuple[list[JsonDict], list[JsonDict]]:
    """KR: 구형 m_glyphInfoList를 신형 테이블 구조로 변환합니다.
    EN: Converts old-format m_glyphInfoList to new-format table structure.
    """
    glyph_table: list[JsonDict] = []
    char_table: list[JsonDict] = []
    glyph_idx = 0
    for glyph in glyph_info_list:
        uid = glyph.get("id", 0)
        old_rect_y = float(glyph.get("y", 0))
        glyph_h = float(glyph.get("height", 0))
        new_rect_y = _tmp_flip_y_between_old_new(old_rect_y, glyph_h, atlas_height)
        glyph_table.append(
            {
                "m_Index": glyph_idx,
                "m_Metrics": {
                    "m_Width": glyph.get("width", 0),
                    "m_Height": glyph.get("height", 0),
                    "m_HorizontalBearingX": glyph.get("xOffset", 0),
                    "m_HorizontalBearingY": glyph.get("yOffset", 0),
                    "m_HorizontalAdvance": glyph.get("xAdvance", 0),
                },
                "m_GlyphRect": {
                    "m_X": int(glyph.get("x", 0)),
                    "m_Y": int(round(new_rect_y)),
                    "m_Width": int(glyph.get("width", 0)),
                    "m_Height": int(glyph.get("height", 0)),
                },
                "m_Scale": glyph.get("scale", 1.0),
                "m_AtlasIndex": 0,
                "m_ClassDefinitionType": 0,
            }
        )
        char_table.append(
            {
                "m_ElementType": 1,
                "m_Unicode": int(uid),
                "m_GlyphIndex": glyph_idx,
                "m_Scale": 1.0,
            }
        )
        glyph_idx += 1
    return glyph_table, char_table


def normalize_sdf_data(data: JsonDict, deep_copy: bool = True) -> JsonDict:
    """KR: SDF 교체 데이터를 신형 TMP 형식으로 정규화해 반환합니다.
    deep_copy=True면 입력 데이터를 복사해 원본 변형을 방지합니다.
    EN: Normalizes SDF replacement data to new-format TMP and returns it.
    deep_copy=True copies input data to prevent mutation of the original.
    """
    result: JsonDict = copy.deepcopy(data) if deep_copy else data
    version = detect_tmp_version(result)

    if version == "old":
        font_info = result.get("m_fontInfo", {})
        glyph_info_list = result.get("m_glyphInfoList", [])
        atlas_padding = font_info.get("Padding", 0)
        atlas_width = font_info.get("AtlasWidth", 0)
        atlas_height = font_info.get("AtlasHeight", 0)

        # KR: 구형 face/glyph 구조를 신형 TMP 필드로 승격합니다.
        # EN: Promotes old-format face/glyph structures to new-format TMP fields.
        result["m_FaceInfo"] = convert_face_info_old_to_new(font_info)

        try:
            atlas_height_int = int(atlas_height) if atlas_height is not None else None
        except Exception:
            atlas_height_int = None
        glyph_table, char_table = convert_glyphs_old_to_new(
            glyph_info_list,
            atlas_height=atlas_height_int,
        )
        result["m_GlyphTable"] = glyph_table
        result["m_CharacterTable"] = char_table

        # KR: 구형 atlas 참조를 신형 atlas 배열 필드로 보정합니다.
        # EN: Adjusts old-format atlas references to new-format atlas array fields.
        if "m_AtlasTextures" not in result or not result["m_AtlasTextures"]:
            atlas_ref = result.get("atlas", {"m_FileID": 0, "m_PathID": 0})
            result["m_AtlasTextures"] = [atlas_ref]
        result.setdefault("m_AtlasWidth", int(atlas_width))
        result.setdefault("m_AtlasHeight", int(atlas_height))
        result.setdefault("m_AtlasPadding", int(atlas_padding))
        result.setdefault("m_AtlasRenderMode", 4118)
        result.setdefault("m_UsedGlyphRects", [])
        result.setdefault("m_FreeGlyphRects", [])

        # KR: 구형 데이터에 누락된 weight table은 기본값으로 채웁니다.
        # EN: Fills missing weight tables in old-format data with defaults.
        if "m_FontWeightTable" not in result:
            font_weights = result.get("fontWeights", [])
            result["m_FontWeightTable"] = font_weights if font_weights else []

    # KR: 정규화 후 반복 사용을 위해 숫자 타입/기본값을 한 번만 정리합니다.
    # EN: Cleans up numeric types/defaults once after normalization for repeated use.
    try:
        result["m_AtlasWidth"] = int(result.get("m_AtlasWidth", 0) or 0)
        result["m_AtlasHeight"] = int(result.get("m_AtlasHeight", 0) or 0)
        result["m_AtlasPadding"] = int(result.get("m_AtlasPadding", 0) or 0)
    except Exception:
        pass
    result.setdefault("m_AtlasRenderMode", 4118)
    result.setdefault("m_UsedGlyphRects", [])
    result.setdefault("m_FreeGlyphRects", [])
    result.setdefault("m_FontWeightTable", [])

    face_info = result.get("m_FaceInfo")
    if isinstance(face_info, dict):
        ensure_int(face_info, ["m_PointSize", "m_AtlasWidth", "m_AtlasHeight"])

    # KR: Atlas 참조 목록은 공유 변형을 피하기 위해 독립 딕셔너리로 재구성합니다.
    # EN: Atlas reference list is rebuilt as independent dicts to avoid shared mutation.
    atlas_textures_raw = result.get("m_AtlasTextures", [])
    atlas_textures: list[JsonDict] = []
    if isinstance(atlas_textures_raw, list):
        for tex in atlas_textures_raw:
            if isinstance(tex, dict):
                atlas_textures.append(
                    {
                        "m_FileID": int(tex.get("m_FileID", 0) or 0),
                        "m_PathID": int(tex.get("m_PathID", 0) or 0),
                    }
                )
    if not atlas_textures and isinstance(result.get("atlas"), dict):
        atlas_ref = cast(JsonDict, result.get("atlas"))
        atlas_textures.append(
            {
                "m_FileID": int(atlas_ref.get("m_FileID", 0) or 0),
                "m_PathID": int(atlas_ref.get("m_PathID", 0) or 0),
            }
        )
    result["m_AtlasTextures"] = atlas_textures

    glyph_table = result.get("m_GlyphTable")
    if isinstance(glyph_table, list):
        for glyph in glyph_table:
            if not isinstance(glyph, dict):
                continue
            ensure_int(glyph, ["m_Index", "m_AtlasIndex", "m_ClassDefinitionType"])
            glyph["m_ClassDefinitionType"] = 0
            rect = glyph.get("m_GlyphRect")
            if isinstance(rect, dict):
                ensure_int(rect, ["m_X", "m_Y", "m_Width", "m_Height"])

    char_table = result.get("m_CharacterTable")
    if isinstance(char_table, list):
        for char in char_table:
            if isinstance(char, dict):
                ensure_int(char, ["m_Unicode", "m_GlyphIndex", "m_ElementType"])

    for rect_list_name in ["m_UsedGlyphRects", "m_FreeGlyphRects"]:
        rect_list = result.get(rect_list_name)
        if isinstance(rect_list, list):
            for rect in rect_list:
                if isinstance(rect, dict):
                    ensure_int(rect, ["m_X", "m_Y", "m_Width", "m_Height"])

    creation_settings = result.get("m_CreationSettings")
    if isinstance(creation_settings, dict):
        ensure_int(
            creation_settings, ["pointSize", "atlasWidth", "atlasHeight", "padding"]
        )

    return result


def find_assets_files(
    game_path: str,
    lang: Language = "ko",
    target_files: set[str] | None = None,
    exclude_exts: set[str] | None = None,
) -> list[str]:
    """KR: 게임에서 처리 대상 에셋 파일 목록을 수집합니다.
    target_files가 있으면 해당 파일명으로 스캔 대상을 제한합니다.
    exclude_exts가 있으면 해당 확장자를 추가 제외합니다.
    EN: Collects the list of asset files to process from the game.
    If target_files is provided, limits scan targets to those filenames.
    If exclude_exts is provided, additionally excludes those extensions.
    """
    data_path = get_data_path(game_path, lang=lang)
    assets_files: list[str] = []
    normalized_targets = (
        {os.path.basename(name) for name in target_files} if target_files else None
    )
    blacklist_exts = {
        ".dll",
        ".manifest",
        ".exe",
        ".txt",
        ".json",
        ".xml",
        ".log",
        ".ini",
        ".cfg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".wav",
        ".mp3",
        ".ogg",
        ".mp4",
        ".avi",
        ".mov",
        ".bak",
        ".info",
        ".config",
        ".browser",
        ".aspx",
        ".map",
        ".resource",
        ".resources",
    }
    if exclude_exts:
        blacklist_exts.update({str(ext).lower() for ext in exclude_exts if ext})

    skip_root_prefixes = [
        os.path.normcase(
            os.path.normpath(os.path.join(data_path, "il2cpp_data", "etc", "mono"))
        )
    ]

    for root, dirs, files in os.walk(data_path):
        normalized_root = os.path.normcase(os.path.normpath(root))
        if any(
            normalized_root == prefix
            or normalized_root.startswith(prefix + os.sep)
            for prefix in skip_root_prefixes
        ):
            dirs[:] = []
            continue
        for fn in files:
            if normalized_targets is not None and fn not in normalized_targets:
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext in blacklist_exts:
                continue
            assets_files.append(os.path.join(root, fn))
    assets_files.sort()
    return assets_files


def get_compile_method(datapath: str) -> str:
    """KR: 데이터 폴더의 컴파일 방식을 Mono/Il2cpp로 판별합니다.
    EN: Determines the compile method (Mono/Il2cpp) of the data folder.
    """
    if "Managed" in os.listdir(datapath):
        return "Mono"
    else:
        return "Il2cpp"


def _find_il2cpp_files(
    game_path: str, data_path: str
) -> tuple:
    """KR: IL2CPP 바이너리와 메타데이터 파일 경로를 PC/PS4/콘솔 순서로 탐색합니다.
    EN: Locates the IL2CPP binary and global-metadata.dat for PC and console layouts.

    PC layout:   <game_path>/GameAssembly.dll
                 <data_path>/il2cpp_data/Metadata/global-metadata.dat
    PS4 layout:  <game_path>/eboot.bin
                 <data_path>/Metadata/global-metadata.dat

    Returns:
        (binary_path, metadata_path) — either value may be None if not found.
    """
    binary_candidates = [
        os.path.join(game_path, "GameAssembly.dll"),   # PC / Windows
        os.path.join(game_path, "eboot.bin"),           # PS4
        os.path.join(game_path, "GameAssembly.prx"),    # PS4 (rare)
    ]
    metadata_candidates = [
        os.path.join(data_path, "il2cpp_data", "Metadata", "global-metadata.dat"),  # PC
        os.path.join(data_path, "Metadata", "global-metadata.dat"),                  # PS4
    ]
    binary_path = next((p for p in binary_candidates if os.path.exists(p)), None)
    metadata_path = next((p for p in metadata_candidates if os.path.exists(p)), None)
    return binary_path, metadata_path


def _create_generator(
    unity_version: str,
    game_path: str,
    data_path: str,
    compile_method: str,
    lang: Language = "ko",
):
    """KR: 타입트리 생성기를 구성하고 Mono/Il2cpp 메타데이터를 로드합니다.
    EN: Configures the TypeTree generator and loads Mono/Il2cpp metadata.

    Returns None if TypeTreeGeneratorAPI is not installed; UnityPy will then
    fall back to its built-in type trees (built-in Unity types work, custom
    MonoBehaviours such as TMP_FontAsset are skipped).
    """
    try:
        generator = TypeTreeGenerator(unity_version)
    except Exception as e:
        if lang == "ko":
            _log_console(
                f"[generator] TypeTreeGenerator 초기화 실패 ({e})\n"
                "  TypeTreeGeneratorAPI가 설치되지 않았습니다. 내장 타입트리로 대체합니다.\n"
                "  SDF/TMP 폰트는 스캔되지 않을 수 있습니다."
            )
        else:
            _log_console(
                f"[generator] TypeTreeGenerator init failed ({e})\n"
                "  TypeTreeGeneratorAPI is not installed. Using built-in type trees.\n"
                "  SDF/TMP fonts may not be scanned."
            )
        return None
    if compile_method == "Mono":
        managed_dir = os.path.join(data_path, "Managed")
        for fn in os.listdir(managed_dir):
            if not fn.endswith(".dll"):
                continue
            try:
                with open(os.path.join(managed_dir, fn), "rb") as f:
                    generator.load_dll(f.read())
            except Exception as e:
                if lang == "ko":
                    _log_console(f"[generator] DLL 로드 실패: {fn} ({e})")
                else:
                    _log_console(f"[generator] Failed to load DLL: {fn} ({e})")
    else:
        il2cpp_path, metadata_path = _find_il2cpp_files(game_path, data_path)
        if il2cpp_path and metadata_path:
            try:
                with open(il2cpp_path, "rb") as f:
                    il2cpp = f.read()
                with open(metadata_path, "rb") as f:
                    metadata = f.read()
                generator.load_il2cpp(il2cpp, metadata)
            except Exception as e:
                # PS4/console binaries may not be supported — fall back to built-in type trees.
                # TTF Font objects will still parse; custom MonoBehaviours (TMP) will be skipped.
                if lang == "ko":
                    _log_console(
                        f"[generator] IL2CPP 타입트리 로드 실패 ({e}) — 내장 타입트리로 대체합니다.\n"
                        "  SDF/TMP 폰트는 스캔되지 않을 수 있습니다."
                    )
                else:
                    _log_console(
                        f"[generator] Failed to load IL2CPP type trees ({e}) — using built-in type trees.\n"
                        "  SDF/TMP fonts may not be scanned."
                    )
    return generator


def _scan_fonts_from_env(
    env: Any,
    file_name: str,
    lang: Language = "ko",
    detect_ps5_swizzle: bool = False,
) -> dict[str, list[JsonDict]]:
    """KR: 로드된 UnityPy env에서 TTF/SDF 폰트 정보를 추출합니다.
    EN: Extracts TTF/SDF font information from a loaded UnityPy env.
    """
    scanned: dict[str, list[JsonDict]] = {"ttf": [], "sdf": []}
    texture_lookup: dict[tuple[str, int], Any] = {}
    texture_swizzle_cache: dict[str, str | None] = {}
    if detect_ps5_swizzle:
        for item in env.objects:
            if item.type.name != "Texture2D":
                continue
            texture_lookup[(item.assets_file.name, int(item.path_id))] = item

    for obj in env.objects:
        try:
            if obj.type.name == "Font":
                font_name = obj.peek_name()
                if not font_name:
                    try:
                        font = obj.parse_as_object()
                        font_name = getattr(font, "m_Name", "") or ""
                    except Exception:
                        font_name = ""
                scanned["ttf"].append(
                    {
                        "file": file_name,
                        "assets_name": obj.assets_file.name,
                        "name": font_name,
                        "path_id": obj.path_id,
                    }
                )
            elif obj.type.name == "MonoBehaviour":
                parse_dict = None
                atlas_file_id = 0
                atlas_path_id = 0
                glyph_count = 0
                try:
                    parse_dict = obj.parse_as_dict()
                    unity_version_hint = getattr(obj.assets_file, "unity_version", None)
                    tmp_info = inspect_tmp_font_schema(
                        parse_dict,
                        unity_version=(
                            str(unity_version_hint) if unity_version_hint else None
                        ),
                    )
                except Exception:
                    if lang == "ko":
                        debug_parse_log(
                            f"[scan_fonts] parse_as_dict 실패: {file_name} | PathID {obj.path_id}"
                        )
                    else:
                        debug_parse_log(
                            f"[scan_fonts] parse_as_dict failed: {file_name} | PathID {obj.path_id}"
                        )
                    continue

                if not tmp_info.get("is_tmp"):
                    continue

                try:
                    if parse_dict is None:
                        parse_dict = obj.parse_as_dict()
                    glyph_count = int(tmp_info.get("glyph_count", 0) or 0)
                    atlas_file_id = int(tmp_info.get("atlas_file_id", 0) or 0)
                    atlas_path_id = int(tmp_info.get("atlas_path_id", 0) or 0)
                    # KR: 외부 참조 stub(FileID!=0, PathID=0)은 실제 교체 대상이 아닙니다.
                    # EN: External reference stubs (FileID!=0, PathID=0) are not actual replacement targets.
                    if atlas_file_id != 0 and atlas_path_id == 0:
                        continue
                    if glyph_count == 0:
                        continue
                except Exception:
                    if lang == "ko":
                        debug_parse_log(
                            f"[scan_fonts] SDF 필드 검사 실패: {file_name} | PathID {obj.path_id}"
                        )
                    else:
                        debug_parse_log(
                            f"[scan_fonts] SDF field check failed: {file_name} | PathID {obj.path_id}"
                        )
                    continue

                sdf_info: JsonDict = {
                    "file": file_name,
                    "assets_name": obj.assets_file.name,
                    "name": obj.peek_name(),
                    "path_id": obj.path_id,
                }
                if detect_ps5_swizzle:
                    swizzle_state = False
                    if atlas_file_id == 0 and atlas_path_id != 0:
                        cache_key = f"{obj.assets_file.name}|{atlas_path_id}"
                        if cache_key in texture_swizzle_cache:
                            swizzle_verdict = texture_swizzle_cache[cache_key]
                        else:
                            texture_obj = texture_lookup.get(
                                (obj.assets_file.name, atlas_path_id)
                            )
                            swizzle_verdict = (
                                detect_texture_object_ps5_swizzle(texture_obj)
                                if texture_obj is not None
                                else None
                            )
                            texture_swizzle_cache[cache_key] = swizzle_verdict
                        swizzle_state = swizzle_verdict == "likely_swizzled_input"
                    sdf_info["swizzle"] = "True" if swizzle_state else "False"

                scanned["sdf"].append(sdf_info)
        except Exception as e:
            if lang == "ko":
                _log_console(
                    f"[scan_fonts] 오브젝트 처리 실패: {file_name} | PathID {obj.path_id} ({e})"
                )
            else:
                _log_console(
                    f"[scan_fonts] Object processing failed: {file_name} | PathID {obj.path_id} ({e})"
                )
            continue

    return scanned


def _scan_fonts_in_asset_file(
    assets_file: str,
    generator: TypeTreeGenerator,
    lang: Language = "ko",
    detect_ps5_swizzle: bool = False,
) -> tuple[dict[str, list[JsonDict]], str | None]:
    """KR: 단일 에셋 파일을 로드해 폰트 정보를 추출합니다.
    EN: Loads a single asset file and extracts font information.
    """
    file_name = os.path.basename(assets_file)
    scanned: dict[str, list[JsonDict]] = {"ttf": [], "sdf": []}

    env = None
    try:
        env = UnityPy.load(assets_file)
        if generator is not None:
            env.typetree_generator = generator
    except Exception as e:
        if lang == "ko":
            return scanned, f"UnityPy.load 실패: {assets_file} ({e})"
        return scanned, f"UnityPy.load failed: {assets_file} ({e})"

    try:
        scanned = _scan_fonts_from_env(
            env, file_name, lang=lang, detect_ps5_swizzle=detect_ps5_swizzle
        )
    finally:
        close_unitypy_env(env)
        env = None
        gc.collect()

    return scanned, None


def _scan_fonts_via_worker(
    game_path: str,
    assets_file: str,
    lang: Language = "ko",
    detect_ps5_swizzle: bool = False,
) -> tuple[dict[str, list[JsonDict]], str | None]:
    """KR: 파일 단위 서브프로세스 워커로 스캔해 크래시를 격리합니다.
    EN: Scans via a per-file subprocess worker to isolate crashes.
    """
    fd, output_path = tempfile.mkstemp(prefix="scan_worker_", suffix=".json")
    os.close(fd)
    worker_exit_hints = {
        -1073741819: "ACCESS_VIOLATION(0xC0000005)",
        3221225477: "ACCESS_VIOLATION(0xC0000005)",
    }
    access_violation_codes = set(worker_exit_hints.keys())

    def _run_worker(
    ) -> tuple[dict[str, list[JsonDict]], str | None, str | None, int | None]:
        try:
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception:
                pass

            if getattr(sys, "frozen", False):
                cmd = [
                    sys.executable,
                    "--gamepath",
                    game_path,
                    "--_scan-file-worker",
                    assets_file,
                    "--_scan-file-worker-output",
                    output_path,
                ]
            else:
                cmd = [
                    sys.executable,
                    os.path.abspath(__file__),
                    "--gamepath",
                    game_path,
                    "--_scan-file-worker",
                    assets_file,
                    "--_scan-file-worker-output",
                    output_path,
                ]
            if detect_ps5_swizzle:
                cmd.append("--ps5-swizzle")

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1800,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()
                hint = worker_exit_hints.get(int(proc.returncode))
                hint_text = f" [{hint}]" if hint else ""
                if lang == "ko":
                    return (
                        {"ttf": [], "sdf": []},
                        None,
                        f"scan worker 실패 (exit={proc.returncode}{hint_text}): {detail}",
                        int(proc.returncode),
                    )
                return (
                    {"ttf": [], "sdf": []},
                    None,
                    f"scan worker failed (exit={proc.returncode}{hint_text}): {detail}",
                    int(proc.returncode),
                )

            if not os.path.exists(output_path):
                if lang == "ko":
                    return (
                        {"ttf": [], "sdf": []},
                        None,
                        "scan worker 결과 파일이 없습니다.",
                        None,
                    )
                return (
                    {"ttf": [], "sdf": []},
                    None,
                    "scan worker output file is missing.",
                    None,
                )

            with open(output_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            scanned = {
                "ttf": list(payload.get("ttf", []))
                if isinstance(payload, dict)
                else [],
                "sdf": list(payload.get("sdf", []))
                if isinstance(payload, dict)
                else [],
            }
            worker_error = None
            if isinstance(payload, dict):
                worker_error = payload.get("error")
                if not isinstance(worker_error, str):
                    worker_error = None
            return scanned, worker_error, None, int(proc.returncode)
        except Exception as e:
            if lang == "ko":
                return (
                    {"ttf": [], "sdf": []},
                    None,
                    f"scan worker 실행 실패: {e!r}",
                    None,
                )
            return (
                {"ttf": [], "sdf": []},
                None,
                f"failed to run scan worker: {e!r}",
                None,
            )

    try:
        scanned, worker_error, full_error, full_returncode = _run_worker()
        if full_error is None:
            return scanned, worker_error

        # KR: ACCESS_VIOLATION은 일시적인 경우가 있어 full 모드 1회 재시도합니다.
        # EN: ACCESS_VIOLATION can be transient, so retry once in full mode.
        if full_returncode in access_violation_codes:
            retry_scanned, retry_worker_error, retry_error, _ = _run_worker()
            if retry_error is None:
                if lang == "ko":
                    recovered = "scan worker 재시도로 크래시를 복구했습니다."
                else:
                    recovered = "Recovered scan worker crash by retry."
                if retry_worker_error:
                    return retry_scanned, f"{recovered} {retry_worker_error}"
                return retry_scanned, recovered
            if lang == "ko":
                return {"ttf": [], "sdf": []}, f"{full_error} | 재시도 실패: {retry_error}"
            return {"ttf": [], "sdf": []}, f"{full_error} | retry failed: {retry_error}"

        return {"ttf": [], "sdf": []}, full_error
    finally:
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            pass


def scan_fonts(
    game_path: str,
    lang: Language = "ko",
    target_files: set[str] | None = None,
    exclude_exts: set[str] | None = None,
    isolate_files: bool = True,
    scan_jobs: int = 1,
    ps5_swizzle: bool = False,
) -> dict[str, list[JsonDict]]:
    """KR: 게임 에셋을 스캔해 TTF/SDF 폰트 목록을 반환합니다.

    target_files가 있으면 해당 파일만 스캔합니다.
    exclude_exts가 있으면 해당 확장자는 스캔에서 제외합니다.
    isolate_files=True면 파일 단위 워커 프로세스로 스캔해 크래시를 격리합니다.
    scan_jobs>1이면 isolate_files 경로에서 워커를 병렬 실행합니다.
    EN: Scans game assets and returns a list of TTF/SDF fonts.

    If target_files is provided, only those files are scanned.
    If exclude_exts is provided, those extensions are excluded from scanning.
    If isolate_files=True, scans via per-file worker processes to isolate crashes.
    If scan_jobs>1, runs workers in parallel on the isolate_files path.
    """
    data_path = get_data_path(game_path, lang=lang)
    unity_version = get_unity_version(game_path, lang=lang)
    assets_files = find_assets_files(
        game_path,
        lang=lang,
        target_files=target_files,
        exclude_exts=exclude_exts,
    )
    compile_method = get_compile_method(data_path)
    generator = _create_generator(
        unity_version, game_path, data_path, compile_method, lang=lang
    )

    fonts: dict[str, list[JsonDict]] = {
        "ttf": [],
        "sdf": [],
    }

    total_files = len(assets_files)
    try:
        scan_jobs = int(scan_jobs)
    except Exception:
        scan_jobs = 1
    if scan_jobs < 1:
        scan_jobs = 1
    if lang == "ko":
        if target_files:
            _log_console(
                f"[scan_fonts] --target-file 기준 스캔 시작: {total_files}개 파일"
            )
        else:
            _log_console(f"[scan_fonts] 전체 스캔 시작: {total_files}개 파일")
    else:
        if target_files:
            _log_console(
                f"[scan_fonts] Starting target-file scan: {total_files} file(s)"
            )
        else:
            _log_console(f"[scan_fonts] Starting full scan: {total_files} file(s)")

    if isolate_files and scan_jobs > 1 and total_files > 1:
        max_workers = min(scan_jobs, total_files)
        if lang == "ko":
            _log_console(f"[scan_fonts] 병렬 워커 모드: {max_workers}개")
        else:
            _log_console(f"[scan_fonts] Parallel worker mode: {max_workers}")

        indexed_results: dict[
            int, tuple[dict[str, list[JsonDict]], str | None, str]
        ] = {}
        retry_candidates: list[tuple[int, str, str]] = []
        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_meta = {
                executor.submit(
                    _scan_fonts_via_worker,
                    game_path,
                    assets_file,
                    lang,
                    ps5_swizzle,
                ): (idx, os.path.basename(assets_file), assets_file)
                for idx, assets_file in enumerate(assets_files)
            }
            for future in as_completed(future_to_meta):
                idx, fn, assets_file = future_to_meta[future]
                try:
                    scanned, worker_error = future.result()
                except Exception as e:
                    scanned = {"ttf": [], "sdf": []}
                    worker_error = (
                        f"scan worker 실행 실패: {e!r}"
                        if lang == "ko"
                        else f"failed to run scan worker: {e!r}"
                    )
                indexed_results[idx] = (scanned, worker_error, fn)
                if _is_scan_retry_candidate(scanned, worker_error):
                    retry_candidates.append((idx, assets_file, fn))
                completed += 1
                if lang == "ko":
                    _log_console(f"[scan_fonts] 진행 {completed}/{total_files}: {fn}")
                else:
                    _log_console(
                        f"[scan_fonts] Progress {completed}/{total_files}: {fn}"
                    )

        if retry_candidates:
            if lang == "ko":
                _log_console(
                    f"[scan_fonts] 최종 순차 재시도 시작: {len(retry_candidates)}개 파일"
                )
            else:
                _log_console(
                    f"[scan_fonts] Starting final sequential retries: {len(retry_candidates)} file(s)"
                )
            retry_total = len(retry_candidates)
            for retry_idx, (idx, assets_file, fn) in enumerate(
                retry_candidates, start=1
            ):
                if lang == "ko":
                    _log_console(f"[scan_fonts] 재시도 {retry_idx}/{retry_total}: {fn}")
                else:
                    _log_console(f"[scan_fonts] Retry {retry_idx}/{retry_total}: {fn}")
                retry_scanned, retry_worker_error = _scan_fonts_via_worker(
                    game_path,
                    assets_file,
                    lang=lang,
                    detect_ps5_swizzle=ps5_swizzle,
                )
                previous_scanned, previous_error, _ = indexed_results.get(
                    idx, ({"ttf": [], "sdf": []}, None, fn)
                )
                if (
                    retry_worker_error
                    and not list(retry_scanned.get("ttf", []))
                    and not list(retry_scanned.get("sdf", []))
                    and isinstance(previous_error, str)
                    and previous_error.strip()
                ):
                    if lang == "ko":
                        merged_error = (
                            f"{previous_error} | 최종 순차 재시도 실패: {retry_worker_error}"
                        )
                    else:
                        merged_error = (
                            f"{previous_error} | final sequential retry failed: {retry_worker_error}"
                        )
                    indexed_results[idx] = (previous_scanned, merged_error, fn)
                else:
                    indexed_results[idx] = (retry_scanned, retry_worker_error, fn)
                gc.collect()

        for idx in range(total_files):
            scanned, worker_error, processed_file_name = indexed_results.get(
                idx, ({"ttf": [], "sdf": []}, None, "")
            )
            if worker_error:
                if lang == "ko":
                    _log_console(
                        f"[scan_fonts] 워커 경고: {processed_file_name} | {worker_error}"
                    )
                else:
                    _log_console(
                        f"[scan_fonts] Worker warning: {processed_file_name} | {worker_error}"
                    )
            _log_scan_result_details(processed_file_name or f"index_{idx}", scanned)
            fonts["ttf"].extend(scanned.get("ttf", []))
            fonts["sdf"].extend(scanned.get("sdf", []))
    else:
        for idx, assets_file in enumerate(assets_files, start=1):
            fn = os.path.basename(assets_file)
            if lang == "ko":
                _log_console(f"[scan_fonts] 진행 {idx}/{total_files}: {fn}")
            else:
                _log_console(f"[scan_fonts] Progress {idx}/{total_files}: {fn}")

            if isolate_files:
                scanned, worker_error = _scan_fonts_via_worker(
                    game_path,
                    assets_file,
                    lang=lang,
                    detect_ps5_swizzle=ps5_swizzle,
                )
                if worker_error:
                    if lang == "ko":
                        _log_console(f"[scan_fonts] 워커 경고: {fn} | {worker_error}")
                    else:
                        _log_console(
                            f"[scan_fonts] Worker warning: {fn} | {worker_error}"
                        )
                _log_scan_result_details(fn, scanned)
                fonts["ttf"].extend(scanned.get("ttf", []))
                fonts["sdf"].extend(scanned.get("sdf", []))
                continue

            scanned, load_error = _scan_fonts_in_asset_file(
                assets_file,
                generator,
                lang=lang,
                detect_ps5_swizzle=ps5_swizzle,
            )
            if load_error:
                _log_console(f"[scan_fonts] {load_error}")
                continue
            _log_scan_result_details(fn, scanned)
            fonts["ttf"].extend(scanned.get("ttf", []))
            fonts["sdf"].extend(scanned.get("sdf", []))

    return fonts


def parse_fonts(
    game_path: str,
    lang: Language = "ko",
    target_files: set[str] | None = None,
    exclude_exts: set[str] | None = None,
    scan_jobs: int = 1,
    ps5_swizzle: bool = False,
) -> str:
    """KR: 스캔한 폰트를 JSON으로 저장하고 결과 파일 경로를 반환합니다.

    target_files가 있으면 해당 파일만 파싱합니다.
    exclude_exts가 있으면 해당 확장자는 스캔에서 제외합니다.
    EN: Saves scanned fonts as JSON and returns the result file path.

    If target_files is provided, only those files are parsed.
    If exclude_exts is provided, those extensions are excluded from scanning.
    """
    # KR: parse 모드는 파일 단위 워커로 스캔해 UnityPy 하드 크래시를 격리합니다.
    # EN: Parse mode scans via per-file workers to isolate UnityPy hard crashes.
    fonts = scan_fonts(
        game_path,
        lang=lang,
        target_files=target_files,
        exclude_exts=exclude_exts,
        isolate_files=True,
        scan_jobs=scan_jobs,
        ps5_swizzle=ps5_swizzle,
    )
    game_name = os.path.basename(game_path)
    output_file = os.path.join(get_script_dir(), f"{game_name}.json")

    result: dict[str, JsonDict] = {}

    for font in fonts["ttf"]:
        key = (
            f"{font['file']}|{font['assets_name']}|{font['name']}|TTF|{font['path_id']}"
        )
        result[key] = {
            "File": font["file"],
            "assets_name": font["assets_name"],
            "Path_ID": font["path_id"],
            "Type": "TTF",
            "Name": font["name"],
            "Replace_to": "",
        }

    for font in fonts["sdf"]:
        key = (
            f"{font['file']}|{font['assets_name']}|{font['name']}|SDF|{font['path_id']}"
        )
        if ps5_swizzle:
            swizzle_flag = "True" if parse_bool_flag(font.get("swizzle")) else "False"
            process_swizzle_flag = (
                "True" if parse_bool_flag(font.get("process_swizzle")) else "False"
            )
            entry: JsonDict = {
                "File": font["file"],
                "assets_name": font["assets_name"],
                "Path_ID": font["path_id"],
                "Type": "SDF",
                "Name": font["name"],
                "force_raster": "False",
                "swizzle": swizzle_flag,
                "process_swizzle": process_swizzle_flag,
                "Replace_to": "",
            }
        else:
            entry = {
                "File": font["file"],
                "assets_name": font["assets_name"],
                "Path_ID": font["path_id"],
                "Type": "SDF",
                "Name": font["name"],
                "force_raster": "False",
                "Replace_to": "",
            }
        result[key] = entry

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=4, ensure_ascii=False)

    if lang == "ko":
        _log_console(f"폰트 정보가 '{output_file}'에 저장되었습니다.")
        _log_console(f"  - TTF 폰트: {len(fonts['ttf'])}개")
        _log_console(f"  - SDF 폰트: {len(fonts['sdf'])}개")
    else:
        _log_console(f"Font information saved to '{output_file}'.")
        _log_console(f"  - TTF fonts: {len(fonts['ttf'])}")
        _log_console(f"  - SDF fonts: {len(fonts['sdf'])}")
    return output_file


def _format_byte_size(num_bytes: int) -> str:
    size = float(max(0, int(num_bytes or 0)))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{int(num_bytes or 0)} B"


def _dedupe_preserve_order_str(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in values:
        key = str(item).strip()
        if not key:
            continue
        lowered = key.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(key)
    return ordered


def _build_font_asset_name_candidates(
    normalized: str,
    prefer_raster: bool = False,
) -> tuple[list[str], list[str]]:
    raw_name = str(normalized).strip()

    def _strip_render_suffix(name: str) -> str:
        if name.endswith(" SDF"):
            return name[: -len(" SDF")]
        if name.endswith(" Raster"):
            return name[: -len(" Raster")]
        return name

    base_name = _strip_render_suffix(raw_name)
    if prefer_raster:
        name_candidates = _dedupe_preserve_order_str(
            [raw_name, f"{base_name} Raster", f"{base_name} SDF"]
        )
    else:
        name_candidates = _dedupe_preserve_order_str(
            [raw_name, f"{base_name} SDF", f"{base_name} Raster"]
        )

    font_name_candidates = _dedupe_preserve_order_str(
        [raw_name, base_name] + name_candidates
    )
    return font_name_candidates, name_candidates


_BULK_SDF_PADDING_VARIANTS = (5, 7, 15)


def _select_builtin_bulk_padding_variant(
    normalized: str,
    source_padding: float | int | None,
) -> int | None:
    base_name = normalize_font_name(normalized).strip().lower()
    if base_name not in {"nanumgothic", "mulmaru", "sarabun", "notosansthai"}:
        return None
    try:
        numeric_padding = float(source_padding) if source_padding is not None else 0.0
    except Exception:
        numeric_padding = 0.0
    if numeric_padding <= 0:
        return None
    return min(
        _BULK_SDF_PADDING_VARIANTS,
        key=lambda value: (abs(float(value) - numeric_padding), -int(value)),
    )


def _iter_kr_asset_roots(
    kr_assets: str,
    padding_variant: int | None = None,
) -> list[str]:
    roots: list[str] = []
    if padding_variant is not None:
        roots.append(os.path.join(kr_assets, f"Padding_{int(padding_variant)}"))
    roots.append(kr_assets)
    return roots


def _find_replacement_sdf_atlas_path(
    script_dir: str,
    normalized: str,
    prefer_raster: bool = False,
) -> str | None:
    th_assets = os.path.join(script_dir, "TH_ASSETS")
    kr_assets = os.path.join(script_dir, "KR_ASSETS")
    _, name_candidates = _build_font_asset_name_candidates(
        normalized, bool(prefer_raster)
    )
    for asset_dir in (th_assets, kr_assets):
        for name_candidate in name_candidates:
            atlas_path = os.path.join(asset_dir, f"{name_candidate} Atlas.png")
            if os.path.exists(atlas_path):
                return atlas_path
    return None


@lru_cache(maxsize=128)
def _estimate_replacement_sdf_texture_bytes(
    script_dir: str,
    normalized: str,
    prefer_raster: bool = False,
) -> int:
    atlas_path = _find_replacement_sdf_atlas_path(
        script_dir,
        normalized,
        bool(prefer_raster),
    )
    if not atlas_path:
        return 0

    try:
        with Image.open(atlas_path) as atlas_image:
            width = int(atlas_image.width)
            height = int(atlas_image.height)
            try:
                channel_count = max(1, len(atlas_image.getbands()))
            except Exception:
                channel_count = 4
        if width <= 0 or height <= 0:
            return 0
        return width * height * channel_count
    except Exception:
        return 0


def _estimate_sdf_texture_batch_profile(
    file_sdf_replacements: dict[str, JsonDict],
    *,
    force_raster: bool = False,
    script_dir: str | None = None,
    batch_target_bytes: int = _AUTO_SPLIT_TEXTURE_BATCH_TARGET_BYTES,
) -> JsonDict:
    if script_dir is None:
        script_dir = get_script_dir()

    estimated_total_bytes = 0
    estimated_target_count = 0
    max_target_bytes = 0

    for info in file_sdf_replacements.values():
        if not isinstance(info, dict):
            continue
        replacement_font = str(info.get("Replace_to") or "").strip()
        if not replacement_font:
            continue
        prefer_raster = bool(force_raster) or parse_bool_flag(info.get("force_raster"))
        estimated_bytes = _estimate_replacement_sdf_texture_bytes(
            script_dir,
            normalize_font_name(replacement_font),
            prefer_raster,
        )
        if estimated_bytes <= 0:
            continue
        estimated_target_count += 1
        estimated_total_bytes += estimated_bytes
        max_target_bytes = max(max_target_bytes, estimated_bytes)

    suggested_batch_size = 0
    if estimated_target_count > 0 and max_target_bytes > 0:
        safe_target = max(1, int(batch_target_bytes or 0))
        suggested_batch_size = max(1, safe_target // max_target_bytes)
        suggested_batch_size = min(estimated_target_count, suggested_batch_size)

    return {
        "estimated_target_count": estimated_target_count,
        "estimated_total_bytes": estimated_total_bytes,
        "max_target_bytes": max_target_bytes,
        "suggested_batch_size": suggested_batch_size,
    }


@lru_cache(maxsize=64)
def _load_font_assets_cached(
    script_dir: str,
    normalized: str,
    prefer_raster: bool = False,
    padding_variant: int | None = None,
) -> JsonDict:
    """KR: TH_ASSETS(우선) 또는 KR_ASSETS에서 폰트 리소스를 읽어 캐시에 저장합니다.
    EN: Reads font resources from TH_ASSETS (preferred) or KR_ASSETS and stores them in cache.
    """
    th_assets = os.path.join(script_dir, "TH_ASSETS")
    kr_assets = os.path.join(script_dir, "KR_ASSETS")
    # Prefer TH_ASSETS; fall back to KR_ASSETS if not found
    if os.path.isdir(th_assets):
        asset_roots = _iter_kr_asset_roots(th_assets, padding_variant=padding_variant)
    else:
        asset_roots = _iter_kr_asset_roots(kr_assets, padding_variant=padding_variant)
    font_name_candidates, name_candidates = _build_font_asset_name_candidates(
        normalized,
        bool(prefer_raster),
    )

    ttf_data = None
    for font_name in font_name_candidates:
        for ext in (".ttf", ".otf"):
            for asset_root in asset_roots:
                font_path = os.path.join(asset_root, f"{font_name}{ext}")
                if os.path.exists(font_path):
                    with open(font_path, "rb") as f:
                        ttf_data = f.read()
                    break
            if ttf_data is not None:
                break
        if ttf_data is not None:
            break

    sdf_data = None
    sdf_data_normalized = None
    sdf_swizzle = False
    sdf_process_swizzle = False
    for name_candidate in name_candidates:
        for asset_root in asset_roots:
            sdf_json_path = os.path.join(asset_root, f"{name_candidate}.json")
            if not os.path.exists(sdf_json_path):
                continue
            with open(sdf_json_path, "r", encoding="utf-8") as f:
                sdf_data = json.load(f)
            if isinstance(sdf_data, dict):
                sdf_data_normalized = normalize_sdf_data(sdf_data, deep_copy=True)
                sdf_swizzle = parse_bool_flag(sdf_data.get("swizzle"))
                sdf_process_swizzle = parse_bool_flag(sdf_data.get("process_swizzle"))
            break
        if sdf_data is not None:
            break

    sdf_atlas = None
    for name_candidate in name_candidates:
        for asset_root in asset_roots:
            sdf_atlas_path = os.path.join(asset_root, f"{name_candidate} Atlas.png")
            if not os.path.exists(sdf_atlas_path):
                continue
            with open(sdf_atlas_path, "rb") as f:
                sdf_atlas = Image.open(f)
                sdf_atlas.load()
            break
        if sdf_atlas is not None:
            break

    sdf_material_data = None
    for name_candidate in name_candidates:
        for asset_root in asset_roots:
            sdf_material_path = os.path.join(
                asset_root, f"{name_candidate} Material.json"
            )
            if not os.path.exists(sdf_material_path):
                continue
            with open(sdf_material_path, "r", encoding="utf-8") as f:
                sdf_material_data = json.load(f)
            break
        if sdf_material_data is not None:
            break

    return {
        "ttf_data": ttf_data,
        "sdf_data": sdf_data,
        "sdf_data_normalized": sdf_data_normalized,
        "sdf_atlas": sdf_atlas,
        "sdf_materials": sdf_material_data,
        "sdf_swizzle": sdf_swizzle,
        "sdf_process_swizzle": sdf_process_swizzle,
        "padding_variant": int(padding_variant) if padding_variant is not None else None,
    }


def load_font_assets(
    font_name: str,
    prefer_raster: bool = False,
    padding_variant: int | None = None,
) -> JsonDict:
    """KR: 지정 폰트명의 교체용 리소스(TTF/SDF/Atlas/Material)를 로드합니다.
    EN: Loads replacement resources (TTF/SDF/Atlas/Material) for the specified font name.
    """
    normalized = normalize_font_name(font_name)
    cached_assets = _load_font_assets_cached(
        get_script_dir(),
        normalized,
        bool(prefer_raster),
        int(padding_variant) if padding_variant is not None else None,
    )
    atlas = cached_assets["sdf_atlas"]
    return {
        "ttf_data": cached_assets["ttf_data"],
        "sdf_data": cached_assets["sdf_data"],
        "sdf_data_normalized": cached_assets.get("sdf_data_normalized"),
        # KR: 캐시된 atlas 객체를 재사용하여 교체 시 이미지 중복 생성을 방지합니다.
    # EN: Reuses cached atlas objects to prevent duplicate image creation during replacement.
        "sdf_atlas": atlas,
        "sdf_materials": cached_assets["sdf_materials"],
        "sdf_swizzle": cached_assets.get("sdf_swizzle"),
        "sdf_process_swizzle": bool(cached_assets.get("sdf_process_swizzle", False)),
        "padding_variant": cached_assets.get("padding_variant"),
    }


# KR: TypeTree에 정의되지 않은 trailing bytes를 ObjectReader path_id 기준으로 보존합니다.
# EN: Preserves trailing bytes not defined in TypeTree, keyed by ObjectReader path_id.
_trailing_bytes_store: dict[int, bytes] = {}


def _capture_trailing_bytes(obj: Any) -> bytes:
    """KR: TypeTree 파싱 후 읽히지 않은 trailing bytes를 캡처합니다.
    EN: Captures unread trailing bytes after TypeTree parsing.
    """
    pos = obj.reader.Position
    end = obj.byte_start + obj.byte_size
    if pos < end:
        remaining = obj.reader.read_bytes(end - pos)
        obj.reader.Position = pos
        return remaining
    return b""


def _safe_parse_as_object(obj: Any, **kwargs: Any) -> Any:
    """KR: parse_as_object()를 check_read=True로 먼저 시도하고,
    바이트 크기 불일치(중국판 Unity 등)로 실패하면 check_read=False로 재시도하고
    trailing bytes를 별도 저장소에 보존합니다.
    EN: Tries parse_as_object() with check_read=True first.
    On byte size mismatch (e.g. China Unity), retries with check_read=False
    and preserves trailing bytes in a separate store.
    """
    obj_id = id(obj)
    try:
        result = obj.parse_as_object(check_read=True, **kwargs)
        _trailing_bytes_store.pop(obj_id, None)
        return result
    except ValueError as e:
        if "Expected to read" in str(e) and "bytes" in str(e):
            result = obj.parse_as_object(check_read=False, **kwargs)
            trailing = _capture_trailing_bytes(obj)
            if trailing:
                _trailing_bytes_store[obj_id] = trailing
            else:
                _trailing_bytes_store.pop(obj_id, None)
            return result
        raise


def _safe_parse_as_dict(obj: Any, **kwargs: Any) -> dict[str, Any]:
    """KR: parse_as_dict()를 check_read=True로 먼저 시도하고,
    바이트 크기 불일치로 실패하면 check_read=False로 재시도하고
    trailing bytes를 별도 저장소에 보존합니다.
    EN: Tries parse_as_dict() with check_read=True first.
    On byte size mismatch, retries with check_read=False
    and preserves trailing bytes in a separate store.
    """
    obj_id = id(obj)
    try:
        result = obj.parse_as_dict(check_read=True, **kwargs)
        _trailing_bytes_store.pop(obj_id, None)
        return result
    except ValueError as e:
        if "Expected to read" in str(e) and "bytes" in str(e):
            result = obj.parse_as_dict(check_read=False, **kwargs)
            trailing = _capture_trailing_bytes(obj)
            if trailing:
                _trailing_bytes_store[obj_id] = trailing
            else:
                _trailing_bytes_store.pop(obj_id, None)
            return result
        raise


def _safe_save(obj: Any, parse_dict: Any) -> None:
    """KR: save() 후 trailing bytes가 있으면 raw data에 append합니다.
    EN: After save(), appends trailing bytes to raw data if present.
    """
    parse_dict.save()
    obj_id = id(obj)
    trailing = _trailing_bytes_store.pop(obj_id, b"")
    if trailing:
        current_data = obj.get_raw_data()
        obj.set_raw_data(current_data + trailing)


def _has_trailing_bytes(obj: Any) -> bool:
    """KR: 이 오브젝트에 TypeTree로 읽히지 않는 trailing bytes가 있는지 확인합니다.
    EN: Checks whether this object has trailing bytes not read by TypeTree.
    """
    return id(obj) in _trailing_bytes_store


def _detect_typetree_size_mismatch(obj: Any) -> bool:
    """KR: TypeTree로 읽은 후 다시 쓰면 원본보다 작아지는지 감지합니다.
    중국판 Unity 등에서 TypeTree에 없는 추가 필드가 있으면 True를 반환합니다.
    EN: Detects if re-writing after TypeTree read produces smaller output than the original.
    Returns True if extra fields not in TypeTree exist (e.g. China Unity).
    """
    try:
        from UnityPy.helpers.TypeTreeHelper import write_typetree
        from UnityPy.streams import EndianBinaryWriter
        original_raw = obj.get_raw_data()
        d = obj.read_typetree(check_read=False)
        node = obj._get_typetree_node()
        w = EndianBinaryWriter(endian=obj.reader.endian)
        write_typetree(d, node, w, obj.assets_file)
        rewritten_size = w.Length
        w.dispose()
        return rewritten_size < len(original_raw)
    except Exception:
        return False


def _binary_patch_texture2d(
    obj: Any,
    *,
    image_data: bytes,
    width: int,
    height: int,
    lang: str = "ko",
) -> bool:
    """KR: Texture2D를 TypeTree 재직렬화 없이 바이너리 패치합니다.
    중국판 Unity 등에서 TypeTree가 커버하지 못하는 extra bytes가 있을 때 사용합니다.
    EN: Binary-patches Texture2D without TypeTree re-serialization.
    Used when extra bytes not covered by TypeTree exist (e.g. China Unity).
    """
    import struct as _struct

    original_raw = obj.get_raw_data()
    if len(original_raw) < 48:
        return False

    # KR: 원본 raw에서 스트림 경로 문자열을 찾아 필드 위치를 역추적합니다.
    # 스트리밍 경로 문자열 검색 (.resS 또는 .resource)
    # EN: Finds stream path strings in the original raw data to trace back field positions.
    # Searches for streaming path strings (.resS or .resource)
    stream_path_marker = None
    for marker in [b".resS", b".resource"]:
        idx = original_raw.find(marker)
        if idx >= 0:
            # KR: 문자열 시작 위치를 찾기 위해 앞쪽으로 탐색
            # EN: Scan backwards to find the start position of the string
            str_start = idx
            while str_start > 0 and original_raw[str_start - 1:str_start] not in (b"\x00",):
                str_start -= 1
                if idx - str_start > 200:
                    break
            # KR: string length prefix는 str_start - 4 위치
            # EN: The string length prefix is at position str_start - 4
            path_len_pos = str_start - 4
            if path_len_pos < 0:
                continue
            try:
                path_len = _struct.unpack_from("<i", original_raw, path_len_pos)[0]
                if 0 < path_len < 256 and path_len_pos + 4 + path_len <= len(original_raw):
                    stream_path_marker = (path_len_pos, path_len, str_start)
                    break
            except Exception:
                continue

    # KR: TypeTree로 파싱하여 image data 위치와 trailing bytes를 정확히 파악합니다.
    # EN: Parses with TypeTree to precisely identify image data position and trailing bytes.
    try:
        d_temp = obj.read_typetree(check_read=False)
        orig_w = int(d_temp.get("m_Width", 0))
        orig_h = int(d_temp.get("m_Height", 0))
        orig_cis = int(d_temp.get("m_CompleteImageSize", 0))
        orig_img_data = d_temp.get("image data", b"")
        orig_img_len = len(orig_img_data) if isinstance(orig_img_data, (bytes, bytearray, memoryview)) else 0
    except Exception:
        return False

    if stream_path_marker is not None:
        # KR: 스트리밍 모드 — 경로 문자열 기준으로 필드 위치를 역추적합니다.
        # EN: Streaming mode -- traces back field positions based on the path string.
        path_len_pos, path_len, path_str_start = stream_path_marker
        stream_size_pos = path_len_pos - 4
        stream_offset_pos = stream_size_pos - 8
        image_data_size_pos = stream_offset_pos - 4
        orig_stream_end = path_str_start + path_len
        orig_stream_end += (4 - orig_stream_end % 4) % 4
    else:
        # KR: 이미 인라인 모드 (이전 교체로 .resS 참조가 제거됨) —
        # raw 끝에서 trailing + empty StreamData + image data 역순으로 위치를 계산합니다.
        # EN: Already inline mode (.resS reference removed by previous replacement) --
        # Calculates positions in reverse from raw end: trailing + empty StreamData + image data.
        #
        # KR: 레이아웃 (인라인, 빈 StreamData):
        #   ... 메타데이터 ...
        # EN: Layout (inline, empty StreamData):
        #   ... metadata ...
        #   int image_data_size
        #   byte[] image_data (+ 4바이트 정렬)
        #   uint64 stream_offset = 0
        #   uint32 stream_size = 0
        #   int path_len = 0
        #   (4바이트 정렬)
        #   [trailing bytes]
        #
        # KR: StreamData (빈 상태) = 8 + 4 + 4 = 16바이트 (이미 정렬됨)
        # Image data 블록 = 4 (크기 prefix) + orig_img_len + 정렬 패딩
        # EN: StreamData (empty) = 8 + 4 + 4 = 16 bytes (already aligned)
        # Image data block = 4 (size prefix) + orig_img_len + alignment padding

        # KR: TypeTree가 읽은 바이트 수를 계산합니다
        # EN: Calculates the number of bytes read by TypeTree
        obj.reset()
        pos0 = obj.reader.Position
        obj.read_typetree(check_read=False)
        pos1 = obj.reader.Position
        typetree_bytes = pos1 - pos0

        trailing_size = len(original_raw) - typetree_bytes
        # KR: StreamData (빈 상태) 크기: uint64(8) + uint32(4) + int32(4) = 16
        # EN: StreamData (empty state) size: uint64(8) + uint32(4) + int32(4) = 16
        empty_stream_data_size = 16
        # KR: image data 블록: 4 (크기 prefix) + 데이터 + 패딩
        # EN: image data block: 4 (size prefix) + data + padding
        img_block_size = 4 + orig_img_len
        img_block_padded = img_block_size + (4 - img_block_size % 4) % 4

        image_data_size_pos = typetree_bytes - trailing_size - empty_stream_data_size - img_block_padded
        if image_data_size_pos < 0:
            # KR: 위치 계산 실패 — 대안: metadata 크기를 직접 계산
            # 메타데이터 = total_raw - trailing - empty_stream - img_block
            # EN: Position calculation failed -- fallback: compute metadata size directly
            # metadata = total_raw - trailing - empty_stream - img_block
            image_data_size_pos = len(original_raw) - trailing_size - empty_stream_data_size - img_block_padded
        orig_stream_end = len(original_raw) - trailing_size

    if image_data_size_pos < 0 or image_data_size_pos >= len(original_raw):
        return False

    # KR: TypeTree 파싱으로 정확한 필드 오프셋을 구하고, 원본 raw를 직접 패치합니다.
    # EN: Obtains exact field offsets via TypeTree parsing and directly patches the original raw data.
    from UnityPy.helpers.TypeTreeHelper import TypeTreeConfig as _TTC, read_value as _rv
    from UnityPy.streams import EndianBinaryReader as _EBR
    field_offsets: dict[str, int] = {}
    try:
        _tmp_reader = _EBR(original_raw, endian=obj.reader.endian)
        _tmp_config = _TTC(True, obj.assets_file, False)
        _node = obj._get_typetree_node()
        for _child in _node.m_Children:
            _pos_before = _tmp_reader.Position
            _rv(_child, _tmp_reader, _tmp_config)
            field_offsets[_child.m_Name] = _pos_before
    except Exception:
        pass

    # KR: image data 필드의 시작 오프셋 = image_data_size_pos (TypeTree 기준)
    # EN: Start offset of the image data field = image_data_size_pos (TypeTree basis)
    if "image data" in field_offsets:
        image_data_size_pos = field_offsets["image data"]

    part1 = bytearray(original_raw[:image_data_size_pos])

    # KR: 정확한 오프셋으로 필드 패치 (패턴 검색 대신 직접 오프셋 사용)
    # EN: Patches fields at exact offsets (uses direct offsets instead of pattern search)
    if "m_Width" in field_offsets and field_offsets["m_Width"] + 4 <= len(part1):
        _struct.pack_into("<i", part1, field_offsets["m_Width"], width)
    if "m_Height" in field_offsets and field_offsets["m_Height"] + 4 <= len(part1):
        _struct.pack_into("<i", part1, field_offsets["m_Height"], height)
    if "m_CompleteImageSize" in field_offsets and field_offsets["m_CompleteImageSize"] + 4 <= len(part1):
        _struct.pack_into("<I", part1, field_offsets["m_CompleteImageSize"], len(image_data))

    part1 = bytes(part1)

    # KR: Part 2+3 — inline image data + 빈 StreamingInfo
    # EN: Part 2+3 -- inline image data + empty StreamingInfo
    from UnityPy.streams import EndianBinaryWriter
    w = EndianBinaryWriter(endian="<")
    w.write_int(len(image_data))
    w.write(image_data)
    pos = w.Length
    pad = (4 - pos % 4) % 4
    if pad:
        w.write(b"\x00" * pad)
    # KR: 빈 StreamingInfo
    # EN: Empty StreamingInfo
    w.write_u_long(0)   # offset
    w.write_u_int(0)    # size
    w.write_int(0)      # empty path (length=0)
    pos = w.Length
    pad = (4 - pos % 4) % 4
    if pad:
        w.write(b"\x00" * pad)
    part2_3 = w.bytes
    w.dispose()

    # KR: Part 4 — trailing bytes (TypeTree가 읽은 이후의 바이트)
    # EN: Part 4 -- trailing bytes (bytes after what TypeTree read)
    if "image data" in field_offsets and _tmp_reader is not None:
        # KR: TypeTree가 읽은 총 바이트 수를 사용
        # EN: Uses the total number of bytes read by TypeTree
        typetree_end = _tmp_reader.Position
        part4 = original_raw[typetree_end:]
    else:
        part4 = original_raw[orig_stream_end:]

    new_raw = part1 + part2_3 + part4
    obj.set_raw_data(new_raw)
    obj.assets_file.mark_changed()

    if lang == "ko":
        _log_debug(
            f"[binary_patch_texture2d] PathID={obj.path_id} "
            f"orig_raw={len(original_raw)}B new_raw={len(new_raw):,}B "
            f"trailing={len(part4)}B"
        )
    return True


def replace_fonts_in_file(
    unity_version: str,
    game_path: str,
    assets_file: str,
    replacements: dict[str, JsonDict],
    replace_ttf: bool = True,
    replace_sdf: bool = True,
    use_game_mat: bool = False,
    use_game_line_metrics: bool = False,
    force_raster: bool = False,
    material_scale_by_padding: bool = True,
    outline_ratio: float = 1.0,
    prefer_original_compress: bool = False,
    temp_root_dir: str | None = None,
    generator: TypeTreeGenerator | None = None,
    replacement_lookup: dict[tuple[str, str, str, int], str] | None = None,
    ps5_swizzle: bool = False,
    preview_export: bool = False,
    preview_root: str | None = None,
    prefer_builtin_padding_variants: bool = False,
    asset_file_index: dict[str, Any] | None = None,
    deferred_texture_plans: dict[str, dict[str, Any]] | None = None,
    deferred_material_plans: dict[str, dict[str, Any]] | None = None,
    deferred_material_atlas_plans: dict[str, dict[str, Any]] | None = None,
    pending_external_patch_files: set[str] | None = None,
    phase_callback: Callable[[str, JsonDict], None] | None = None,
    lang: Language = "ko",
) -> bool:
    """KR: 단일 assets 파일의 TTF/SDF 폰트를 교체하고 저장합니다.

    기본 모드는 줄 간격 관련 메트릭(LineHeight/Ascender/Descender 등)을 게임 원본 비율로 보정해
    교체 pointSize에 맞춰 적용합니다.
    use_game_line_metrics=True면 게임 원본 줄 간격 메트릭을 그대로 사용합니다.
    pointSize는 옵션과 무관하게 교체 폰트 값을 유지합니다.
    material_scale_by_padding=True면 SDF 머티리얼 float를 (게임 padding / 교체 padding) 비율로 보정합니다.
    outline_ratio는 현재 선택된 Material 기준(_OutlineWidth/_OutlineSoftness)에 배율로 적용합니다.
    prefer_original_compress=True면 원본 압축 우선, False면 무압축 계열 우선 저장 전략을 사용합니다.
    ps5_swizzle=True면 대상 Atlas의 swizzle 상태를 판별해 교체 Atlas를 자동 swizzle/unswizzle합니다.
    preview_export=True면 preview 폴더에 Atlas/Glyph crop 미리보기를 저장합니다.
    ps5_swizzle=True일 때는 unswizzle 기준으로 저장합니다.
    temp_root_dir가 지정되면 임시 저장 디렉터리 루트로 사용합니다.
    EN: Replaces TTF/SDF fonts in a single assets file and saves it.

    Default mode adjusts line-spacing metrics (LineHeight/Ascender/Descender etc.) by the game's original ratio
    and applies them scaled to the replacement pointSize.
    use_game_line_metrics=True uses the game's original line-spacing metrics as-is.
    pointSize always retains the replacement font's value regardless of options.
    material_scale_by_padding=True adjusts SDF material floats by (game padding / replacement padding) ratio.
    outline_ratio applies as a multiplier to the selected Material's _OutlineWidth/_OutlineSoftness.
    prefer_original_compress=True uses original compression first; False uses uncompressed-first strategy.
    ps5_swizzle=True detects target Atlas swizzle state and auto-swizzles/unswizzles the replacement Atlas.
    preview_export=True saves Atlas/Glyph crop previews to the preview folder.
    When ps5_swizzle=True, saves based on unswizzle.
    temp_root_dir, if specified, is used as the temp storage directory root.
    """
    fn_without_path = os.path.basename(assets_file)
    current_file_key = _normalize_asset_file_key(assets_file) or os.path.abspath(
        assets_file
    )
    data_path = get_data_path(game_path, lang=lang)
    using_custom_temp_root = temp_root_dir is not None
    tmp_root = (
        os.path.abspath(temp_root_dir)
        if using_custom_temp_root
        else os.path.join(data_path, "temp")
    )
    tmp_path = os.path.join(tmp_root, "unity_font_replacer_temp")
    if using_custom_temp_root:
        register_temp_dir_for_cleanup(tmp_path)
    else:
        register_temp_dir_for_cleanup(tmp_root)
    bundle_signatures = BUNDLE_SIGNATURES
    source_bundle_signature = _read_bundle_signature(assets_file, bundle_signatures)

    if not os.path.exists(tmp_root):
        os.makedirs(tmp_root, exist_ok=True)

    if os.path.exists(tmp_path):
        shutil.rmtree(tmp_path)
    os.makedirs(tmp_path, exist_ok=True)
    deferred_payload_dir = os.path.join(tmp_root, "deferred_patch_payloads")
    os.makedirs(deferred_payload_dir, exist_ok=True)

    phase_started_at = time.perf_counter()
    _emit_phase_callback(
        phase_callback,
        "load_begin",
        file=fn_without_path,
        path=assets_file,
    )
    env = UnityPy.load(assets_file)
    _emit_phase_callback(
        phase_callback,
        "load_end",
        file=fn_without_path,
        elapsed_sec=(time.perf_counter() - phase_started_at),
    )
    env_file = getattr(env, "file", None)
    if env_file is None:
        files = getattr(env, "files", None)
        if isinstance(files, dict) and len(files) == 1:
            env_file = next(iter(files.values()))
    if env_file is None:
        raise RuntimeError(
            "Could not determine primary UnityPy file object for saving."
        )
    if not preview_export:
        _ensure_custom_unitypy_streaming_save(lang=lang)
    if generator is None:
        compile_method = get_compile_method(data_path)
        generator = _create_generator(
            unity_version, game_path, data_path, compile_method, lang=lang
        )
    if generator is not None:
        env.typetree_generator = generator
    if replacement_lookup is None:
        replacement_lookup, _ = build_replacement_lookup(replacements)
    replacement_meta_lookup: dict[tuple[str, str, str, int], JsonDict] = {}
    preview_target_lookup: dict[tuple[str, str, int], JsonDict] = {}
    for info in replacements.values():
        if not isinstance(info, dict):
            continue
        type_raw = info.get("Type")
        file_raw = info.get("File")
        assets_raw = info.get("assets_name")
        path_raw = info.get("Path_ID")
        if (
            not isinstance(type_raw, str)
            or not isinstance(file_raw, str)
            or not isinstance(assets_raw, str)
        ):
            continue
        try:
            path_id = int(path_raw)
        except (TypeError, ValueError):
            continue
        if type_raw == "SDF":
            preview_target_lookup[(file_raw, assets_raw, path_id)] = info
        if not info.get("Replace_to"):
            continue
        replacement_meta_lookup[(type_raw, file_raw, assets_raw, path_id)] = info

    texture_object_lookup: dict[tuple[str, int], Any] = {}
    texture_swizzle_state_cache: dict[str, tuple[str | None, str | None]] = {}
    material_object_count_by_pathid: dict[int, int] = {}
    for item in env.objects:
        item_type = item.type.name
        if item_type == "Texture2D":
            texture_object_lookup[(item.assets_file.name, int(item.path_id))] = item
            continue
        if item_type == "Material":
            material_path_id = int(item.path_id)
            material_object_count_by_pathid[material_path_id] = (
                material_object_count_by_pathid.get(material_path_id, 0) + 1
            )

    target_sdf_targets: set[tuple[str, int]] = set()
    target_sdf_pathids: set[int] = set()
    target_sdf_font_by_target: dict[tuple[str, int], str] = {}
    old_line_metric_keys = _OLD_LINE_METRIC_KEYS
    old_line_metric_scale_keys = _OLD_LINE_METRIC_SCALE_KEYS
    new_line_metric_keys = _NEW_LINE_METRIC_KEYS
    new_line_metric_scale_keys = _NEW_LINE_METRIC_SCALE_KEYS
    material_padding_scale_keys = _MATERIAL_PADDING_SCALE_KEYS
    replacement_padding_limit_warned: set[tuple[str, str, int]] = set()

    if replace_sdf:
        for key, value in replacement_lookup.items():
            if len(key) == 4 and key[0] == "SDF" and key[1] == fn_without_path:
                assets_key = key[2]
                path_id = key[3]
                target_key = (str(assets_key), int(path_id))
                target_sdf_targets.add(target_key)
                target_sdf_pathids.add(path_id)
                target_sdf_font_by_target.setdefault(target_key, value)
        if preview_export:
            for file_name, assets_name, path_id in preview_target_lookup.keys():
                if file_name != fn_without_path:
                    continue
                target_key = (str(assets_name), int(path_id))
                target_sdf_targets.add(target_key)
                target_sdf_pathids.add(int(path_id))
    matched_sdf_targets = 0
    patched_sdf_targets = 0
    sdf_parse_failure_reasons: list[str] = []

    texture_patch_plans: dict[str, Any] = _copy_patch_bucket(
        deferred_texture_plans, current_file_key
    )
    material_replacements: dict[str, JsonDict] = cast(
        dict[str, JsonDict],
        _copy_patch_bucket(deferred_material_plans, current_file_key),
    )
    material_replacements_by_pathid: dict[int, JsonDict] = {}
    material_replacements_by_atlas: dict[str, JsonDict] = cast(
        dict[str, JsonDict],
        _copy_patch_bucket(deferred_material_atlas_plans, current_file_key),
    )
    ambiguous_material_fallback_warned: set[int] = set()
    modified = False

    for obj in env.objects:
        assets_name = obj.assets_file.name
        if obj.type.name == "Font" and replace_ttf:
            font_pathid = obj.path_id
            replacement_font = replacement_lookup.get(
                ("TTF", fn_without_path, assets_name, font_pathid)
            )

            if replacement_font:
                assets = load_font_assets(replacement_font)
                if assets["ttf_data"]:
                    font = _safe_parse_as_object(obj)
                    _raw_font_data = getattr(font, "m_FontData", b"")
                    current_ttf_data = _raw_font_data if isinstance(_raw_font_data, bytes) else bytes(_raw_font_data)
                    if current_ttf_data == assets["ttf_data"]:
                        _log_debug(
                            f"[replace_ttf] file={fn_without_path} assets={assets_name} path_id={font_pathid} "
                            f"name={font.m_Name} target={replacement_font} action=skip_same size={len(current_ttf_data)}"
                        )
                        if lang == "ko":
                            _log_console(
                                f"TTF 폰트 동일(건너뜀): {assets_name} | {font.m_Name} | "
                                f"(PathID: {font_pathid} == {replacement_font})"
                            )
                        else:
                            _log_console(
                                f"TTF already same (skip): {assets_name} | {font.m_Name} | "
                                f"(PathID: {font_pathid} == {replacement_font})"
                            )
                        continue
                    if lang == "ko":
                        _log_console(
                            f"TTF 폰트 교체: {assets_name} | {font.m_Name} | (PathID: {font_pathid} -> {replacement_font})"
                        )
                    else:
                        _log_console(
                            f"TTF font replaced: {assets_name} | {font.m_Name} | (PathID: {font_pathid} -> {replacement_font})"
                        )
                    _log_debug(
                        f"[replace_ttf] file={fn_without_path} assets={assets_name} path_id={font_pathid} "
                        f"name={font.m_Name} target={replacement_font} "
                        f"old_size={len(current_ttf_data)} new_size={len(assets['ttf_data'])}"
                    )
                    font.m_FontData = assets["ttf_data"]
                    _safe_save(obj, font)
                    modified = True

        if obj.type.name == "MonoBehaviour" and replace_sdf:
            pathid = obj.path_id
            target_key = (assets_name, int(pathid))
            if target_sdf_targets and target_key not in target_sdf_targets:
                continue
            try:
                parse_dict = _safe_parse_as_dict(obj)
            except Exception as e:
                reason = f"PathID {obj.path_id} parse_as_dict 실패 [{type(e).__name__}]: {e!r}"
                sdf_parse_failure_reasons.append(reason)
                _log_debug(
                    f"[replace_sdf] file={fn_without_path} assets={assets_name} path_id={obj.path_id} "
                    f"action=parse_as_dict_failed error={type(e).__name__}: {e!r}"
                )
                if lang == "ko":
                    _log_console(f"  경고: {reason}")
                    debug_parse_log(
                        f"[replace_fonts] MonoBehaviour parse_as_dict 실패: {fn_without_path} | {reason}"
                    )
                else:
                    _log_console(
                        f"  Warning: PathID {obj.path_id} parse_as_dict failed [{type(e).__name__}]: {e!r}"
                    )
                    debug_parse_log(
                        f"[replace_fonts] MonoBehaviour parse_as_dict failed: {fn_without_path} | {reason}"
                    )
                continue
            unity_version_hint_raw = getattr(obj.assets_file, "unity_version", None)
            unity_version_hint = str(unity_version_hint_raw or unity_version or "")
            tmp_info = inspect_tmp_font_schema(
                parse_dict,
                unity_version=unity_version_hint or None,
            )
            if not tmp_info.get("is_tmp"):
                continue
            glyph_count = int(tmp_info.get("glyph_count", 0) or 0)
            atlas_file_id = int(tmp_info.get("atlas_file_id", 0) or 0)
            atlas_path_id = int(tmp_info.get("atlas_path_id", 0) or 0)

            # KR: 외부 참조 stub만 제외하고 실제 TMP 폰트만 처리합니다.
            # EN: Excludes only external reference stubs and processes actual TMP fonts.
            if atlas_file_id != 0 and atlas_path_id == 0:
                continue
            if glyph_count == 0:
                continue

            objname = obj.peek_name()
            replacement_font = replacement_lookup.get(
                ("SDF", fn_without_path, assets_name, pathid)
            )
            if replacement_font is None:
                replacement_font = target_sdf_font_by_target.get(target_key)

            preview_target_meta = preview_target_lookup.get(
                (fn_without_path, assets_name, int(pathid))
            )
            if (
                replacement_font is None
                and preview_target_meta is not None
                and preview_export
            ):
                atlas_path_id_preview = int(tmp_info.get("atlas_path_id", 0) or 0)
                if atlas_path_id_preview:
                    target_swizzle_verdict: str | None = None
                    if ps5_swizzle:
                        target_swizzle_verdict, _ = _detect_target_texture_swizzle(
                            texture_object_lookup,
                            texture_swizzle_state_cache,
                            assets_name,
                            int(atlas_path_id_preview),
                        )
                    target_preview_image = _load_target_unswizzled_preview_image(
                        texture_object_lookup,
                        assets_name,
                        int(atlas_path_id_preview),
                        target_swizzle_verdict,
                        preview_rotate=PS5_SWIZZLE_ROTATE if ps5_swizzle else 0,
                    )
                    if isinstance(target_preview_image, Image.Image):
                        _save_swizzle_preview(
                            target_preview_image,
                            preview_enabled=preview_export,
                            preview_root=preview_root,
                            assets_file_name=fn_without_path,
                            assets_name=assets_name,
                            atlas_path_id=int(atlas_path_id_preview),
                            font_name=str(objname),
                            target_swizzled=bool(
                                target_swizzle_verdict == "likely_swizzled_input"
                            ),
                            lang=lang,
                        )
                        preview_sdf_data = normalize_sdf_data(parse_dict)
                        _save_glyph_crop_previews(
                            target_preview_image,
                            preview_enabled=preview_export,
                            preview_root=preview_root,
                            assets_file_name=fn_without_path,
                            assets_name=assets_name,
                            atlas_path_id=int(atlas_path_id_preview),
                            font_name=str(objname),
                            sdf_data=preview_sdf_data,
                            lang=lang,
                        )

            if replacement_font:
                replacement_meta = replacement_meta_lookup.get(
                    ("SDF", fn_without_path, assets_name, int(pathid)),
                    {},
                )
                replacement_process_swizzle = parse_bool_flag(
                    replacement_meta.get("process_swizzle")
                )
                replacement_swizzle_hint = parse_bool_flag(
                    replacement_meta.get("swizzle")
                )
                replacement_force_raster = parse_bool_flag(
                    replacement_meta.get("force_raster")
                )
                effective_force_raster = force_raster or replacement_force_raster
                _log_debug(
                    f"[replace_sdf] file={fn_without_path} assets={assets_name} path_id={pathid} "
                    f"font={objname} target={replacement_font} "
                    f"effective_force_raster={effective_force_raster} "
                    f"replacement_swizzle_hint={replacement_swizzle_hint} "
                    f"replacement_process_swizzle={replacement_process_swizzle}"
                )
                matched_sdf_targets += 1
                source_padding_hint = extract_tmp_atlas_padding(
                    parse_dict,
                    unity_version=unity_version_hint or None,
                )
                selected_padding_variant = (
                    _select_builtin_bulk_padding_variant(
                        replacement_font,
                        source_padding_hint,
                    )
                    if prefer_builtin_padding_variants
                    else None
                )
                assets = load_font_assets(
                    replacement_font,
                    prefer_raster=effective_force_raster,
                    padding_variant=selected_padding_variant,
                )
                if assets["sdf_data"] and assets["sdf_atlas"]:
                    if lang == "ko":
                        _log_console(
                            f"SDF 폰트 교체: {assets_name} | {objname} | (PathID: {pathid}) -> {replacement_font}"
                        )
                    else:
                        _log_console(
                            f"SDF font replaced: {assets_name} | {objname} | (PathID: {pathid}) -> {replacement_font}"
                        )
                    if selected_padding_variant is not None:
                        if lang == "ko":
                            _log_console(
                                f"  가장 가까운 내장 padding preset 선택: source {source_padding_hint:.2f} -> Padding_{selected_padding_variant}"
                            )
                        else:
                            _log_console(
                                f"  Selected nearest built-in padding preset: source {source_padding_hint:.2f} -> Padding_{selected_padding_variant}"
                            )
                    source_atlas = assets["sdf_atlas"]
                    source_swizzled = parse_bool_flag(assets.get("sdf_swizzle"))
                    asset_process_swizzle = parse_bool_flag(
                        assets.get("sdf_process_swizzle")
                    )
                    atlas_linear_for_alpha8 = source_atlas
                    if ps5_swizzle and source_swizzled:
                        try:
                            atlas_linear_for_alpha8 = apply_ps5_unswizzle_to_image(
                                source_atlas,
                                allow_axis_swap=True,
                                roughness_guard=True,
                            )
                        except Exception:
                            atlas_linear_for_alpha8 = source_atlas
                    target_swizzle_verdict: str | None = None
                    target_swizzle_source: str | None = None
                    target_is_swizzled: bool | None = None

                    # KR: 입력 JSON이 신형/구형이어도 내부 교체는 신형 TMP 스키마로 통일합니다.
                    # EN: Regardless of whether input JSON uses new or old format, internal replacement is unified to the new TMP schema.
                    replace_data = assets.get("sdf_data_normalized")
                    if not isinstance(replace_data, dict):
                        replace_data = normalize_sdf_data(assets["sdf_data"])
                    try:
                        replacement_render_mode = int(
                            replace_data.get("m_AtlasRenderMode", 4118) or 0
                        )
                    except Exception:
                        replacement_render_mode = 4118
                    if effective_force_raster:
                        replacement_render_mode &= ~0x1000
                    replacement_is_sdf = (replacement_render_mode & 0x1000) != 0
                    game_padding_for_material = 0.0

                    # KR: GameObject/Script/Material/Atlas 참조는 기존 PathID를 유지해야 런타임 연결이 깨지지 않습니다.
                    # EN: GameObject/Script/Material/Atlas references must keep existing PathIDs to avoid breaking runtime linkage.
                    m_GameObject_FileID = parse_dict["m_GameObject"]["m_FileID"]
                    m_GameObject_PathID = parse_dict["m_GameObject"]["m_PathID"]
                    m_Script_FileID = parse_dict["m_Script"]["m_FileID"]
                    m_Script_PathID = parse_dict["m_Script"]["m_PathID"]
                    has_source_font_ref = isinstance(
                        parse_dict.get("m_SourceFontFile"), dict
                    )
                    if has_source_font_ref:
                        m_SourceFontFile_FileID = int(
                            parse_dict["m_SourceFontFile"].get("m_FileID", 0) or 0
                        )
                        m_SourceFontFile_PathID = int(
                            parse_dict["m_SourceFontFile"].get("m_PathID", 0) or 0
                        )
                    else:
                        m_SourceFontFile_FileID = 0
                        m_SourceFontFile_PathID = 0

                    if parse_dict.get("m_Material") is not None:
                        m_Material_FileID = parse_dict["m_Material"]["m_FileID"]
                        m_Material_PathID = parse_dict["m_Material"]["m_PathID"]
                    else:
                        m_Material_FileID = parse_dict["material"]["m_FileID"]
                        m_Material_PathID = parse_dict["material"]["m_PathID"]

                    target_new_atlas_ref = _first_valid_atlas_ref(
                        parse_dict.get("m_AtlasTextures")
                    ) or _first_atlas_ref(parse_dict.get("m_AtlasTextures"))
                    target_old_atlas_ref = (
                        cast(JsonDict, parse_dict.get("atlas"))
                        if isinstance(parse_dict.get("atlas"), dict)
                        else None
                    )
                    target_has_new_face = isinstance(parse_dict.get("m_FaceInfo"), dict)
                    target_has_new_glyphs = isinstance(
                        parse_dict.get("m_GlyphTable"), list
                    )
                    target_has_new_chars = isinstance(
                        parse_dict.get("m_CharacterTable"), list
                    )
                    target_has_old_face = isinstance(parse_dict.get("m_fontInfo"), dict)
                    target_has_old_glyphs = isinstance(
                        parse_dict.get("m_glyphInfoList"), list
                    )
                    target_creation_settings_key = _resolve_creation_settings_key(
                        parse_dict,
                        unity_version=unity_version_hint or None,
                    )
                    target_creation_settings = (
                        cast(JsonDict, parse_dict.get(target_creation_settings_key))
                        if target_creation_settings_key
                        and isinstance(
                            parse_dict.get(target_creation_settings_key), dict
                        )
                        else None
                    )

                    if target_new_atlas_ref is not None:
                        m_AtlasTextures_FileID, m_AtlasTextures_PathID = _atlas_ref_ids(
                            target_new_atlas_ref
                        )
                    elif target_old_atlas_ref is not None:
                        m_AtlasTextures_FileID, m_AtlasTextures_PathID = _atlas_ref_ids(
                            target_old_atlas_ref
                        )
                    else:
                        m_AtlasTextures_FileID = int(atlas_file_id)
                        m_AtlasTextures_PathID = int(atlas_path_id)

                    if target_has_new_face:
                        game_face_info = parse_dict.get("m_FaceInfo", {})
                        try:
                            game_padding_for_material = float(
                                parse_dict.get(
                                    "m_AtlasPadding",
                                    (
                                        target_creation_settings.get("padding", 0)
                                        if isinstance(target_creation_settings, dict)
                                        else 0
                                    ),
                                )
                            )
                        except Exception:
                            game_padding_for_material = 0.0

                        target_face_info = dict(replace_data["m_FaceInfo"])
                        if isinstance(game_face_info, dict):
                            if use_game_line_metrics:
                                metric_scale = 1.0
                            else:
                                metric_scale = _safe_metric_scale(
                                    game_face_info.get("m_PointSize", 0),
                                    target_face_info.get("m_PointSize", 0),
                                )
                            for metric_key in new_line_metric_keys:
                                if metric_key in game_face_info:
                                    metric_value = game_face_info[metric_key]
                                    if (
                                        metric_key in new_line_metric_scale_keys
                                        and metric_scale != 1.0
                                    ):
                                        try:
                                            metric_value = (
                                                float(metric_value) * metric_scale
                                            )
                                        except Exception:
                                            pass
                                    target_face_info[metric_key] = metric_value
                        ensure_int(
                            target_face_info,
                            ["m_PointSize", "m_AtlasWidth", "m_AtlasHeight"],
                        )
                        parse_dict["m_FaceInfo"] = target_face_info

                    replacement_glyph_table = (
                        replace_data.get("m_GlyphTable", [])
                        if isinstance(replace_data.get("m_GlyphTable", []), list)
                        else []
                    )
                    replacement_character_table = (
                        replace_data.get("m_CharacterTable", [])
                        if isinstance(replace_data.get("m_CharacterTable", []), list)
                        else []
                    )

                    if target_has_new_glyphs:
                        parse_dict["m_GlyphTable"] = replacement_glyph_table
                    if target_has_new_chars:
                        parse_dict["m_CharacterTable"] = replacement_character_table

                    if replacement_glyph_table:
                        replacement_glyph_indexes = [
                            int(g.get("m_Index", 0) or 0)
                            for g in replacement_glyph_table
                            if isinstance(g, dict)
                        ]
                        for glyph_index_key in _TMP_GLYPH_INDEX_LIST_KEYS:
                            if glyph_index_key in parse_dict:
                                parse_dict[glyph_index_key] = list(
                                    replacement_glyph_indexes
                                )

                    if "m_AtlasWidth" in parse_dict:
                        parse_dict["m_AtlasWidth"] = int(
                            replace_data.get(
                                "m_AtlasWidth", parse_dict.get("m_AtlasWidth", 0)
                            )
                            or 0
                        )
                    if "m_AtlasHeight" in parse_dict:
                        parse_dict["m_AtlasHeight"] = int(
                            replace_data.get(
                                "m_AtlasHeight", parse_dict.get("m_AtlasHeight", 0)
                            )
                            or 0
                        )
                    if "m_AtlasPadding" in parse_dict:
                        parse_dict["m_AtlasPadding"] = int(
                            replace_data.get(
                                "m_AtlasPadding", parse_dict.get("m_AtlasPadding", 0)
                            )
                            or 0
                        )
                    if "m_AtlasRenderMode" in parse_dict:
                        parse_dict["m_AtlasRenderMode"] = replacement_render_mode
                    if "m_UsedGlyphRects" in parse_dict:
                        parse_dict["m_UsedGlyphRects"] = replace_data.get(
                            "m_UsedGlyphRects", parse_dict.get("m_UsedGlyphRects", [])
                        )
                    if "m_FreeGlyphRects" in parse_dict:
                        parse_dict["m_FreeGlyphRects"] = replace_data.get(
                            "m_FreeGlyphRects", parse_dict.get("m_FreeGlyphRects", [])
                        )
                    if "m_FontWeightTable" in parse_dict:
                        parse_dict["m_FontWeightTable"] = replace_data.get(
                            "m_FontWeightTable", parse_dict.get("m_FontWeightTable", [])
                        )

                    if target_has_old_face or target_has_old_glyphs:
                        game_font_info = parse_dict.get("m_fontInfo", {})
                        if game_padding_for_material <= 0:
                            try:
                                game_padding_for_material = float(
                                    game_font_info.get(
                                        "Padding",
                                        (
                                            target_creation_settings.get("padding", 0)
                                            if isinstance(
                                                target_creation_settings, dict
                                            )
                                            else 0
                                        ),
                                    )
                                )
                            except Exception:
                                game_padding_for_material = 0.0

                        old_font_info = convert_face_info_new_to_old(
                            replace_data["m_FaceInfo"],
                            replace_data.get("m_AtlasPadding", 0),
                            replace_data.get("m_AtlasWidth", 0),
                            replace_data.get("m_AtlasHeight", 0),
                        )
                        if isinstance(game_font_info, dict):
                            if use_game_line_metrics:
                                metric_scale = 1.0
                            else:
                                metric_scale = _safe_metric_scale(
                                    game_font_info.get("PointSize", 0),
                                    old_font_info.get("PointSize", 0),
                                )
                            for metric_key in old_line_metric_keys:
                                if metric_key in game_font_info:
                                    metric_value = game_font_info[metric_key]
                                    if (
                                        metric_key in old_line_metric_scale_keys
                                        and metric_scale != 1.0
                                    ):
                                        try:
                                            metric_value = (
                                                float(metric_value) * metric_scale
                                            )
                                        except Exception:
                                            pass
                                    old_font_info[metric_key] = metric_value

                        replacement_atlas = assets.get("sdf_atlas")
                        atlas_height = int(
                            replace_data.get(
                                "m_AtlasHeight",
                                (
                                    replacement_atlas.height
                                    if replacement_atlas is not None
                                    else 0
                                ),
                            )
                        )
                        old_glyph_list = convert_glyphs_new_to_old(
                            replacement_glyph_table,
                            replacement_character_table,
                            atlas_height=atlas_height,
                        )
                        old_font_info["CharacterCount"] = len(old_glyph_list)
                        if target_has_old_face:
                            parse_dict["m_fontInfo"] = old_font_info
                        if target_has_old_glyphs:
                            parse_dict["m_glyphInfoList"] = old_glyph_list

                    if isinstance(target_creation_settings, dict):
                        atlas_width_for_cs = int(
                            parse_dict.get(
                                "m_AtlasWidth", replace_data.get("m_AtlasWidth", 0)
                            )
                            or 0
                        )
                        atlas_height_for_cs = int(
                            parse_dict.get(
                                "m_AtlasHeight", replace_data.get("m_AtlasHeight", 0)
                            )
                            or 0
                        )
                        padding_for_cs = int(
                            parse_dict.get(
                                "m_AtlasPadding", replace_data.get("m_AtlasPadding", 0)
                            )
                            or 0
                        )
                        if target_has_old_face and not use_game_line_metrics:
                            try:
                                padding_for_cs = int(
                                    parse_dict.get("m_fontInfo", {}).get(
                                        "Padding", padding_for_cs
                                    )
                                    or padding_for_cs
                                )
                            except Exception:
                                pass

                        point_size_for_cs = int(
                            replace_data.get("m_FaceInfo", {}).get("m_PointSize", 0)
                            or 0
                        )
                        if target_has_new_face:
                            point_size_for_cs = int(
                                parse_dict.get("m_FaceInfo", {}).get(
                                    "m_PointSize", point_size_for_cs
                                )
                                or point_size_for_cs
                            )
                        elif target_has_old_face:
                            point_size_for_cs = int(
                                parse_dict.get("m_fontInfo", {}).get(
                                    "PointSize", point_size_for_cs
                                )
                                or point_size_for_cs
                            )

                        _sync_creation_settings_payload(
                            target_creation_settings,
                            atlas_width=atlas_width_for_cs,
                            atlas_height=atlas_height_for_cs,
                            padding=padding_for_cs,
                            point_size=point_size_for_cs,
                        )

                    # KR: 신형/구형 필드가 공존하면 신형 face 기준으로 legacy face도 동기화합니다.
                    # EN: When both new and old fields coexist, synchronize the legacy face based on the new face info.
                    if target_has_new_face and target_has_old_face:
                        parse_dict["m_fontInfo"] = convert_face_info_new_to_old(
                            parse_dict["m_FaceInfo"],
                            int(
                                parse_dict.get(
                                    "m_AtlasPadding",
                                    replace_data.get("m_AtlasPadding", 0),
                                )
                                or 0
                            ),
                            int(
                                parse_dict.get(
                                    "m_AtlasWidth", replace_data.get("m_AtlasWidth", 0)
                                )
                                or 0
                            ),
                            int(
                                parse_dict.get(
                                    "m_AtlasHeight",
                                    replace_data.get("m_AtlasHeight", 0),
                                )
                                or 0
                            ),
                        )

                    for dirty_key in _TMP_DIRTY_FLAG_KEYS:
                        if dirty_key in parse_dict:
                            parse_dict[dirty_key] = True

                    # KR: 포맷 분기 후 공통 참조를 원래 값으로 되돌립니다.
                    # EN: After format branching, restore common references to their original values.
                    parse_dict["m_GameObject"]["m_FileID"] = m_GameObject_FileID
                    parse_dict["m_GameObject"]["m_PathID"] = m_GameObject_PathID
                    parse_dict["m_Script"]["m_FileID"] = m_Script_FileID
                    parse_dict["m_Script"]["m_PathID"] = m_Script_PathID

                    if parse_dict.get("m_Material") is not None:
                        parse_dict["m_Material"]["m_FileID"] = m_Material_FileID
                        parse_dict["m_Material"]["m_PathID"] = m_Material_PathID
                    else:
                        parse_dict["material"]["m_FileID"] = m_Material_FileID
                        parse_dict["material"]["m_PathID"] = m_Material_PathID

                    if has_source_font_ref and isinstance(
                        parse_dict.get("m_SourceFontFile"), dict
                    ):
                        parse_dict["m_SourceFontFile"][
                            "m_FileID"
                        ] = m_SourceFontFile_FileID
                        parse_dict["m_SourceFontFile"][
                            "m_PathID"
                        ] = m_SourceFontFile_PathID

                    current_new_atlas_ref = _first_valid_atlas_ref(
                        parse_dict.get("m_AtlasTextures")
                    ) or _first_atlas_ref(parse_dict.get("m_AtlasTextures"))
                    if current_new_atlas_ref is not None:
                        current_new_atlas_ref["m_FileID"] = m_AtlasTextures_FileID
                        current_new_atlas_ref["m_PathID"] = m_AtlasTextures_PathID
                    if isinstance(parse_dict.get("atlas"), dict):
                        parse_dict["atlas"]["m_FileID"] = m_AtlasTextures_FileID
                        parse_dict["atlas"]["m_PathID"] = m_AtlasTextures_PathID

                    atlas_metadata_width = int(source_atlas.width)
                    atlas_metadata_height = int(source_atlas.height)
                    texture_target_assets_name = _resolve_target_assets_name(
                        obj.assets_file,
                        assets_name,
                        int(m_AtlasTextures_FileID),
                    )
                    texture_target_file_key = _resolve_target_outer_file_key(
                        current_file_key,
                        obj.assets_file,
                        int(m_AtlasTextures_FileID),
                        texture_target_assets_name,
                        source_bundle_signature=source_bundle_signature,
                        asset_file_index=asset_file_index,
                    )
                    texture_key = ""
                    if (
                        int(m_AtlasTextures_PathID) != 0
                        and texture_target_assets_name
                        and texture_target_file_key
                    ):
                        texture_key = _make_assets_object_key(
                            texture_target_assets_name,
                            int(m_AtlasTextures_PathID),
                        )
                        texture_plan: JsonDict = {
                            "replacement_font": replacement_font,
                            "source_entry": f"{fn_without_path}|{assets_name}|{pathid}",
                            "font_name": str(objname),
                            "source_atlas": source_atlas,
                            "source_swizzled": bool(source_swizzled),
                            "replacement_swizzle_hint": bool(
                                replacement_swizzle_hint
                            ),
                            "replacement_process_swizzle": bool(
                                replacement_process_swizzle
                            ),
                            "asset_process_swizzle": bool(asset_process_swizzle),
                            "alpha8_linear_source": atlas_linear_for_alpha8,
                            "metadata_width": atlas_metadata_width,
                            "metadata_height": atlas_metadata_height,
                            "preview_sdf_data": replace_data,
                        }
                        if texture_target_file_key == current_file_key:
                            _store_patch_value(
                                texture_patch_plans,
                                texture_key,
                                texture_plan,
                            )
                        else:
                            texture_plan = _spill_deferred_texture_plan_to_disk(
                                texture_plan,
                                deferred_payload_dir,
                            )
                            _register_deferred_patch(
                                deferred_texture_plans,
                                texture_target_file_key,
                                texture_key,
                                texture_plan,
                                pending_files=pending_external_patch_files,
                                patch_kind="texture",
                            )
                    elif int(m_AtlasTextures_PathID) != 0:
                        _log_warning(
                            f"[replace_sdf] file={fn_without_path} assets={assets_name} "
                            f"path_id={pathid} atlas_ref={m_AtlasTextures_FileID}:{m_AtlasTextures_PathID} "
                            "could_not_resolve_texture_target=True"
                        )

                    atlas_fallback_payload: JsonDict = {
                        "w": atlas_metadata_width,
                        "h": atlas_metadata_height,
                        "gs": None,
                        "float_overrides": {},
                        "color_overrides": {},
                        "outline_ratio": outline_ratio,
                        "reset_keywords": False,
                        "prune_raster_material": False,
                        "preserve_gradient_floor": False,
                        "replacement_font": replacement_font,
                        "source_entry": f"{fn_without_path}|{assets_name}|{pathid}",
                    }
                    if texture_key and texture_target_file_key:
                        if texture_target_file_key == current_file_key:
                            _store_patch_value(
                                material_replacements_by_atlas,
                                texture_key,
                                atlas_fallback_payload,
                            )
                        else:
                            _register_deferred_patch(
                                deferred_material_atlas_plans,
                                texture_target_file_key,
                                texture_key,
                                atlas_fallback_payload,
                                pending_files=pending_external_patch_files,
                                patch_kind="material_atlas",
                            )
                    if m_Material_PathID != 0:
                        gradient_scale = None
                        apply_replacement_material = not use_game_mat
                        float_overrides: dict[str, float] = {}
                        color_overrides: dict[str, JsonDict] = {}
                        reset_keywords = False
                        prune_raster_material = False
                        preserve_gradient_floor = False
                        preserve_game_style = False
                        material_padding_ratio = 1.0
                        material_data = assets.get("sdf_materials")
                        if effective_force_raster and use_game_mat:
                            if lang == "ko":
                                _log_console(
                                    "  경고: Raster 폰트에 --use-game-material 사용 시 박스 아티팩트가 생길 수 있습니다."
                                )
                            else:
                                _log_console(
                                    "  Warning: using --use-game-material with Raster fonts may cause box artifacts."
                                )
                        try:
                            replacement_padding = float(
                                replace_data.get("m_AtlasPadding", 0)
                            )
                        except Exception:
                            replacement_padding = 0.0
                        if (
                            replacement_is_sdf
                            and game_padding_for_material > 0
                            and replacement_padding > 0
                            and game_padding_for_material > replacement_padding
                        ):
                            warn_key = (
                                str(assets_name),
                                str(objname),
                                int(pathid),
                            )
                            if warn_key not in replacement_padding_limit_warned:
                                replacement_padding_limit_warned.add(warn_key)
                                if lang == "ko":
                                    _log_console(
                                        "  경고: 원본 padding "
                                        f"{game_padding_for_material:.2f}가 교체 padding {replacement_padding:.2f}보다 큽니다. "
                                        "Material 보정을 적용하지만 외곽선/언더레이를 원본과 완전히 같게 복원하지 못할 수 있습니다."
                                    )
                                else:
                                    _log_console(
                                        "  Warning: source padding "
                                        f"{game_padding_for_material:.2f} exceeds replacement padding {replacement_padding:.2f}. "
                                        "Material correction is applied, but outline/underlay may not match the original exactly."
                                    )
                        if (
                            replacement_is_sdf
                            and material_scale_by_padding
                            and game_padding_for_material > 0
                            and replacement_padding > 0
                        ):
                            material_padding_ratio = (
                                game_padding_for_material / replacement_padding
                            )
                            if material_padding_ratio <= 0:
                                material_padding_ratio = 1.0
                        if material_data and apply_replacement_material:
                            preserve_game_style = (
                                replacement_is_sdf and (not effective_force_raster)
                            )
                            material_props = material_data.get("m_SavedProperties", {})
                            float_properties = material_props.get("m_Floats", [])
                            color_properties = material_props.get("m_Colors", [])
                            for prop in float_properties:
                                if not isinstance(prop, (list, tuple)) or len(prop) < 2:
                                    continue
                                key = str(prop[0])
                                if preserve_game_style and key in _MATERIAL_STYLE_FLOAT_KEYS:
                                    continue
                                try:
                                    value = float(prop[1])
                                except (TypeError, ValueError):
                                    continue
                                float_overrides[key] = value
                            for prop in color_properties:
                                if not isinstance(prop, (list, tuple)) or len(prop) < 2:
                                    continue
                                key = str(prop[0])
                                if preserve_game_style and key in _MATERIAL_STYLE_COLOR_KEYS:
                                    continue
                                color_value = _color_value_to_dict(
                                    prop[1],
                                    {"r": 0.0, "g": 0.0, "b": 0.0, "a": 0.0},
                                )
                                color_overrides[key] = color_value
                            if material_padding_ratio != 1.0:
                                for key in material_padding_scale_keys:
                                    if key in float_overrides:
                                        float_overrides[key] = float(
                                            float_overrides[key]
                                            * material_padding_ratio
                                        )
                            gradient_scale = float_overrides.get("_GradientScale")
                        # KR: 교체 material에 _GradientScale이 없으면 m_AtlasPadding+1로 자동 추론합니다.
                        # EN: If _GradientScale is missing from replacement material, auto-infer it as m_AtlasPadding+1.
                        if gradient_scale is None and replacement_is_sdf and replacement_padding > 0:
                            gradient_scale = float(replacement_padding + 1)
                        if apply_replacement_material and effective_force_raster:
                            # KR: Raster 모드에서는 SDF 계열 필드 0 덮기 대신 최소 필드만 남깁니다.
                            # EN: In Raster mode, keep only minimal fields instead of zeroing out SDF-related fields.
                            reset_keywords = True
                            prune_raster_material = True
                            gradient_scale = 1.0
                            if lang == "ko":
                                _log_console(
                                    "  Raster 모드 감지: Material 필드를 최소 구성으로 재구성합니다."
                                )
                            else:
                                _log_console(
                                    "  Raster mode detected: rebuilding Material to minimal raster-safe fields."
                                )
                        if (
                            apply_replacement_material
                            and replacement_is_sdf
                            and (not effective_force_raster)
                        ):
                            preserve_gradient_floor = True
                        if (
                            material_scale_by_padding
                            and apply_replacement_material
                            and material_padding_ratio != 1.0
                        ):
                            if lang == "ko":
                                _log_console(
                                    f"  Material padding 비율 보정 적용: {game_padding_for_material:.2f}/{replacement_padding:.2f} "
                                    f"(x{material_padding_ratio:.3f})"
                                )
                            else:
                                _log_console(
                                    f"  Applied material padding ratio: {game_padding_for_material:.2f}/{replacement_padding:.2f} "
                                    f"(x{material_padding_ratio:.3f})"
                                )
                        material_target_assets_name = _resolve_target_assets_name(
                            obj.assets_file,
                            assets_name,
                            int(m_Material_FileID),
                        )
                        material_target_file_key = _resolve_target_outer_file_key(
                            current_file_key,
                            obj.assets_file,
                            int(m_Material_FileID),
                            material_target_assets_name,
                            source_bundle_signature=source_bundle_signature,
                            asset_file_index=asset_file_index,
                        )
                        material_payload = {
                            "w": atlas_metadata_width,
                            "h": atlas_metadata_height,
                            "gs": gradient_scale,
                            "float_overrides": float_overrides,
                            "color_overrides": color_overrides,
                            "outline_ratio": outline_ratio,
                            "reset_keywords": reset_keywords,
                            "prune_raster_material": bool(prune_raster_material),
                            "preserve_game_style": bool(preserve_game_style),
                            "style_padding_scale_ratio": material_padding_ratio,
                            "preserve_gradient_floor": bool(
                                preserve_gradient_floor
                            ),
                            "replacement_padding": replacement_padding,
                            "replacement_font": replacement_font,
                            "source_entry": f"{fn_without_path}|{assets_name}|{pathid}",
                        }
                        if material_target_assets_name and material_target_file_key:
                            material_key_exact = _make_assets_object_key(
                                material_target_assets_name,
                                int(m_Material_PathID),
                            )
                            if material_target_file_key == current_file_key:
                                _store_patch_value(
                                    material_replacements,
                                    material_key_exact,
                                    material_payload,
                                )
                            else:
                                _register_deferred_patch(
                                    deferred_material_plans,
                                    material_target_file_key,
                                    material_key_exact,
                                    material_payload,
                                    pending_files=pending_external_patch_files,
                                    patch_kind="material",
                                )
                        elif material_target_file_key == current_file_key:
                            material_replacements_by_pathid[int(m_Material_PathID)] = (
                                material_payload
                            )
                            _log_warning(
                                f"[replace_sdf] file={fn_without_path} assets={assets_name} path_id={pathid} "
                                f"material_ref={m_Material_FileID}:{m_Material_PathID} "
                                "could_not_resolve_material_assets_name=True; fallback_to_pathid_only=True"
                            )
                        else:
                            _log_warning(
                                f"[replace_sdf] file={fn_without_path} assets={assets_name} path_id={pathid} "
                                f"material_ref={m_Material_FileID}:{m_Material_PathID} "
                                "could_not_resolve_material_target=True"
                            )
                    obj.patch(parse_dict)
                    trailing = _trailing_bytes_store.pop(id(obj), b"")
                    if trailing:
                        current_data = obj.get_raw_data()
                        obj.set_raw_data(current_data + trailing)
                    patched_sdf_targets += 1
                    modified = True
                else:
                    missing_parts: list[str] = []
                    if assets.get("sdf_data") is None:
                        missing_parts.append("json")
                    if assets.get("sdf_atlas") is None:
                        missing_parts.append("atlas")
                    if lang == "ko":
                        _log_console(
                            f"  경고: 교체 리소스 누락으로 SDF 적용 건너뜀: {replacement_font} "
                            f"(누락: {', '.join(missing_parts) if missing_parts else 'unknown'})"
                        )
                    else:
                        _log_console(
                            f"  Warning: skipping SDF patch due to missing replacement assets: {replacement_font} "
                            f"(missing: {', '.join(missing_parts) if missing_parts else 'unknown'})"
                        )

    phase_started_at = time.perf_counter()
    _emit_phase_callback(
        phase_callback,
        "patch_begin",
        file=fn_without_path,
        object_count=(
            len(getattr(env_file, "objects", {}))
            if hasattr(env_file, "objects")
            else None
        ),
    )
    for obj in env.objects:
        assets_name = obj.assets_file.name
        if obj.type.name == "Texture2D":
            replacement_key = _make_assets_object_key(assets_name, int(obj.path_id))
            texture_plan = _lookup_patch_value(texture_patch_plans, replacement_key)
            if isinstance(texture_plan, dict):
                parse_dict = _safe_parse_as_object(obj)
                if lang == "ko":
                    _log_console(
                        f"텍스처 교체: {obj.peek_name()} (PathID: {obj.path_id})"
                    )
                else:
                    _log_console(
                        f"Texture replaced: {obj.peek_name()} (PathID: {obj.path_id})"
                    )
                prepared_texture = _prepare_texture_replacement_for_target(
                    texture_plan,
                    assets_file_name=fn_without_path,
                    target_assets_name=assets_name,
                    target_path_id=int(obj.path_id),
                    texture_object_lookup=texture_object_lookup,
                    texture_swizzle_state_cache=texture_swizzle_state_cache,
                    ps5_swizzle=ps5_swizzle,
                    preview_export=preview_export,
                    preview_root=preview_root,
                    lang=lang,
                )
                if not isinstance(prepared_texture, dict):
                    continue
                replacement_image = prepared_texture.get("replacement_image")
                target_swizzled_state = prepared_texture.get(
                    "target_swizzled_state"
                )
                replacement_linear_source = prepared_texture.get(
                    "replacement_linear_source"
                )
                metadata_size = prepared_texture.get("metadata_size", (0, 0))
                if (
                    not isinstance(metadata_size, tuple)
                    or len(metadata_size) != 2
                ):
                    metadata_size = (0, 0)
                metadata_w, metadata_h = cast(tuple[int, int], metadata_size)
                applied_raw_alpha8 = False
                try:
                    texture_format = int(
                        getattr(parse_dict, "m_TextureFormat", -1) or -1
                    )
                except Exception:
                    texture_format = -1
                _log_debug(
                    f"[replace_texture] file={fn_without_path} assets={assets_name} path_id={obj.path_id} "
                    f"name={obj.peek_name()} texture_format={texture_format} metadata={metadata_w}x{metadata_h}"
                )
                if (
                    texture_format == 1
                    and isinstance(replacement_image, Image.Image)
                ):
                    try:
                        alpha_source = (
                            replacement_linear_source
                            if isinstance(replacement_linear_source, Image.Image)
                            else replacement_image
                        )
                        # KR: Alpha8은 반드시 bpe=1 경로로 인코딩해야 합니다.
                        #     RGBA 기준 swizzle 후 알파만 추출하면 바이트 순서가 깨질 수 있습니다.
                        # EN: Alpha8 must be encoded via the bpe=1 path.
                        #     Extracting only the alpha channel after RGBA-based swizzle can corrupt byte order.
                        alpha_raw, aw, ah, alpha_mode = _encode_alpha8_replacement_bytes(
                            alpha_source,
                            ps5_swizzle=ps5_swizzle,
                            target_swizzled_state=target_swizzled_state,
                        )
                        parse_dict.m_Width = int(metadata_w if metadata_w > 0 else aw)
                        parse_dict.m_Height = int(metadata_h if metadata_h > 0 else ah)
                        if hasattr(parse_dict, "m_CompleteImageSize"):
                            parse_dict.m_CompleteImageSize = int(len(alpha_raw))
                        parse_dict.image_data = alpha_raw
                        stream_data = getattr(parse_dict, "m_StreamData", None)
                        if stream_data is not None:
                            try:
                                stream_data.offset = 0
                                stream_data.size = 0
                                stream_data.path = ""
                            except Exception:
                                pass
                        applied_raw_alpha8 = True
                        _log_debug(
                            f"[replace_texture] file={fn_without_path} assets={assets_name} path_id={obj.path_id} "
                            f"action=alpha8_raw_injection target_swizzled={target_swizzled_state} "
                            f"mode={alpha_mode} raw_size={len(alpha_raw)} width={aw} height={ah}"
                        )
                        if lang == "ko":
                            if alpha_mode == "swizzled":
                                _log_console(
                                    "  Alpha8 raw 주입 적용: swizzled 바이트를 image_data에 직접 기록합니다."
                                )
                            elif alpha_mode == "linear_flipped":
                                _log_console(
                                    "  Alpha8 raw 주입 적용: linear 바이트(상하 반전 보정)를 image_data에 직접 기록합니다."
                                )
                            else:
                                _log_console(
                                    "  Alpha8 raw 주입 적용: 판정 불명(inconclusive) 상태로 image_data에 직접 기록합니다."
                                )
                        else:
                            if alpha_mode == "swizzled":
                                _log_console(
                                    "  Applied Alpha8 raw injection: writing swizzled bytes directly to image_data."
                                )
                            elif alpha_mode == "linear_flipped":
                                _log_console(
                                    "  Applied Alpha8 raw injection: writing linear bytes (with vertical-flip compensation) to image_data."
                                )
                            else:
                                _log_console(
                                    "  Applied Alpha8 raw injection: writing bytes directly to image_data (target state inconclusive)."
                                )
                    except Exception as raw_inject_error:
                        if lang == "ko":
                            _log_console(
                                f"  경고: Alpha8 raw 주입 실패, 일반 image 저장으로 폴백합니다. ({raw_inject_error})"
                            )
                        else:
                            _log_console(
                                f"  Warning: Alpha8 raw injection failed; falling back to image save. ({raw_inject_error})"
                            )
                if not applied_raw_alpha8:
                    parse_dict.image = replacement_image

                # KR: TypeTree 재직렬화 시 원본보다 작아지는 Texture2D (중국판 Unity 등)는
                # KR: 바이너리 패치를 사용하여 extra bytes를 보존합니다.
                # EN: For Texture2D that becomes smaller than original on TypeTree re-serialization (e.g. China Unity),
                # EN: use binary patching to preserve extra bytes.
                if _has_trailing_bytes(obj) or _detect_typetree_size_mismatch(obj):
                    tex_w = int(getattr(parse_dict, "m_Width", 0) or 0)
                    tex_h = int(getattr(parse_dict, "m_Height", 0) or 0)
                    tex_image_data = getattr(parse_dict, "image_data", b"")
                    if not isinstance(tex_image_data, (bytes, bytearray)):
                        tex_image_data = bytes(tex_image_data)
                    if tex_image_data and tex_w > 0 and tex_h > 0:
                        if _binary_patch_texture2d(
                            obj,
                            image_data=tex_image_data,
                            width=tex_w,
                            height=tex_h,
                            lang=lang,
                        ):
                            if lang == "ko":
                                _log_console(
                                    "  바이너리 패치 적용 (TypeTree 외 extra bytes 보존)"
                                )
                            else:
                                _log_console(
                                    "  Applied binary patch (preserving extra bytes outside TypeTree)"
                                )
                        else:
                            _safe_save(obj, parse_dict)
                    else:
                        _safe_save(obj, parse_dict)
                else:
                    _safe_save(obj, parse_dict)
                modified = True
                parse_dict = None
        if obj.type.name == "Material":
            parse_dict = None
            material_key = _make_assets_object_key(assets_name, int(obj.path_id))
            mat_info = _lookup_patch_value(material_replacements, material_key)
            if mat_info is None:
                fallback_path_id = int(obj.path_id)
                if fallback_path_id in material_replacements_by_pathid:
                    if material_object_count_by_pathid.get(fallback_path_id, 0) == 1:
                        mat_info = material_replacements_by_pathid[fallback_path_id]
                    elif fallback_path_id not in ambiguous_material_fallback_warned:
                        ambiguous_material_fallback_warned.add(fallback_path_id)
                        _log_warning(
                            f"[replace_material] file={fn_without_path} path_id={fallback_path_id} "
                            "fallback_pathid_only_match_ambiguous=True; skipped"
                        )
            if mat_info is None:
                if parse_dict is None:
                    parse_dict = _safe_parse_as_object(obj)
                saved_props = getattr(parse_dict, "m_SavedProperties", None)
                tex_envs = getattr(saved_props, "m_TexEnvs", None)
                main_tex_path_id = 0
                if isinstance(tex_envs, list):
                    for entry in tex_envs:
                        if (
                            isinstance(entry, (list, tuple))
                            and len(entry) >= 2
                            and str(entry[0]) == "_MainTex"
                        ):
                            tex_env_val = entry[1]
                            tex_ref = (
                                tex_env_val.get("m_Texture")
                                if isinstance(tex_env_val, dict)
                                else getattr(tex_env_val, "m_Texture", None)
                            )
                            if isinstance(tex_ref, dict):
                                main_tex_path_id = int(tex_ref.get("m_PathID", 0) or 0)
                            else:
                                main_tex_path_id = int(
                                    getattr(tex_ref, "m_PathID", 0) or 0
                                )
                            break
                if main_tex_path_id > 0:
                    atlas_key = _make_assets_object_key(assets_name, main_tex_path_id)
                    mat_info = _lookup_patch_value(
                        material_replacements_by_atlas,
                        atlas_key,
                    )
            if mat_info is not None:
                if parse_dict is None:
                    parse_dict = _safe_parse_as_object(obj)
                if _apply_material_replacement_to_object(parse_dict, mat_info):
                    _safe_save(obj, parse_dict)

    _emit_phase_callback(
        phase_callback,
        "patch_end",
        file=fn_without_path,
        elapsed_sec=(time.perf_counter() - phase_started_at),
        modified=bool(modified),
    )

    if modified:
        if lang == "ko":
            _log_console(f"'{fn_without_path}' 저장 중...")
        else:
            _log_console(f"Saving '{fn_without_path}'...")

        save_success = False
        last_save_failure_reason: str | None = None

        def _save_env_file(
            packer: Any = None,
            save_path: str | None = None,
            use_save_to: bool = False,
        ) -> bytes | int:
            """KR: 지정 packer로 기본 파일 객체의 save/save_to를 호출합니다.
            save_path가 주어지면 save_to()로 파일에 직접 기록하여 메모리를 절약합니다.
            반환값은 bytes(legacy) 또는 저장된 파일 크기(int)입니다.

            EN: Invokes save/save_to on the base file object with the given packer.
            If save_path is given, writes directly to file via save_to() to save memory.
            Returns bytes (legacy) or the saved file size (int).
            """
            # KR: use_save_to=True 이고 save_to()가 존재하면 파일에 직접 저장합니다.
            # EN: If use_save_to=True and save_to() exists, save directly to file.
            save_to_fn = getattr(env_file, "save_to", None)
            if use_save_to and save_path and callable(save_to_fn):
                try:
                    supports_packer = (
                        "packer" in inspect.signature(save_to_fn).parameters
                    )
                except (TypeError, ValueError):
                    supports_packer = False
                if packer is None or not supports_packer:
                    return save_to_fn(save_path)
                return save_to_fn(save_path, packer=packer)

            # KR: 기존 bytes 반환 방식 폴백
            # EN: Fallback to legacy bytes-returning approach
            save_fn = getattr(env_file, "save", None)
            if not callable(save_fn):
                raise AttributeError(
                    "UnityPy environment file object has no callable save()."
                )
            typed_save = cast(Callable[..., bytes], save_fn)
            # KR: save() 시그니처를 기준으로 packer 지원 여부를 판별해 내부 TypeError를 가리지 않도록 합니다.
            # EN: Check packer support based on save() signature to avoid masking internal TypeErrors.
            try:
                supports_packer = "packer" in inspect.signature(typed_save).parameters
            except (TypeError, ValueError):
                supports_packer = False

            if packer is None or not supports_packer:
                return typed_save()
            return typed_save(packer=packer)

        def _validate_saved_file(saved_path: str) -> tuple[bool, str | None]:
            """KR: 저장 결과 파일이 Unity bundle로 다시 열리는지 검증합니다.
            EN: Validates that the saved file can be re-opened as a Unity bundle.
            """
            signature = source_bundle_signature or getattr(env_file, "signature", None)
            if signature not in bundle_signatures:
                return True, None
            saved_signature = _read_bundle_signature(saved_path, bundle_signatures)
            if saved_signature != signature:
                reason = (
                    f"번들 시그니처 불일치 (기대: {signature}, 결과: {saved_signature or 'None'})"
                    if lang == "ko"
                    else f"bundle signature mismatch (expected: {signature}, got: {saved_signature or 'None'})"
                )
                if lang == "ko":
                    _log_console(f"  저장 검증 실패: {reason}")
                else:
                    _log_console(f"  Save validation failed: {reason}")
                return False, reason
            try:
                _emit_phase_callback(
                    phase_callback,
                    "validate_begin",
                    file=fn_without_path,
                    path=saved_path,
                )
                validation_started_at = time.perf_counter()
                validation_inner_names = _collect_validation_inner_names(env_file)
                if getattr(sys, "frozen", False):
                    cmd = [sys.executable, "--_validate-bundle", saved_path]
                else:
                    cmd = [
                        sys.executable,
                        os.path.abspath(__file__),
                        "--_validate-bundle",
                        saved_path,
                    ]
                for inner_name in validation_inner_names:
                    cmd.extend(["--_validate-inner-name", inner_name])
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=1800,
                )
                if proc.returncode == 0:
                    _emit_phase_callback(
                        phase_callback,
                        "validate_end",
                        file=fn_without_path,
                        path=saved_path,
                        elapsed_sec=(time.perf_counter() - validation_started_at),
                        ok=True,
                    )
                    return True, None
                detail = (proc.stderr or proc.stdout or "").strip()
                reason = (
                    f"worker exit={proc.returncode}: {detail}"
                    if detail
                    else f"worker exit={proc.returncode}"
                )
                if lang == "ko":
                    _log_console(f"  저장 검증 실패 [{reason}]")
                else:
                    _log_console(f"  Save validation failed [{reason}]")
                _emit_phase_callback(
                    phase_callback,
                    "validate_end",
                    file=fn_without_path,
                    path=saved_path,
                    elapsed_sec=(time.perf_counter() - validation_started_at),
                    ok=False,
                    reason=reason,
                )
                return False, reason
            except Exception as e:
                reason = (
                    f"검증 워커 실행 실패: {e!r}"
                    if lang == "ko"
                    else f"failed to run validation worker: {e!r}"
                )
                if lang == "ko":
                    _log_console(f"  저장 검증 워커 실행 실패: {e!r}")
                else:
                    _log_console(f"  Failed to run save validation worker: {e!r}")
                _emit_phase_callback(
                    phase_callback,
                    "validate_end",
                    file=fn_without_path,
                    path=saved_path,
                    ok=False,
                    reason=reason,
                )
                return False, reason

        def _try_save(packer_label: Any, log_label: str) -> bool:
            """KR: 단일 저장 전략을 시도하고 성공 여부를 반환합니다.
            EN: Attempts a single save strategy and returns whether it succeeded.
            """
            nonlocal save_success, last_save_failure_reason
            tmp_file = os.path.join(tmp_path, fn_without_path)
            has_save_to = callable(getattr(env_file, "save_to", None))
            saved_blob: bytes | None = None
            try:
                _emit_phase_callback(
                    phase_callback,
                    "save_begin",
                    file=fn_without_path,
                    packer=packer_label,
                    method=log_label,
                )
                save_started_at = time.perf_counter()
                use_stream_fallback = False
                if has_save_to and source_bundle_signature in bundle_signatures:
                    # KR: 번들은 기본적으로 save_to()를 우선 사용해 최종 bytes blob 생성을 피합니다.
                    #     save_to() 실패 시에만 legacy save()로 폴백합니다.
                    # EN: Bundles prefer save_to() by default to avoid creating a final bytes blob.
                    #     Fall back to legacy save() only when save_to() fails.
                    try:
                        _save_env_file(
                            packer_label, save_path=tmp_file, use_save_to=True
                        )
                    except Exception as primary_save_error:
                        use_stream_fallback = True
                        if lang == "ko":
                            _log_console(
                                "  save_to() 저장 실패로 legacy save()로 폴백합니다... "
                                f"({type(primary_save_error).__name__}: {primary_save_error})"
                            )
                        else:
                            _log_console(
                                "  save_to() failed; falling back to legacy save()... "
                                f"({type(primary_save_error).__name__}: {primary_save_error})"
                            )

                    if use_stream_fallback:
                        saved_blob = _save_env_file(packer_label, use_save_to=False)
                        with open(tmp_file, "wb") as f:
                            f.write(cast(bytes, saved_blob))
                        saved_blob = None
                elif has_save_to:
                    # KR: save_to()로 파일에 직접 저장 — bytes 중간 변수 없음 (메모리 절약)
                    # EN: Save directly to file via save_to() — no intermediate bytes variable (saves memory)
                    _save_env_file(packer_label, save_path=tmp_file, use_save_to=True)
                else:
                    # KR: 기존 bytes 반환 방식 폴백
                    # EN: Fallback to legacy bytes-returning approach
                    saved_blob = _save_env_file(packer_label, use_save_to=False)
                    with open(tmp_file, "wb") as f:
                        f.write(cast(bytes, saved_blob))
                    # KR: 검증 전에 큰 메모리 블록을 해제하여 피크 메모리 사용량을 낮춥니다.
                    # EN: Free large memory blocks before validation to reduce peak memory usage.
                    saved_blob = None
                gc.collect()
                is_valid, validation_reason = _validate_saved_file(tmp_file)
                if not is_valid:
                    try:
                        saved_size = os.path.getsize(tmp_file)
                    except Exception:
                        saved_size = 0
                    if saved_size > 0:
                        if lang == "ko":
                            _log_console(
                                "  경고: 저장 검증에 실패했지만 무검증 저장으로 계속 진행합니다."
                            )
                            if validation_reason:
                                _log_console(f"  검증 실패 원인: {validation_reason}")
                        else:
                            _log_console(
                                "  Warning: save validation failed, continuing with unvalidated save."
                            )
                            if validation_reason:
                                _log_console(
                                    f"  Validation failure reason: {validation_reason}"
                                )
                        save_success = True
                        _emit_phase_callback(
                            phase_callback,
                            "save_end",
                            file=fn_without_path,
                            packer=packer_label,
                            method=log_label,
                            elapsed_sec=(time.perf_counter() - save_started_at),
                            ok=True,
                            validated=False,
                        )
                        return True
                    last_save_failure_reason = (
                        validation_reason or "validation failed (empty output file)"
                    )
                    try:
                        if os.path.exists(tmp_file):
                            os.remove(tmp_file)
                    except Exception:
                        pass
                    return False
                save_success = True
                _emit_phase_callback(
                    phase_callback,
                    "save_end",
                    file=fn_without_path,
                    packer=packer_label,
                    method=log_label,
                    elapsed_sec=(time.perf_counter() - save_started_at),
                    ok=True,
                    validated=True,
                )
                return True
            except Exception as e:
                last_save_failure_reason = (
                    f"method {log_label} [{type(e).__name__}]: {e!r}"
                )
                if lang == "ko":
                    _log_console(
                        f"  저장 방법 {log_label} 실패 [{type(e).__name__}]: {e!r}"
                    )
                else:
                    _log_console(
                        f"  Save method {log_label} failed [{type(e).__name__}]: {e!r}"
                    )
                if debug_parse_enabled():
                    tb_module.print_exc()
                try:
                    if os.path.exists(tmp_file):
                        os.remove(tmp_file)
                except Exception:
                    pass
                _emit_phase_callback(
                    phase_callback,
                    "save_end",
                    file=fn_without_path,
                    packer=packer_label,
                    method=log_label,
                    ok=False,
                    reason=last_save_failure_reason,
                )
                return False
            finally:
                saved_blob = None
                gc.collect()

        dataflags = getattr(env_file, "dataflags", None)
        safe_none_packer = (int(dataflags), 0) if dataflags is not None else "none"
        legacy_none_packer = (
            ((int(dataflags) & ~0x3F), 0) if dataflags is not None else None
        )

        if prefer_original_compress:
            # KR: 옵션이 있으면 원본 압축 우선으로 저장합니다.
            # EN: If the option is set, save with original compression first.
            if not _try_save("original", "1"):
                if lang == "ko":
                    _log_console("  lz4 압축 모드로 재시도...")
                else:
                    _log_console("  Retrying with lz4 packer...")
                if not _try_save("lz4", "2"):
                    if lang == "ko":
                        _log_console("  비압축 계열 모드로 재시도...")
                    else:
                        _log_console("  Retrying with uncompressed-style packer...")
                    if (
                        not _try_save(safe_none_packer, "3")
                        and legacy_none_packer is not None
                    ):
                        if lang == "ko":
                            _log_console("  레거시 비트마스크 모드로 재시도...")
                        else:
                            _log_console("  Retrying with legacy bitmask packer...")
                        _try_save(legacy_none_packer, "4")
        else:
            # KR: 기본은 무압축 계열 우선으로 저장해 시간을 줄이고, 실패 시 압축 모드로 폴백합니다.
            # EN: By default, save with uncompressed-family first to reduce time; fall back to compressed mode on failure.
            if not _try_save(safe_none_packer, "1"):
                if legacy_none_packer is not None:
                    if lang == "ko":
                        _log_console("  레거시 비트마스크 무압축 모드로 재시도...")
                    else:
                        _log_console(
                            "  Retrying with legacy bitmask uncompressed packer..."
                        )
                    if _try_save(legacy_none_packer, "2"):
                        pass
                    else:
                        if lang == "ko":
                            _log_console("  원본 압축 모드로 재시도...")
                        else:
                            _log_console("  Retrying with original compression...")
                        if not _try_save("original", "3"):
                            if lang == "ko":
                                _log_console("  lz4 압축 모드로 재시도...")
                            else:
                                _log_console("  Retrying with lz4 packer...")
                            _try_save("lz4", "4")
                else:
                    if lang == "ko":
                        _log_console("  원본 압축 모드로 재시도...")
                    else:
                        _log_console("  Retrying with original compression...")
                    if not _try_save("original", "2"):
                        if lang == "ko":
                            _log_console("  lz4 압축 모드로 재시도...")
                        else:
                            _log_console("  Retrying with lz4 packer...")
                        _try_save("lz4", "3")

        close_unitypy_env(env)
        gc.collect()

        if save_success:
            saved_file_path = os.path.join(tmp_path, fn_without_path)
            if os.path.exists(saved_file_path):
                saved_size = os.path.getsize(saved_file_path)
                shutil.move(saved_file_path, assets_file)
                _log_debug(
                    f"[save] file={fn_without_path} output={assets_file} temp={saved_file_path} bytes={saved_size}"
                )
                if lang == "ko":
                    _log_console(f"  저장 완료 (크기: {saved_size} bytes)")
                else:
                    _log_console(f"  Save complete (size: {saved_size} bytes)")
            else:
                _log_debug(
                    f"[save] file={fn_without_path} output={assets_file} temp={saved_file_path} missing_after_save=True"
                )
                if lang == "ko":
                    _log_console("  경고: 저장된 파일을 찾을 수 없습니다")
                else:
                    _log_console("  Warning: saved file was not found")
                last_save_failure_reason = "saved file was not found after save phase"
                save_success = False

        if not save_success:
            _log_debug(
                f"[save] file={fn_without_path} output={assets_file} failed=True reason={last_save_failure_reason}"
            )
            if lang == "ko":
                _log_console("  오류: 파일 저장에 실패했습니다.")
                if last_save_failure_reason:
                    _log_console(f"  실패 원인: {last_save_failure_reason}")
            else:
                _log_console("  Error: failed to save file.")
                if last_save_failure_reason:
                    _log_console(f"  Failure reason: {last_save_failure_reason}")
    elif replace_sdf and target_sdf_targets and not preview_export:
        if lang == "ko":
            _log_console(
                f"  경고: SDF 대상 {len(target_sdf_targets)}건 중 매칭 {matched_sdf_targets}건, 적용 {patched_sdf_targets}건"
            )
            if sdf_parse_failure_reasons:
                _log_console(f"  파싱 오류: {sdf_parse_failure_reasons[-1]}")
        else:
            _log_console(
                f"  Warning: SDF targets={len(target_sdf_targets)}, matched={matched_sdf_targets}, patched={patched_sdf_targets}"
            )
            if sdf_parse_failure_reasons:
                _log_console(f"  Parse error: {sdf_parse_failure_reasons[-1]}")

    if os.path.exists(tmp_path):
        shutil.rmtree(tmp_path)
    if not using_custom_temp_root and os.path.isdir(tmp_root):
        try:
            os.rmdir(tmp_root)
        except OSError:
            pass

    return save_success if modified else False


def create_batch_replacements(
    game_path: str,
    font_name: str,
    replace_ttf: bool = True,
    replace_sdf: bool = True,
    target_files: set[str] | None = None,
    exclude_exts: set[str] | None = None,
    scan_jobs: int = 1,
    lang: Language = "ko",
    ps5_swizzle: bool = False,
) -> dict[str, JsonDict]:
    """KR: 게임 내 모든 폰트를 지정 폰트로 치환하는 배치 매핑을 생성합니다.
    target_files가 있으면 해당 파일만 대상으로 매핑을 생성합니다.
    exclude_exts가 있으면 해당 확장자는 스캔에서 제외합니다.

    EN: Creates a batch mapping to replace all fonts in the game with the specified font.
    If target_files is provided, only those files are included in the mapping.
    If exclude_exts is provided, those extensions are excluded from scanning.
    """
    fonts = scan_fonts(
        game_path,
        lang=lang,
        target_files=target_files,
        exclude_exts=exclude_exts,
        scan_jobs=scan_jobs,
        ps5_swizzle=ps5_swizzle,
    )
    replacements: dict[str, JsonDict] = {}

    if replace_ttf:
        for font in fonts["ttf"]:
            key = f"{font['file']}|TTF|{font['path_id']}"
            replacements[key] = {
                "Name": font["name"],
                "assets_name": font["assets_name"],
                "Path_ID": font["path_id"],
                "Type": "TTF",
                "File": font["file"],
                "Replace_to": font_name,
            }

    if replace_sdf:
        for font in fonts["sdf"]:
            key = f"{font['file']}|SDF|{font['path_id']}"
            if ps5_swizzle:
                swizzle_flag = (
                    "True" if parse_bool_flag(font.get("swizzle")) else "False"
                )
                process_swizzle_flag = (
                    "True" if parse_bool_flag(font.get("process_swizzle")) else "False"
                )
                entry: JsonDict = {
                    "File": font["file"],
                    "assets_name": font["assets_name"],
                    "Path_ID": font["path_id"],
                    "Type": "SDF",
                    "Name": font["name"],
                    "force_raster": "False",
                    "swizzle": swizzle_flag,
                    "process_swizzle": process_swizzle_flag,
                    "Replace_to": font_name,
                }
            else:
                entry = {
                    "File": font["file"],
                    "assets_name": font["assets_name"],
                    "Path_ID": font["path_id"],
                    "Type": "SDF",
                    "Name": font["name"],
                    "force_raster": "False",
                    "Replace_to": font_name,
                }
            replacements[key] = entry

    return replacements


def create_preview_export_targets(
    game_path: str,
    target_files: set[str] | None = None,
    exclude_exts: set[str] | None = None,
    scan_jobs: int = 1,
    lang: Language = "ko",
    ps5_swizzle: bool = False,
) -> dict[str, JsonDict]:
    """KR: preview-export 전용 SDF 대상 매핑(Replace_to 비어 있음)을 생성합니다.
    scan_jobs/target_files/exclude_exts 조건을 그대로 반영합니다.

    EN: Creates an SDF target mapping for preview-export (Replace_to left empty).
    Honors scan_jobs/target_files/exclude_exts conditions as-is.
    """
    fonts = scan_fonts(
        game_path,
        lang=lang,
        target_files=target_files,
        exclude_exts=exclude_exts,
        scan_jobs=scan_jobs,
        ps5_swizzle=ps5_swizzle,
    )
    targets: dict[str, JsonDict] = {}
    for font in fonts["sdf"]:
        key = f"{font['file']}|PREVIEW|{font['path_id']}"
        entry: JsonDict = {
            "File": font["file"],
            "assets_name": font["assets_name"],
            "Path_ID": font["path_id"],
            "Type": "SDF",
            "Name": font["name"],
            "force_raster": "False",
            "Replace_to": "",
        }
        if ps5_swizzle:
            entry["swizzle"] = (
                "True" if parse_bool_flag(font.get("swizzle")) else "False"
            )
            entry["process_swizzle"] = (
                "True" if parse_bool_flag(font.get("process_swizzle")) else "False"
            )
        targets[key] = entry
    return targets


def exit_with_error(
    message: str,
    lang: Language = "ko",
    *,
    pause: bool | None = None,
) -> NoReturn:
    """KR: 로컬라이즈된 오류 메시지를 출력하고 종료합니다.
    EN: Prints a localized error message and exits.
    """
    if lang == "ko":
        _log_console(f"오류: {message}")
    else:
        _log_console(f"Error: {message}")
    if pause is None:
        pause = _should_pause_before_exit(interactive_session=False)
    if pause:
        _pause_before_exit(lang=lang, interactive_session=False)
    sys.exit(1)


def exit_with_error_en(message: str) -> NoReturn:
    """KR: 영문 오류 메시지를 출력하고 종료합니다.
    EN: Prints an English error message and exits.
    """
    exit_with_error(message, lang="en")


_STRUCTURAL_VALIDATE_MAX_METADATA_BYTES = 64 * 1024 * 1024


def _align_value(value: int, alignment: int = 16) -> int:
    return int(value) + ((alignment - (int(value) % alignment)) % alignment)


def _read_c_string(handle: Any, limit: int = 4096) -> str:
    chunks = bytearray()
    while len(chunks) < limit:
        byte = handle.read(1)
        if not byte:
            raise ValueError("Unexpected EOF while reading C string")
        if byte == b"\0":
            return chunks.decode("utf-8", "surrogateescape")
        chunks.extend(byte)
    raise ValueError("C string exceeds limit")


def _parse_version_triplet(version_text: str) -> tuple[int, int, int]:
    match = re.match(r"^\s*(\d+)\.(\d+)\.(\d+)", version_text or "")
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _read_unityfs_structure(bundle_path: str) -> tuple[JsonDict | None, str | None]:
    try:
        with open(bundle_path, "rb") as handle:
            signature = _read_c_string(handle, limit=64)
            if signature != "UnityFS":
                return None, f"unsupported signature: {signature}"

            version = struct_module.unpack(">I", handle.read(4))[0]
            version_player = _read_c_string(handle)
            version_engine = _read_c_string(handle)
            total_file_size = struct_module.unpack(">Q", handle.read(8))[0]
            compressed_size, uncompressed_size, data_flags = struct_module.unpack(
                ">III", handle.read(12)
            )

            version_triplet = _parse_version_triplet(version_engine)
            uses_block_alignment = bool(
                version >= 7
                or (
                    version_triplet[0] == 2019
                    and version_triplet >= (2019, 4, 15)
                )
            )
            if uses_block_alignment:
                handle.seek(_align_value(handle.tell(), 16), os.SEEK_SET)

            blocks_info_start = handle.tell()
            file_length = os.path.getsize(bundle_path)
            if data_flags & 0x80:
                handle.seek(file_length - compressed_size, os.SEEK_SET)
                compressed_blocks_info = handle.read(compressed_size)
                data_start = blocks_info_start
            else:
                compressed_blocks_info = handle.read(compressed_size)
                data_start = blocks_info_start + compressed_size

            compression_flag = CompressionFlags(data_flags & 0x3F)
            blocks_info_bytes = cast(
                bytes,
                CompressionHelper.DECOMPRESSION_MAP[compression_flag](
                    compressed_blocks_info,
                    uncompressed_size,
                ),
            )
            if data_flags & 0x200:
                data_start = _align_value(data_start, 16)

            blocks_reader = UnityPy.streams.EndianBinaryReader(blocks_info_bytes)
            blocks_reader.read_bytes(16)
            block_count = blocks_reader.read_int()
            blocks: list[JsonDict] = []
            compressed_offset = 0
            uncompressed_offset = 0
            for _ in range(block_count):
                block_uncompressed = int(blocks_reader.read_u_int())
                block_compressed = int(blocks_reader.read_u_int())
                block_flags = int(blocks_reader.read_u_short())
                blocks.append(
                    {
                        "compressed_offset": compressed_offset,
                        "compressed_size": block_compressed,
                        "uncompressed_offset": uncompressed_offset,
                        "uncompressed_size": block_uncompressed,
                        "flags": block_flags,
                    }
                )
                compressed_offset += block_compressed
                uncompressed_offset += block_uncompressed

            directory_count = blocks_reader.read_int()
            directory_infos: list[JsonDict] = []
            for _ in range(directory_count):
                directory_infos.append(
                    {
                        "offset": int(blocks_reader.read_long()),
                        "size": int(blocks_reader.read_long()),
                        "flags": int(blocks_reader.read_u_int()),
                        "path": blocks_reader.read_string_to_null(),
                    }
                )

            return (
                {
                    "signature": signature,
                    "version": version,
                    "version_player": version_player,
                    "version_engine": version_engine,
                    "total_file_size": int(total_file_size),
                    "data_flags": int(data_flags),
                    "data_start": int(data_start),
                    "blocks": blocks,
                    "directory_infos": directory_infos,
                    "bundle_data_size": int(uncompressed_offset),
                },
                None,
            )
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _read_unityfs_range(
    bundle_path: str,
    structure: JsonDict,
    offset: int,
    size: int,
) -> bytes:
    blocks = cast(list[JsonDict], structure.get("blocks") or [])
    data_start = int(structure.get("data_start") or 0)
    range_start = int(offset)
    range_end = range_start + int(size)
    out = bytearray()
    with open(bundle_path, "rb") as handle:
        for block in blocks:
            block_start = int(block["uncompressed_offset"])
            block_end = block_start + int(block["uncompressed_size"])
            if block_end <= range_start or block_start >= range_end:
                continue
            compression_flag = CompressionFlags(int(block["flags"]) & 0x3F)
            local_start = max(range_start, block_start) - block_start
            local_end = min(range_end, block_end) - block_start
            slice_len = local_end - local_start
            if slice_len <= 0:
                continue
            if compression_flag == CompressionFlags.NONE:
                handle.seek(
                    data_start + int(block["compressed_offset"]) + local_start,
                    os.SEEK_SET,
                )
                out.extend(handle.read(slice_len))
            else:
                handle.seek(data_start + int(block["compressed_offset"]), os.SEEK_SET)
                compressed_payload = handle.read(int(block["compressed_size"]))
                decompressed_payload = cast(
                    bytes,
                    CompressionHelper.DECOMPRESSION_MAP[compression_flag](
                        compressed_payload,
                        int(block["uncompressed_size"]),
                    ),
                )
                out.extend(decompressed_payload[local_start:local_end])
            if len(out) >= size:
                break
    return bytes(out[:size])


def _parse_serialized_header_info(
    header_bytes: bytes,
    entry_size: int,
) -> tuple[JsonDict | None, str | None]:
    if len(header_bytes) < 20:
        return None, "serialized header too small"
    metadata_size, file_size, version, data_offset = struct_module.unpack(
        ">4I", header_bytes[:16]
    )
    if version < 9:
        return None, f"unsupported serialized header version: {version}"
    endian = ">" if header_bytes[16] else "<"
    header_size = 20
    if version >= 22:
        if len(header_bytes) < 48:
            return None, "v22 serialized header too small"
        metadata_size = struct_module.unpack(">I", header_bytes[20:24])[0]
        file_size = struct_module.unpack(">Q", header_bytes[24:32])[0]
        data_offset = struct_module.unpack(">Q", header_bytes[32:40])[0]
        header_size = 48
    if metadata_size <= 0 or file_size <= 0 or data_offset <= 0:
        return None, "serialized header has non-positive critical fields"
    if file_size > entry_size:
        return None, f"serialized file_size {file_size} exceeds entry size {entry_size}"
    if data_offset > entry_size:
        return None, f"serialized data_offset {data_offset} exceeds entry size {entry_size}"
    return (
        {
            "metadata_size": int(metadata_size),
            "file_size": int(file_size),
            "version": int(version),
            "data_offset": int(data_offset),
            "endian": endian,
            "header_size": header_size,
        },
        None,
    )


def _is_probable_serialized_entry(header_bytes: bytes, entry_size: int) -> bool:
    info, _ = _parse_serialized_header_info(header_bytes, entry_size)
    return info is not None


def _parse_serialized_metadata_summary(
    metadata_bytes: bytes,
    entry_size: int,
) -> tuple[JsonDict | None, str | None]:
    header_info, reason = _parse_serialized_header_info(metadata_bytes[:48], entry_size)
    if header_info is None:
        return None, reason

    version = int(header_info["version"])
    endian = cast(str, header_info["endian"])
    data_offset = int(header_info["data_offset"])
    if len(metadata_bytes) < data_offset:
        return None, f"metadata bytes truncated: need {data_offset}, got {len(metadata_bytes)}"

    reader = UnityPy.streams.EndianBinaryReader(metadata_bytes, endian=endian)
    reader.Position = int(header_info["header_size"])

    unity_version = ""
    if version >= 7:
        unity_version = reader.read_string_to_null()
    if version >= 8:
        reader.read_int()
    enable_type_tree = True
    if version >= 13:
        enable_type_tree = bool(reader.read_boolean())

    type_count = int(reader.read_int())
    if type_count < 0 or type_count > 100000:
        return None, f"invalid type_count: {type_count}"

    dummy_file = SimpleNamespace(
        header=SimpleNamespace(version=version),
        _enable_type_tree=enable_type_tree,
    )
    for _ in range(type_count):
        SerializedType(reader, dummy_file, False)

    if 7 <= version < 14:
        reader.read_int()

    object_count = int(reader.read_int())
    if object_count <= 0:
        return None, f"invalid object_count: {object_count}"

    return (
        {
            "version": version,
            "unity_version": unity_version,
            "type_count": type_count,
            "object_count": object_count,
            "data_offset": data_offset,
        },
        None,
    )


def _structural_validate_unityfs_bundle(
    bundle_path: str,
    *,
    inner_names: list[str] | None = None,
) -> tuple[bool, str | None]:
    structure, reason = _read_unityfs_structure(bundle_path)
    if structure is None:
        return False, reason

    directory_infos = cast(list[JsonDict], structure.get("directory_infos") or [])
    if not directory_infos:
        return False, "bundle has no directory infos"

    bundle_data_size = int(structure.get("bundle_data_size") or 0)
    selected_paths = set(str(name) for name in (inner_names or []) if str(name).strip())
    selected_entries = [
        entry
        for entry in directory_infos
        if not selected_paths or str(entry.get("path")) in selected_paths
    ]
    if selected_paths and not selected_entries:
        return False, f"validation targets not found: {sorted(selected_paths)}"

    validated_serialized = 0
    for entry in selected_entries:
        entry_name = str(entry.get("path") or "")
        entry_offset = int(entry.get("offset") or 0)
        entry_size = int(entry.get("size") or 0)
        if entry_size <= 0:
            return False, f"entry has invalid size: {entry_name}"
        if entry_offset < 0 or entry_offset + entry_size > bundle_data_size:
            return False, f"entry range exceeds bundle data: {entry_name}"

        header_sample = _read_unityfs_range(
            bundle_path,
            structure,
            entry_offset,
            min(entry_size, 64),
        )
        if not _is_probable_serialized_entry(header_sample, entry_size):
            continue

        header_info, header_reason = _parse_serialized_header_info(header_sample, entry_size)
        if header_info is None:
            return False, f"{entry_name}: {header_reason}"

        metadata_span = int(header_info["data_offset"])
        if metadata_span > _STRUCTURAL_VALIDATE_MAX_METADATA_BYTES:
            return False, f"{entry_name}: metadata span too large ({metadata_span} bytes)"

        metadata_bytes = _read_unityfs_range(
            bundle_path,
            structure,
            entry_offset,
            metadata_span,
        )
        metadata_summary, metadata_reason = _parse_serialized_metadata_summary(
            metadata_bytes,
            entry_size,
        )
        if metadata_summary is None:
            return False, f"{entry_name}: {metadata_reason}"

        validated_serialized += 1

    if validated_serialized <= 0 and not selected_paths:
        return False, "no serialized entries validated"

    return True, None


def _collect_validation_inner_names(env_file: Any) -> list[str]:
    files = getattr(env_file, "files", None)
    if not isinstance(files, dict):
        return []
    names = [
        str(name)
        for name, value in files.items()
        if getattr(value, "is_changed", False)
    ]
    return sorted(set(names))


def run_validation_worker(
    bundle_path: str,
    lang: Language = "ko",
    inner_names: list[str] | None = None,
) -> int:
    """KR: 저장 검증 전용 워커입니다. 가능한 경우 경량 structural 검증을 수행합니다.
    EN: Dedicated save-validation worker. Performs lightweight structural validation when possible.
    """
    try:
        if not os.path.exists(bundle_path):
            if lang == "ko":
                _log_console("[validate] 검증 실패: 저장 파일이 존재하지 않습니다.")
            else:
                _log_console("[validate] Validation failed: saved file does not exist.")
            return 2

        signature = _read_bundle_signature(bundle_path, BUNDLE_SIGNATURES)
        if signature == "UnityFS":
            ok, reason = _structural_validate_unityfs_bundle(
                bundle_path,
                inner_names=inner_names,
            )
            if ok:
                return 0
            if lang == "ko":
                _log_console(f"[validate] structural 검증 실패: {reason}")
            else:
                _log_console(f"[validate] Structural validation failed: {reason}")
            return 2

        env = UnityPy.load(bundle_path)
        files = getattr(env, "files", None)
        if not isinstance(files, dict) or len(files) == 0:
            if lang == "ko":
                _log_console(
                    "[validate] 검증 실패: UnityPy.load 결과에 파일이 없습니다."
                )
            else:
                _log_console(
                    "[validate] Validation failed: UnityPy.load returned no files."
                )
            return 2
        if not getattr(env, "objects", None):
            if lang == "ko":
                _log_console("[validate] 검증 실패: 로드된 오브젝트가 없습니다.")
            else:
                _log_console(
                    "[validate] Validation failed: loaded object list is empty."
                )
            return 2
        return 0
    except Exception as e:
        if lang == "ko":
            _log_console(f"[validate] 검증 실패: {e!r}")
        else:
            _log_console(f"[validate] Validation failed: {e!r}")
        if debug_parse_enabled():
            tb_module.print_exc()
        return 2


def run_scan_file_worker(
    game_path: str,
    assets_file: str,
    output_path: str,
    lang: Language = "ko",
    detect_ps5_swizzle: bool = False,
) -> int:
    """KR: 단일 파일 파싱 워커입니다. 결과를 JSON 파일로 저장합니다.
    EN: Single-file parsing worker. Saves results to a JSON file.
    """
    try:
        game_path, data_path = resolve_game_path(game_path, lang=lang)
        unity_version = get_unity_version(game_path, lang=lang)
        compile_method = get_compile_method(data_path)
        generator = _create_generator(
            unity_version, game_path, data_path, compile_method, lang=lang
        )
        scanned, load_error = _scan_fonts_in_asset_file(
            assets_file,
            generator,
            lang=lang,
            detect_ps5_swizzle=detect_ps5_swizzle,
        )
        payload: JsonDict = {
            "ttf": scanned.get("ttf", []),
            "sdf": scanned.get("sdf", []),
            "error": load_error,
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return 0
    except Exception as e:
        if lang == "ko":
            _log_console(f"[scan_worker] 실패: {e!r}")
        else:
            _log_console(f"[scan_worker] failed: {e!r}")
        if debug_parse_enabled():
            tb_module.print_exc()
        return 2


def main_cli(lang: Language = "ko") -> None:
    """KR: 언어별 공통 CLI 진입점입니다.
    EN: Common CLI entry point per language.
    """
    is_ko = lang == "ko"

    if is_ko:
        description = "Unity 게임의 폰트를 태국어 폰트로 교체합니다."
        epilog = """
예시:
  %(prog)s --gamepath "C:/path/to/game" --parse
  %(prog)s --gamepath "C:/path/to/game" --preview-export
  %(prog)s --gamepath "C:/path/to/game" --sarabun
  %(prog)s --gamepath "C:/path/to/game" --notosansthai --sdfonly
  %(prog)s --gamepath "C:/path/to/game" --mulmaru
  %(prog)s --gamepath "C:/path/to/game" --nanumgothic --sdfonly
  %(prog)s --gamepath "C:/path/to/game" --list font_map.json
        """
        gamepath_help = "게임의 루트 경로 (예: C:/path/to/game)"
        parse_help = "폰트 정보를 JSON으로 출력"
        mulmaru_help = "모든 폰트를 Mulmaru로 일괄 교체"
        nanum_help = "모든 폰트를 NanumGothic으로 일괄 교체"
        sarabun_help = "모든 폰트를 Sarabun(태국어)으로 일괄 교체"
        notosansthai_help = "모든 폰트를 Noto Sans Thai(태국어)로 일괄 교체"
        sdf_help = "SDF 폰트만 교체"
        ttf_help = "TTF 폰트만 교체"
        list_help = "JSON 파일을 읽어서 폰트 교체"
        target_file_help = "지정한 파일명만 교체 대상에 포함 (여러 번 사용 가능)"
        exclude_ext_help = (
            "스캔 제외 확장자 목록 (콤마 구분, 예: \"resS,.resource\")"
        )
        game_mat_help = "SDF 교체 시 게임 원본 Material 파라미터를 보정 없이 그대로 유지 (기본: 원본 스타일 유지 + atlas/padding 자동 보정)"
        force_raster_help = "SDF 교체 시 교체 폰트를 Raster 모드로 강제 (렌더 모드/Material 효과값 Raster 기준 적용)"
        game_line_metrics_help = "SDF 교체 시 게임 원본 줄 간격 메트릭 사용 (기본: 교체 폰트 메트릭 보정 적용)"
        outline_ratio_help = (
            "SDF 외곽선 비율 배율 (기본: 1.0, _OutlineWidth/_OutlineSoftness에 적용)"
        )
        original_compress_help = (
            "저장 시 원본 압축 모드를 우선 사용 (기본: 무압축 계열 우선)"
        )
        temp_dir_help = "임시 저장 폴더 루트 경로 (가능하면 빠른 SSD/NVMe 권장)"
        output_only_help = (
            "원본 파일은 유지하고, 수정된 파일만 지정 폴더에 원본 상대 경로로 저장"
        )
        preview_help = "모든 SDF 폰트 Atlas/Glyph crop 미리보기를 preview 폴더에 저장 (--ps5-swizzle와 함께면 unswizzle 기준)"
        scan_jobs_help = "폰트 스캔 병렬 워커 수 (기본: 1, parse/일괄교체 스캔에 적용, 별칭: --max-workers)"
        split_save_force_help = (
            "대형 SDF 다건 교체에서 one-shot을 건너뛰고 SDF 1개씩 강제 분할 저장"
        )
        oneshot_save_force_help = (
            "대형 SDF 다건 교체에서도 분할 저장 폴백 없이 one-shot 저장만 시도"
        )
        ps5_swizzle_help = "PS5 swizzle 자동 판별/변환 모드 (mask_x=0x385F0, mask_y=0x07A0F, rotate=90 보정)"
        verbose_help = "콘솔 로그는 유지하고, 상세 DEBUG 로그(파일/폰트/경로/버전)를 verbose.txt에 저장"
    else:
        description = "Replace Unity game fonts with Thai fonts."
        epilog = """
Examples:
  %(prog)s --gamepath "C:/path/to/game" --parse
  %(prog)s --gamepath "C:/path/to/game" --preview-export
  %(prog)s --gamepath "C:/path/to/game" --sarabun
  %(prog)s --gamepath "C:/path/to/game" --notosansthai --sdfonly
  %(prog)s --gamepath "C:/path/to/game" --mulmaru
  %(prog)s --gamepath "C:/path/to/game" --nanumgothic --sdfonly
  %(prog)s --gamepath "C:/path/to/game" --list font_map.json
        """
        gamepath_help = "Game root path (e.g. C:/path/to/game)"
        parse_help = "Export font info to JSON"
        mulmaru_help = "Replace all fonts with Mulmaru"
        nanum_help = "Replace all fonts with NanumGothic"
        sarabun_help = "Replace all fonts with Sarabun (Thai)"
        notosansthai_help = "Replace all fonts with Noto Sans Thai"
        sdf_help = "Replace SDF fonts only"
        ttf_help = "Replace TTF fonts only"
        list_help = "Replace fonts using a JSON file"
        target_file_help = (
            "Limit replacement targets to specific file name(s) (repeatable)"
        )
        exclude_ext_help = (
            "Additional scan-excluded extensions (comma-separated, e.g. \"resS,.resource\")"
        )
        game_mat_help = "Keep original in-game Material parameters without correction for SDF replacement (default: preserve original style with automatic atlas/padding correction)"
        force_raster_help = "Force replacement fonts into Raster mode for SDF replacement (render mode/material effects follow Raster behavior)"
        game_line_metrics_help = "Use original in-game line metrics for SDF replacement (default: adjusted replacement font metrics)"
        outline_ratio_help = (
            "SDF outline ratio multiplier (default: 1.0, applied to _OutlineWidth/_OutlineSoftness)"
        )
        original_compress_help = "Prefer original compression mode on save (default: uncompressed-family first)"
        temp_dir_help = "Root path for temporary save files (fast SSD/NVMe recommended)"
        output_only_help = "Keep originals untouched and write modified files only to this folder (preserve relative paths)"
        preview_help = "Export preview PNGs (Atlas + glyph crops) for all SDF fonts into preview folder (unswizzled when used with --ps5-swizzle)"
        scan_jobs_help = "Number of parallel scan workers (default: 1, used for parse/bulk scan paths, alias: --max-workers)"
        split_save_force_help = "Skip one-shot and force one-by-one SDF split save for large multi-SDF replacements"
        oneshot_save_force_help = "Force one-shot save even for large multi-SDF targets (disable split-save fallback)"
        ps5_swizzle_help = "Enable PS5 swizzle detect/transform mode (mask_x=0x385F0, mask_y=0x07A0F, rotate=90 compensation)"
        verbose_help = "Keep concise console logs and save detailed DEBUG logs (file/font/path/version) to verbose.txt"

    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    parser.add_argument("--gamepath", type=str, help=gamepath_help)
    parser.add_argument("--parse", action="store_true", help=parse_help)
    parser.add_argument("--mulmaru", action="store_true", help=mulmaru_help)
    parser.add_argument("--nanumgothic", action="store_true", help=nanum_help)
    parser.add_argument("--sarabun", action="store_true", help=sarabun_help)
    parser.add_argument("--notosansthai", action="store_true", help=notosansthai_help)
    parser.add_argument("--sdfonly", action="store_true", help=sdf_help)
    parser.add_argument("--ttfonly", action="store_true", help=ttf_help)
    parser.add_argument("--list", type=str, metavar="JSON_FILE", help=list_help)
    parser.add_argument(
        "--target-file", action="append", metavar="FILE_NAME", help=target_file_help
    )
    parser.add_argument(
        "--exclude-ext", action="append", metavar="EXTS", help=exclude_ext_help
    )
    parser.add_argument("--use-game-material", action="store_true", help=game_mat_help)
    parser.add_argument("--force-raster", action="store_true", help=force_raster_help)
    parser.add_argument("--use-game-mat", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--use-game-line-metrics", action="store_true", help=game_line_metrics_help
    )
    parser.add_argument(
        "--outline-ratio",
        type=float,
        default=1.0,
        metavar="RATIO",
        help=outline_ratio_help,
    )
    parser.add_argument(
        "--use-game-line-matrics", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--original-compress", action="store_true", help=original_compress_help
    )
    parser.add_argument("--temp-dir", type=str, metavar="PATH", help=temp_dir_help)
    parser.add_argument(
        "--output-only", type=str, metavar="PATH", help=output_only_help
    )
    parser.add_argument("--preview-export", action="store_true", help=preview_help)
    parser.add_argument("--preview", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--scan-jobs",
        "--max-workers",
        dest="scan_jobs",
        type=int,
        default=1,
        metavar="N",
        help=scan_jobs_help,
    )
    parser.add_argument(
        "--split-save-force", action="store_true", help=split_save_force_help
    )
    parser.add_argument(
        "--oneshot-save-force", action="store_true", help=oneshot_save_force_help
    )
    parser.add_argument("--ps5-swizzle", action="store_true", help=ps5_swizzle_help)
    parser.add_argument("--verbose", action="store_true", help=verbose_help)
    parser.add_argument(
        "--_validate-bundle", type=str, metavar="BUNDLE_PATH", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--_validate-inner-name",
        action="append",
        metavar="INNER_NAME",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_scan-file-worker",
        type=str,
        metavar="ASSET_FILE_PATH",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--_scan-file-worker-output",
        type=str,
        metavar="OUTPUT_JSON_PATH",
        help=argparse.SUPPRESS,
    )

    args = parser.parse_args()
    if isinstance(args.gamepath, str):
        args.gamepath = strip_wrapping_quotes_repeated(args.gamepath)
    if isinstance(args.list, str):
        args.list = strip_wrapping_quotes_repeated(args.list)
    if isinstance(args.output_only, str):
        args.output_only = strip_wrapping_quotes_repeated(args.output_only)
    if isinstance(getattr(args, "exclude_ext", None), list):
        args.exclude_ext = [
            strip_wrapping_quotes_repeated(str(item))
            for item in args.exclude_ext
            if str(item).strip()
        ]

    verbose_path: str | None = None
    if args.verbose:
        verbose_path = os.path.join(get_script_dir(), VERBOSE_LOG_FILENAME)
    _configure_logging(
        console_level=logging.INFO,
        verbose_log_path=verbose_path,
    )
    py_bits = struct.calcsize("P") * 8
    _log_console(f"Python {sys.version} ({py_bits}-bit)")

    if verbose_path:
        if is_ko:
            _log_info(f"[verbose] 상세 로그를 '{verbose_path}'에 저장합니다.")
        else:
            _log_info(f"[verbose] Writing detailed logs to '{verbose_path}'.")
    _log_debug(
        f"[runtime] cwd={os.getcwd()} script_dir={get_script_dir()} args={vars(args)}"
    )

    # KR: 이전 옵션(--use-game-mat) 호환을 위해 새 옵션에 병합합니다.
    # EN: Merge the legacy option (--use-game-mat) into the new option for backward compatibility.
    args.use_game_material = bool(
        getattr(args, "use_game_material", False)
        or getattr(args, "use_game_mat", False)
    )
    # KR: 오타/레거시 옵션(--use-game-line-matrics)도 동일 동작으로 병합합니다.
    # EN: Also merge the typo/legacy option (--use-game-line-matrics) with the same behavior.
    args.use_game_line_metrics = bool(
        getattr(args, "use_game_line_metrics", False)
        or getattr(args, "use_game_line_matrics", False)
    )
    # KR: 레거시 옵션(--preview)도 새 옵션(--preview-export)으로 병합합니다.
    # EN: Also merge the legacy option (--preview) into the new option (--preview-export).
    args.preview_export = bool(
        getattr(args, "preview_export", False) or getattr(args, "preview", False)
    )
    explicit_primary_modes = _selected_primary_modes(args)
    if len(explicit_primary_modes) > 1:
        joined = ", ".join(explicit_primary_modes)
        if is_ko:
            exit_with_error(
                f"작업 모드 인자는 하나만 사용할 수 있습니다: {joined}",
                lang=lang,
            )
        else:
            exit_with_error(
                f"Only one primary mode may be selected: {joined}",
                lang=lang,
            )
    selected_files = parse_target_files_arg(getattr(args, "target_file", None))
    if args.target_file and not selected_files:
        if is_ko:
            exit_with_error("--target-file 값이 비어 있습니다.", lang=lang)
        else:
            exit_with_error("--target-file values are empty.", lang=lang)
    excluded_exts = parse_exclude_exts_arg(getattr(args, "exclude_ext", None))
    if args.exclude_ext and not excluded_exts:
        if is_ko:
            exit_with_error("--exclude-ext 값이 비어 있습니다.", lang=lang)
        else:
            exit_with_error("--exclude-ext values are empty.", lang=lang)

    if args.split_save_force and args.oneshot_save_force:
        if is_ko:
            exit_with_error(
                "--split-save-force와 --oneshot-save-force를 동시에 사용할 수 없습니다.",
                lang=lang,
            )
        else:
            exit_with_error(
                "Cannot use --split-save-force and --oneshot-save-force at the same time.",
                lang=lang,
            )

    # KR: 기본은 split-save 폴백을 활성화합니다.
    # EN: By default, enable split-save fallback.
    args.split_save = not args.oneshot_save_force
    if args.scan_jobs < 1:
        if is_ko:
            exit_with_error("--scan-jobs는 1 이상의 정수여야 합니다.", lang=lang)
        else:
            exit_with_error(
                "--scan-jobs must be an integer greater than or equal to 1.", lang=lang
            )
    if args.outline_ratio <= 0:
        if is_ko:
            exit_with_error(
                "--outline-ratio는 0보다 큰 실수여야 합니다.",
                lang=lang,
            )
        else:
            exit_with_error(
                "--outline-ratio must be a float greater than 0.",
                lang=lang,
            )
    interactive_mode_requested = len(explicit_primary_modes) == 0
    scan_jobs_explicit = any(
        arg == "--scan-jobs"
        or arg == "--max-workers"
        or arg.startswith("--scan-jobs=")
        or arg.startswith("--max-workers=")
        for arg in sys.argv[1:]
    )

    if args._scan_file_worker:
        if not args.gamepath:
            if is_ko:
                _log_console("[scan_worker] 오류: --gamepath가 필요합니다.")
            else:
                _log_console("[scan_worker] Error: --gamepath is required.")
            raise SystemExit(2)
        if not args._scan_file_worker_output:
            if is_ko:
                _log_console(
                    "[scan_worker] 오류: --_scan-file-worker-output 경로가 필요합니다."
                )
            else:
                _log_console(
                    "[scan_worker] Error: --_scan-file-worker-output path is required."
                )
            raise SystemExit(2)
        raise SystemExit(
            run_scan_file_worker(
                args.gamepath,
                args._scan_file_worker,
                args._scan_file_worker_output,
                lang=lang,
                detect_ps5_swizzle=args.ps5_swizzle,
            )
        )

    if args.temp_dir:
        args.temp_dir = os.path.abspath(str(args.temp_dir))
        try:
            os.makedirs(args.temp_dir, exist_ok=True)
        except Exception as e:
            if is_ko:
                exit_with_error(
                    f"임시 폴더를 만들 수 없습니다: {args.temp_dir} ({e})", lang=lang
                )
            else:
                exit_with_error(
                    f"Failed to create temp directory: {args.temp_dir} ({e})", lang=lang
                )
        if is_ko:
            _log_console(f"임시 저장 경로: {args.temp_dir}")
        else:
            _log_console(f"Temp save path: {args.temp_dir}")
        register_temp_dir_for_cleanup(
            os.path.join(args.temp_dir, "unity_font_replacer_temp")
        )

    output_only_root: str | None = (
        os.path.abspath(str(args.output_only)) if args.output_only else None
    )
    preview_root: str | None = None

    if args.use_game_line_metrics:
        if is_ko:
            _log_console("줄 간격 메트릭 모드: 게임 원본 줄 간격 메트릭을 사용합니다.")
        else:
            _log_console("Line metrics mode: using original in-game line metrics.")
    else:
        if is_ko:
            _log_console(
                "줄 간격 메트릭 모드: 교체 폰트 메트릭 보정을 기본 적용합니다."
            )
        else:
            _log_console(
                "Line metrics mode: using adjusted replacement font metrics by default."
            )

    if args.use_game_material:
        if is_ko:
            _log_console("Material 모드: 게임 원본 Material 파라미터를 사용합니다.")
        else:
            _log_console("Material mode: using original in-game Material parameters.")
    else:
        if is_ko:
            _log_console(
                "Material 모드: 게임 원본 Material 스타일을 유지하고 atlas/padding 차이를 자동 보정합니다."
            )
        else:
            _log_console(
                "Material mode: preserving original in-game Material style with automatic atlas/padding correction."
            )
    if args.force_raster:
        if is_ko:
            _log_console(
                "Raster 강제 모드: SDF 교체를 Raster 기준으로 처리합니다 (렌더 모드 + Material 효과값 보정)."
            )
        else:
            _log_console(
                "Forced Raster mode: processing SDF replacements with Raster behavior (render mode + material effect neutralization)."
            )
    if args.ps5_swizzle:
        if is_ko:
            _log_console(
                "PS5 swizzle 모드: 대상 Atlas swizzle을 자동 판별해 교체 Atlas를 변환합니다 "
                f"(마스크는 텍스처 크기에 따라 자동 계산, rotate={PS5_SWIZZLE_ROTATE})."
            )
        else:
            _log_console(
                "PS5 swizzle mode: auto-detecting target atlas swizzle state and transforming replacement atlas "
                f"(masks computed per texture size, rotate={PS5_SWIZZLE_ROTATE})."
            )
    else:
        if is_ko:
            _log_console("PS5 swizzle 모드: 비활성화")
        else:
            _log_console("PS5 swizzle mode: disabled")
    if args.outline_ratio != 1.0:
        if is_ko:
            _log_console(
                f"외곽선 비율 모드: Material _OutlineWidth/_OutlineSoftness에 x{args.outline_ratio:.3f} 배율을 적용합니다."
            )
        else:
            _log_console(
                f"Outline ratio mode: applying x{args.outline_ratio:.3f} to Material _OutlineWidth/_OutlineSoftness."
            )

    if args._validate_bundle:
        raise SystemExit(
            run_validation_worker(
                args._validate_bundle,
                lang=lang,
                inner_names=args._validate_inner_name,
            )
        )

    input_path = strip_wrapping_quotes_repeated(args.gamepath) if args.gamepath else ""
    _log_debug(f"[runtime] requested_gamepath={input_path!r}")
    if not input_path:
        while True:
            if is_ko:
                entered_path = input("게임 경로를 입력하세요: ").strip()
            else:
                entered_path = input("Enter game path: ").strip()
            input_path = strip_wrapping_quotes_repeated(entered_path)
            if not input_path:
                if is_ko:
                    _log_console("게임 경로가 필요합니다. 다시 입력해주세요.")
                else:
                    _log_console("Game path is required. Please try again.")
                continue
            if not os.path.isdir(input_path):
                if is_ko:
                    _log_console(
                        f"'{input_path}'는 유효한 디렉토리가 아닙니다. 다시 입력해주세요."
                    )
                else:
                    _log_console(
                        f"'{input_path}' is not a valid directory. Please try again."
                    )
                continue
            try:
                game_path, data_path = resolve_game_path(input_path, lang=lang)
            except FileNotFoundError as e:
                if is_ko:
                    _log_console(f"{e}\n다시 입력해주세요.")
                else:
                    _log_console(f"{e}\nPlease try again.")
                continue
            break
    else:
        if not os.path.isdir(input_path):
            if is_ko:
                exit_with_error(
                    f"'{input_path}'는 유효한 디렉토리가 아닙니다.", lang=lang
                )
            else:
                exit_with_error(f"'{input_path}' is not a valid directory.", lang=lang)
        try:
            game_path, data_path = resolve_game_path(input_path, lang=lang)
        except FileNotFoundError as e:
            exit_with_error(str(e), lang=lang)

    replacements: dict[str, JsonDict] | None = None
    mode: str | None = None
    interactive_session = False
    if args.parse:
        mode = "parse"
    elif args.sarabun:
        mode = "sarabun"
    elif args.notosansthai:
        mode = "notosansthai"
    elif args.mulmaru:
        mode = "mulmaru"
    elif args.nanumgothic:
        mode = "nanumgothic"
    elif args.list:
        mode = "list"
    elif args.preview_export:
        mode = "preview_export"
    else:
        interactive_session = True
        if is_ko:
            while True:
                _log_console("작업을 선택하세요:")
                _log_console("  1. 폰트 정보 추출 (JSON 파일 생성)")
                _log_console("  2. JSON 파일로 폰트 교체")
                _log_console("  3. Sarabun(태국어)으로 일괄 교체")
                _log_console("  4. Noto Sans Thai(태국어)로 일괄 교체")
                _log_console("  5. Mulmaru(물마루체)로 일괄 교체")
                _log_console("  6. NanumGothic(나눔고딕)으로 일괄 교체")
                _log_console("  7. Preview export (Atlas/Glyph crop 추출)")
                _log_console()
                choice = input("선택 (1-7): ").strip()
                if choice in {"1", "2", "3", "4", "5", "6", "7"}:
                    break
                _log_console("잘못된 선택입니다. 다시 입력해주세요.")
        else:
            while True:
                _log_console("Select a task:")
                _log_console("  1. Export font info (create JSON)")
                _log_console("  2. Replace fonts using JSON")
                _log_console("  3. Bulk replace with Sarabun (Thai)")
                _log_console("  4. Bulk replace with Noto Sans Thai")
                _log_console("  5. Bulk replace with Mulmaru")
                _log_console("  6. Bulk replace with NanumGothic")
                _log_console("  7. Preview export (Atlas/Glyph crops)")
                _log_console()
                choice = input("Choose (1-7): ").strip()
                if choice in {"1", "2", "3", "4", "5", "6", "7"}:
                    break
                _log_console("Invalid selection. Please try again.")

        if choice == "1":
            mode = "parse"
        elif choice == "2":
            mode = "list"
            while True:
                if is_ko:
                    entered = input("JSON 파일 경로를 입력하세요: ").strip()
                else:
                    entered = input("Enter JSON file path: ").strip()
                entered = strip_wrapping_quotes_repeated(entered)
                if not entered:
                    if is_ko:
                        _log_console("JSON 파일 경로가 필요합니다. 다시 입력해주세요.")
                    else:
                        _log_console("JSON file path is required. Please try again.")
                    continue
                if os.path.exists(entered):
                    args.list = entered
                    break
                if is_ko:
                    _log_console(f"파일을 찾을 수 없습니다: '{entered}'")
                else:
                    _log_console(f"File not found: '{entered}'")
        elif choice == "3":
            mode = "sarabun"
        elif choice == "4":
            mode = "notosansthai"
        elif choice == "5":
            mode = "mulmaru"
        elif choice == "6":
            mode = "nanumgothic"
        elif choice == "7":
            mode = "preview_export"

    args.preview_export = mode == "preview_export"

    if output_only_root and mode == "preview_export":
        if is_ko:
            exit_with_error(
                "--output-only는 --preview-export와 함께 사용할 수 없습니다.",
                lang=lang,
            )
        else:
            exit_with_error(
                "--output-only cannot be used with --preview-export.",
                lang=lang,
            )

    if output_only_root:
        try:
            os.makedirs(output_only_root, exist_ok=True)
        except Exception as e:
            if is_ko:
                exit_with_error(
                    f"출력 폴더를 만들 수 없습니다: {output_only_root} ({e})",
                    lang=lang,
                )
            else:
                exit_with_error(
                    f"Failed to create output folder: {output_only_root} ({e})",
                    lang=lang,
                )
        if is_ko:
            _log_console(
                f"출력 전용 모드: 수정 파일을 '{output_only_root}'에 저장합니다."
            )
        else:
            _log_console(
                f"Output-only mode: writing modified files to '{output_only_root}'."
            )

    if mode == "preview_export":
        preview_root = os.path.join(get_script_dir(), "preview")
        try:
            os.makedirs(preview_root, exist_ok=True)
        except Exception as e:
            if is_ko:
                exit_with_error(
                    f"preview 폴더를 만들 수 없습니다: {preview_root} ({e})",
                    lang=lang,
                )
            else:
                exit_with_error(
                    f"Failed to create preview folder: {preview_root} ({e})",
                    lang=lang,
                )
        if is_ko:
            _log_console(f"Preview 모드: '{preview_root}'에 미리보기를 저장합니다.")
        else:
            _log_console(f"Preview mode: saving previews to '{preview_root}'.")
        if args.ps5_swizzle:
            if is_ko:
                _log_console(
                    "  PS5 swizzle 활성화: preview를 unswizzle 기준으로 저장합니다."
                )
            else:
                _log_console(
                    "  PS5 swizzle enabled: saving previews in unswizzled view."
                )

    if interactive_mode_requested and not scan_jobs_explicit and _mode_uses_scan_jobs(mode):
        while True:
            if is_ko:
                entered_workers = input(
                    f"스캔 워커 수를 입력하세요 (기본 {args.scan_jobs}): "
                ).strip()
            else:
                entered_workers = input(
                    f"Enter scan worker count (default {args.scan_jobs}): "
                ).strip()
            if not entered_workers:
                break
            try:
                parsed_workers = int(entered_workers)
            except (TypeError, ValueError):
                if is_ko:
                    _log_console("숫자를 입력해주세요. (1 이상의 정수)")
                else:
                    _log_console("Please enter a number. (integer >= 1)")
                continue
            if parsed_workers < 1:
                if is_ko:
                    _log_console("스캔 워커 수는 1 이상이어야 합니다.")
                else:
                    _log_console("Scan worker count must be >= 1.")
                continue
            args.scan_jobs = parsed_workers
            break

    compile_method = get_compile_method(data_path)
    detected_unity_version = get_unity_version(game_path, lang=lang)
    default_temp_root = register_temp_dir_for_cleanup(os.path.join(data_path, "temp"))
    if os.path.exists(default_temp_root):
        shutil.rmtree(default_temp_root)

    replace_ttf = not args.sdfonly
    replace_sdf = not args.ttfonly
    material_scale_by_padding = not args.use_game_material
    prefer_builtin_padding_variants = mode in {"mulmaru", "nanumgothic", "sarabun", "notosansthai"}
    if args.sdfonly and args.ttfonly:
        if is_ko:
            exit_with_error(
                "--sdfonly와 --ttfonly를 동시에 사용할 수 없습니다.", lang=lang
            )
        else:
            exit_with_error(
                "Cannot use --sdfonly and --ttfonly at the same time.", lang=lang
            )

    if is_ko:
        _log_console(f"게임 경로: {game_path}")
        _log_console(f"데이터 경로: {data_path}")
        _log_console(f"컴파일 방식: {compile_method}")
        _log_console(f"스캔 워커 수: {args.scan_jobs}")
    else:
        _log_console(f"Game path: {game_path}")
        _log_console(f"Data path: {data_path}")
        _log_console(f"Compile method: {compile_method}")
        _log_console(f"Scan workers: {args.scan_jobs}")
    _log_debug(
        f"[runtime] input_path={input_path} game_path={game_path} data_path={data_path} "
        f"compile_method={compile_method} scan_jobs={args.scan_jobs} "
        f"ps5_swizzle={args.ps5_swizzle} preview_export={args.preview_export}"
    )
    _log_debug(f"[runtime] unity_version={detected_unity_version}")

    if selected_files:
        target_text = ", ".join(sorted(selected_files))
        if is_ko:
            _log_console(f"--target-file 적용: {target_text}")
        else:
            _log_console(f"Applied --target-file: {target_text}")
        _log_debug(f"[runtime] target_files={target_text}")
    if excluded_exts:
        excluded_text = ", ".join(sorted(excluded_exts))
        if is_ko:
            _log_console(f"--exclude-ext 적용: {excluded_text}")
        else:
            _log_console(f"Applied --exclude-ext: {excluded_text}")
        _log_debug(f"[runtime] exclude_exts={excluded_text}")

    _log_debug(
        f"[runtime] mode={mode} interactive={interactive_session} "
        f"replace_ttf={replace_ttf} replace_sdf={replace_sdf}"
    )

    if compile_method == "Il2cpp" and not os.path.exists(
        os.path.join(data_path, "Managed")
    ):
        binary_path, metadata_path = _find_il2cpp_files(game_path, data_path)
        if binary_path is None or metadata_path is None:
            missing = []
            if binary_path is None:
                missing.append("GameAssembly.dll / eboot.bin")
            if metadata_path is None:
                missing.append("global-metadata.dat")
            missing_str = ", ".join(missing)
            if is_ko:
                exit_with_error(
                    f"Il2cpp 게임의 경우 'Managed' 폴더 또는 IL2CPP 파일({missing_str})이 필요합니다.\n"
                    f"PC 게임: <game_path>/GameAssembly.dll + <data_path>/il2cpp_data/Metadata/global-metadata.dat\n"
                    f"PS4 게임: <game_path>/eboot.bin + <data_path>/Metadata/global-metadata.dat\n"
                    "올바른 Unity 게임 폴더인지 확인해주세요.",
                    lang=lang,
                )
            else:
                exit_with_error(
                    f"For Il2cpp games, the 'Managed' folder or IL2CPP files ({missing_str}) are required.\n"
                    f"PC:  <game_path>/GameAssembly.dll + <data_path>/il2cpp_data/Metadata/global-metadata.dat\n"
                    f"PS4: <game_path>/eboot.bin + <data_path>/Metadata/global-metadata.dat\n"
                    "Please check that this is a valid Unity game folder.",
                    lang=lang,
                )

        dumper_path = os.path.join(get_script_dir(), "Il2CppDumper", "Il2CppDumper.exe")
        target_path = os.path.join(data_path, "Managed_")
        os.makedirs(target_path, exist_ok=True)
        command = [
            os.path.abspath(dumper_path),
            os.path.abspath(binary_path),
            os.path.abspath(metadata_path),
            os.path.abspath(target_path),
        ]
        if is_ko:
            _log_console("Il2cpp 게임을 위한 Managed 폴더를 생성합니다...")
        else:
            _log_console("Creating Managed folder for Il2cpp game...")
        _log_console(os.path.abspath(target_path))

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                startupinfo=startupinfo,
                encoding="utf-8",
            )
            if process.returncode == 0:
                _log_console(process.stdout)
                shutil.move(
                    os.path.join(data_path, "Managed_", "DummyDll"),
                    os.path.join(data_path, "Managed"),
                )
                shutil.rmtree(os.path.join(data_path, "Managed_"))
                if is_ko:
                    _log_console("더미 DLL 생성에 성공했습니다!")
                else:
                    _log_console("Dummy DLL generated successfully!")
                compile_method = get_compile_method(data_path)
                if is_ko:
                    _log_console(f"컴파일 방식 재감지: {compile_method}")
                else:
                    _log_console(f"Compile method re-detected: {compile_method}")
            else:
                _log_console(process.stderr)
                if mode == "parse":
                    if is_ko:
                        _log_console(
                            "[경고] Il2cpp 더미 DLL 생성 실패 — --parse 모드에서는 계속 진행합니다.\n"
                            "  TTF 폰트만 스캔되며 SDF/TMP 폰트는 생략될 수 있습니다."
                        )
                    else:
                        _log_console(
                            "[warning] Failed to generate Il2cpp dummy DLL — continuing in --parse mode.\n"
                            "  Only TTF fonts will be scanned; SDF/TMP fonts may be skipped."
                        )
                else:
                    if is_ko:
                        exit_with_error("Il2cpp 더미 DLL 생성 실패", lang=lang)
                    else:
                        exit_with_error("Failed to generate Il2cpp dummy DLL", lang=lang)
        except Exception as e:
            if mode == "parse":
                if is_ko:
                    _log_console(
                        f"[경고] Il2CppDumper 실행 실패: {e} — --parse 모드에서는 계속 진행합니다."
                    )
                else:
                    _log_console(
                        f"[warning] Il2CppDumper failed: {e} — continuing in --parse mode."
                    )
            else:
                if is_ko:
                    exit_with_error(f"Il2CppDumper 실행 중 예외 발생: {e}", lang=lang)
                else:
                    exit_with_error(f"Exception while running Il2CppDumper: {e}", lang=lang)

    if mode == "parse":
        parse_fonts(
            game_path,
            lang=lang,
            target_files=selected_files if selected_files else None,
            exclude_exts=excluded_exts if excluded_exts else None,
            scan_jobs=args.scan_jobs,
            ps5_swizzle=args.ps5_swizzle,
        )
        _pause_before_exit(lang=lang, interactive_session=interactive_session)
        return

    if mode == "preview_export":
        if is_ko:
            _log_console(
                "Preview export 모드: 모든 SDF 폰트 Atlas/Glyph crop 미리보기를 추출합니다..."
            )
        else:
            _log_console(
                "Preview export mode: exporting Atlas/Glyph crop previews for all SDF fonts..."
            )
        replacements = create_preview_export_targets(
            game_path,
            target_files=selected_files if selected_files else None,
            exclude_exts=excluded_exts if excluded_exts else None,
            scan_jobs=args.scan_jobs,
            lang=lang,
            ps5_swizzle=args.ps5_swizzle,
        )
        if not replacements:
            if is_ko:
                _log_console("Preview 대상 SDF 폰트를 찾지 못했습니다.")
            else:
                _log_console("No SDF fonts found for preview export.")
            _pause_before_exit(lang=lang, interactive_session=interactive_session)
            return
        if is_ko:
            _log_console(f"Preview 대상 SDF 폰트: {len(replacements)}개")
        else:
            _log_console(f"Preview target SDF fonts: {len(replacements)}")
    elif mode == "mulmaru":
        if is_ko:
            _log_console("Mulmaru 폰트로 일괄 교체합니다...")
        else:
            _log_console("Bulk replacing with Mulmaru...")
        replacements = create_batch_replacements(
            game_path,
            "Mulmaru",
            replace_ttf,
            replace_sdf,
            target_files=selected_files if selected_files else None,
            exclude_exts=excluded_exts if excluded_exts else None,
            scan_jobs=args.scan_jobs,
            lang=lang,
            ps5_swizzle=args.ps5_swizzle,
        )
        ttf_count = sum(1 for v in replacements.values() if v["Type"] == "TTF")
        sdf_count = sum(1 for v in replacements.values() if v["Type"] == "SDF")
        if is_ko:
            _log_console(f"발견된 폰트: TTF {ttf_count}개, SDF {sdf_count}개")
        else:
            _log_console(f"Found fonts: TTF {ttf_count}, SDF {sdf_count}")
    elif mode == "nanumgothic":
        if is_ko:
            _log_console("NanumGothic 폰트로 일괄 교체합니다...")
        else:
            _log_console("Bulk replacing with NanumGothic...")
        replacements = create_batch_replacements(
            game_path,
            "NanumGothic",
            replace_ttf,
            replace_sdf,
            target_files=selected_files if selected_files else None,
            exclude_exts=excluded_exts if excluded_exts else None,
            scan_jobs=args.scan_jobs,
            lang=lang,
            ps5_swizzle=args.ps5_swizzle,
        )
        ttf_count = sum(1 for v in replacements.values() if v["Type"] == "TTF")
        sdf_count = sum(1 for v in replacements.values() if v["Type"] == "SDF")
        if is_ko:
            _log_console(f"발견된 폰트: TTF {ttf_count}개, SDF {sdf_count}개")
        else:
            _log_console(f"Found fonts: TTF {ttf_count}, SDF {sdf_count}")
    elif mode == "sarabun":
        if is_ko:
            _log_console("Sarabun 폰트로 일괄 교체합니다...")
        else:
            _log_console("Bulk replacing with Sarabun (Thai)...")
        replacements = create_batch_replacements(
            game_path,
            "Sarabun",
            replace_ttf,
            replace_sdf,
            target_files=selected_files if selected_files else None,
            exclude_exts=excluded_exts if excluded_exts else None,
            scan_jobs=args.scan_jobs,
            lang=lang,
            ps5_swizzle=args.ps5_swizzle,
        )
        ttf_count = sum(1 for v in replacements.values() if v["Type"] == "TTF")
        sdf_count = sum(1 for v in replacements.values() if v["Type"] == "SDF")
        if is_ko:
            _log_console(f"발견된 폰트: TTF {ttf_count}개, SDF {sdf_count}개")
        else:
            _log_console(f"Found fonts: TTF {ttf_count}, SDF {sdf_count}")
    elif mode == "notosansthai":
        if is_ko:
            _log_console("Noto Sans Thai 폰트로 일괄 교체합니다...")
        else:
            _log_console("Bulk replacing with Noto Sans Thai...")
        replacements = create_batch_replacements(
            game_path,
            "NotoSansThai",
            replace_ttf,
            replace_sdf,
            target_files=selected_files if selected_files else None,
            exclude_exts=excluded_exts if excluded_exts else None,
            scan_jobs=args.scan_jobs,
            lang=lang,
            ps5_swizzle=args.ps5_swizzle,
        )
        ttf_count = sum(1 for v in replacements.values() if v["Type"] == "TTF")
        sdf_count = sum(1 for v in replacements.values() if v["Type"] == "SDF")
        if is_ko:
            _log_console(f"발견된 폰트: TTF {ttf_count}개, SDF {sdf_count}개")
        else:
            _log_console(f"Found fonts: TTF {ttf_count}, SDF {sdf_count}")
    elif mode == "list":
        if isinstance(args.list, str):
            args.list = strip_wrapping_quotes_repeated(args.list)

        if interactive_session:
            while not args.list or not os.path.exists(args.list):
                if args.list:
                    if is_ko:
                        _log_console(f"'{args.list}' 파일을 찾을 수 없습니다.")
                    else:
                        _log_console(f"File not found: '{args.list}'")
                if is_ko:
                    entered = input("JSON 파일 경로를 다시 입력하세요: ").strip()
                else:
                    entered = input("Re-enter JSON file path: ").strip()
                args.list = strip_wrapping_quotes_repeated(entered)

        if not args.list or not os.path.exists(args.list):
            if is_ko:
                exit_with_error(f"'{args.list}' 파일을 찾을 수 없습니다.", lang=lang)
            else:
                exit_with_error(f"File not found: '{args.list}'", lang=lang)

        if is_ko:
            _log_console(f"'{args.list}' 파일을 읽어서 교체합니다...")
        else:
            _log_console(f"Replacing using '{args.list}'...")
        with open(args.list, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            if is_ko:
                exit_with_error("JSON 루트는 객체(dict)여야 합니다.", lang=lang)
            else:
                exit_with_error("JSON root must be an object (dict).", lang=lang)
        replacements = cast(dict[str, JsonDict], loaded)

    if replacements is None:
        if is_ko:
            exit_with_error("교체 정보가 생성되지 않았습니다.", lang=lang)
        else:
            exit_with_error("Replacement mapping was not generated.", lang=lang)

    if selected_files:
        replacements = {
            key: value
            for key, value in replacements.items()
            if isinstance(value, dict)
            and os.path.basename(str(value.get("File", ""))) in selected_files
        }

        if not replacements:
            target_text = ", ".join(sorted(selected_files))
            if is_ko:
                exit_with_error(
                    f"--target-file 조건에 맞는 교체 대상이 없습니다: {target_text}",
                    lang=lang,
                )
            else:
                exit_with_error(
                    f"No replacement targets matched --target-file: {target_text}",
                    lang=lang,
                )

    if mode != "preview_export":
        _ensure_custom_unitypy_streaming_save(lang=lang)

    unity_version = detected_unity_version
    generator = _create_generator(
        unity_version, game_path, data_path, compile_method, lang=lang
    )
    replacement_lookup, files_to_process = build_replacement_lookup(replacements)
    _log_debug(
        f"[runtime] replacement_entries={len(replacements)} "
        f"lookup_entries={len(replacement_lookup)} files_to_process={len(files_to_process)}"
    )
    preview_files_to_process: set[str] = set()
    if args.preview_export:
        preview_files_to_process = {
            os.path.basename(str(value.get("File", "")))
            for value in replacements.values()
            if isinstance(value, dict) and str(value.get("Type", "")) == "SDF"
        }
        preview_files_to_process.discard("")
    process_files = set(files_to_process) | preview_files_to_process
    _log_debug(
        f"[runtime] process_files={len(process_files)} "
        f"preview_only_files={len(preview_files_to_process)}"
    )
    all_assets_files = find_assets_files(
        game_path,
        lang=lang,
        exclude_exts=excluded_exts if excluded_exts else None,
    )
    asset_file_index = _build_asset_file_index(all_assets_files, data_path)
    asset_path_by_key = cast(dict[str, str], asset_file_index.get("path_by_key", {}))
    basename_by_key = cast(dict[str, str], asset_file_index.get("basename_by_key", {}))
    basename_to_keys = cast(
        dict[str, list[str]],
        asset_file_index.get("basename_to_keys", {}),
    )
    duplicate_asset_names: dict[str, list[str]] = {
        basename: [asset_path_by_key[key] for key in keys if key in asset_path_by_key]
        for basename, keys in basename_to_keys.items()
        if len(keys) > 1
    }
    if duplicate_asset_names:
        for duplicate_name, duplicate_paths in sorted(duplicate_asset_names.items()):
            _log_warning(
                f"[runtime] duplicate_asset_basename={duplicate_name} "
                f"count={len(duplicate_paths)} paths={duplicate_paths}"
            )
    asset_file_queue: list[str] = [
        asset_key
        for asset_key, asset_path in asset_path_by_key.items()
        if os.path.basename(asset_path) in process_files
    ]
    _log_debug(
        f"[runtime] matched_asset_files={len(asset_file_queue)} all_candidates={len(all_assets_files)}"
    )
    if output_only_root and mode != "preview_export":
        prepare_output_only_dependencies(data_path, output_only_root, lang=lang)

    deferred_texture_plans: dict[str, dict[str, Any]] = {}
    deferred_material_plans: dict[str, dict[str, Any]] = {}
    deferred_material_atlas_plans: dict[str, dict[str, Any]] = {}
    pending_external_patch_files: set[str] = set()
    pending_queue_keys: set[str] = set(asset_file_queue)
    prepared_output_targets: set[str] = set()
    modified_count = 0
    queue_index = 0
    while queue_index < len(asset_file_queue):
        asset_file_key = asset_file_queue[queue_index]
        queue_index += 1
        pending_queue_keys.discard(asset_file_key)
        assets_file = asset_path_by_key.get(asset_file_key)
        if not assets_file:
            _log_warning(f"[runtime] queued file not found: {asset_file_key}")
            continue
        fn = os.path.basename(assets_file)
        working_assets_file = assets_file
        if output_only_root and mode != "preview_export":
            working_assets_file = resolve_output_only_path(
                assets_file, data_path, output_only_root
            )
            working_dir = os.path.dirname(working_assets_file)
            if working_dir and not os.path.exists(working_dir):
                os.makedirs(working_dir, exist_ok=True)
            working_assets_key = (
                _normalize_asset_file_key(working_assets_file) or working_assets_file
            )
            if working_assets_key not in prepared_output_targets:
                shutil.copy2(assets_file, working_assets_file)
                prepared_output_targets.add(working_assets_key)
                if is_ko:
                    rel_out = os.path.relpath(working_assets_file, output_only_root)
                    _log_console(f"  출력 대상 준비: {rel_out}")
                else:
                    rel_out = os.path.relpath(working_assets_file, output_only_root)
                    _log_console(f"  Prepared output target: {rel_out}")
        if (
            fn in process_files
            or asset_file_key in deferred_texture_plans
            or asset_file_key in deferred_material_plans
            or asset_file_key in deferred_material_atlas_plans
        ):
            if is_ko:
                _log_console(f"\n처리 중: {fn}")
            else:
                _log_console(f"\nProcessing: {fn}")
            # KR: 기본은 split-save 폴백을 사용하고, --oneshot-save-force일 때만 비활성화합니다.
            # EN: By default, use split-save fallback; only disable when --oneshot-save-force is set.
            file_replacements = {
                key: value
                for key, value in replacements.items()
                if isinstance(value, dict)
                and value.get("File") == fn
                and value.get("Replace_to")
            }
            file_ttf_replacements = {
                key: value
                for key, value in file_replacements.items()
                if value.get("Type") == "TTF"
            }
            file_sdf_replacements = {
                key: value
                for key, value in file_replacements.items()
                if value.get("Type") == "SDF"
            }
            _log_replacement_plan_details(fn, file_replacements)

            file_modified = False
            use_split_sdf_save = (
                args.split_save and replace_sdf and len(file_sdf_replacements) > 1
            )

            if use_split_sdf_save:
                if is_ko:
                    _log_console(
                        f"  SDF 대상 {len(file_sdf_replacements)}건: one-shot 실패 시 적응형 분할 저장으로 폴백합니다..."
                    )
                else:
                    _log_console(
                        f"  {len(file_sdf_replacements)} SDF targets: will fall back to adaptive split save if one-shot fails..."
                    )

                # KR: 먼저 한 번에 저장을 시도하고, 실패 시에만 적응형 분할 저장으로 폴백합니다.
                # EN: First attempt a one-shot save; fall back to adaptive split save only on failure.
                file_lookup, _ = build_replacement_lookup(file_replacements)
                one_shot_ok = False
                if args.split_save_force:
                    if is_ko:
                        _log_console(
                            "  --split-save-force 활성화: one-shot을 건너뛰고 SDF 1개씩 강제 분할 저장을 시작합니다..."
                        )
                    else:
                        _log_console(
                            "  --split-save-force enabled: skipping one-shot and forcing one-by-one SDF split save..."
                        )
                else:
                    try:
                        one_shot_ok = replace_fonts_in_file(
                            unity_version,
                            game_path,
                            working_assets_file,
                            file_replacements,
                            replace_ttf=replace_ttf,
                            replace_sdf=replace_sdf,
                            use_game_mat=args.use_game_material,
                            force_raster=args.force_raster,
                            use_game_line_metrics=args.use_game_line_metrics,
                            material_scale_by_padding=material_scale_by_padding,
                            outline_ratio=args.outline_ratio,
                            prefer_original_compress=args.original_compress,
                            temp_root_dir=args.temp_dir,
                            generator=generator,
                            replacement_lookup=file_lookup,
                            ps5_swizzle=args.ps5_swizzle,
                            preview_export=args.preview_export,
                            preview_root=preview_root,
                            prefer_builtin_padding_variants=prefer_builtin_padding_variants,
                            asset_file_index=asset_file_index,
                            deferred_texture_plans=deferred_texture_plans,
                            deferred_material_plans=deferred_material_plans,
                            deferred_material_atlas_plans=deferred_material_atlas_plans,
                            pending_external_patch_files=pending_external_patch_files,
                            lang=lang,
                        )
                    except MemoryError as e:
                        if is_ko:
                            _log_console(f"  one-shot 저장 실패 [MemoryError]: {e!r}")
                            _log_console("  적응형 분할 저장으로 폴백합니다...")
                        else:
                            _log_console(f"  One-shot save failed [MemoryError]: {e!r}")
                            _log_console("  Falling back to adaptive split save...")
                    except Exception as e:
                        if is_ko:
                            _log_console(
                                f"  one-shot 저장 실패 [{type(e).__name__}]: {e!r}"
                            )
                            _log_console("  적응형 분할 저장으로 폴백합니다...")
                        else:
                            _log_console(
                                f"  One-shot save failed [{type(e).__name__}]: {e!r}"
                            )
                            _log_console("  Falling back to adaptive split save...")

                if one_shot_ok:
                    file_modified = True
                else:
                    auto_split_profile: JsonDict | None = None
                    suggested_sdf_batch_size = 0
                    split_stopped = False
                    if replace_ttf and file_ttf_replacements:
                        file_ttf_lookup, _ = build_replacement_lookup(
                            file_ttf_replacements
                        )
                        try:
                            if replace_fonts_in_file(
                                unity_version,
                                game_path,
                                working_assets_file,
                                file_ttf_replacements,
                                replace_ttf=True,
                                replace_sdf=False,
                                use_game_mat=args.use_game_material,
                                force_raster=args.force_raster,
                                use_game_line_metrics=args.use_game_line_metrics,
                                material_scale_by_padding=material_scale_by_padding,
                                outline_ratio=args.outline_ratio,
                                prefer_original_compress=args.original_compress,
                                temp_root_dir=args.temp_dir,
                                generator=generator,
                                replacement_lookup=file_ttf_lookup,
                                ps5_swizzle=args.ps5_swizzle,
                                preview_export=args.preview_export,
                                preview_root=preview_root,
                                prefer_builtin_padding_variants=prefer_builtin_padding_variants,
                                asset_file_index=asset_file_index,
                                deferred_texture_plans=deferred_texture_plans,
                                deferred_material_plans=deferred_material_plans,
                                deferred_material_atlas_plans=deferred_material_atlas_plans,
                                pending_external_patch_files=pending_external_patch_files,
                                lang=lang,
                            ):
                                file_modified = True
                        except Exception as e:
                            if is_ko:
                                _log_console(
                                    f"  TTF 분할 저장 실패 [{type(e).__name__}]: {e!r}"
                                )
                            else:
                                _log_console(
                                    f"  TTF split save failed [{type(e).__name__}]: {e!r}"
                                )
                            split_stopped = True

                    if replace_sdf and not split_stopped:
                        if not args.split_save_force:
                            auto_split_profile = _estimate_sdf_texture_batch_profile(
                                file_sdf_replacements,
                                force_raster=args.force_raster,
                            )
                            suggested_sdf_batch_size = int(
                                auto_split_profile.get("suggested_batch_size", 0) or 0
                            )
                            estimated_texture_bytes = int(
                                auto_split_profile.get("estimated_total_bytes", 0)
                                or 0
                            )
                            estimated_texture_targets = int(
                                auto_split_profile.get("estimated_target_count", 0)
                                or 0
                            )
                            if estimated_texture_bytes > 0:
                                _log_debug(
                                    f"[split_save_estimate] file={fn} targets={estimated_texture_targets} "
                                    f"estimated_total={estimated_texture_bytes} "
                                    f"suggested_batch_size={suggested_sdf_batch_size}"
                                )
                                if suggested_sdf_batch_size > 0:
                                    if is_ko:
                                        _log_console(
                                            "  one-shot 실패 후 적응형 분할 저장 초기 배치를 "
                                            f"{suggested_sdf_batch_size}로 시작합니다 "
                                            f"(예상 texture payload: {_format_byte_size(estimated_texture_bytes)})."
                                        )
                                    else:
                                        _log_console(
                                            "  One-shot failed; starting adaptive split save with "
                                            f"initial batch {suggested_sdf_batch_size} "
                                            f"(estimated texture payload: {_format_byte_size(estimated_texture_bytes)})."
                                        )
                        sdf_items = list(file_sdf_replacements.items())
                        sdf_total = len(sdf_items)
                        if sdf_total > 0:
                            if args.split_save_force:
                                batch_size = 1
                            elif suggested_sdf_batch_size > 0:
                                batch_size = min(
                                    sdf_total,
                                    max(1, suggested_sdf_batch_size),
                                )
                            else:
                                batch_size = min(sdf_total, max(1, sdf_total // 2))

                            idx = 0
                            while idx < sdf_total:
                                current_batch = min(batch_size, sdf_total - idx)
                                batch_dict = dict(sdf_items[idx : idx + current_batch])
                                batch_lookup, _ = build_replacement_lookup(batch_dict)

                                try:
                                    ok = replace_fonts_in_file(
                                        unity_version,
                                        game_path,
                                        working_assets_file,
                                        batch_dict,
                                        replace_ttf=False,
                                        replace_sdf=True,
                                        use_game_mat=args.use_game_material,
                                        force_raster=args.force_raster,
                                        use_game_line_metrics=args.use_game_line_metrics,
                                        material_scale_by_padding=material_scale_by_padding,
                                        outline_ratio=args.outline_ratio,
                                        prefer_original_compress=args.original_compress,
                                        temp_root_dir=args.temp_dir,
                                        generator=generator,
                                        replacement_lookup=batch_lookup,
                                        ps5_swizzle=args.ps5_swizzle,
                                        preview_export=args.preview_export,
                                        preview_root=preview_root,
                                        prefer_builtin_padding_variants=prefer_builtin_padding_variants,
                                        asset_file_index=asset_file_index,
                                        deferred_texture_plans=deferred_texture_plans,
                                        deferred_material_plans=deferred_material_plans,
                                        deferred_material_atlas_plans=deferred_material_atlas_plans,
                                        pending_external_patch_files=pending_external_patch_files,
                                        lang=lang,
                                    )
                                except Exception as e:
                                    ok = False
                                    if is_ko:
                                        _log_console(
                                            f"  SDF 배치 저장 실패 [{type(e).__name__}]: {e!r}"
                                        )
                                    else:
                                        _log_console(
                                            f"  SDF batch save failed [{type(e).__name__}]: {e!r}"
                                        )

                                if ok:
                                    file_modified = True
                                    idx += current_batch
                                    gc.collect()
                                    if idx < sdf_total:
                                        if args.split_save_force:
                                            if is_ko:
                                                _log_console(
                                                    f"  SDF 배치 진행: {idx}/{sdf_total} (다음 배치: 1, 강제)"
                                                )
                                            else:
                                                _log_console(
                                                    f"  SDF batch progress: {idx}/{sdf_total} (next batch: 1, forced)"
                                                )
                                        else:
                                            # KR: 성공하면 배치를 키워 쓰기 횟수를 줄입니다.
                                            # EN: On success, increase batch size to reduce the number of writes.
                                            batch_size = min(
                                                sdf_total - idx,
                                                max(
                                                    current_batch + 1, current_batch * 2
                                                ),
                                            )
                                            if is_ko:
                                                _log_console(
                                                    f"  SDF 배치 진행: {idx}/{sdf_total} (다음 배치: {batch_size})"
                                                )
                                            else:
                                                _log_console(
                                                    f"  SDF batch progress: {idx}/{sdf_total} (next batch: {batch_size})"
                                                )
                                else:
                                    if is_ko:
                                        _log_console(
                                            "  SDF 배치 저장 실패: 내부 저장 단계가 False를 반환했습니다. 위 오류 로그를 확인하세요."
                                        )
                                    else:
                                        _log_console(
                                            "  SDF batch save failed: internal save stage returned False. Check previous error logs."
                                        )
                                    if current_batch <= 1:
                                        split_stopped = True
                                        if is_ko:
                                            _log_console(
                                                "  SDF 분할 저장 중단: 배치 1개에서도 저장 실패"
                                            )
                                        else:
                                            _log_console(
                                                "  Stopping SDF split save: failed even with batch size 1"
                                            )
                                        break

                                    batch_size = max(1, current_batch // 2)
                                    gc.collect()
                                    if is_ko:
                                        _log_console(
                                            f"  SDF 배치 크기를 {batch_size}로 줄여 재시도합니다..."
                                        )
                                    else:
                                        _log_console(
                                            f"  Reducing SDF batch size to {batch_size} and retrying..."
                                        )
            else:
                if (
                    replace_sdf
                    and len(file_sdf_replacements) > 1
                    and not args.split_save
                ):
                    if is_ko:
                        _log_console(
                            "  참고: --oneshot-save-force로 split-save 폴백이 비활성화되어 메모리 피크가 증가할 수 있습니다."
                        )
                    else:
                        _log_console(
                            "  Note: --oneshot-save-force disables split-save fallback and may increase memory peak."
                        )
                try:
                    if replace_fonts_in_file(
                        unity_version,
                        game_path,
                        working_assets_file,
                        replacements,
                        replace_ttf,
                        replace_sdf,
                        use_game_mat=args.use_game_material,
                        force_raster=args.force_raster,
                        use_game_line_metrics=args.use_game_line_metrics,
                        material_scale_by_padding=material_scale_by_padding,
                        outline_ratio=args.outline_ratio,
                        prefer_original_compress=args.original_compress,
                        temp_root_dir=args.temp_dir,
                        generator=generator,
                        replacement_lookup=replacement_lookup,
                        ps5_swizzle=args.ps5_swizzle,
                        preview_export=args.preview_export,
                        preview_root=preview_root,
                        prefer_builtin_padding_variants=prefer_builtin_padding_variants,
                        asset_file_index=asset_file_index,
                        deferred_texture_plans=deferred_texture_plans,
                        deferred_material_plans=deferred_material_plans,
                        deferred_material_atlas_plans=deferred_material_atlas_plans,
                        pending_external_patch_files=pending_external_patch_files,
                        lang=lang,
                    ):
                        file_modified = True
                except Exception as e:
                    if is_ko:
                        _log_console(f"  파일 처리 실패 [{type(e).__name__}]: {e!r}")
                    else:
                        _log_console(
                            f"  File processing failed [{type(e).__name__}]: {e!r}"
                        )

            if file_modified:
                modified_count += 1
                _cleanup_deferred_patch_bucket(
                    deferred_texture_plans.pop(asset_file_key, None)
                )
                _cleanup_deferred_patch_bucket(
                    deferred_material_plans.pop(asset_file_key, None)
                )
                _cleanup_deferred_patch_bucket(
                    deferred_material_atlas_plans.pop(asset_file_key, None)
                )

        if pending_external_patch_files:
            queued_from_external = sorted(pending_external_patch_files)
            pending_external_patch_files.clear()
            for pending_key in queued_from_external:
                pending_path = asset_path_by_key.get(pending_key)
                if not pending_path:
                    _log_warning(
                        f"[runtime] deferred target file not found: {pending_key}"
                    )
                    continue
                if pending_key in pending_queue_keys:
                    continue
                asset_file_queue.append(pending_key)
                pending_queue_keys.add(pending_key)
                _log_debug(
                    f"[runtime] queued_deferred_patch_file={pending_path} "
                    f"queue_size={len(asset_file_queue)}"
                )

    if mode == "preview_export":
        if is_ko:
            _log_console(
                f"\n완료! preview export 처리 파일: {len(process_files)}개 (원본 수정 없음)"
            )
        else:
            _log_console(
                f"\nDone! Preview-export processed {len(process_files)} file(s) (no source modifications)."
            )
        _pause_before_exit(lang=lang, interactive_session=interactive_session)
    else:
        if is_ko:
            _log_console(f"\n완료! {modified_count}개의 파일이 수정되었습니다.")
        else:
            _log_console(f"\nDone! Modified {modified_count} file(s).")
        _pause_before_exit(lang=lang, interactive_session=interactive_session)


def main() -> None:
    """KR: 한국어 CLI 진입점입니다.
    EN: Korean CLI entry point.
    """
    main_cli(lang="ko")


def main_en() -> None:
    """KR: 영어 CLI 진입점입니다.
    EN: English CLI entry point.
    """
    main_cli(lang="en")


def run_main_ko() -> None:
    """KR: 한국어 실행 진입점을 예외 처리와 함께 실행합니다.
    EN: Runs the Korean entry point with exception handling.
    """
    try:
        main()
    except Exception as e:
        _log_exception(f"\n예상치 못한 오류가 발생했습니다: {e}")
        _pause_before_exit(lang="ko", interactive_session=False)
        sys.exit(1)
    finally:
        logging.shutdown()
        cleanup_registered_temp_dirs()


def run_main_en() -> None:
    """KR: 영어 실행 진입점을 예외 처리와 함께 실행합니다.
    EN: Runs the English entry point with exception handling.
    """
    try:
        main_en()
    except Exception as e:
        _log_exception(f"\nAn unexpected error occurred: {e}")
        _pause_before_exit(lang="en", interactive_session=False)
        sys.exit(1)
    finally:
        logging.shutdown()
        cleanup_registered_temp_dirs()


if __name__ == "__main__":
    try:
        run_main_ko()
    except Exception as e:
        _log_exception(f"\n예상치 못한 오류가 발생했습니다: {e}")
        _pause_before_exit(lang="ko", interactive_session=False)
        sys.exit(1)
