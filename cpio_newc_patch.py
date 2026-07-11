#!/usr/bin/env python3
"""Surgically patch a newc-format cpio archive: edit one or more text
files' content in place and insert one or more new files, leaving
every other entry byte-for-byte untouched (same metadata, same order).
Much lower risk than a full extract-modify-repack cycle (no root/
fakeroot needed, no chance of losing permissions/ownership on the
4000+ files we don't touch).

Usage: cpio_newc_patch.py <in.cpio> <out.cpio> <spec.json>

spec.json:
{
  "edits": [{"path": "init.recovery.service.rc", "new_content_file": "/tmp/new.rc"}, ...],
  "adds":  [{"path": "system/lib64/libfoo.so", "src_file": "/tmp/libfoo.so"}, ...]
}
Each "adds" entry copies mode/uid/gid from a sibling file already in
the same cpio directory (matched by shared file extension), falling
back to 0644/root:root if no sibling is found.
"""
import sys
import json

HEADER_MAGIC = b"070701"
HEADER_LEN = 110  # 6 magic + 13*8 hex fields


def pad4(n):
    return (4 - (n % 4)) % 4


def read_entries(data):
    entries = []
    off = 0
    while True:
        magic = data[off:off + 6]
        assert magic == HEADER_MAGIC, f"bad magic at {off}: {magic}"
        fields = [int(data[off + 6 + i * 8: off + 6 + i * 8 + 8], 16) for i in range(13)]
        (ino, mode, uid, gid, nlink, mtime, filesize,
         devmajor, devminor, rdevmajor, rdevminor, namesize, check) = fields
        name_start = off + HEADER_LEN
        name = data[name_start:name_start + namesize - 1]  # strip trailing NUL
        header_total = HEADER_LEN + namesize
        header_total += pad4(header_total)
        data_start = off + header_total
        filedata = data[data_start:data_start + filesize]
        data_total = filesize + pad4(filesize)
        entry_end = data_start + data_total
        entries.append({
            "ino": ino, "mode": mode, "uid": uid, "gid": gid, "nlink": nlink,
            "mtime": mtime, "devmajor": devmajor, "devminor": devminor,
            "rdevmajor": rdevmajor, "rdevminor": rdevminor, "check": check,
            "name": name, "data": filedata,
        })
        if name == b"TRAILER!!!":
            off = entry_end
            break
        off = entry_end
    return entries, off


def build_entry(name, data, mode, uid=0, gid=0, nlink=1, mtime=0,
                 devmajor=0, devminor=0, rdevmajor=0, rdevminor=0, ino=0, check=0):
    namesize = len(name) + 1  # include NUL
    filesize = len(data)
    header = HEADER_MAGIC + b"".join(
        f"{v:08x}".encode() for v in [
            ino, mode, uid, gid, nlink, mtime, filesize,
            devmajor, devminor, rdevmajor, rdevminor, namesize, check,
        ]
    )
    out = header + name + b"\x00"
    out += b"\x00" * pad4(len(out))
    out += data
    out += b"\x00" * pad4(len(data))
    return out


def sibling_meta(entries, new_path):
    """Find an existing entry in the same directory with the same
    extension to copy mode/uid/gid from; fall back to 0644/root/root."""
    new_dir = "/".join(new_path.split("/")[:-1]).encode()
    ext = new_path.rsplit(".", 1)[-1] if "." in new_path else ""
    for e in entries:
        name = e["name"]
        if name.startswith(new_dir + b"/") and ext and name.endswith(b"." + ext.encode()):
            return e["mode"], e["uid"], e["gid"]
    return 0o100644, 0, 0


def main():
    in_path, out_path, spec_path = sys.argv[1:4]
    spec = json.load(open(spec_path))
    data = open(in_path, "rb").read()
    entries, consumed = read_entries(data)
    trailing_padding = data[consumed:]  # zero-padding to block boundary after TRAILER

    edits = {e["path"].encode(): e["new_content_file"] for e in spec.get("edits", [])}
    adds = spec.get("adds", [])

    edited_paths = set()
    out_chunks = []

    for e in entries:
        if e["name"] == b"TRAILER!!!":
            for add in adds:
                mode, uid, gid = sibling_meta(entries, add["path"])
                new_data = open(add["src_file"], "rb").read()
                out_chunks.append(build_entry(
                    add["path"].encode(), new_data, mode=mode, uid=uid, gid=gid,
                ))
                print(f"added {add['path']}: {len(new_data)} bytes "
                      f"(mode={oct(mode)} uid={uid} gid={gid})")
            out_chunks.append(build_entry(e["name"], e["data"], mode=e["mode"],
                                           uid=e["uid"], gid=e["gid"], nlink=e["nlink"]))
            continue
        if e["name"] in edits:
            new_content = open(edits[e["name"]], "rb").read()
            out_chunks.append(build_entry(e["name"], new_content, mode=e["mode"],
                                           uid=e["uid"], gid=e["gid"], nlink=e["nlink"],
                                           mtime=e["mtime"], ino=e["ino"]))
            edited_paths.add(e["name"])
            print(f"edited {e['name'].decode()}: {len(new_content)} bytes")
            continue
        out_chunks.append(build_entry(e["name"], e["data"], mode=e["mode"],
                                       uid=e["uid"], gid=e["gid"], nlink=e["nlink"],
                                       mtime=e["mtime"], ino=e["ino"],
                                       devmajor=e["devmajor"], devminor=e["devminor"],
                                       rdevmajor=e["rdevmajor"], rdevminor=e["rdevminor"]))

    missing = set(edits) - edited_paths
    assert not missing, f"never found entries to edit: {missing}"

    result = b"".join(out_chunks) + trailing_padding
    open(out_path, "wb").write(result)
    print(f"wrote {out_path}: {len(result)} bytes, "
          f"{len(entries) + len(adds)} entries (was {len(entries)})")


if __name__ == "__main__":
    main()
