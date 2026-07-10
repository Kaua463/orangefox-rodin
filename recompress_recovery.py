#!/usr/bin/env python3
"""Build vendor_boot_patched.img by taking the STOCK vendor_boot.img as the
complete source of truth (platform ramdisk, dtb, load addresses, cmdline,
vendor_ramdisk_table structure -- everything) and swapping in ONLY the
compiled recovery ramdisk, recompressed with zstd.

## History (why this approach, not "fix the native build's output")

The native `mka vendorbootimage` ninja recipe assembles its own
vendor_boot.img, but every attempt to ship ITS output (even after patching
individual fields) bootlooped the device twice:

1st attempt: recompressed the recovery fragment with xz. BOOTLOOPED.
Root cause: the device kernel's actual config (verified live via
`adb shell su 0 -c "zcat /proc/config.gz"`) has CONFIG_RD_XZ NOT SET, only
CONFIG_RD_GZIP/CONFIG_RD_LZ4/CONFIG_RD_ZSTD. "mkbootimg is
compression-agnostic, kernel auto-detects by magic bytes" is only true
among the decompressors the kernel was actually built with.

2nd attempt: switched to zstd (confirmed kernel-supported), also manually
patched --kernel_offset/--ramdisk_offset/--tags_offset/--dtb_offset and
--vendor_cmdline to match the real device (the native build's ninja recipe
never passes those flags at all, silently falling back to mkbootimg
defaults instead of BoardConfig.mk's declared BOARD_RAMDISK_OFFSET etc).
BOOTLOOPED AGAIN. Root cause, found by diffing the FULL vendor_ramdisk_table
against a live backup of the device's actual vendor_boot_a
(`adb shell su 0 -c "cat /dev/block/by-name/vendor_boot_a"`):
  - The native build's vendor_boot.img has an extra, essentially-empty
    (4-byte) fragment tagged type=PLATFORM(1), while the REAL 29MB
    platform ramdisk content ends up tagged type=NONE(0) instead -- the
    boot process looks for the platform ramdisk in the PLATFORM-tagged
    slot and finds 4 empty bytes.
  - Separately, this device tree's prebuilt/vendor_ramdisk00 (the platform
    ramdisk baked into the repo) and prebuilt/dtb are BOTH byte-different
    from what's actually flashed on the device right now (sha256 mismatch
    on both) -- the device most likely received an OTA update after this
    device tree's prebuilts were extracted from an earlier firmware dump.

Given the native vendorbootimage build's own field-by-field output has
proven unreliable twice, the robust fix is to stop trusting it for
anything except the compiled recovery ramdisk content, and rebuild
everything else directly from `prebuilt/vendor_boot_stock.img` (which
should be kept up to date -- see README) via `unpack_bootimg --format
mkbootimg`, which gives the exact reconstruction argument list already
matching whatever's really flashed.
"""
import sys
import subprocess
import shlex
import os

native_vendor_boot, stock_vendor_boot, out_img, unpack_bootimg_bin, \
    mkbootimg_bin, avbtool_bin, fingerprint_file = sys.argv[1:8]

work_dir = os.path.dirname(out_img)
native_unpack = os.path.join(work_dir, "native_unpack")
stock_unpack = os.path.join(work_dir, "stock_unpack")
os.makedirs(native_unpack, exist_ok=True)
os.makedirs(stock_unpack, exist_ok=True)


def run(cmd, **kw):
    print("running:", " ".join(shlex.quote(c) for c in cmd))
    return subprocess.run(cmd, check=True, **kw)


# 1. Pull the compiled recovery ramdisk out of the NATIVE build's
#    vendor_boot.img (the only thing we trust it for).
run([unpack_bootimg_bin, "--boot_img", native_vendor_boot,
     "--out", native_unpack])
recovery_lz4 = None
for name in os.listdir(native_unpack):
    if name.startswith("vendor_ramdisk") and len(name) == len("vendor_ramdisk00"):
        # Identify by trying to lz4-decompress; only the recovery fragment
        # is lz4 framed at this stage (the platform one, if this native
        # build even tags it right, is raw cpio and would fail lz4 -t).
        path = os.path.join(native_unpack, name)
        if subprocess.run(["lz4", "-t", path], capture_output=True).returncode == 0 \
                and os.path.getsize(path) > 1_000_000:
            recovery_lz4 = path
            break
assert recovery_lz4 is not None, "could not find the lz4 recovery fragment in the native build output"

recovery_raw = os.path.join(work_dir, "recovery.raw.cpio")
recovery_zst = os.path.join(work_dir, "recovery.zst")
run(["lz4", "-d", "-f", recovery_lz4, recovery_raw])
# -19 with an explicit --zstd=wlog=23 (8MiB window): the default window for
# this input size is already 8MiB, but pin it explicitly so this stays
# under lib/decompress_unzstd.c's window-size rejection check regardless
# of zstd CLI version drift.
run(["zstd", "-19", "--zstd=wlog=23", "-T0", "-f", recovery_raw, "-o", recovery_zst])
before = os.path.getsize(recovery_lz4)
after = os.path.getsize(recovery_zst)
print(f"recovery fragment: {before:,} bytes (lz4) -> {after:,} bytes (zstd), "
      f"saved {before - after:,} bytes")

# 2. Get the exact reconstruction args for the STOCK image -- this carries
#    the real platform ramdisk, real dtb, real load addresses, real
#    cmdline, and the real (2-fragment) vendor_ramdisk_table structure.
run([unpack_bootimg_bin, "--boot_img", stock_vendor_boot,
     "--out", stock_unpack, "--format", "mkbootimg"],
    stdout=open(os.path.join(stock_unpack, "mkbootimg_args.txt"), "w"))
args = shlex.split(open(os.path.join(stock_unpack, "mkbootimg_args.txt")).read())

# 3. Swap the stock's recovery fragment path for our newly-built one.
recovery_frag_idx = None
for i, a in enumerate(args):
    if a == "--ramdisk_name" and args[i + 1] == "recovery":
        for j in range(i, len(args) - 1):
            if args[j] == "--vendor_ramdisk_fragment":
                recovery_frag_idx = j + 1
                break
        break
assert recovery_frag_idx is not None, \
    "stock vendor_boot.img has no 'recovery' ramdisk_name fragment to replace"
args[recovery_frag_idx] = recovery_zst

cmd = [mkbootimg_bin] + args + ["--vendor_boot", out_img]
run(cmd)

# 4. AVB hash footer (unsigned -- no --key available; matches an unlocked
#    bootloader's trust model, same as every other attempt so far).
partition_size = 67108864
fingerprint = open(fingerprint_file).read().strip()
run([
    avbtool_bin, "add_hash_footer",
    "--image", out_img,
    "--partition_size", str(partition_size),
    "--partition_name", "vendor_boot",
    "--prop", f"com.android.build.vendor_boot.fingerprint:{fingerprint}",
])

final_size = os.path.getsize(out_img)
print(f"final vendor_boot_patched.img: {final_size:,} bytes")
