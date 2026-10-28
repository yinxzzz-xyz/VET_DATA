"""Object-level BLF window copy.

Python-can decodes every BLF object into a ``Message`` and re-encodes it when a
window is cut.  That round trip costs several microseconds per message.  This
module copies the already-compressed object bytes instead: containers are
inflated, object headers are inspected for the timestamp, and the objects
inside the window are copied verbatim into fresh containers.

The fast path is intentionally conservative: any unsupported container layout,
unknown object header version or damaged object makes it raise
:class:`RawCopyUnavailable` so the caller can fall back to python-can.
"""

from __future__ import annotations

import os
import struct
import zlib
from typing import Callable

from .data_cutout import CutoutCancelled


class RawCopyUnavailable(Exception):
    """Raised when the file layout cannot be copied object-for-object."""


_MAX_CONTAINER_SIZE = 128 * 1024
_PROGRESS_CONTAINER_INTERVAL = 16
# Same safety margin as the python-can path: BLF objects are written in capture
# order, and adjacent channels may interleave by a small amount.
_EARLY_STOP_MARGIN_SECONDS = 1.0


def _layout():
    """Return python-can's BLF layout constants, or None when unavailable."""
    try:
        from can.io import blf
    except Exception:  # pragma: no cover - python-can is a hard dependency
        return None
    required = (
        "FILE_HEADER_STRUCT",
        "FILE_HEADER_SIZE",
        "OBJ_HEADER_BASE_STRUCT",
        "OBJ_HEADER_V1_STRUCT",
        "OBJ_HEADER_V2_STRUCT",
        "LOG_CONTAINER_STRUCT",
        "LOG_CONTAINER",
        "ZLIB_DEFLATE",
        "NO_COMPRESSION",
        "TIME_ONE_NANS_FACTOR",
        "TIME_TEN_MICS_FACTOR",
        "CAN_MESSAGE",
        "CAN_MESSAGE2",
        "CAN_ERROR_EXT",
        "CAN_FD_MESSAGE",
        "CAN_FD_MESSAGE_64",
        "systemtime_to_timestamp",
        "timestamp_to_systemtime",
    )
    if not all(hasattr(blf, name) for name in required):
        return None
    return blf


def is_supported() -> bool:
    return _layout() is not None


def _message_types(blf) -> frozenset:
    return frozenset(
        {
            blf.CAN_MESSAGE,
            blf.CAN_MESSAGE2,
            blf.CAN_ERROR_EXT,
            blf.CAN_FD_MESSAGE,
            blf.CAN_FD_MESSAGE_64,
        }
    )


def copy_blf_window(
    source_path: str | os.PathLike,
    output_path: str | os.PathLike,
    window,
    *,
    mapping=None,
    cancel_event=None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[int, int, tuple]:
    """Copy the messages of ``window`` (relative seconds) into a new BLF.

    Returns ``(message_count, scanned_message_count, bus_counts)``.  Raises
    :class:`RawCopyUnavailable` when the layout is not supported.
    """
    blf = _layout()
    if blf is None:
        raise RawCopyUnavailable("BLF 布局常量不可用")
    message_types = _message_types(blf)
    header_struct = blf.FILE_HEADER_STRUCT
    base_struct = blf.OBJ_HEADER_BASE_STRUCT
    base_size = base_struct.size
    v1_struct = blf.OBJ_HEADER_V1_STRUCT
    v2_struct = blf.OBJ_HEADER_V2_STRUCT
    container_struct = blf.LOG_CONTAINER_STRUCT
    raw_to_bus = getattr(mapping, "raw_to_bus", None) if mapping is not None else None
    start_relative = float(window.start)
    stop_relative = float(window.stop)
    early_stop = stop_relative + _EARLY_STOP_MARGIN_SECONDS
    file_size = os.path.getsize(source_path)

    written = 0
    scanned = 0
    carry = b""
    pending = bytearray()
    uncompressed_size = blf.FILE_HEADER_SIZE
    last_ns = None
    time_base_ns = None
    last_absolute = None
    saw_inversion = False
    stop_scanning = False
    containers = 0
    bus_counts: dict[int, int] = {}
    view = None

    # Validate the header before creating the output so an unsupported file
    # never leaves an empty result behind.
    with open(source_path, "rb") as probe:
        header_bytes = probe.read(blf.FILE_HEADER_SIZE)
    if len(header_bytes) < blf.FILE_HEADER_SIZE:
        raise RawCopyUnavailable("文件头不完整")
    header = header_struct.unpack_from(header_bytes)
    if header[0] != b"LOGG":
        raise RawCopyUnavailable("不是 BLF 文件")
    start_timestamp = blf.systemtime_to_timestamp(header[14:22])
    if not start_timestamp:
        raise RawCopyUnavailable("无法读取 BLF 起始时间")
    start_ns = int(round(start_timestamp * 1000)) * 1_000_000

    with open(source_path, "rb") as source, open(output_path, "wb") as target:
        source.seek(blf.FILE_HEADER_SIZE)
        target.write(header_bytes)
        while not stop_scanning:
            if cancel_event is not None and cancel_event.is_set():
                raise CutoutCancelled()
            base = source.read(base_size)
            if not base:
                break
            if len(base) < base_size:
                raise RawCopyUnavailable("对象头不完整")
            signature, header_size, _version, object_size, object_type = base_struct.unpack(base)
            if signature != b"LOBJ" or object_type != blf.LOG_CONTAINER:
                raise RawCopyUnavailable("不支持的顶层对象")
            if object_size < base_size or header_size < base_size:
                raise RawCopyUnavailable("无效的对象尺寸")
            rest = source.read(object_size - base_size)
            if len(rest) != object_size - base_size:
                raise RawCopyUnavailable("容器被截断")
            padding = object_size % 4
            if padding and len(source.read(padding)) != padding:
                raise RawCopyUnavailable("容器填充不完整")

            method, uncompressed = container_struct.unpack_from(rest)
            payload = rest[container_struct.size:]
            if method == blf.NO_COMPRESSION:
                data = payload
            elif method == blf.ZLIB_DEFLATE:
                data = zlib.decompress(payload)
            else:
                raise RawCopyUnavailable("不支持的压缩方法")
            if len(data) != uncompressed:
                raise RawCopyUnavailable("容器尺寸不一致")

            data = carry + data
            view = memoryview(data)
            position = 0
            containers += 1
            while position + base_size <= len(data):
                obj_sig, obj_header_size, obj_version, obj_size, obj_type = base_struct.unpack_from(data, position)
                if obj_sig != b"LOBJ" or obj_header_size < base_size:
                    raise RawCopyUnavailable("无效的对象")
                next_position = position + obj_size
                if next_position > len(data):
                    break
                if obj_version == 1:
                    flags, _, _, raw_timestamp = v1_struct.unpack_from(data, position + base_size)
                elif obj_version == 2:
                    flags, _, _, raw_timestamp = v2_struct.unpack_from(data, position + base_size)
                else:
                    raise RawCopyUnavailable("未知的对象头版本")
                unit_ns = 10_000 if flags == 1 else 1
                absolute_ns = start_ns + raw_timestamp * unit_ns
                if last_ns is not None and absolute_ns < last_ns:
                    saw_inversion = True
                last_ns = absolute_ns
                if obj_type in message_types:
                    if time_base_ns is None:
                        # Same time base as parsing/plotting: the first message.
                        time_base_ns = absolute_ns
                    relative = (absolute_ns - time_base_ns) / 1_000_000_000
                    scanned += 1
                    if relative > early_stop and not saw_inversion:
                        stop_scanning = True
                        break
                    if start_relative <= relative <= stop_relative:
                        payload_size = obj_size - obj_header_size
                        pending.extend(view[position:next_position])
                        if payload_size % 4:
                            pending += b"\x00" * (payload_size % 4)
                        written += 1
                        if payload_size >= 1:
                            # BLF stores the channel number plus one; python-can
                            # subtracts it again and normalizes invalid values.
                            raw_channel = data[position + obj_header_size] - 1
                            if raw_channel < 0:
                                raw_channel = 1
                            bus_id = (
                                raw_to_bus.get(raw_channel, raw_channel)
                                if raw_to_bus is not None else raw_channel
                            )
                            bus_counts[bus_id] = bus_counts.get(bus_id, 0) + 1
                        last_absolute = start_timestamp + (absolute_ns - start_ns) / 1_000_000_000
                position = next_position
            carry = b"" if stop_scanning else data[position:]

            while len(pending) >= _MAX_CONTAINER_SIZE:
                chunk = bytes(pending[:_MAX_CONTAINER_SIZE])
                del pending[:_MAX_CONTAINER_SIZE]
                _write_container(target, blf, chunk)
                uncompressed_size += 16 + 16 + len(chunk)

            if progress_callback is not None and containers % _PROGRESS_CONTAINER_INTERVAL == 0:
                progress_callback(scanned, written)
            if cancel_event is not None and cancel_event.is_set():
                raise CutoutCancelled()

        if carry:
            # A trailing partial object means the source itself is truncated.
            raise RawCopyUnavailable("文件末尾存在不完整的对象")

        if pending:
            chunk = bytes(pending)
            pending.clear()
            _write_container(target, blf, chunk)
            uncompressed_size += 16 + 16 + len(chunk)

        if written == 0:
            raise ValueError(f"指定时间范围内没有找到任何报文 (总报文数: {scanned})")

        end_position = target.tell()
        updated = bytearray(header_bytes)
        struct.pack_into("<Q", updated, 16, end_position)
        struct.pack_into("<Q", updated, 24, uncompressed_size)
        struct.pack_into("<L", updated, 32, written)
        struct.pack_into("<L", updated, 36, 0)
        stop_systemtime = blf.timestamp_to_systemtime(last_absolute)
        if not any(stop_systemtime):
            raise RawCopyUnavailable("无法写入结束时间")
        struct.pack_into("<8H", updated, 56, *stop_systemtime)
        target.seek(0)
        target.write(updated)
        target.flush()

    if progress_callback is not None:
        progress_callback(scanned, written)
    return written, scanned, tuple(sorted(bus_counts.items()))


def _write_container(target, blf, data: bytes) -> None:
    compressed = zlib.compress(data, 1)
    object_size = 16 + 16 + len(compressed)
    target.write(blf.OBJ_HEADER_BASE_STRUCT.pack(b"LOBJ", 16, 1, object_size, blf.LOG_CONTAINER))
    target.write(blf.LOG_CONTAINER_STRUCT.pack(blf.ZLIB_DEFLATE, len(data)))
    target.write(compressed)
    if object_size % 4:
        target.write(b"\x00" * (object_size % 4))


__all__ = ["RawCopyUnavailable", "copy_blf_window", "is_supported"]
