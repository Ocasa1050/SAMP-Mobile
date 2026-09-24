#!/usr/bin/env python3
"""Convert GTA mobile DXT texture databases to alpha-safe UNC databases.

The game database format stores one record per texture in DAT and signed
32-bit offsets in TOC. DXT5 contains alpha while ETC1 does not, so UNC is the
safe fallback for devices without a GPU texture compression extension. Most
databases use RGBA8888; the oversized gta_int database uses RGBA4444 so its
TOC remains addressable while retaining an alpha channel.

Usage:
    python3 convert_dxt_to_unc.py /path/to/texdb /path/to/output-texdb

The input directory must contain folders such as gta3/, gta_int/, and menu/
with <name>.dxt.dat/.toc/.tmb plus <name>.txt.
"""

from __future__ import annotations

import argparse
import shutil
import struct
from pathlib import Path

try:
    import texture2ddecoder as _texture2ddecoder
except ImportError:
    _texture2ddecoder = None

try:
    from PIL import Image as _PILImage
except ImportError:
    _PILImage = None


ENCODING_DXT1 = 0x83F0
ENCODING_DXT5 = 0x83F3
ENCODING_RGBA4444 = 0x8033
ENCODING_RGBA8888 = 0x1401

_repair_count = 0
_repair_bytes = 0


def _rgb565(value: int) -> tuple[int, int, int]:
    return (
        ((value >> 11) & 0x1F) * 255 // 31,
        ((value >> 5) & 0x3F) * 255 // 63,
        (value & 0x1F) * 255 // 31,
    )


def _decode_color_block(data: bytes, *, allow_transparent: bool) -> list[tuple[int, int, int, int]]:
    c0, c1, indices = struct.unpack_from("<HHI", data, 0)
    color0 = _rgb565(c0)
    color1 = _rgb565(c1)
    colors: list[tuple[int, int, int, int]] = [
        (*color0, 255),
        (*color1, 255),
    ]
    if c0 > c1 or not allow_transparent:
        colors.extend(
            (
                (
                    (2 * color0[0] + color1[0]) // 3,
                    (2 * color0[1] + color1[1]) // 3,
                    (2 * color0[2] + color1[2]) // 3,
                    255,
                ),
                (
                    (color0[0] + 2 * color1[0]) // 3,
                    (color0[1] + 2 * color1[1]) // 3,
                    (color0[2] + 2 * color1[2]) // 3,
                    255,
                ),
            )
        )
    else:
        colors.extend(
            (
                (
                    (color0[0] + color1[0]) // 2,
                    (color0[1] + color1[1]) // 2,
                    (color0[2] + color1[2]) // 2,
                    255,
                ),
                (0, 0, 0, 0),
            )
        )
    return [colors[(indices >> (2 * pixel)) & 0x3] for pixel in range(16)]


def _decode_dxt1(data: bytes, width: int, height: int) -> bytes:
    blocks_w = max(1, (width + 3) // 4)
    blocks_h = max(1, (height + 3) // 4)
    expected = blocks_w * blocks_h * 8
    if len(data) < expected:
        raise ValueError(f"DXT1 level is truncated: need {expected}, got {len(data)}")

    if _texture2ddecoder is not None:
        decoded = bytearray(
            _texture2ddecoder.decode_bc1(data[:expected], width, height)
        )
        for index in range(0, len(decoded), 4):
            decoded[index], decoded[index + 2] = decoded[index + 2], decoded[index]
        return bytes(decoded)

    output = bytearray(width * height * 4)
    cursor = 0
    for block_y in range(blocks_h):
        for block_x in range(blocks_w):
            colors = _decode_color_block(
                data[cursor : cursor + 8],
                allow_transparent=True,
            )
            cursor += 8
            for local_y in range(4):
                for local_x in range(4):
                    x = block_x * 4 + local_x
                    y = block_y * 4 + local_y
                    if x >= width or y >= height:
                        continue
                    start = (y * width + x) * 4
                    output[start : start + 4] = bytes(colors[local_y * 4 + local_x])
    return bytes(output)


def _decode_dxt5(data: bytes, width: int, height: int) -> bytes:
    blocks_w = max(1, (width + 3) // 4)
    blocks_h = max(1, (height + 3) // 4)
    expected = blocks_w * blocks_h * 16
    if len(data) < expected:
        raise ValueError(f"DXT5 level is truncated: need {expected}, got {len(data)}")

    if _texture2ddecoder is not None:
        decoded = bytearray(
            _texture2ddecoder.decode_bc3(data[:expected], width, height)
        )
        for index in range(0, len(decoded), 4):
            decoded[index], decoded[index + 2] = decoded[index + 2], decoded[index]
        return bytes(decoded)

    output = bytearray(width * height * 4)
    cursor = 0
    for block_y in range(blocks_h):
        for block_x in range(blocks_w):
            alpha0 = data[cursor]
            alpha1 = data[cursor + 1]
            alpha_bits = int.from_bytes(data[cursor + 2 : cursor + 8], "little")
            if alpha0 > alpha1:
                alphas = [
                    alpha0,
                    alpha1,
                    (6 * alpha0 + alpha1) // 7,
                    (5 * alpha0 + 2 * alpha1) // 7,
                    (4 * alpha0 + 3 * alpha1) // 7,
                    (3 * alpha0 + 4 * alpha1) // 7,
                    (2 * alpha0 + 5 * alpha1) // 7,
                    (alpha0 + 6 * alpha1) // 7,
                ]
            else:
                alphas = [
                    alpha0,
                    alpha1,
                    (4 * alpha0 + alpha1) // 5,
                    (3 * alpha0 + 2 * alpha1) // 5,
                    (2 * alpha0 + 3 * alpha1) // 5,
                    (alpha0 + 4 * alpha1) // 5,
                    0,
                    255,
                ]
            color_block = _decode_color_block(
                data[cursor + 8 : cursor + 16],
                allow_transparent=False,
            )
            cursor += 16
            for local_y in range(4):
                for local_x in range(4):
                    x = block_x * 4 + local_x
                    y = block_y * 4 + local_y
                    if x >= width or y >= height:
                        continue
                    pixel = local_y * 4 + local_x
                    color = color_block[pixel]
                    alpha = alphas[(alpha_bits >> (3 * pixel)) & 0x7]
                    start = (y * width + x) * 4
                    output[start : start + 4] = bytes((*color[:3], alpha))
    return bytes(output)


def _downsample_rgba(data: bytes, width: int, height: int) -> tuple[bytes, int, int]:
    new_width = max(1, width // 2)
    new_height = max(1, height // 2)
    if _PILImage is not None:
        resized = _PILImage.frombytes("RGBA", (width, height), data).resize(
            (new_width, new_height),
            _PILImage.Resampling.BOX,
        )
        return resized.tobytes(), new_width, new_height

    output = bytearray(new_width * new_height * 4)
    for y in range(new_height):
        source_y0 = min(height - 1, y * 2)
        source_y1 = min(height - 1, source_y0 + 1)
        for x in range(new_width):
            source_x0 = min(width - 1, x * 2)
            source_x1 = min(width - 1, source_x0 + 1)
            positions = (
                (source_y0 * width + source_x0) * 4,
                (source_y0 * width + source_x1) * 4,
                (source_y1 * width + source_x0) * 4,
                (source_y1 * width + source_x1) * 4,
            )
            target = (y * new_width + x) * 4
            for channel in range(4):
                output[target + channel] = sum(data[pos + channel] for pos in positions) // 4
    return bytes(output), new_width, new_height


def _pack_rgba4444(data: bytes) -> bytes:
    packed = bytearray((len(data) // 4) * 2)
    target = 0
    for source in range(0, len(data), 4):
        # Write little-endian RGBA4444 directly, avoiding a slice and a
        # struct.pack call for every pixel in the multi-gigabyte database.
        packed[target] = (data[source + 2] & 0xF0) | (data[source + 3] >> 4)
        packed[target + 1] = (data[source] & 0xF0) | (data[source + 1] >> 4)
        target += 2
    return bytes(packed)


def _rle_decompress(
    data: bytes,
    segment_size: int,
    indicator: int,
    *,
    tolerate_truncated_tail: bool = False,
) -> bytes:
    if not indicator:
        return data
    marker = indicator & 0xFF
    output = bytearray()
    cursor = 0
    while cursor < len(data):
        first = data[cursor]
        cursor += 1
        if first == marker:
            if cursor + 1 + segment_size > len(data):
                if tolerate_truncated_tail:
                    break
                raise ValueError("truncated RLE repeat segment")
            repeat = data[cursor]
            cursor += 1
            segment = data[cursor : cursor + segment_size]
            cursor += segment_size
            output.extend(segment * repeat)
        else:
            remaining = segment_size - 1
            if cursor + remaining > len(data):
                if tolerate_truncated_tail:
                    break
                raise ValueError("truncated RLE literal segment")
            output.append(first)
            output.extend(data[cursor : cursor + remaining])
            cursor += remaining
    return bytes(output)


def _mip_sizes(width: int, height: int, has_mips: bool, block_size: int):
    while True:
        yield width, height, max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * block_size
        if not has_mips or (width == 1 and height == 1):
            return
        width = max(1, width // 2)
        height = max(1, height // 2)


def _decode_dxt_payload(
    payload: bytes,
    encoding: int,
    width: int,
    height: int,
    has_mips: bool,
    rle_indicator: int,
) -> bytes:
    block_size = 8 if encoding == ENCODING_DXT1 else 16
    raw = _rle_decompress(
        payload,
        block_size,
        rle_indicator,
        tolerate_truncated_tail=True,
    )
    base_size = next(_mip_sizes(width, height, False, block_size))[2]
    if len(raw) < base_size:
        missing = base_size - len(raw)
        if missing > 4096:
            raise ValueError(
                f"DXT payload does not contain the base level: "
                f"raw={len(raw)} base={base_size}"
            )
        global _repair_count, _repair_bytes
        _repair_count += 1
        _repair_bytes += missing
        raw += bytes(missing)

    decoder = _decode_dxt1 if encoding == ENCODING_DXT1 else _decode_dxt5
    first_level = decoder(raw[:base_size], width, height)
    output = bytearray(first_level)
    if not has_mips:
        return bytes(output)

    mip = first_level
    mip_width, mip_height = width, height
    while mip_width > 1 or mip_height > 1:
        mip, mip_width, mip_height = _downsample_rgba(
            mip,
            mip_width,
            mip_height,
        )
        output.extend(mip)
    return bytes(output)


def _read_record(data: bytes, offset: int) -> tuple[tuple[int, int, int, int, int, int], bytes, int]:
    if offset < 0 or offset + 16 > len(data):
        raise ValueError(f"record offset out of range: {offset}")
    header = struct.unpack_from("<HHHHII", data, offset)
    _, _, _, _, stored_count, _ = header
    record_end = offset + 12 + stored_count
    if record_end > len(data):
        raise ValueError("record exceeds DAT size")
    return header, data[offset + 16 : record_end], record_end


def _pack_record(header: tuple[int, int, int, int, int, int], payload: bytes) -> bytes:
    name_hash, encoding, width, height_mask, _, _ = header
    return struct.pack(
        "<HHHHII",
        name_hash,
        encoding,
        width,
        height_mask,
        len(payload) + 4,
        0,
    ) + payload


def _convert_record(
    header: tuple[int, int, int, int, int, int],
    payload: bytes,
    output_encoding: int,
) -> bytes:
    name_hash, encoding, width, height_mask, _, rle_indicator = header
    if encoding not in (ENCODING_DXT1, ENCODING_DXT5):
        return _pack_record(header, _rle_decompress(payload, 4, rle_indicator)) if rle_indicator else _pack_record(header, payload)

    # In these Android databases the high height bit is set when the record
    # contains one level only. A clear high bit means the record contains the
    # full mip chain down to 1x1.
    has_mips = not bool(height_mask & 0x8000)
    rgba = _decode_dxt_payload(
        payload,
        encoding,
        width,
        height_mask & 0x7FFF,
        has_mips,
        rle_indicator,
    )
    converted_payload = (
        _pack_rgba4444(rgba)
        if output_encoding == ENCODING_RGBA4444
        else rgba
    )
    return _pack_record(
        (name_hash, output_encoding, width, height_mask, 0, 0),
        converted_payload,
    )


def _convert_database(folder: Path, output_folder: Path, name: str) -> tuple[int, int]:
    dat_path = folder / f"{name}.dxt.dat"
    toc_path = folder / f"{name}.dxt.toc"
    txt_path = folder / f"{name}.txt"
    if not dat_path.exists() or not toc_path.exists() or not txt_path.exists():
        raise FileNotFoundError(f"missing DXT database files for {name} in {folder}")

    dat = dat_path.read_bytes()
    toc = toc_path.read_bytes()
    if len(toc) < 4 or (len(toc) - 4) % 4:
        raise ValueError(f"invalid TOC: {toc_path}")
    count = (len(toc) - 4) // 4
    offsets = struct.unpack_from(f"<{count}i", toc, 4)

    new_offsets: list[int] = []
    converted = 0
    output_folder.mkdir(parents=True, exist_ok=True)
    # The TOC stores signed 32-bit offsets. gta_int is the only database
    # whose RGBA8888 representation exceeds that limit, so keep its alpha
    # with the supported 16-bit RGBA4444 UNC encoding.
    output_encoding = (
        ENCODING_RGBA4444 if folder.name == "gta_int" else ENCODING_RGBA8888
    )
    dat_output = output_folder / f"{name}.unc.dat"
    dat_partial = output_folder / f"{name}.unc.dat.partial"
    with dat_partial.open("wb") as output:
        for offset in offsets:
            if offset < 0:
                new_offsets.append(-1)
                continue
            header, payload, _ = _read_record(dat, offset)
            new_offsets.append(output.tell())
            output.write(_convert_record(header, payload, output_encoding))
            if header[1] in (ENCODING_DXT1, ENCODING_DXT5):
                converted += 1
    dat_partial.replace(dat_output)
    dat_size = dat_output.stat().st_size
    if dat_size > 0x7FFFFFFF:
        raise ValueError(
            f"{name}.unc.dat is {dat_size} bytes; the signed 32-bit TOC "
            "cannot address it"
        )
    (output_folder / f"{name}.unc.toc").write_bytes(
        struct.pack("<I", dat_size)
        + struct.pack(f"<{count}i", *new_offsets)
    )
    shutil.copy2(txt_path, output_folder / f"{name}.txt")

    tmb_path = folder / f"{name}.dxt.tmb"
    if tmb_path.exists():
        tmb = tmb_path.read_bytes()
        tmb_output = bytearray()
        cursor = 0
        while cursor < len(tmb):
            header, payload, end = _read_record(tmb, cursor)
            tmb_output.extend(_convert_record(header, payload, output_encoding))
            cursor = end
        (output_folder / f"{name}.unc.tmb").write_bytes(tmb_output)
    return len(offsets), converted


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="directory containing DXT database folders")
    parser.add_argument("output", type=Path, help="directory for UNC database folders")
    args = parser.parse_args()

    total_records = 0
    total_converted = 0
    for folder in sorted(args.input.iterdir()):
        if not folder.is_dir():
            continue
        databases = sorted(folder.glob("*.dxt.dat"))
        if not databases:
            continue
        name = databases[0].name.removesuffix(".dxt.dat")
        records, converted = _convert_database(
            folder,
            args.output / folder.name,
            name,
        )
        total_records += records
        total_converted += converted
        print(f"{folder.name}/{name}: {converted}/{records} DXT records converted")
    repair_note = (
        f"; padded {_repair_bytes} bytes across {_repair_count} truncated DXT base levels"
        if _repair_count
        else ""
    )
    print(
        f"Converted {total_converted} DXT records across {total_records} "
        f"database entries{repair_note}"
    )


if __name__ == "__main__":
    main()