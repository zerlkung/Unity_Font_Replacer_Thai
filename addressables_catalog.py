"""addressables_catalog.py
Python port of nesrak1/AddressablesTools (https://github.com/nesrak1/AddressablesTools)
Original C# library by nesrak1, MIT License.

Reads and writes Unity Addressables catalog files:
  - catalog.json  (JSON format, all Addressables versions)
  - catalog.bin   (binary format, Addressables 1.x+)
  - catalog.bundle (UnityFS bundle wrapping either of the above)

Main entry points
-----------------
  read_catalog(path)            -> ContentCatalogData
  patch_crc(catalog)            -> None  (zeros out all bundle CRCs in-place)
  find_resources(catalog, pat)  -> list[ResourceLocation]
  find_font_resources(catalog)  -> list[ResourceLocation]
  write_catalog_json(catalog, path) -> None
  print_catalog_summary(catalog)    -> None
"""

from __future__ import annotations

import base64
import io
import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BINARY_MAGIC_LE = 0x0DE38942
_BINARY_MAGIC_BE = 0x4289E30D  # big-endian, not supported

_ABRO_MATCHNAME = "Unity.ResourceManager; UnityEngine.ResourceManagement.ResourceProviders.AssetBundleRequestOptions"
_INT_MATCHNAME   = "mscorlib; System.Int32"
_LONG_MATCHNAME  = "mscorlib; System.Int64"
_BOOL_MATCHNAME  = "mscorlib; System.Boolean"
_STR_MATCHNAME   = "mscorlib; System.String"
_HASH_MATCHNAME  = "UnityEngine.CoreModule; UnityEngine.Hash128"

_FONT_PROVIDER_PATTERNS = re.compile(
    r"font|tmp|textmesh|TextMeshPro|FontAsset", re.IGNORECASE
)
_FONT_ID_PATTERNS = re.compile(
    r"\.ttf$|\.otf$|SDF$| SDF$|FontAsset|font", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SerializedType:
    assembly_name: str = ""
    class_name: str = ""

    @property
    def match_name(self) -> str:
        short = self.assembly_name.split(",")[0] if self.assembly_name else ""
        return f"{short}; {self.class_name}"


@dataclass
class CommonInfo:
    version: int = 3
    timeout: int = 0
    redirect_limit: int = -1
    retry_count: int = 0
    chunked_transfer: bool = False
    asset_load_mode: int = 0
    use_crc_for_cached_bundle: bool = False
    use_uwr_for_local_bundles: bool = False
    clear_other_cached_versions: bool = False


@dataclass
class AssetBundleRequestOptions:
    hash: str = ""
    crc: int = 0
    bundle_name: str = ""
    bundle_size: int = 0
    com_info: CommonInfo = field(default_factory=CommonInfo)


@dataclass
class ResourceLocation:
    internal_id: str = ""
    provider_id: str = ""
    dependency_key: Any = None
    dependencies: list = field(default_factory=list)
    data: Any = None
    hash_code: int = 0
    dependency_hash_code: int = 0
    primary_key: str = ""
    resource_type: Optional[SerializedType] = None

    def __hash__(self):
        return id(self)

    def __eq__(self, other):
        return self is other


@dataclass
class ObjectInitializationData:
    id: str = ""
    object_type: Optional[SerializedType] = None
    data: str = ""


@dataclass
class ContentCatalogData:
    version: int = 2
    locator_id: str = ""
    build_result_hash: str = ""
    instance_provider: Optional[ObjectInitializationData] = None
    scene_provider: Optional[ObjectInitializationData] = None
    resource_providers: list = field(default_factory=list)
    resources: dict = field(default_factory=dict)
    # internal json state for round-trip writing
    _provider_ids: list = field(default_factory=list, repr=False)
    _internal_ids: list = field(default_factory=list, repr=False)
    _resource_types: list = field(default_factory=list, repr=False)
    _internal_id_prefixes: Optional[list] = field(default=None, repr=False)
    _write_compact: bool = field(default=False, repr=False)


# ---------------------------------------------------------------------------
# Binary reader helpers
# ---------------------------------------------------------------------------

class _BinReader:
    """Wraps a bytes buffer with offset-cached reads (mirrors CatalogBinaryReader)."""

    def __init__(self, data: bytes):
        self._data = data
        self._cache: dict[int, Any] = {}
        self.version = 1

    # --- primitive reads at absolute position ---
    def u8(self, pos: int) -> int:
        return self._data[pos]

    def u16(self, pos: int) -> int:
        return struct.unpack_from("<H", self._data, pos)[0]

    def i32(self, pos: int) -> int:
        return struct.unpack_from("<i", self._data, pos)[0]

    def u32(self, pos: int) -> int:
        return struct.unpack_from("<I", self._data, pos)[0]

    def i64(self, pos: int) -> int:
        return struct.unpack_from("<q", self._data, pos)[0]

    def u64(self, pos: int) -> int:
        return struct.unpack_from("<Q", self._data, pos)[0]

    def bytes_at(self, pos: int, n: int) -> bytes:
        return self._data[pos:pos + n]

    # --- encoded string ---
    def read_encoded_string(self, encoded_offset: int, dyn_sep: str = "\0") -> Optional[str]:
        if encoded_offset == 0xFFFFFFFF:
            return None

        cache_key = encoded_offset
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            if isinstance(cached, str) or cached is None:
                return cached

        is_unicode = bool(encoded_offset & 0x80000000)
        is_dynamic = bool(encoded_offset & 0x40000000) and dyn_sep != "\0"
        offset = encoded_offset & 0x3FFFFFFF

        if not is_dynamic:
            result = self._read_basic_string(offset, is_unicode)
        else:
            result = self._read_dynamic_string(offset, is_unicode, dyn_sep)

        self._cache[cache_key] = result
        return result

    def _read_basic_string(self, offset: int, unicode: bool) -> str:
        length = self.i32(offset - 4)
        raw = self.bytes_at(offset, length)
        return raw.decode("utf-16-le" if unicode else "ascii")

    def _read_dynamic_string(self, offset: int, unicode: bool, sep: str) -> str:
        parts: list[str] = []
        pos = offset
        while True:
            part_str_offset = self.u32(pos)
            next_part_offset = self.u32(pos + 4)
            parts.append(self.read_encoded_string(part_str_offset))
            if next_part_offset == 0xFFFFFFFF:
                break
            pos = next_part_offset

        if len(parts) == 1:
            return parts[0]
        if self.version > 1:
            return sep.join(reversed(parts))
        return sep.join(parts)

    # --- offset array ---
    def read_offset_array(self, encoded_offset: int) -> list[int]:
        if encoded_offset == 0xFFFFFFFF:
            return []

        if encoded_offset in self._cache:
            cached = self._cache[encoded_offset]
            if isinstance(cached, list):
                return cached

        byte_size = self.i32(encoded_offset - 4)
        count = byte_size // 4
        result = list(struct.unpack_from(f"<{count}I", self._data, encoded_offset))
        self._cache[encoded_offset] = result
        return result

    # --- custom cached read ---
    def read_custom(self, offset: int, factory):
        if offset in self._cache:
            return self._cache[offset]
        obj = factory()
        self._cache[offset] = obj
        return obj


# ---------------------------------------------------------------------------
# SerializedObjectDecoder  (V1 = JSON blob format, V2 = binary format)
# ---------------------------------------------------------------------------

def _decode_v1(br: io.RawIOBase) -> Any:
    """Decode a single object from the JSON-blob binary stream (V1 encoding)."""
    type_byte = _read_byte(br)

    if type_byte == 0:   # AsciiString
        return _read_str4(br, "ascii")
    elif type_byte == 1:  # UnicodeString
        return _read_str4(br, "utf-16-le")
    elif type_byte == 2:  # UInt16
        return struct.unpack("<H", br.read(2))[0]
    elif type_byte == 3:  # UInt32
        return struct.unpack("<I", br.read(4))[0]
    elif type_byte == 4:  # Int32
        return struct.unpack("<i", br.read(4))[0]
    elif type_byte == 5:  # Hash128
        length = _read_byte(br)
        return br.read(length).decode("ascii")
    elif type_byte == 6:  # TypeReference
        length = _read_byte(br)
        return ("__typeref__", br.read(length).decode("ascii"))
    elif type_byte == 7:  # JsonObject
        asm_len = _read_byte(br)
        asm_name = br.read(asm_len).decode("ascii")
        cls_len = _read_byte(br)
        cls_name = br.read(cls_len).decode("ascii")
        json_text = _read_str4(br, "utf-16-le")
        match = f"{asm_name.split(',')[0]}; {cls_name}"
        if match == _ABRO_MATCHNAME:
            return _parse_abro_json(json_text)
        return ("__json__", asm_name, cls_name, json_text)
    return None


def _encode_v1(bw: io.RawIOBase, obj: Any) -> None:
    """Encode a single object to the JSON-blob binary stream (V1 encoding)."""
    if isinstance(obj, str):
        try:
            encoded = obj.encode("ascii")
            bw.write(bytes([0]))  # AsciiString
            bw.write(struct.pack("<i", len(encoded)))
            bw.write(encoded)
        except UnicodeEncodeError:
            encoded = obj.encode("utf-16-le")
            bw.write(bytes([1]))  # UnicodeString
            bw.write(struct.pack("<i", len(encoded)))
            bw.write(encoded)
    elif isinstance(obj, bool):
        raise TypeError("bool not directly supported in V1; wrap in int")
    elif isinstance(obj, int):
        if 0 <= obj <= 0xFFFF:
            bw.write(bytes([3]))  # UInt32
            bw.write(struct.pack("<I", obj))
        else:
            bw.write(bytes([4]))  # Int32
            bw.write(struct.pack("<i", obj))
    elif isinstance(obj, AssetBundleRequestOptions):
        abro_json = _abro_to_json_text(obj)
        asm = "Unity.ResourceManager, Version=0.0.0.0, Culture=neutral, PublicKeyToken=null"
        cls = "UnityEngine.ResourceManagement.ResourceProviders.AssetBundleRequestOptions"
        asm_b = asm.encode("ascii")
        cls_b = cls.encode("ascii")
        json_b = abro_json.encode("utf-16-le")
        bw.write(bytes([7]))  # JsonObject
        bw.write(bytes([len(asm_b)])); bw.write(asm_b)
        bw.write(bytes([len(cls_b)])); bw.write(cls_b)
        bw.write(struct.pack("<i", len(json_b))); bw.write(json_b)
    elif isinstance(obj, tuple) and obj[0] == "__json__":
        _, asm_name, cls_name, json_text = obj
        asm_b = asm_name.encode("ascii")
        cls_b = cls_name.encode("ascii")
        json_b = json_text.encode("utf-16-le")
        bw.write(bytes([7]))
        bw.write(bytes([len(asm_b)])); bw.write(asm_b)
        bw.write(bytes([len(cls_b)])); bw.write(cls_b)
        bw.write(struct.pack("<i", len(json_b))); bw.write(json_b)
    elif isinstance(obj, tuple) and obj[0] == "__typeref__":
        ref_b = obj[1].encode("ascii")
        bw.write(bytes([6]))
        bw.write(bytes([len(ref_b)])); bw.write(ref_b)
    else:
        raise TypeError(f"Unsupported type for V1 encode: {type(obj)}")


def _decode_v2(reader: _BinReader, offset: int) -> Any:
    """Decode a single object from the binary catalog (V2 encoding)."""
    if offset == 0xFFFFFFFF:
        return None

    type_name_offset = reader.u32(offset)
    object_offset = reader.u32(offset + 4)
    is_default = object_offset == 0xFFFFFFFF

    asm_offset = reader.u32(type_name_offset)
    cls_offset = reader.u32(type_name_offset + 4)
    asm_name = reader.read_encoded_string(asm_offset, ".")
    cls_name = reader.read_encoded_string(cls_offset, ".")
    match = f"{asm_name.split(',')[0]}; {cls_name}" if asm_name else cls_name

    if match == _INT_MATCHNAME:
        return 0 if is_default else reader.i32(object_offset)
    elif match == _LONG_MATCHNAME:
        return 0 if is_default else reader.i64(object_offset)
    elif match == _BOOL_MATCHNAME:
        return False if is_default else bool(reader.u8(object_offset))
    elif match == _STR_MATCHNAME:
        if is_default:
            return ""
        str_offset = reader.u32(object_offset)
        sep_byte = reader.u8(object_offset + 4)
        sep = chr(sep_byte) if sep_byte else "\0"
        return reader.read_encoded_string(str_offset, sep)
    elif match == _HASH_MATCHNAME:
        if is_default:
            return ""
        v0, v1, v2, v3 = struct.unpack_from("<4I", reader._data, object_offset)
        raw = struct.pack(">4I", v0, v1, v2, v3)
        return raw.hex()
    elif match == _ABRO_MATCHNAME:
        if is_default:
            return None
        return reader.read_custom(object_offset, lambda: _read_abro_binary(reader, object_offset))
    else:
        return None  # unknown types ignored gracefully


# ---------------------------------------------------------------------------
# AssetBundleRequestOptions helpers
# ---------------------------------------------------------------------------

def _parse_abro_json(json_text: str) -> AssetBundleRequestOptions:
    d = json.loads(json_text)
    ci = CommonInfo(
        version=3,
        timeout=d.get("m_Timeout", 0),
        redirect_limit=d.get("m_RedirectLimit", -1),
        retry_count=d.get("m_RetryCount", 0),
        chunked_transfer=d.get("m_ChunkedTransfer", False),
        asset_load_mode=d.get("m_AssetLoadMode", 0),
        use_crc_for_cached_bundle=d.get("m_UseCrcForCachedBundles", False),
        use_uwr_for_local_bundles=d.get("m_UseUWRForLocalBundles", False),
        clear_other_cached_versions=d.get("m_ClearOtherCachedVersionsWhenLoaded", False),
    )
    return AssetBundleRequestOptions(
        hash=d.get("m_Hash", ""),
        crc=d.get("m_Crc", 0),
        bundle_name=d.get("m_BundleName", ""),
        bundle_size=d.get("m_BundleSize", 0),
        com_info=ci,
    )


def _abro_to_json_text(abro: AssetBundleRequestOptions) -> str:
    ci = abro.com_info
    d: dict = {
        "m_Hash": abro.hash,
        "m_Crc": abro.crc,
        "m_Timeout": ci.timeout,
        "m_RedirectLimit": ci.redirect_limit,
        "m_RetryCount": ci.retry_count,
        "m_BundleName": abro.bundle_name,
        "m_BundleSize": abro.bundle_size,
    }
    if ci.version > 1:
        d["m_ChunkedTransfer"] = ci.chunked_transfer
    if ci.version > 2:
        d["m_AssetLoadMode"] = ci.asset_load_mode
        d["m_UseCrcForCachedBundles"] = ci.use_crc_for_cached_bundle
        d["m_UseUWRForLocalBundles"] = ci.use_uwr_for_local_bundles
        d["m_ClearOtherCachedVersionsWhenLoaded"] = ci.clear_other_cached_versions
    return json.dumps(d, separators=(",", ":"), ensure_ascii=False)


def _read_abro_binary(reader: _BinReader, offset: int) -> AssetBundleRequestOptions:
    hash_offset     = reader.u32(offset)
    bundle_name_off = reader.u32(offset + 4)
    crc             = reader.u32(offset + 8)
    bundle_size     = reader.u32(offset + 12)
    common_info_off = reader.u32(offset + 16)

    v0, v1, v2, v3 = struct.unpack_from("<4I", reader._data, hash_offset)
    hash_str = struct.pack(">4I", v0, v1, v2, v3).hex()

    bundle_name = reader.read_encoded_string(bundle_name_off, "_") or ""

    ci = _read_common_info_binary(reader, common_info_off)

    return AssetBundleRequestOptions(
        hash=hash_str,
        crc=crc,
        bundle_name=bundle_name,
        bundle_size=bundle_size,
        com_info=ci,
    )


def _read_common_info_binary(reader: _BinReader, offset: int) -> CommonInfo:
    # CommonInfo binary layout (version 3 assumed):
    # i16 timeout, u8 redirectLimit, u8 retryCount,
    # bool chunkedTransfer, i32 assetLoadMode, bool useCrcForCached,
    # bool useUWR, bool clearOtherCached
    timeout        = struct.unpack_from("<h", reader._data, offset)[0]
    redirect_limit = reader.u8(offset + 2)
    retry_count    = reader.u8(offset + 3)
    chunked        = bool(reader.u8(offset + 4))
    load_mode      = reader.i32(offset + 5)
    use_crc        = bool(reader.u8(offset + 9))
    use_uwr        = bool(reader.u8(offset + 10))
    clear_other    = bool(reader.u8(offset + 11))
    return CommonInfo(
        version=3,
        timeout=timeout,
        redirect_limit=redirect_limit,
        retry_count=retry_count,
        chunked_transfer=chunked,
        asset_load_mode=load_mode,
        use_crc_for_cached_bundle=use_crc,
        use_uwr_for_local_bundles=use_uwr,
        clear_other_cached_versions=clear_other,
    )


# ---------------------------------------------------------------------------
# Binary catalog reader
# ---------------------------------------------------------------------------

def _read_binary(data: bytes) -> ContentCatalogData:
    r = _BinReader(data)

    magic   = r.i32(0)
    version = r.i32(4)
    if magic != _BINARY_MAGIC_LE:
        raise ValueError(f"Unknown binary magic: 0x{magic:08X}")
    if version not in (1, 2):
        raise ValueError(f"Unsupported binary version: {version}")

    r.version = version

    keys_offset              = r.u32(8)
    id_offset                = r.u32(12)
    instance_provider_offset = r.u32(16)
    scene_provider_offset    = r.u32(20)
    init_objects_offset      = r.u32(24)

    # version 1.1 header is shorter (no BuildResultHash field)
    if version == 1 and keys_offset == 0x20:
        build_result_hash_offset = 0xFFFFFFFF
    else:
        build_result_hash_offset = r.u32(28)

    ccd = ContentCatalogData(version=version)
    ccd.locator_id        = r.read_encoded_string(id_offset) or ""
    ccd.build_result_hash = r.read_encoded_string(build_result_hash_offset) or ""

    ccd.instance_provider = _read_oid_binary(r, instance_provider_offset)
    ccd.scene_provider    = _read_oid_binary(r, scene_provider_offset)

    init_offsets = r.read_offset_array(init_objects_offset)
    ccd.resource_providers = [_read_oid_binary(r, off) for off in init_offsets]

    # Resources: keys_offset → [keyOff, locListOff, keyOff, locListOff, ...]
    key_loc_offsets = r.read_offset_array(keys_offset)
    resources: dict[Any, list[ResourceLocation]] = {}
    for i in range(0, len(key_loc_offsets), 2):
        key_off = key_loc_offsets[i]
        loc_list_off = key_loc_offsets[i + 1]
        key = _decode_v2(r, key_off)
        loc_offsets = r.read_offset_array(loc_list_off)
        locations = [_read_resource_location_binary(r, lo) for lo in loc_offsets]
        resources[key] = locations

    ccd.resources = resources
    return ccd


def _read_oid_binary(r: _BinReader, offset: int) -> ObjectInitializationData:
    id_off   = r.u32(offset)
    type_off = r.u32(offset + 4)
    data_off = r.u32(offset + 8)

    asm_off = r.u32(type_off)
    cls_off = r.u32(type_off + 4)

    st = SerializedType(
        assembly_name=r.read_encoded_string(asm_off, ".") or "",
        class_name=r.read_encoded_string(cls_off, ".") or "",
    )
    return ObjectInitializationData(
        id=r.read_encoded_string(id_off) or "",
        object_type=st,
        data=r.read_encoded_string(data_off) or "",
    )


def _read_resource_location_binary(r: _BinReader, offset: int) -> ResourceLocation:
    primary_key_off  = r.u32(offset)
    internal_id_off  = r.u32(offset + 4)
    provider_id_off  = r.u32(offset + 8)
    deps_off         = r.u32(offset + 12)
    dep_hash         = r.i32(offset + 16)
    data_off         = r.u32(offset + 20)
    type_off         = r.u32(offset + 24)

    primary_key = r.read_encoded_string(primary_key_off, "/") or ""
    internal_id = r.read_encoded_string(internal_id_off, "/") or ""
    provider_id = r.read_encoded_string(provider_id_off, ".") or ""

    dep_offsets = r.read_offset_array(deps_off)

    def _make_dep(lo=None):
        loc = _read_resource_location_binary(r, lo)
        return loc

    deps = [r.read_custom(lo, lambda lo=lo: _make_dep(lo)) for lo in dep_offsets]

    data = _decode_v2(r, data_off)

    asm_off2 = r.u32(type_off)
    cls_off2 = r.u32(type_off + 4)
    res_type = SerializedType(
        assembly_name=r.read_encoded_string(asm_off2, ".") or "",
        class_name=r.read_encoded_string(cls_off2, ".") or "",
    )

    loc = ResourceLocation(
        primary_key=primary_key,
        internal_id=internal_id,
        provider_id=provider_id,
        dependencies=deps,
        dependency_hash_code=dep_hash,
        data=data,
        resource_type=res_type,
    )
    loc.hash_code = hash(internal_id) * 31 + hash(provider_id)
    return loc


# ---------------------------------------------------------------------------
# JSON catalog reader
# ---------------------------------------------------------------------------

def _read_json(json_text: str) -> ContentCatalogData:
    d = json.loads(json_text)

    ccd = ContentCatalogData()
    ccd.locator_id        = d.get("m_LocatorId", "")
    ccd.build_result_hash = d.get("m_BuildResultHash", "")

    ccd.instance_provider = _read_oid_json(d.get("m_InstanceProviderData", {}))
    ccd.scene_provider    = _read_oid_json(d.get("m_SceneProviderData", {}))
    ccd.resource_providers = [_read_oid_json(x) for x in d.get("m_ResourceProviderData", [])]

    provider_ids  = d.get("m_ProviderIds", [])
    internal_ids  = d.get("m_InternalIds", [])
    resource_types_raw = d.get("m_resourceTypes", [])
    prefixes      = d.get("m_InternalIdPrefixes")
    old_keys      = d.get("m_Keys")

    resource_types = [
        SerializedType(
            assembly_name=rt.get("m_AssemblyName", ""),
            class_name=rt.get("m_ClassName", ""),
        )
        for rt in resource_types_raw
    ]

    ccd._provider_ids         = provider_ids
    ccd._internal_ids         = internal_ids
    ccd._resource_types       = resource_types
    ccd._internal_id_prefixes = prefixes
    ccd._write_compact        = bool(prefixes)

    # Decode base64 blobs
    bucket_data = base64.b64decode(d["m_BucketDataString"])
    key_data    = base64.b64decode(d["m_KeyDataString"])
    entry_data  = base64.b64decode(d["m_EntryDataString"])
    extra_data  = base64.b64decode(d["m_ExtraDataString"])

    # --- parse buckets ---
    buckets: list[tuple[int, list[int]]] = []
    with io.BytesIO(bucket_data) as bs:
        bucket_count = struct.unpack("<i", bs.read(4))[0]
        for _ in range(bucket_count):
            offset = struct.unpack("<i", bs.read(4))[0]
            entry_count = struct.unpack("<i", bs.read(4))[0]
            entries = list(struct.unpack(f"<{entry_count}i", bs.read(4 * entry_count)))
            buckets.append((offset, entries))

    # --- parse keys ---
    keys: list[Any] = []
    with io.BytesIO(key_data) as ks:
        key_count = struct.unpack("<i", ks.read(4))[0]
        for i in range(key_count):
            ks.seek(buckets[i][0])
            keys.append(_decode_v1(ks))

    # --- parse entries ---
    locations: list[ResourceLocation] = []
    with io.BytesIO(entry_data) as es, io.BytesIO(extra_data) as xs:
        entry_count = struct.unpack("<i", es.read(4))[0]
        for _ in range(entry_count):
            iid_idx      = struct.unpack("<i", es.read(4))[0]
            prov_idx     = struct.unpack("<i", es.read(4))[0]
            dep_key_idx  = struct.unpack("<i", es.read(4))[0]
            dep_hash     = struct.unpack("<i", es.read(4))[0]
            data_idx     = struct.unpack("<i", es.read(4))[0]
            pkey_idx     = struct.unpack("<i", es.read(4))[0]
            rtype_idx    = struct.unpack("<i", es.read(4))[0]

            internal_id = internal_ids[iid_idx]
            # expand prefix-compressed IDs
            if prefixes:
                hash_pos = internal_id.find("#")
                if hash_pos != -1:
                    try:
                        prefix_idx = int(internal_id[:hash_pos])
                        internal_id = prefixes[prefix_idx] + internal_id[hash_pos + 1:]
                    except ValueError:
                        pass

            provider_id  = provider_ids[prov_idx]
            dep_key      = keys[dep_key_idx] if dep_key_idx >= 0 else None
            primary_key  = (Keys[pkey_idx] if old_keys else keys[pkey_idx]) if True else keys[pkey_idx]
            if old_keys:
                primary_key = old_keys[pkey_idx]
            else:
                primary_key = keys[pkey_idx]

            obj_data = None
            if data_idx >= 0:
                xs.seek(data_idx)
                obj_data = _decode_v1(xs)

            res_type = resource_types[rtype_idx] if rtype_idx < len(resource_types) else None

            loc = ResourceLocation(
                internal_id=internal_id,
                provider_id=provider_id,
                dependency_key=dep_key,
                dependency_hash_code=dep_hash,
                data=obj_data,
                primary_key=str(primary_key) if primary_key is not None else "",
                resource_type=res_type,
            )
            loc.hash_code = hash(internal_id) * 31 + hash(provider_id)
            locations.append(loc)

    # --- build resources dict ---
    resources: dict[Any, list[ResourceLocation]] = {}
    for i, (_, entries) in enumerate(buckets):
        resources[keys[i]] = [locations[e] for e in entries]

    ccd.resources = resources
    return ccd


def _read_oid_json(d: dict) -> ObjectInitializationData:
    if not d:
        return ObjectInitializationData()
    ot_raw = d.get("m_ObjectType", {})
    return ObjectInitializationData(
        id=d.get("m_Id", ""),
        object_type=SerializedType(
            assembly_name=ot_raw.get("m_AssemblyName", ""),
            class_name=ot_raw.get("m_ClassName", ""),
        ),
        data=d.get("m_Data", ""),
    )


# ---------------------------------------------------------------------------
# Bundle reader (via UnityPy)
# ---------------------------------------------------------------------------

def _read_bundle(path: str) -> ContentCatalogData:
    try:
        import UnityPy
    except ImportError:
        raise ImportError("UnityPy is required to read .bundle files: pip install UnityPy")

    env = UnityPy.load(path)
    for obj in env.objects:
        if obj.type.name == "TextAsset":
            ta = obj.read()
            raw: bytes = ta.m_Script.encode("utf-8", errors="replace") if isinstance(ta.m_Script, str) else bytes(ta.m_Script)
            if len(raw) < 4:
                continue
            magic = struct.unpack_from("<i", raw, 0)[0]
            if magic == _BINARY_MAGIC_LE:
                return _read_binary(raw)
            elif magic == _BINARY_MAGIC_BE:
                raise ValueError("Big-endian binary catalogs are not supported")
            else:
                return _read_json(raw.decode("utf-8"))
    raise ValueError("No TextAsset found in bundle — not an Addressables catalog bundle?")


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def detect_catalog_type(path: str) -> str:
    """Return 'binary', 'json', or 'bundle'."""
    p = Path(path)
    if p.suffix.lower() == ".bundle":
        return "bundle"
    with open(path, "rb") as f:
        header = f.read(8)
    if len(header) < 4:
        return "unknown"
    magic = struct.unpack_from("<i", header, 0)[0]
    if magic == _BINARY_MAGIC_LE or magic == _BINARY_MAGIC_BE:
        return "binary"
    # check UnityFS magic for misnamed bundles
    if header[:7] == b"UnityFS":
        return "bundle"
    # JSON: skip whitespace, look for {
    with open(path, "rb") as f:
        for _ in range(64):
            b = f.read(1)
            if not b:
                break
            if b in (b" ", b"\t", b"\r", b"\n"):
                continue
            if b == b"{":
                return "json"
            break
    return "unknown"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_catalog(path: str) -> ContentCatalogData:
    """Auto-detect format and read a Unity Addressables catalog file."""
    fmt = detect_catalog_type(path)
    if fmt == "binary":
        with open(path, "rb") as f:
            return _read_binary(f.read())
    elif fmt == "json":
        with open(path, "r", encoding="utf-8") as f:
            return _read_json(f.read())
    elif fmt == "bundle":
        return _read_bundle(path)
    else:
        raise ValueError(f"Unknown catalog format: {path}")


def patch_crc(catalog: ContentCatalogData) -> int:
    """Set CRC to 0 on all AssetBundleRequestOptions in-place.
    Returns the number of locations patched."""
    count = 0
    seen = set()
    for locs in catalog.resources.values():
        for loc in locs:
            if id(loc) in seen:
                continue
            seen.add(id(loc))
            abro = _get_abro(loc.data)
            if abro is not None and abro.crc != 0:
                abro.crc = 0
                count += 1
    return count


def find_resources(catalog: ContentCatalogData, pattern: str) -> list[ResourceLocation]:
    """Find all ResourceLocations whose primary key or internal ID matches pattern (case-insensitive regex)."""
    rx = re.compile(pattern, re.IGNORECASE)
    seen = set()
    results = []
    for locs in catalog.resources.values():
        for loc in locs:
            if id(loc) in seen:
                continue
            seen.add(id(loc))
            if rx.search(loc.primary_key) or rx.search(loc.internal_id):
                results.append(loc)
    return results


def find_font_resources(catalog: ContentCatalogData) -> list[ResourceLocation]:
    """Find ResourceLocations likely to be fonts (by provider ID or internal ID patterns)."""
    seen = set()
    results = []
    for locs in catalog.resources.values():
        for loc in locs:
            if id(loc) in seen:
                continue
            seen.add(id(loc))
            if (_FONT_PROVIDER_PATTERNS.search(loc.provider_id)
                    or _FONT_ID_PATTERNS.search(loc.internal_id)
                    or _FONT_ID_PATTERNS.search(loc.primary_key)):
                results.append(loc)
    return results


def list_all_resources(catalog: ContentCatalogData) -> list[ResourceLocation]:
    """Return a deduplicated list of all ResourceLocations."""
    seen = set()
    results = []
    for locs in catalog.resources.values():
        for loc in locs:
            if id(loc) not in seen:
                seen.add(id(loc))
                results.append(loc)
    return results


def get_bundle_for_location(loc: ResourceLocation) -> Optional[str]:
    """Return the bundle filename for a ResourceLocation.

    Checks, in order:
      1. loc.data itself if it is an AssetBundleRequestOptions
      2. loc.dependencies — the first dep whose internal_id ends with .bundle
         or whose data contains an AssetBundleRequestOptions
    Returns the bundle filename (e.g. '000f318...cb8c.bundle'), or None.
    """
    # Case 1: the location itself carries ABRO (e.g. it IS a bundle entry)
    abro = _get_abro(loc.data)
    if abro is not None:
        return abro.bundle_name or _basename(loc.internal_id) or None

    # Case 2: look through dependencies
    for dep in loc.dependencies or []:
        dep_abro = _get_abro(dep.data)
        if dep_abro is not None:
            return dep_abro.bundle_name or _basename(dep.internal_id) or None
        # fallback: dependency internal_id looks like a bundle file
        if dep.internal_id and dep.internal_id.endswith(".bundle"):
            return _basename(dep.internal_id)

    return None


def _basename(path: str) -> str:
    """Return the last component of a path string."""
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] if path else ""


def write_catalog_json(catalog: ContentCatalogData, path: str) -> None:
    """Write the catalog back as a catalog.json file.
    Only works on catalogs originally read from JSON format."""
    _check_json_roundtrip(catalog)
    json_str = _build_json(catalog)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json_str)


def print_catalog_summary(catalog: ContentCatalogData) -> None:
    """Print a human-readable summary of the catalog."""
    all_locs = list_all_resources(catalog)
    print(f"Locator ID        : {catalog.locator_id}")
    print(f"Build Result Hash : {catalog.build_result_hash}")
    print(f"Total keys        : {len(catalog.resources)}")
    print(f"Total locations   : {len(all_locs)}")

    # Group by provider
    by_provider: dict[str, int] = {}
    bundle_count = 0
    for loc in all_locs:
        short_prov = loc.provider_id.split(".")[-1] if "." in loc.provider_id else loc.provider_id
        by_provider[short_prov] = by_provider.get(short_prov, 0) + 1
        if _get_abro(loc.data) is not None:
            bundle_count += 1

    print(f"Bundle entries    : {bundle_count}")
    print("By provider:")
    for prov, cnt in sorted(by_provider.items(), key=lambda x: -x[1]):
        print(f"  {cnt:5d}  {prov}")


# ---------------------------------------------------------------------------
# JSON write helpers
# ---------------------------------------------------------------------------

def _check_json_roundtrip(catalog: ContentCatalogData) -> None:
    if not catalog._provider_ids and not catalog._internal_ids:
        raise ValueError(
            "This catalog was read from binary format — write_catalog_json "
            "requires a catalog originally read from JSON."
        )


def _build_json(catalog: ContentCatalogData) -> str:
    """Reconstruct catalog.json from a ContentCatalogData (JSON-sourced only)."""
    all_locs_set: list[ResourceLocation] = []
    seen = set()
    for locs in catalog.resources.values():
        for loc in locs:
            if id(loc) not in seen:
                seen.add(id(loc))
                all_locs_set.append(loc)

    provider_ids  = list(dict.fromkeys(loc.provider_id for loc in all_locs_set))
    internal_ids  = list(dict.fromkeys(loc.internal_id for loc in all_locs_set))
    resource_types = list(dict.fromkeys(
        (rt.assembly_name, rt.class_name)
        for loc in all_locs_set if loc.resource_type
        for rt in [loc.resource_type]
    ))

    prov_idx  = {v: i for i, v in enumerate(provider_ids)}
    iid_idx   = {v: i for i, v in enumerate(internal_ids)}
    rtype_idx = {v: i for i, v in enumerate(resource_types)}
    loc_idx   = {id(loc): i for i, loc in enumerate(all_locs_set)}

    keys = list(catalog.resources.keys())
    key_idx = {id(k) if not isinstance(k, str) else k: i for i, k in enumerate(keys)}

    # build extra_data and entry_data blobs
    extra_buf = io.BytesIO()
    entry_buf = io.BytesIO()
    entry_buf.write(struct.pack("<i", len(all_locs_set)))

    for loc in all_locs_set:
        data_pos = -1
        if loc.data is not None:
            data_pos = extra_buf.tell()
            abro = _get_abro(loc.data)
            _encode_v1(extra_buf, abro if abro is not None else loc.data)

        dep_key_i = -1
        if loc.dependency_key is not None:
            dk = loc.dependency_key
            dk_str = str(dk) if not isinstance(dk, str) else dk
            dep_key_i = key_idx.get(dk_str, -1)
            if dep_key_i == -1 and dk in keys:
                dep_key_i = keys.index(dk)

        pkey_i = 0
        pk = loc.primary_key
        if pk in key_idx:
            pkey_i = key_idx[pk]

        rt_key = (loc.resource_type.assembly_name, loc.resource_type.class_name) if loc.resource_type else ("", "")
        rt_i = rtype_idx.get(rt_key, 0)

        entry_buf.write(struct.pack("<7i",
            iid_idx.get(loc.internal_id, 0),
            prov_idx.get(loc.provider_id, 0),
            dep_key_i,
            loc.dependency_hash_code,
            data_pos,
            pkey_i,
            rt_i,
        ))

    # build key_data and bucket_data blobs
    key_buf    = io.BytesIO()
    bucket_buf = io.BytesIO()
    key_buf.write(struct.pack("<i", len(keys)))
    bucket_buf.write(struct.pack("<i", len(keys)))

    for k, locs in catalog.resources.items():
        key_offset = key_buf.tell()
        _encode_v1(key_buf, k)
        entries = [loc_idx[id(loc)] for loc in locs]
        bucket_buf.write(struct.pack("<i", key_offset))
        bucket_buf.write(struct.pack("<i", len(entries)))
        bucket_buf.write(struct.pack(f"<{len(entries)}i", *entries))

    def _b64(buf: io.BytesIO) -> str:
        return base64.b64encode(buf.getvalue()).decode("ascii")

    # build output dict
    out: dict = {
        "m_LocatorId": catalog.locator_id,
        "m_InstanceProviderData": _oid_to_dict(catalog.instance_provider),
        "m_SceneProviderData": _oid_to_dict(catalog.scene_provider),
        "m_ResourceProviderData": [_oid_to_dict(x) for x in catalog.resource_providers],
        "m_ProviderIds": provider_ids,
        "m_InternalIds": internal_ids,
        "m_KeyDataString": _b64(key_buf),
        "m_BucketDataString": _b64(bucket_buf),
        "m_EntryDataString": _b64(entry_buf),
        "m_ExtraDataString": _b64(extra_buf),
        "m_resourceTypes": [
            {"m_AssemblyName": asm, "m_ClassName": cls}
            for asm, cls in resource_types
        ],
    }
    if catalog.build_result_hash:
        out["m_BuildResultHash"] = catalog.build_result_hash
    if catalog._internal_id_prefixes:
        out["m_InternalIdPrefixes"] = catalog._internal_id_prefixes

    return json.dumps(out, separators=(",", ":"), ensure_ascii=False)


def _oid_to_dict(oid: Optional[ObjectInitializationData]) -> dict:
    if oid is None:
        return {}
    return {
        "m_Id": oid.id,
        "m_ObjectType": {
            "m_AssemblyName": oid.object_type.assembly_name if oid.object_type else "",
            "m_ClassName": oid.object_type.class_name if oid.object_type else "",
        },
        "m_Data": oid.data,
    }


def _get_abro(data: Any) -> Optional[AssetBundleRequestOptions]:
    """Unwrap AssetBundleRequestOptions from whatever wrapper it may be in."""
    if isinstance(data, AssetBundleRequestOptions):
        return data
    if isinstance(data, tuple) and len(data) == 4 and data[0] == "__json__":
        _, asm, cls, json_text = data
        if "AssetBundleRequestOptions" in cls:
            return _parse_abro_json(json_text)
    return None


# ---------------------------------------------------------------------------
# Stream helpers
# ---------------------------------------------------------------------------

def _read_byte(br: io.RawIOBase) -> int:
    b = br.read(1)
    if not b:
        raise EOFError
    return b[0]


def _read_str4(br: io.RawIOBase, encoding: str) -> str:
    length = struct.unpack("<i", br.read(4))[0]
    return br.read(length).decode(encoding)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
# Usage:
#   python addressables_catalog.py <catalog>
#   python addressables_catalog.py <catalog> --fonts   [--output out.txt]
#   python addressables_catalog.py <catalog> <pattern> [--output out.txt]
#   python addressables_catalog.py <catalog> --patch-crc <output_catalog>
# ---------------------------------------------------------------------------

def _format_location_line(loc: ResourceLocation) -> str:
    """Format a single ResourceLocation as a human-readable line."""
    bundle = get_bundle_for_location(loc)
    bundle_str = f"  →  {bundle}" if bundle else ""
    return f"[{loc.primary_key}]  {loc.internal_id}{bundle_str}"


def _write_results(results: list[ResourceLocation], out_path: Optional[str],
                   header: str) -> None:
    """Print results to stdout and optionally save to a file."""
    lines = [header, "=" * len(header)]
    for loc in results:
        lines.append(_format_location_line(loc))

    output = "\n".join(lines)
    print(output)

    if out_path:
        Path(out_path).write_text(output + "\n", encoding="utf-8")
        print(f"\nSaved {len(results)} result(s) → {out_path}")
    else:
        print(f"\nTotal: {len(results)} result(s)  (use --output <file> to save)")


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]

    if not args:
        print("Usage:")
        print("  python addressables_catalog.py <catalog>")
        print("  python addressables_catalog.py <catalog> --fonts   [--output out.txt]")
        print("  python addressables_catalog.py <catalog> <pattern> [--output out.txt]")
        print("  python addressables_catalog.py <catalog> --patch-crc <output_catalog>")
        sys.exit(1)

    catalog_path = args[0]
    remaining    = args[1:]

    # parse --output
    out_path = None
    if "--output" in remaining:
        idx = remaining.index("--output")
        if idx + 1 < len(remaining):
            out_path = remaining[idx + 1]
            remaining = [a for i, a in enumerate(remaining) if i not in (idx, idx + 1)]

    print(f"Reading: {catalog_path}")
    cat = read_catalog(catalog_path)
    print_catalog_summary(cat)
    print()

    if not remaining:
        pass  # summary only

    elif remaining[0] == "--patch-crc":
        patch_out = remaining[1] if len(remaining) > 1 else catalog_path + ".patched.json"
        n = patch_crc(cat)
        write_catalog_json(cat, patch_out)
        print(f"Patched {n} CRC(s)  →  {patch_out}")

    elif remaining[0] == "--fonts":
        results = find_font_resources(cat)
        _write_results(results, out_path, f"Font resources ({len(results)})")

    else:
        pattern = remaining[0]
        results = find_resources(cat, pattern)
        _write_results(results, out_path, f"Search '{pattern}' ({len(results)} result(s))")
