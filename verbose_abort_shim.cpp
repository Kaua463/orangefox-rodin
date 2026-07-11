// Compat shim for a crashing recovery binary. libsysutils.so (and
// therefore system/bin/recovery, which links it directly) references
// std::__1::__libcpp_verbose_abort(char const*, ...) -- a libc++
// hardening-mode symbol -- but NO libc++.so anywhere in this OrangeFox
// branch's build output actually exports it (verified: every libc++.so
// under out/target/product/rodin, deduplicated by content hash, is
// missing it). This is a version-skew bug between the branch's pinned
// clang prebuilt (assumes a newer libc++ hardening ABI) and its pinned
// external/libcxx source (predates that symbol's implementation).
//
// Rather than patch the synced AOSP/OrangeFox source tree, this shim
// just provides the missing symbol directly: same behavior real
// __libcpp_verbose_abort has when hardening actually fires (abort()),
// minus the diagnostic message. It's loaded via LD_PRELOAD on the
// 'recovery' service (see init.recovery.service.rc) so the dynamic
// linker resolves libsysutils.so's undefined reference against this
// instead of failing to link the binary at all.
#include <cstdlib>

namespace std {
inline namespace __1 {

[[noreturn]] void __libcpp_verbose_abort(const char* format, ...) {
    abort();
}

}
}
