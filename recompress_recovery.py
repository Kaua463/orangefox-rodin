#!/usr/bin/env python3
"""Recompress the recovery vendor_ramdisk_fragment with zstd and rebuild
vendor_boot.img.

Root cause of the 79380480 > 67108864 byte size overage: the native
`mka vendorbootimage` build embeds the recovery ramdisk correctly, but the
fragment is lz4-compressed at only ~36% ratio (measured: 138053888 raw ->
49704536 lz4).

FIRST ATTEMPT USED xz -9 (24225088 bytes, ~17.5% ratio) and this BOOTLOOPED
on the real device. Root cause: the device kernel's actual config (pulled
live via `adb shell su 0 -c "zcat /proc/config.gz"`) has CONFIG_RD_XZ NOT
SET -- only CONFIG_RD_GZIP/CONFIG_RD_LZ4/CONFIG_RD_ZSTD. mkbootimg is
compression-agnostic at the packaging level (system/tools/mkbootimg just
concatenates raw bytes per the vendor ramdisk table) but the KERNEL'S
initramfs decompressor only supports whatever CONFIG_RD_* options it was
actually built with -- "auto-detects by magic bytes" is true only among
the compiled-in decompressors, not universally. Always verify
/proc/config.gz on the real device before picking a compression format.

Now uses zstd instead: -19 with an explicit --zstd=wlog=23 (8MiB window).
zstd's default window for this input size is already 8MiB, but wlog=23 is
pinned explicitly to stay under lib/decompress_unzstd.c's window-size
check regardless of zstd CLI version drift (the kernel decompressor
rejects any frame whose header window size exceeds what it's willing to
allocate). Measured: 138053888 raw -> 30505922 zstd (~22% ratio) --
between lz4 and xz, but SUPPORTED (CONFIG_RD_ZSTD=y on this device).
The stock platform fragment is left completely untouched either way.
"""
import sys
import subprocess
import shlex
import os

unpack_dir, out_img, mkbootimg_bin, avbtool_bin, fingerprint_file = (
    sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
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
zst_path = recovery_path + ".zst"

before = os.path.getsize(recovery_path)
subprocess.run(["lz4", "-d", "-f", recovery_path, raw_path], check=True)
subprocess.run(
    ["zstd", "-19", "--zstd=wlog=23", "-T0", "-f", raw_path, "-o", zst_path],
    check=True)
after = os.path.getsize(zst_path)
print(f"recovery fragment: {before:,} bytes (lz4) -> {after:,} bytes (zstd), "
      f"saved {before - after:,} bytes")

args[recovery_frag_idx] = zst_path

# NOTE: unpack_bootimg's mkbootimg-format output sets --base 0x00000000
# *by design* and folds the real base into --kernel_offset/--ramdisk_offset/
# --tags_offset/--dtb_offset directly (mkbootimg computes final address =
# base + offset). Do NOT override --base -- that would double-count it.
#
# BUT: the native `mka vendorbootimage` ninja recipe never passes
# --kernel_offset/--ramdisk_offset/--tags_offset/--dtb_offset at all (only
# --base/--pagesize/--header_version/--dtb), so it silently falls back to
# mkbootimg's built-in defaults (0x8000/0x1000000/0x100/0x1f00000) instead
# of BoardConfig.mk's own BOARD_RAMDISK_OFFSET=0x26f00000 /
# BOARD_KERNEL_TAGS_OFFSET=0x07c80000 / BOARD_DTB_OFFSET=0x07c80000. That
# BoardConfig-declared vendorbootimage target apparently doesn't wire those
# variables in at all. Confirmed by pulling the REAL vendor_boot_a off the
# device (`adb shell su 0 -c "cat /dev/block/by-name/vendor_boot_a"`) and
# comparing: real device has kernel load=0x40000000 (offset 0), ramdisk
# load=0x66f00000 (= base+0x26f00000), tags/dtb load=0x47c80000 (=
# base+0x07c80000), and a non-empty vendor_cmdline -- none of which matched
# our native build's output. Force the correct absolute addresses (base is
# folded to 0 in this unpacked-args representation, so these ARE the final
# addresses) and restore the vendor_cmdline BoardConfig.mk declares.
CORRECT_OFFSETS = {
    "--kernel_offset": "0x40000000",   # base 0x40000000 + BOARD_KERNEL_BASE-relative 0 (no separate kernel offset)
    "--ramdisk_offset": "0x66f00000",  # base 0x40000000 + BOARD_RAMDISK_OFFSET 0x26f00000
    "--tags_offset": "0x47c80000",     # base 0x40000000 + BOARD_KERNEL_TAGS_OFFSET 0x07c80000
    "--dtb_offset": "0x47c80000",      # base 0x40000000 + BOARD_DTB_OFFSET 0x07c80000
}
CORRECT_CMDLINE = "bootopt=64S3,32N2,64N2 erofs.reserved_pages=64"
for i, a in enumerate(args):
    if a in CORRECT_OFFSETS:
        args[i + 1] = CORRECT_OFFSETS[a]
    elif a == "--vendor_cmdline":
        args[i + 1] = CORRECT_CMDLINE

cmd = [mkbootimg_bin] + args + ["--vendor_boot", out_img]
print("running:", " ".join(shlex.quote(c) for c in cmd))
subprocess.run(cmd, check=True)

# The native ninja recipe's chain is: mkbootimg && size-check && avbtool
# add_hash_footer && OrangeFox hook. Our size check always failed before
# avbtool ran, so no build in this whole investigation has ever actually
# added the AVB hash footer -- do it now so this image matches what a
# passing native build would have produced (BOARD_AVB_ENABLE=true).
partition_size = 67108864
fingerprint = open(fingerprint_file).read().strip()
avb_cmd = [
    avbtool_bin, "add_hash_footer",
    "--image", out_img,
    "--partition_size", str(partition_size),
    "--partition_name", "vendor_boot",
    "--prop", f"com.android.build.vendor_boot.fingerprint:{fingerprint}",
]
print("running:", " ".join(shlex.quote(c) for c in avb_cmd))
subprocess.run(avb_cmd, check=True)

final_size = os.path.getsize(out_img)
print(f"final vendor_boot_patched.img (with AVB footer): {final_size:,} bytes")
