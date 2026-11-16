"""Read-only access to the files inside a FAT disk image.

Enough of FAT16/FAT32 to list and extract every file from an SD card image
that holds one FAT partition (with or without an MBR in front of it), so the
files can be copied to a card with ordinary file operations instead of
writing the image to the raw disk.
"""

import struct


class FatError(ValueError):
    pass


class FatImage:
    def __init__(self, data):
        self.data = data
        self.base = self._find_partition()
        b = self.base
        boot = data[b:b + 512]
        if len(boot) < 512 or boot[510:512] != b"\x55\xaa":
            raise FatError("no FAT boot sector found")
        self.bytes_per_sector = struct.unpack_from("<H", boot, 11)[0]
        self.sectors_per_cluster = boot[13]
        reserved = struct.unpack_from("<H", boot, 14)[0]
        fats = boot[16]
        root_entries = struct.unpack_from("<H", boot, 17)[0]
        total = struct.unpack_from("<H", boot, 19)[0] or struct.unpack_from("<I", boot, 32)[0]
        fat_size = struct.unpack_from("<H", boot, 22)[0] or struct.unpack_from("<I", boot, 36)[0]
        if self.bytes_per_sector not in (512, 1024, 2048, 4096) or not self.sectors_per_cluster or not fats:
            raise FatError("not a FAT file system")

        bps = self.bytes_per_sector
        root_sectors = (root_entries * 32 + bps - 1) // bps
        self.fat_offset = b + reserved * bps
        self.data_offset = b + (reserved + fats * fat_size + root_sectors) * bps
        self.cluster_bytes = self.sectors_per_cluster * bps
        clusters = (total - (reserved + fats * fat_size + root_sectors)) // self.sectors_per_cluster
        if clusters < 4085:
            raise FatError("FAT12 images are not supported")
        self.fat32 = clusters >= 65525
        if self.fat32:
            self.root_cluster = struct.unpack_from("<I", boot, 44)[0]
            self.root_dir = None
        else:
            self.root_cluster = None
            start = b + (reserved + fats * fat_size) * bps
            self.root_dir = data[start:start + root_entries * 32]
        self.volume_bytes = total * bps

    def _find_partition(self):
        """Byte offset of the FAT volume: the first MBR partition, or 0."""
        d = self.data
        if len(d) < 512 or d[510:512] != b"\x55\xaa":
            raise FatError("not a disk image")
        # A FAT boot sector starts with a jump instruction; an MBR does not have to.
        if d[0] in (0xEB, 0xE9) and struct.unpack_from("<H", d, 11)[0] in (512, 1024, 2048, 4096):
            return 0
        for n in range(4):
            entry = d[446 + 16 * n:462 + 16 * n]
            if entry[4] in (0x04, 0x06, 0x0B, 0x0C, 0x0E):
                return struct.unpack_from("<I", entry, 8)[0] * 512
        raise FatError("no FAT partition in the image")

    def _next(self, cluster):
        if self.fat32:
            value = struct.unpack_from("<I", self.data, self.fat_offset + cluster * 4)[0] & 0x0FFFFFFF
            return None if value >= 0x0FFFFFF8 or value < 2 else value
        value = struct.unpack_from("<H", self.data, self.fat_offset + cluster * 2)[0]
        return None if value >= 0xFFF8 or value < 2 else value

    def _chain(self, cluster):
        parts, seen = [], set()
        while cluster is not None:
            if cluster in seen:
                raise FatError("cluster chain loops")
            seen.add(cluster)
            offset = self.data_offset + (cluster - 2) * self.cluster_bytes
            parts.append(self.data[offset:offset + self.cluster_bytes])
            cluster = self._next(cluster)
        return b"".join(parts)

    def _entries(self, raw):
        """Yields (name, is_dir, first_cluster, size) for one directory."""
        long_parts = []
        for i in range(0, len(raw) - 31, 32):
            e = raw[i:i + 32]
            if e[0] == 0x00:
                break
            if e[0] == 0xE5:
                long_parts = []
                continue
            attr = e[11]
            if attr == 0x0F:  # long file name piece; they come last piece first
                long_parts.insert(0, e[1:11] + e[14:26] + e[28:32])
                continue
            if attr & 0x08:  # volume label
                long_parts = []
                continue
            if long_parts:
                name = b"".join(long_parts).decode("utf-16-le", "replace").split("\x00")[0]
            else:
                base = e[0:8].decode("cp437").rstrip()
                ext = e[8:11].decode("cp437").rstrip()
                if e[12] & 0x08:
                    base = base.lower()
                if e[12] & 0x10:
                    ext = ext.lower()
                name = base + ("." + ext if ext else "")
            long_parts = []
            if name in (".", ".."):
                continue
            cluster = (struct.unpack_from("<H", e, 20)[0] << 16) | struct.unpack_from("<H", e, 26)[0]
            yield name, bool(attr & 0x10), cluster, struct.unpack_from("<I", e, 28)[0]

    def files(self):
        """Yields (path, bytes) for every file, paths using forward slashes."""
        root = self._chain(self.root_cluster) if self.fat32 else self.root_dir
        stack = [("", root)]
        while stack:
            prefix, raw = stack.pop()
            for name, is_dir, cluster, size in self._entries(raw):
                if any(c in name for c in '/\\:*?"<>|') or name.strip(". ") == "":
                    raise FatError("unsafe file name in image: %r" % name)
                path = prefix + name
                if is_dir:
                    stack.append((path + "/", self._chain(cluster) if cluster else b""))
                else:
                    yield path, (self._chain(cluster)[:size] if cluster else b"")
