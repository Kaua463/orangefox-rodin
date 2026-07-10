OF_MAINTAINER := local
FOX_TARGET_DEVICES := rodin
OF_USE_MAGISKBOOT := 1
OF_USE_NEW_MAGISKBOOT := 1
# vendor_boot.img came out 79474688 bytes vs the 67108864-byte real
# partition (recovery ramdisk fragment alone was 49785797 bytes / ~47.5MB).
# LZMA compresses noticeably better than LZ4 (LZ4 favors decompression
# speed, not ratio) and FOX_VENDOR_BOOT_RECOVERY / FOX_DRASTIC_SIZE_REDUCTION
# are OrangeFox's own documented flags for exactly this size-constrained
# hdr4 vendor_boot scenario.
OF_USE_LZMA_COMPRESSION := 1
FOX_VENDOR_BOOT_RECOVERY := 1
FOX_DRASTIC_SIZE_REDUCTION := 1
OF_USE_SYSTEM_FINGERPRINT := 1
# Virtual A/B devices are treated as Vanilla builds by OrangeFox 14.1.
# All-block OTA support is incompatible with that mode.
OF_SUPPORT_ALL_BLOCK_OTA_UPDATES := 0
OF_NO_TREBLE_COMPATIBILITY_CHECK := 1
OF_MANUAL_ROOT_VENDOR_ERROR_FIX := 1
OF_ENABLE_LPTOOLS := 1
OF_USE_TWRP_SAR_DETECT := 1
OF_FLASHLIGHT_ENABLE := 0
OF_SKIP_MULTIUSER_FOLDERS_BACKUP := 1
