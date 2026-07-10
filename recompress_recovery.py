#!/usr/bin/env python3
"""Recompress the recovery vendor_ramdisk_fragment with xz and rebuild
vendor_boot.img.

Root cause of the 79380480 > 67108864 byte size overage: the native
`mka vendorbootimage` build embeds the recovery ramdisk correctly, but the
fragment is lz4-compressed at only ~36% ratio (measured: 138053888 raw ->
49704536 lz4). xz -9 on the SAME decompressed bytes gets ~17.5% (24225088),
more than half the size, for free -- no content change. mkbootimg is
compression-agnostic per fragment (system/tools/mkbootimg just concatenates
raw bytes per the vendor ramdisk table); the kernel's initramfs unpacker
auto-detects gzip/lzma/xz/lz4/zstd by magic bytes at boot. The stock
platform fragment is left completely untouched.
"""
import sys
import subprocess
import shlex
import os

unpack_dir, out_img, mkbootimg_bin = sys.argv[1], sys.argv[2], sys.argv[3]
args = shlex.split(open(os.path.join(unpack_dir, "mkbootimg_args.txt")).read())

# Find the --vendor_ramdisk_fragment path in the option group that has
# --ramdisk_name recovery.
recovery_frag_idx = None
for i, a in enumerate(args):
    if a == "--ramdisk_name" and args[i + 1] == "recovery":
        for j in range(i, len(args) - 1):
            if args[j] == "--vendor_ramdisk_fragment":
                recovery_frag_idx = j + 1
                break
        break
assert recovery_frag_idx is not None, \
    "could not locate recovery vendor_ramdisk_fragment in mkbootimg args"

recovery_path = args[recovery_frag_idx]
raw_path = recovery_path + ".raw"
xz_path = recovery_path + ".xz"

before = os.path.getsize(recovery_path)
subprocess.run(["lz4", "-d", "-f", recovery_path, raw_path], check=True)
subprocess.run(["xz", "-9", "-T0", "-f", "-k", raw_path], check=True)
os.replace(raw_path + ".xz", xz_path)
after = os.path.getsize(xz_path)
print(f"recovery fragment: {before:,} bytes (lz4) -> {after:,} bytes (xz), "
      f"saved {before - after:,} bytes")

args[recovery_frag_idx] = xz_path

# NOTE: unpack_bootimg's mkbootimg-format output sets --base 0x00000000
# *by design* and folds the real base into --kernel_offset/--ramdisk_offset/
# --tags_offset/--dtb_offset directly (mkbootimg computes final address =
# base + offset, so base=0 + absolute-address-as-offset is mathematically
# equivalent to the original build's base=0x40000000 + small relative
# offsets -- verified: 0x40000000+0x8000=0x40008000 matches the unpacked
# kernel_offset value exactly). Do NOT override --base here -- that would
# double-count it into a wrong (silently corrupted) load address.

cmd = [mkbootimg_bin] + args + ["--vendor_boot", out_img]
print("running:", " ".join(shlex.quote(c) for c in cmd))
subprocess.run(cmd, check=True)
