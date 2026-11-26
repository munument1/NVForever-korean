"""Local ESM text transfer. Container format follows NVForever's raw.rs/esm.md."""
import hashlib
import struct
import warnings
import zlib
from collections import defaultdict

MAX_RECORD = 64 << 20
TEXT = {
    b"FACT": {b"MNAM", b"FNAM"}, b"MESG": {b"ITXT"},
    b"QUST": {b"CNAM", b"NNAM"}, b"DIAL": {b"TDUM"},
    b"INFO": {b"NAM1", b"RNAM"}, b"PERK": {b"EPF2"},
    b"WEAP": {b"VANM"}, b"AMMO": {b"ONAM", b"QNAM"},
    b"TERM": {b"ITXT", b"RNAM"}, b"REGN": {b"RDMP"},
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def is_text(record, field, fields=()):
    if record == b"NOTE" and field == b"TNAM":
        return any(tag == b"DATA" and value == b"\x01" for tag, value, _, _ in fields)
    if record == b"GMST" and field == b"DATA":
        return any(tag == b"EDID" and value.startswith(b"s") for tag, value, _, _ in fields)
    return field in {b"FULL", b"DESC"} or field in TEXT.get(record, ())


def subrecords(data):
    result = []
    pos = 0
    while pos < len(data):
        start = pos
        if pos + 6 > len(data):
            raise ValueError("문자열 필드 헤더가 잘렸습니다")
        tag, size = struct.unpack_from("<4sH", data, pos)
        pos += 6
        extended = tag == b"XXXX"
        if extended:
            if size != 4 or pos + 10 > len(data):
                raise ValueError("잘못된 XXXX 필드입니다")
            size = struct.unpack_from("<I", data, pos)[0]
            tag = data[pos + 4:pos + 8]
            pos += 10
            if tag == b"XXXX":
                raise ValueError("중첩 XXXX 필드입니다")
        if pos + size > len(data):
            raise ValueError("문자열 필드가 레코드 범위를 벗어났습니다")
        result.append((tag, data[pos:pos + size], data[start:pos + size], extended))
        pos += size
    return result


def unpack_payload(header, payload):
    if not struct.unpack_from("<I", header, 8)[0] & 0x40000:
        return payload
    if len(payload) < 4:
        raise ValueError("압축 레코드 헤더가 잘렸습니다")
    size = struct.unpack_from("<I", payload)[0]
    if size > MAX_RECORD:
        raise ValueError("압축 해제 크기 제한을 초과했습니다")
    stream = payload[4:]
    if len(stream) < 3 or stream[0] & 15 != 8 or stream[0] >> 4 > 7 or int.from_bytes(stream[:2], "big") % 31 or stream[1] & 32:
        raise ValueError("잘못된 zlib 헤더입니다")
    decoder = zlib.decompressobj(-15)
    raw = decoder.decompress(stream[2:], size + 1)
    if len(raw) != size or not decoder.eof:
        raise ValueError("압축 레코드 크기 또는 데이터가 올바르지 않습니다")
    trailer = decoder.unused_data
    if 0 < len(trailer) < 4:
        raise ValueError("zlib 체크섬이 잘렸습니다")
    # Match fnv-inflate: shipped records can have a missing/wrong Adler trailer.
    if not trailer or int.from_bytes(trailer[:4], "big") != zlib.adler32(raw):
        warnings.warn(f"레코드 {struct.unpack_from('<I', header, 12)[0]:08X}의 zlib 체크섬 이상을 NVForever 정책에 따라 허용했습니다")
    return raw


def transform(data, callback, depth=0):
    if depth > 64:
        raise ValueError("그룹 중첩 제한을 초과했습니다")
    output = bytearray()
    pos = 0
    while pos < len(data):
        if pos + 24 > len(data):
            raise ValueError("ESM 헤더가 잘렸습니다")
        header = data[pos:pos + 24]
        tag, size = struct.unpack_from("<4sI", header)
        total = size if tag == b"GRUP" else size + 24
        if total < 24 or pos + total > len(data):
            raise ValueError("ESM 레코드 크기가 올바르지 않습니다")
        payload = data[pos + 24:pos + total]
        if tag == b"GRUP":
            changed = transform(payload, callback, depth + 1)
            if changed != payload:
                header = header[:4] + struct.pack("<I", len(changed) + 24) + header[8:]
                payload = changed
        else:
            raw = unpack_payload(header, payload)
            changed = callback(header, subrecords(raw))
            if changed is not None and changed != raw:
                payload = changed
                if struct.unpack_from("<I", header, 8)[0] & 0x40000:
                    payload = struct.pack("<I", len(changed)) + zlib.compress(changed)
                header = header[:4] + struct.pack("<I", len(payload)) + header[8:]
        output.extend(header)
        output.extend(payload)
        pos += total
    return bytes(output)


def identity(header, fields):
    tag = header[:4]
    form = struct.unpack_from("<I", header, 12)[0]
    # Text lengths may change. All other field bytes and field order must agree.
    flags = struct.unpack_from("<I", header, 8)[0] & ~0x40000
    canonical = bytearray(tag + struct.pack("<II", flags, form) + header[20:22])
    for field, value, _, _ in fields:
        canonical.extend(field)
        if not is_text(tag, field, fields):
            canonical.extend(struct.pack("<I", len(value)))
            canonical.extend(value)
    return f"{tag.decode('ascii')}:{form:08X}", digest(canonical)


def extract(data):
    records = {}
    stats = {"fields": 0, "invalid_utf8": 0}
    seen = set()

    def collect(header, fields):
        key, fingerprint = identity(header, fields)
        if key in seen:
            raise ValueError(f"중복 레코드 {key}")
        seen.add(key)
        counts = defaultdict(int)
        values = {}
        for tag, value, _, _ in fields:
            index = counts[tag]
            counts[tag] += 1
            if not is_text(header[:4], tag, fields):
                continue
            try:
                text = value.removesuffix(b"\0").decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                stats["invalid_utf8"] += 1
                continue
            if not text or "\0" in text:
                continue
            values[f"{tag.decode('ascii')}:{index}"] = {"text": text, "nul": value.endswith(b"\0")}
            stats["fields"] += 1
        if values:
            records[key] = {"structure": fingerprint, "values": values}
        return None

    if data[:4] != b"TES4":
        raise ValueError("TES4 ESM/ESP 파일이 아닙니다")
    transform(data, collect)
    return {"records": records, "stats": stats, "sha256": digest(data)}


def translate(data, catalog):
    stats = {"changed": 0, "already": 0, "incompatible": 0, "missing": 0}
    visited = set()

    def replace(header, fields):
        key, fingerprint = identity(header, fields)
        if key in visited:
            raise ValueError(f"중복 레코드 {key}")
        visited.add(key)
        entry = catalog["records"].get(key)
        if entry is None:
            return None
        if fingerprint != entry["structure"]:
            stats["incompatible"] += 1
            return None
        counts = defaultdict(int)
        output = bytearray()
        matched = set()
        for tag, value, original, extended in fields:
            index = counts[tag]
            counts[tag] += 1
            field_key = f"{tag.decode('ascii')}:{index}"
            item = entry["values"].get(field_key)
            if item is None:
                output.extend(original)
                continue
            if not is_text(header[:4], tag, fields) or "\0" in item["text"]:
                raise ValueError(f"허용되지 않은 번역 필드 {key}/{field_key}")
            matched.add(field_key)
            new = item["text"].encode("utf-8") + (b"\0" if item["nul"] else b"")
            if new == value:
                stats["already"] += 1
                output.extend(original)
                continue
            stats["changed"] += 1
            if extended or len(new) > 65535:
                output.extend(b"XXXX\x04\x00" + struct.pack("<I", len(new)))
                output.extend(tag + b"\0\0")
            else:
                output.extend(tag + struct.pack("<H", len(new)))
            output.extend(new)
        if matched != set(entry["values"]):
            raise ValueError(f"번역 필드가 일치하지 않습니다 {key}")
        return bytes(output)

    if data[:4] != b"TES4":
        raise ValueError("TES4 ESM/ESP 파일이 아닙니다")
    output = transform(data, replace)
    stats["missing"] = len(set(catalog["records"]) - visited)
    # Reparse the complete output before allowing any write.
    transform(output, lambda header, fields: None)
    return output, stats
