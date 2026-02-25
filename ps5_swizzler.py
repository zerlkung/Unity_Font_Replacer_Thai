from __future__ import annotations

import argparse
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

PS5_SWIZZLE_MASK_X = 0x385F0
PS5_SWIZZLE_MASK_Y = 0x07A0F
PS5_SWIZZLE_ROTATE = 90
_PS5_MICRO_X_BITS = 5   # 32-pixel wide micro-tile (8bpp)
_PS5_MICRO_Y_BITS = 4   # 16-pixel tall micro-tile (8bpp)


def _dimensions_supported(width: int, height: int) -> bool:
    if width <= 0 or height <= 0:
        return False
    if width & (width - 1) or height & (height - 1):
        return False
    micro_w = 1 << _PS5_MICRO_X_BITS
    micro_h = 1 << _PS5_MICRO_Y_BITS
    return width >= micro_w and height >= micro_h


@lru_cache(maxsize=64)
def compute_ps5_swizzle_masks(width: int, height: int) -> tuple[int, int]:
    """Compute PS5 swizzle bit-masks for the given power-of-two dimensions.

    8bpp micro-tile is 32x16.  Macro-tile bits are interleaved as:
    first-Y, first-X, remaining-Y..., remaining-X... above the micro bits.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid dimensions for PS5 swizzle masks: {width}x{height}")
    if width & (width - 1) or height & (height - 1):
        raise ValueError(f"PS5 swizzle requires power-of-two dimensions: {width}x{height}")
    micro_w = 1 << _PS5_MICRO_X_BITS
    micro_h = 1 << _PS5_MICRO_Y_BITS
    if width < micro_w or height < micro_h:
        raise ValueError(
            f"Texture too small for PS5 swizzle micro-tile ({micro_w}x{micro_h}): {width}x{height}"
        )
    total_x = width.bit_length() - 1
    total_y = height.bit_length() - 1
    macro_x = total_x - _PS5_MICRO_X_BITS
    macro_y = total_y - _PS5_MICRO_Y_BITS
    mask_x = 0; mask_y = 0; pos = 0
    for _ in range(_PS5_MICRO_Y_BITS):
        mask_y |= 1 << pos; pos += 1
    for _ in range(_PS5_MICRO_X_BITS):
        mask_x |= 1 << pos; pos += 1
    mx_rem = macro_x; my_rem = macro_y
    if my_rem > 0:
        mask_y |= 1 << pos; pos += 1; my_rem -= 1
    if mx_rem > 0:
        mask_x |= 1 << pos; pos += 1; mx_rem -= 1
    for _ in range(my_rem):
        mask_y |= 1 << pos; pos += 1
    for _ in range(mx_rem):
        mask_x |= 1 << pos; pos += 1
    return mask_x, mask_y


@lru_cache(maxsize=128)
def _bit_positions(mask: int) -> tuple[int, ...]:
    return tuple(i for i in range(max(mask.bit_length(), 0)) if (mask >> i) & 1)


@lru_cache(maxsize=128)
def _axis_tile_size(mask: int) -> int:
    positions = _bit_positions(mask)
    return 1 << len(positions) if positions else 1


@lru_cache(maxsize=128)
def _deposit_table(mask: int) -> tuple[int, ...]:
    """Build a lookup table for pdep-like bit deposit (tile-local axis)."""
    positions = _bit_positions(mask)
    axis_size = _axis_tile_size(mask)
    table: list[int] = [0] * axis_size
    for value in range(axis_size):
        deposited = 0
        for bit_index, dst_bit in enumerate(positions):
            if (value >> bit_index) & 1:
                deposited |= (1 << dst_bit)
        table[value] = deposited
    return tuple(table)


def _validate_shape(data: bytes, width: int, height: int, bytes_per_element: int) -> int:
    if width <= 0 or height <= 0 or bytes_per_element <= 0:
        raise ValueError(
            f"Invalid texture shape: w={width}, h={height}, bpe={bytes_per_element}"
        )
    total_elements = width * height
    expected_size = total_elements * bytes_per_element
    if len(data) < expected_size:
        raise ValueError(
            f"Size mismatch: expected at least {expected_size}, got {len(data)} "
            f"(w={width}, h={height}, bpe={bytes_per_element})"
        )
    return total_elements


def _clip_to_base_level(data: bytes, width: int, height: int, bytes_per_element: int) -> tuple[bytes, int]:
    total_elements = _validate_shape(data, width, height, bytes_per_element)
    expected_size = total_elements * bytes_per_element
    if len(data) > expected_size:
        return data[:expected_size], len(data) - expected_size
    return data, 0


def _bytes_to_image(data: bytes, width: int, height: int, bytes_per_element: int) -> Image.Image:
    arr = np.frombuffer(data, dtype=np.uint8)
    if bytes_per_element == 1:
        return Image.fromarray(arr.reshape(height, width), mode="L")
    if bytes_per_element == 2:
        return Image.fromarray(arr.reshape(height, width, 2), mode="LA")
    if bytes_per_element == 3:
        return Image.fromarray(arr.reshape(height, width, 3), mode="RGB")
    if bytes_per_element == 4:
        return Image.fromarray(arr.reshape(height, width, 4), mode="RGBA")
    # Fallback preview: first channel only.
    ch0 = arr.reshape(height, width, bytes_per_element)[:, :, 0]
    return Image.fromarray(ch0, mode="L")


def _image_to_bytes(path: Path, bytes_per_element: int | None) -> tuple[bytes, int, int, int]:
    img = Image.open(path)
    if bytes_per_element is None:
        if img.mode in ("L", "P"):
            img = img.convert("L")
            bytes_per_element = 1
        elif img.mode == "LA":
            bytes_per_element = 2
        elif img.mode == "RGB":
            bytes_per_element = 3
        elif img.mode == "RGBA":
            bytes_per_element = 4
        else:
            img = img.convert("RGBA")
            bytes_per_element = 4
    else:
        if bytes_per_element == 1:
            img = img.convert("L")
        elif bytes_per_element == 2:
            img = img.convert("LA")
        elif bytes_per_element == 3:
            img = img.convert("RGB")
        elif bytes_per_element == 4:
            img = img.convert("RGBA")
        else:
            raise ValueError("PNG input supports bytes-per-element 1/2/3/4 only.")

    return img.tobytes(), img.width, img.height, bytes_per_element


def unswizzle(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int,
    mask_y: int,
) -> bytes:
    data, _ = _clip_to_base_level(data, width, height, bytes_per_element)
    total_elements = _validate_shape(data, width, height, bytes_per_element)

    src = np.frombuffer(data, dtype=np.uint8).reshape(total_elements, bytes_per_element)
    dst = np.empty_like(src)

    tile_w = _axis_tile_size(mask_x)
    tile_h = _axis_tile_size(mask_y)
    xdep = np.array(_deposit_table(mask_x), dtype=np.int64)
    ydep = np.array(_deposit_table(mask_y), dtype=np.int64)
    macro_cols = (width + tile_w - 1) // tile_w
    tile_elements = tile_w * tile_h
    x = np.arange(width, dtype=np.int64)
    local_x = x % tile_w
    macro_x = x // tile_w

    for y in range(height):
        macro_y = y // tile_h
        local_y = y % tile_h
        row_offset = int(ydep[local_y])
        tile_base = ((macro_y * macro_cols) + macro_x) * tile_elements
        src_idx = tile_base + row_offset + xdep[local_x]
        row_start = y * width
        dst[row_start : row_start + width] = src[src_idx]

    return dst.reshape(-1).tobytes()


def swizzle(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int,
    mask_y: int,
) -> bytes:
    data, _ = _clip_to_base_level(data, width, height, bytes_per_element)
    total_elements = _validate_shape(data, width, height, bytes_per_element)

    src = np.frombuffer(data, dtype=np.uint8).reshape(total_elements, bytes_per_element)
    dst = np.empty_like(src)

    tile_w = _axis_tile_size(mask_x)
    tile_h = _axis_tile_size(mask_y)
    xdep = np.array(_deposit_table(mask_x), dtype=np.int64)
    ydep = np.array(_deposit_table(mask_y), dtype=np.int64)
    macro_cols = (width + tile_w - 1) // tile_w
    tile_elements = tile_w * tile_h
    x = np.arange(width, dtype=np.int64)
    local_x = x % tile_w
    macro_x = x // tile_w

    for y in range(height):
        macro_y = y // tile_h
        local_y = y % tile_h
        row_offset = int(ydep[local_y])
        tile_base = ((macro_y * macro_cols) + macro_x) * tile_elements
        dst_idx = tile_base + row_offset + xdep[local_x]
        row_start = y * width
        dst[dst_idx] = src[row_start : row_start + width]

    return dst.reshape(-1).tobytes()


def roughness_score(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    max_axis_samples: int = 256,
) -> float:
    data, _ = _clip_to_base_level(data, width, height, bytes_per_element)
    _validate_shape(data, width, height, bytes_per_element)
    arr = np.frombuffer(data, dtype=np.uint8).reshape(height, width, bytes_per_element)

    step_x = max(1, width // max_axis_samples)
    step_y = max(1, height // max_axis_samples)

    if bytes_per_element == 1:
        channel_index = 0
    else:
        best_score = -1.0
        channel_index = 0
        for ch in range(bytes_per_element):
            y = arr[:, :, ch].astype(np.float32)
            dx = np.abs(y[:, 1:] - y[:, :-1]).mean() if width > 1 else 0.0
            dy = np.abs(y[1:, :] - y[:-1, :]).mean() if height > 1 else 0.0
            score = float(dx + dy)
            if score > best_score:
                best_score = score
                channel_index = ch

    y = arr[:, :, channel_index].astype(np.float32)
    if width > step_x:
        dx = np.abs(y[:, step_x:] - y[:, :-step_x]).mean()
    else:
        dx = 0.0
    if height > step_y:
        dy = np.abs(y[step_y:, :] - y[:-step_y, :]).mean()
    else:
        dy = 0.0
    return float(dx + dy)


def unswizzle_best_variant(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int | None = None,
    mask_y: int | None = None,
    allow_axis_swap: bool = False,
) -> tuple[bytes, int, int, str, float]:
    """Pick the best unswizzle candidate between normal and swapped-axis variants."""
    data, _ = _clip_to_base_level(data, width, height, bytes_per_element)
    if mask_x is None or mask_y is None:
        mask_x, mask_y = compute_ps5_swizzle_masks(width, height)

    normal = unswizzle(data, width, height, bytes_per_element, mask_x, mask_y)
    normal_score = roughness_score(normal, width, height, bytes_per_element)

    best_data = normal
    best_width = width
    best_height = height
    best_variant = "normal"
    best_score = normal_score

    if allow_axis_swap and width != height and _dimensions_supported(height, width):
        try:
            swap_mask_x, swap_mask_y = compute_ps5_swizzle_masks(height, width)
            swapped = unswizzle(
                data,
                height,
                width,
                bytes_per_element,
                swap_mask_x,
                swap_mask_y,
            )
            swapped_score = roughness_score(swapped, height, width, bytes_per_element)
            if swapped_score <= normal_score * 0.985:
                best_data = swapped
                best_width = height
                best_height = width
                best_variant = "swapped_axes"
                best_score = swapped_score
        except Exception:
            pass

    return best_data, best_width, best_height, best_variant, best_score


def detect_swizzle_state_detail(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int,
    mask_y: int,
    allow_axis_swap: bool = False,
) -> tuple[str, float, float, float, bytes, bytes, int, int, str]:
    data, _ = _clip_to_base_level(data, width, height, bytes_per_element)
    raw_score = roughness_score(data, width, height, bytes_per_element)
    unswizzled, unsw_w, unsw_h, unsw_variant, unsw_score = unswizzle_best_variant(
        data,
        width,
        height,
        bytes_per_element,
        mask_x=mask_x,
        mask_y=mask_y,
        allow_axis_swap=allow_axis_swap,
    )
    swizzled = swizzle(data, width, height, bytes_per_element, mask_x, mask_y)
    swz_score = roughness_score(swizzled, width, height, bytes_per_element)

    if unsw_score < raw_score * 0.92 and unsw_score <= swz_score * 0.98:
        verdict = "likely_swizzled_input"
    elif raw_score <= unsw_score * 0.92 and raw_score <= swz_score * 0.92:
        verdict = "likely_linear_input"
    else:
        verdict = "inconclusive"

    return (
        verdict,
        raw_score,
        unsw_score,
        swz_score,
        unswizzled,
        swizzled,
        unsw_w,
        unsw_h,
        unsw_variant,
    )


def detect_swizzle_state(
    data: bytes,
    width: int,
    height: int,
    bytes_per_element: int,
    mask_x: int,
    mask_y: int,
) -> tuple[str, float, float, float, bytes, bytes]:
    verdict, raw_score, unsw_score, swz_score, unswizzled, swizzled, _, _, _ = detect_swizzle_state_detail(
        data,
        width,
        height,
        bytes_per_element,
        mask_x,
        mask_y,
        allow_axis_swap=False,
    )
    return verdict, raw_score, unsw_score, swz_score, unswizzled, swizzled


def apply_transforms(img: Image.Image, rotate: int, hflip: bool, vflip: bool) -> Image.Image:
    out = img
    if rotate:
        out = out.rotate(rotate, expand=True)
    if hflip:
        out = out.transpose(Image.FLIP_LEFT_RIGHT)
    if vflip:
        out = out.transpose(Image.FLIP_TOP_BOTTOM)
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PS5 swizzler/unswizzler CLI (bin/png detect + convert)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  BIN -> BIN (unswizzle)\n"
            "    ps5_swizzler.py --mode unswizzle --input atlas_swizzled.bin --width 2048 --height 2048 --bytes-per-element 1 --output-bin atlas_linear.bin --skip-png\n"
            "  BIN -> PNG (unswizzle preview)\n"
            "    ps5_swizzler.py --mode unswizzle --input atlas_swizzled.bin --width 2048 --height 2048 --bytes-per-element 1 --output-png atlas_linear.png --skip-bin\n"
            "  PNG -> BIN (swizzle)\n"
            "    ps5_swizzler.py --mode swizzle --input atlas_linear.png --input-format png --bytes-per-element 1 --output-bin atlas_swizzled.bin --skip-png\n"
            "  PNG -> PNG (swizzle preview)\n"
            "    ps5_swizzler.py --mode swizzle --input atlas_linear.png --input-format png --bytes-per-element 1 --output-png atlas_swizzled.png --skip-bin\n"
        ),
    )
    p.add_argument("--mode", choices=["unswizzle", "swizzle", "detect"], default="unswizzle",
                   help="Processing mode: detect / unswizzle / swizzle (default: unswizzle)")
    p.add_argument("--input", required=True, help="Input texture file path (.bin or .png)")
    p.add_argument("--input-format", choices=["auto", "bin", "png"], default="auto",
                   help="Input format override (default: auto by extension)")
    p.add_argument("--width", type=int, default=None,
                   help="Texture width (required for bin input unless defaults are acceptable)")
    p.add_argument("--height", type=int, default=None,
                   help="Texture height (required for bin input unless defaults are acceptable)")
    p.add_argument("--bytes-per-element", type=int, default=None,
                   help="Bytes per pixel element (bin: default 1, png: auto by mode if omitted)")
    p.add_argument("--mask-x", type=lambda s: int(s, 0), default=None,
                   help="X-axis swizzle mask (auto-computed from dimensions if omitted)")
    p.add_argument("--mask-y", type=lambda s: int(s, 0), default=None,
                   help="Y-axis swizzle mask (auto-computed from dimensions if omitted)")
    p.add_argument("--output-bin", default=None,
                   help="Binary output path (default: unswizzled.bin or swizzled.bin)")
    p.add_argument("--output-png", default=None,
                   help="PNG output path (default: unswizzled.png or swizzled.png; detect: detect_compare.png)")
    p.add_argument("--skip-bin", action="store_true", help="Skip writing binary output")
    p.add_argument("--skip-png", action="store_true", help="Skip writing PNG output")
    p.add_argument("--rotate", type=int, default=None,
                   help="Preview/output image rotation: 0/90/180/270 (default: unswizzle=90, else=0)")
    p.add_argument("--hflip", action="store_true", help="Apply horizontal flip to output preview image(s)")
    p.add_argument("--vflip", action="store_true", help="Apply vertical flip to output preview image(s)")
    p.add_argument(
        "--axis-swap",
        choices=["auto", "off"],
        default="auto",
        help="For non-square unswizzle: try swapped width/height candidate and keep the more coherent result (default: auto)",
    )
    return p.parse_args()


def _resolve_input_format(path: Path, input_format: str) -> str:
    if input_format != "auto":
        return input_format
    return "png" if path.suffix.lower() == ".png" else "bin"


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    input_format = _resolve_input_format(input_path, args.input_format)
    mask_was_explicit = args.mask_x is not None and args.mask_y is not None
    allow_axis_swap = args.axis_swap == "auto" and not mask_was_explicit

    if args.rotate is None:
        args.rotate = PS5_SWIZZLE_ROTATE if args.mode == "unswizzle" else 0
    if args.rotate not in (0, 90, 180, 270):
        raise ValueError("--rotate must be one of 0/90/180/270")

    if args.mode == "detect":
        if args.output_png is None and not args.skip_png:
            args.output_png = "detect_compare.png"
    else:
        if args.output_bin is None and not args.skip_bin:
            args.output_bin = "unswizzled.bin" if args.mode == "unswizzle" else "swizzled.bin"
        if args.output_png is None and not args.skip_png:
            args.output_png = "unswizzled.png" if args.mode == "unswizzle" else "swizzled.png"

    if input_format == "png":
        data, in_w, in_h, in_bpe = _image_to_bytes(input_path, args.bytes_per_element)
        if args.width is not None and args.width != in_w:
            raise ValueError(f"PNG width mismatch: arg={args.width}, image={in_w}")
        if args.height is not None and args.height != in_h:
            raise ValueError(f"PNG height mismatch: arg={args.height}, image={in_h}")
        width = in_w
        height = in_h
        bytes_per_element = in_bpe
    else:
        width = 512 if args.width is None else args.width
        height = 512 if args.height is None else args.height
        bytes_per_element = 1 if args.bytes_per_element is None else args.bytes_per_element
        raw_data = input_path.read_bytes()
        expected = width * height * bytes_per_element
        if len(raw_data) < expected:
            raise ValueError(
                f"BIN size mismatch: expected at least {expected}, got {len(raw_data)} "
                f"(w={width}, h={height}, bpe={bytes_per_element})"
            )
        data, trailing = _clip_to_base_level(raw_data, width, height, bytes_per_element)
        if trailing:
            print(
                "Note: input contains extra trailing bytes "
                f"({trailing}). Only base level ({expected} bytes) will be processed."
            )

    # Auto-compute masks from dimensions when not explicitly provided
    if args.mask_x is None or args.mask_y is None:
        args.mask_x, args.mask_y = compute_ps5_swizzle_masks(width, height)

    if args.mode == "detect":
        verdict, raw_score, unsw_score, swz_score, unsw_data, swz_data, unsw_w, unsw_h, unsw_variant = detect_swizzle_state_detail(
            data,
            width,
            height,
            bytes_per_element,
            args.mask_x,
            args.mask_y,
            allow_axis_swap=allow_axis_swap,
        )

        print("Detect")
        print(f"  input       : {args.input}")
        print(f"  format      : {input_format}")
        print(f"  size        : {width}x{height}")
        print(f"  bpe         : {bytes_per_element}")
        print(f"  raw score   : {raw_score:.6f}")
        print(f"  unsw score  : {unsw_score:.6f}")
        print(f"  unsw variant: {unsw_variant}")
        print(f"  swz score   : {swz_score:.6f}")
        print(f"  verdict     : {verdict}")
        if verdict == "likely_swizzled_input":
            print("  suggestion  : use --mode unswizzle")
        elif verdict == "likely_linear_input":
            print("  suggestion  : already linear (or use --mode swizzle to repack)")
        else:
            print("  suggestion  : inconclusive, inspect previews or try different mask/format")

        if not args.skip_png:
            raw_img = apply_transforms(
                _bytes_to_image(data, width, height, bytes_per_element),
                args.rotate,
                args.hflip,
                args.vflip,
            ).convert("RGB")
            unsw_img = apply_transforms(
                _bytes_to_image(unsw_data, unsw_w, unsw_h, bytes_per_element),
                args.rotate,
                args.hflip,
                args.vflip,
            ).convert("RGB")
            swz_img = apply_transforms(
                _bytes_to_image(swz_data, width, height, bytes_per_element),
                args.rotate,
                args.hflip,
                args.vflip,
            ).convert("RGB")

            raw_path = Path("detect_raw.png")
            unsw_path = Path("detect_unswizzled_candidate.png")
            swz_path = Path("detect_swizzled_candidate.png")
            raw_img.save(raw_path)
            unsw_img.save(unsw_path)
            swz_img.save(swz_path)

            w = 320
            h = 320
            sheet = Image.new("RGB", (w * 3, h), (0, 0, 0))
            draw = ImageDraw.Draw(sheet)
            tiles = [
                ("raw", raw_img),
                ("unswizzled_candidate", unsw_img),
                ("swizzled_candidate", swz_img),
            ]
            for i, (label, im) in enumerate(tiles):
                x = i * w
                sheet.paste(im.resize((w, h), Image.NEAREST), (x, 0))
                draw.text((x + 6, 6), label, fill=(255, 0, 0))

            if args.output_png:
                sheet.save(args.output_png)
                print(f"  compare png : {args.output_png}")
            print(f"  raw png     : {raw_path}")
            print(f"  unsw png    : {unsw_path}")
            print(f"  swz png     : {swz_path}")
        return

    if args.mode == "unswizzle":
        out, out_w, out_h, out_variant, _ = unswizzle_best_variant(
            data=data,
            width=width,
            height=height,
            bytes_per_element=bytes_per_element,
            mask_x=args.mask_x,
            mask_y=args.mask_y,
            allow_axis_swap=allow_axis_swap,
        )
    else:
        out_w = width
        out_h = height
        out_variant = "n/a"
        out = swizzle(
            data=data,
            width=width,
            height=height,
            bytes_per_element=bytes_per_element,
            mask_x=args.mask_x,
            mask_y=args.mask_y,
        )

    if args.output_bin:
        Path(args.output_bin).write_bytes(out)
    if args.output_png:
        img = _bytes_to_image(out, out_w, out_h, bytes_per_element)
        img = apply_transforms(img, args.rotate, args.hflip, args.vflip)
        img.save(args.output_png)

    print("Done")
    print(f"  mode       : {args.mode}")
    print(f"  input      : {args.input}")
    print(f"  format     : {input_format}")
    print(f"  size       : {width}x{height}")
    if args.mode == "unswizzle":
        print(f"  unsw variant: {out_variant}")
        print(f"  out size   : {out_w}x{out_h}")
    print(f"  bpe        : {bytes_per_element}")
    if args.output_bin:
        print(f"  output bin : {args.output_bin}")
    if args.output_png:
        print(f"  output png : {args.output_png}")
    print(f"  mask_x     : {args.mask_x:#x}")
    print(f"  mask_y     : {args.mask_y:#x}")


if __name__ == "__main__":
    main()
