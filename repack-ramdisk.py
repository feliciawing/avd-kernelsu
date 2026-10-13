#!/usr/bin/env python3
"""Repack AVD ramdisk with matching KernelSU kernel modules."""
import sys, os, io, gzip, subprocess, tempfile

def decompress(raw):
    """Return (cpio_bytes, compress_type). Handles gzip, LZ4 frame, LZ4 legacy."""
    if raw[:2] == b'\x1f\x8b':
        return gzip.decompress(raw), 'gzip'
    if raw[:4] in (b'\x04\x22\x4d\x18', b'\x02\x21\x4c\x18'):  # LZ4 frame or legacy
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.lz4')
        tmp.write(raw); tmp.close()
        out = subprocess.check_output(['lz4', '-d', '-c', tmp.name])
        os.unlink(tmp.name)
        return out, 'lz4'
    return raw, 'none'

def compress(data, compress_type):
    if compress_type == 'gzip':
        return gzip.compress(data)
    if compress_type == 'lz4':
        # Must use LZ4 legacy format (magic 0x02214c18) — Android kernel only understands legacy.
        # lz4 -l flag = legacy format.
        tmp_in = tempfile.NamedTemporaryFile(delete=False)
        tmp_out = tempfile.NamedTemporaryFile(delete=False, suffix='.lz4')
        tmp_in.write(data); tmp_in.close(); tmp_out.close()
        subprocess.check_call(['lz4', '-l', '-f', tmp_in.name, tmp_out.name])
        with open(tmp_out.name, 'rb') as f: result = f.read()
        os.unlink(tmp_in.name); os.unlink(tmp_out.name)
        return result
    return data

def parse_cpio(data, start=0):
    """Parse one CPIO archive starting at offset. Returns (entries, end_offset)."""
    entries = []
    # Skip null padding between concatenated archives
    pos = start
    while pos < len(data) and data[pos:pos+6] != b'070701':
        pos += 1
    while pos < len(data):
        if data[pos:pos+6] != b'070701':
            break
        mode = int(data[pos+14:pos+22], 16)
        filesize = int(data[pos+54:pos+62], 16)
        rdevmajor = int(data[pos+78:pos+86], 16)
        rdevminor = int(data[pos+86:pos+94], 16)
        namesize = int(data[pos+94:pos+102], 16)
        hdr_end = pos + 110
        name = data[hdr_end:hdr_end+namesize-1].decode('utf-8', errors='replace')
        data_start = (hdr_end + namesize + 3) & ~3
        data_end = data_start + filesize
        content = data[data_start:data_end]
        pos = (data_end + 3) & ~3
        if name == 'TRAILER!!!' or not name:
            break
        entries.append((name, mode, content, rdevmajor, rdevminor))
    return entries, pos

def write_cpio(entries):
    out = io.BytesIO()
    for ino, entry in enumerate(entries, 1):
        name, mode, content = entry[0], entry[1], entry[2]
        rdevmajor = entry[3] if len(entry) > 3 else 0
        rdevminor = entry[4] if len(entry) > 4 else 0
        name_b = name.encode() + b'\x00'
        ns, fs = len(name_b), len(content)
        hdr = f'070701{ino:08X}{mode:08X}00000000000000000000000100000000{fs:08X}0000000000000000{rdevmajor:08X}{rdevminor:08X}{ns:08X}00000000'
        out.write(hdr.encode())
        out.write(name_b)
        out.write(b'\x00' * ((4 - (110 + ns) % 4) % 4))
        out.write(content)
        out.write(b'\x00' * ((4 - fs % 4) % 4))
    trailer = b'TRAILER!!!\x00'
    ns = len(trailer)
    hdr = ('070701'
            + '00000000' * 4   # ino, mode, uid, gid
            + '00000001'       # nlink
            + '00000000' * 6   # mtime, filesize, devmajor, devminor, rdevmajor, rdevminor
            + f'{ns:08X}'      # namesize
            + '00000000')      # check
    out.write(hdr.encode())
    out.write(trailer)
    out.write(b'\x00' * ((4 - (110 + ns) % 4) % 4))
    return out.getvalue()

def apply_modules(entries, module_files, available_kos):
    """Replace .ko files we have; drop .ko files and modules.load entries for ones we don't."""
    # First pass: determine which KOs will be dropped (modules.load/dep come before .ko in CPIO)
    dropped_kos = set()
    for entry in entries:
        name = entry[0]
        basename = os.path.basename(name)
        if basename.endswith('.ko') and basename not in module_files and basename not in available_kos:
            dropped_kos.add(basename)

    new_entries = []
    replaced = 0
    for entry in entries:
        name, mode, content = entry[0], entry[1], entry[2]
        rdevs = entry[3:] if len(entry) > 3 else ()
        basename = os.path.basename(name)
        if basename.endswith('.ko'):
            if basename in dropped_kos:
                print(f"  dropped (built-in/missing): {name}")
                continue
            if basename in module_files:
                with open(module_files[basename], 'rb') as f:
                    content = f.read()
                print(f"  replaced: {name}")
                replaced += 1
            new_entries.append((name, mode, content) + rdevs)
        elif basename == 'modules.load':
            lines = content.decode('utf-8', errors='replace').splitlines()
            kept = [l for l in lines if l.strip() not in dropped_kos]
            new_content = '\n'.join(kept).encode() + b'\n'
            new_entries.append((name, mode, new_content) + rdevs)
        elif basename == 'modules.dep':
            lines = content.decode('utf-8', errors='replace').splitlines()
            kept = [l for l in lines if not any(d in l for d in dropped_kos)]
            new_content = '\n'.join(kept).encode() + b'\n'
            new_entries.append((name, mode, new_content) + rdevs)
        else:
            new_entries.append(entry)

    return new_entries, replaced

def main():
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} <stock-ramdisk.img> <modules-dir> <output-ramdisk.img>")
        sys.exit(1)

    ramdisk_path, modules_dir, output_path = sys.argv[1], sys.argv[2], sys.argv[3]

    print(f"Reading: {ramdisk_path}")
    with open(ramdisk_path, 'rb') as f:
        raw = f.read()

    cpio_data, compress_type = decompress(raw)
    print(f"Decompressed ({compress_type}): {len(cpio_data)} bytes")

    # Build module lookup
    module_files = {}
    for fn in os.listdir(modules_dir):
        if fn.endswith('.ko'):
            module_files[fn] = os.path.join(modules_dir, fn)
    available_kos = set(module_files.keys())
    print(f"Available modules from artifact: {len(available_kos)}")

    # Parse both CPIOs (concatenated ramdisk)
    entries1, second_start = parse_cpio(cpio_data)
    print(f"First CPIO: {len(entries1)} entries")

    entries2, _ = parse_cpio(cpio_data, second_start)
    print(f"Second CPIO: {len(entries2)} entries")

    # Apply modules to both CPIOs
    new1, r1 = apply_modules(entries1, module_files, available_kos)
    new2, r2 = apply_modules(entries2, module_files, available_kos)
    print(f"\nReplaced {r1 + r2} modules total")

    # Repack as concatenated CPIO
    new_cpio = write_cpio(new1) + write_cpio(new2)
    compressed = compress(new_cpio, compress_type)

    with open(output_path, 'wb') as f:
        f.write(compressed)

    print(f"Output: {output_path} ({len(compressed)} bytes)")

if __name__ == '__main__':
    main()
