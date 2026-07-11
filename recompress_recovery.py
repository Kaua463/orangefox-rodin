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

3rd attempt: the above (zstd recovery fragment, stock-sourced platform
fragment/dtb/addresses/table) fixed the structural bug -- confirmed via
two independent builds (local + fresh CI compile) producing byte-identical
platform ramdisk/dtb and a correct 2-fragment table -- but flashing it
produced a NEW failure mode: normal boot worked fine, but entering
recovery (`fastboot reboot recovery` or the volume-up key combo) hung at
the bootloader logo, before the kernel ever got control. Root cause per
AOSP's own vendor_boot docs (source.android.com/docs/core/architecture/
partitions/vendor-boot-partitions): fragments that get concatenated
together (PLATFORM + RECOVERY, for the recovery-boot path) are decompressed
as ONE combined stream, so they must share the same compression format.
The stock platform fragment is lz4; we'd left it untouched while recompressing
only the recovery fragment as zstd -- so normal boot (platform fragment
alone, no concatenation) worked, but recovery boot (platform+recovery
concatenated, mismatched lz4/zstd) hung.

Fix: recompress BOTH fragments as zstd, not just recovery, so whatever
gets concatenated is internally consistent. Bonus: platform compresses
much better under zstd too (29.3MB lz4 -> 17.4MB zstd), so the combined
total (~47.9MB) comfortably fits the ~66.7MB combined budget with zero
content cuts to either fragment.

4th attempt: the above fixed the boot hang -- confirmed via live dmesg
on-device that the kernel, init, and adbd all boot successfully now.
But the recovery UI never draws (frozen on the bootloader's last frame)
because /system/bin/recovery crash-loops every ~5s: "CANNOT LINK
EXECUTABLE ... cannot locate symbol
_ZNSt3__122__libcpp_verbose_abortEPKcz referenced by
/system/lib64/libsysutils.so". Confirmed via a CI diagnostic step that
EVERY libc++.so in the whole build output (system/vendor/recovery/
symbols/intermediates, deduplicated by content hash) is missing this
symbol -- a genuine version-skew bug between this OrangeFox branch's
pinned clang prebuilt (assumes a libc++ hardening ABI with
__libcpp_verbose_abort) and its pinned external/libcxx source (predates
that symbol's implementation). system/bin/recovery and libtar.so both
directly need libsysutils.so (DT_NEEDED), so it can't be dropped.

Fix: rather than patch the synced AOSP/OrangeFox source tree, build a
tiny standalone shim (verbose_abort_shim.cpp, compiled with the Android
NDK, independent of the broken in-tree toolchain/libcxx pairing) that
provides the missing symbol as a plain abort() call, and surgically
patch it into the recovery ramdisk: add it as a new file
(cpio_newc_patch.py edits the newc cpio in place, byte-for-byte
untouched except the one new file and one edited line -- no
extract/repack risk to the other ~4200 files' permissions/ownership),
and add `setenv LD_PRELOAD /system/lib64/libverbose_abort_shim.so` to
the 'recovery' service in init.recovery.service.rc so the dynamic
linker resolves libsysutils.so's undefined reference against it.

5th attempt: the above got OrangeFox's UI actually drawing on-screen
for the first time (huge win -- confirms the verbose_abort fix
worked), but the touchscreen doesn't respond at all, leaving the
lock/decrypt swipe screen unreachable (worked around live via
OrangeFox's "HW GUI Control" hold-both-volume-keys-3s feature,
see wiki.orangefox.tech/en/guides/recovery_no_touch, to confirm
recovery itself is fine and this is purely a touch problem).
dmesg (both our OrangeFox build AND stock recovery) shows ZERO
touchscreen driver activity, and neither vendor_boot's own
lib/modules nor modules.load/modules.load.recovery contain any
touch-related .ko at all. Found the real driver modules
(goodix_core_rodin.ko, focaltech_touch_rodin.ko, xiaomi_touch_rodin.ko
-- this device supports both vendor ICs, like the display panel
situation) inside vendor_dlkm_a.img, a separate partition recovery
never mounts. So touch was never wired into the recovery ramdisk at
all -- not something this fix broke, an existing gap for any custom
recovery on this device that just never got tested with a real touch
gesture before now. Their modules.dep only needed one more dependency
not already present in vendor_boot's own module set: scp.ko (its own
deps -- mtk_tinysys_ipi.ko, mtk_rpmsg_mbox.ko, mtk-mbox.ko -- were
already there and already loading fine per dmesg).

Fix: same surgical-cpio-patch approach as the verbose_abort shim --
add the 4 missing .ko files (prebuilt/touch_modules/, pulled from
vendor_dlkm_a.img) into the ramdisk, and insert their names into
lib/modules/modules.load.recovery (right after mtk_tinysys_ipi.ko,
their last already-present dependency) so init actually loads them.
Both touch IC drivers get bundled and loaded; only the one matching
this unit's real hardware will actually probe successfully, same
pattern already proven safe for the panel driver variants.
"""
import sys
import subprocess
import shlex
import os
import json

(native_vendor_boot, stock_vendor_boot, out_img, unpack_bootimg_bin,
 mkbootimg_bin, avbtool_bin, fingerprint_file, shim_so, cpio_patch_script,
 scp_ko, xiaomi_touch_ko, goodix_ko, focaltech_ko) = sys.argv[1:14]

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
recovery_fixed = os.path.join(work_dir, "recovery.fixed.cpio")
recovery_zst = os.path.join(work_dir, "recovery.zst")
run(["lz4", "-d", "-f", recovery_lz4, recovery_raw])

# 1b. Patch in the verbose_abort compat shim (4th attempt) AND the
# missing touch driver modules (5th attempt). Read the current
# init.recovery.service.rc and lib/modules/modules.load.recovery out
# of the cpio, edit both in memory, then surgically patch just those
# two files' content plus add the 5 new files via cpio_newc_patch.py.
import importlib.util
_spec = importlib.util.spec_from_file_location("cpio_newc_patch", cpio_patch_script)
_cpio = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cpio)
entries, _ = _cpio.read_entries(open(recovery_raw, "rb").read())

service_rc = next(e for e in entries if e["name"] == b"init.recovery.service.rc")
old_text = service_rc["data"].decode()
assert "service recovery /system/bin/recovery" in old_text, \
    "init.recovery.service.rc format changed, can't inject LD_PRELOAD"
new_service_rc = old_text.replace(
    "service recovery /system/bin/recovery",
    "service recovery /system/bin/recovery\n"
    "    setenv LD_PRELOAD /system/lib64/libverbose_abort_shim.so",
    1,
)
service_rc_new_path = os.path.join(work_dir, "init.recovery.service.rc.new")
open(service_rc_new_path, "w").write(new_service_rc)

modules_load = next(e for e in entries if e["name"] == b"lib/modules/modules.load.recovery")
old_modules = modules_load["data"].decode()
assert "mtk_tinysys_ipi.ko" in old_modules, \
    "modules.load.recovery format changed, can't insert touch modules"
new_modules = old_modules.replace(
    "mtk_tinysys_ipi.ko\n",
    "mtk_tinysys_ipi.ko\n"
    "scp.ko\n"
    "xiaomi_touch_rodin.ko\n"
    "goodix_core_rodin.ko\n"
    "focaltech_touch_rodin.ko\n",
    1,
)
modules_load_new_path = os.path.join(work_dir, "modules.load.recovery.new")
open(modules_load_new_path, "w").write(new_modules)

patch_spec = {
    "edits": [
        {"path": "init.recovery.service.rc", "new_content_file": service_rc_new_path},
        {"path": "lib/modules/modules.load.recovery", "new_content_file": modules_load_new_path},
    ],
    "adds": [
        {"path": "system/lib64/libverbose_abort_shim.so", "src_file": shim_so},
        {"path": "lib/modules/scp.ko", "src_file": scp_ko},
        {"path": "lib/modules/xiaomi_touch_rodin.ko", "src_file": xiaomi_touch_ko},
        {"path": "lib/modules/goodix_core_rodin.ko", "src_file": goodix_ko},
        {"path": "lib/modules/focaltech_touch_rodin.ko", "src_file": focaltech_ko},
    ],
}
patch_spec_path = os.path.join(work_dir, "cpio_patch_spec.json")
json.dump(patch_spec, open(patch_spec_path, "w"))

run([sys.executable, cpio_patch_script, recovery_raw, recovery_fixed, patch_spec_path])

# -19 with an explicit --zstd=wlog=23 (8MiB window): the default window for
# this input size is already 8MiB, but pin it explicitly so this stays
# under lib/decompress_unzstd.c's window-size rejection check regardless
# of zstd CLI version drift.
run(["zstd", "-19", "--zstd=wlog=23", "-T0", "-f", recovery_fixed, "-o", recovery_zst])
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

# 3b. Recompress the STOCK platform fragment as zstd too (it ships as lz4).
# Fragments that get concatenated at boot (platform+recovery, for the
# recovery-boot path) are decompressed as a single stream, so both must
# share one compression format -- see docstring above (3rd attempt).
platform_frag_idx = None
for i, a in enumerate(args):
    if a == "--vendor_ramdisk_fragment" and i + 1 != recovery_frag_idx:
        platform_frag_idx = i + 1
        break
assert platform_frag_idx is not None, \
    "stock vendor_boot.img has no platform vendor_ramdisk_fragment to replace"

platform_lz4 = args[platform_frag_idx]
platform_raw = os.path.join(work_dir, "platform.raw.cpio")
platform_zst = os.path.join(work_dir, "platform.zst")
run(["lz4", "-d", "-f", platform_lz4, platform_raw])
run(["zstd", "-19", "--zstd=wlog=23", "-T0", "-f", platform_raw, "-o", platform_zst])
p_before = os.path.getsize(platform_lz4)
p_after = os.path.getsize(platform_zst)
print(f"platform fragment: {p_before:,} bytes (lz4) -> {p_after:,} bytes (zstd), "
      f"saved {p_before - p_after:,} bytes")
args[platform_frag_idx] = platform_zst
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
