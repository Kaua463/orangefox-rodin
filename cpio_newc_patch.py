#!/usr/bin/env python3
"""Surgically patch a newc-format cpio archive: modify one text file's
content in place and insert one new file, leaving every other entry
byte-for-byte untouched (same metadata, same order). Much lower risk
than a full extract-modify-repack cycle (no root/fakeroot needed, no
chance of losing permissions/ownership on the 4000+ files we don't
touch).
"""
import sys

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


def main():
    in_path, out_path, edit_name, edit_new_content, new_file_name, new_file_path = sys.argv[1:7]
    data = open(in_path, "rb").read()
    entries, consumed = read_entries(data)
    trailing_padding = data[consumed:]  # zero-padding to block boundary after TRAILER

    edit_name_b = edit_name.encode()
    new_file_name_b = new_file_name.encode()
    new_file_data = open(new_file_path, "rb").read()

    edited = False
    out_chunks = []
    # find a sibling .so entry in the same dir as the new file to copy mode/uid/gid from
    new_dir = "/".join(new_file_name.split("/")[:-1]).encode()
    sibling_mode, sibling_uid, sibling_gid = 0o100644, 0, 0
    for e in entries:
        if e["name"].startswith(new_dir + b"/") and e["name"].endswith(b".so"):
            sibling_mode, sibling_uid, sibling_gid = e["mode"], e["uid"], e["gid"]
            break

    for e in entries:
        if e["name"] == b"TRAILER!!!":
            # insert our new file entry right before TRAILER
            out_chunks.append(build_entry(
                new_file_name_b, new_file_data,
                mode=sibling_mode, uid=sibling_uid, gid=sibling_gid,
            ))
            out_chunks.append(build_entry(e["name"], e["data"], mode=e["mode"],
                                           uid=e["uid"], gid=e["gid"], nlink=e["nlink"]))
            continue
        if e["name"] == edit_name_b:
            new_content = open(edit_new_content, "rb").read()
            out_chunks.append(build_entry(e["name"], new_content, mode=e["mode"],
                                           uid=e["uid"], gid=e["gid"], nlink=e["nlink"],
                                           mtime=e["mtime"], ino=e["ino"]))
            edited = True
            continue
        out_chunks.append(build_entry(e["name"], e["data"], mode=e["mode"],
                                       uid=e["uid"], gid=e["gid"], nlink=e["nlink"],
                                       mtime=e["mtime"], ino=e["ino"],
                                       devmajor=e["devmajor"], devminor=e["devminor"],
                                       rdevmajor=e["rdevmajor"], rdevminor=e["rdevminor"]))

    assert edited, f"never found entry to edit: {edit_name}"
    result = b"".join(out_chunks) + trailing_padding
    open(out_path, "wb").write(result)
    print(f"wrote {out_path}: {len(result)} bytes, {len(entries)+1} entries (was {len(entries)})")


if __name__ == "__main__":
    main()
