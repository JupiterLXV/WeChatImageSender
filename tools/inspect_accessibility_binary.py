"""Inspect a local signed Weixin.dll on disk. This script never writes memory."""
import hashlib
import json
import re
import struct
import sys
from pathlib import Path


def inspect(path):
    data = Path(path).read_bytes()
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    if data[pe:pe + 4] != b'PE\0\0':
        raise ValueError('Not a PE image')
    machine, count = struct.unpack_from('<HH', data, pe + 4)
    if machine != 0x8664:
        raise ValueError('Expected x64')
    table = pe + 24 + struct.unpack_from('<H', data, pe + 20)[0]
    sections = []
    for i in range(count):
        entry = table + i * 40
        virtual_size, rva, raw_size, raw = struct.unpack_from('<IIII', data, entry + 8)
        sections.append((rva, max(virtual_size, raw_size), raw, raw_size,
                         struct.unpack_from('<I', data, entry + 36)[0]))

    def rva(offset):
        for va, size, raw, raw_size, flags in sections:
            if raw <= offset < raw + raw_size:
                return va + offset - raw
        raise ValueError('Offset outside sections')

    target_strings = [rva(m.start()) for m in re.finditer(re.escape(b'qt.accessibility.core\0'), data)]
    xrefs = []
    for va, size, raw, raw_size, flags in sections:
        if not flags & 0x20000000:
            continue
        code = data[raw:raw + raw_size]
        for match in re.finditer(rb'[\x40-\x4f]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d](....)', code, re.DOTALL):
            address = va + match.start()
            if address + 7 + struct.unpack('<i', match.group(1))[0] in target_strings:
                xrefs.append(address)
    candidates = []
    pattern = rb'\x48\x85\xc9\x0f\x84....\x80\x3d(....)\x00\x0f\x84'
    for match in re.finditer(pattern, data, re.DOTALL):
        instruction = rva(match.start())
        target = rva(match.start(1)) + 5 + struct.unpack('<i', match.group(1))[0]
        writable = any(va <= target < va + size and flags & 0x80000000
                       for va, size, raw, raw_size, flags in sections)
        distance = min((abs(instruction - ref) for ref in xrefs), default=2**32)
        if writable and distance <= 0x20000:
            candidates.append({'rva': hex(target), 'instruction_rva': hex(instruction), 'distance': distance})
    return {'sha256': hashlib.sha256(data).hexdigest(), 'qt_xrefs': [hex(v) for v in xrefs],
            'candidates': sorted(candidates, key=lambda value: value['distance'])}


if __name__ == '__main__':
    print(json.dumps(inspect(sys.argv[1]), indent=2))
