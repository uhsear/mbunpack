#!/usr/bin/env python
"""Unpack an MBTiles file, naming every tile by what its bytes are.

An MBTiles file stores each tile as a blob, and a blob has no file name. An
exporter has to invent the extension. Some take it from a command line flag
whose default is png and never look at the blob, so a gzip-compressed vector
tile is written under .png or under a bare .pbf, and the directory looks
fine until a viewer asks for it. mbunpack reads the format from the first
bytes of each tile instead: PNG, JPEG and WebP by their signatures, a Mapbox
Vector Tile by parsing its protobuf structure, and gzip by its magic number.
The extension it writes says what the bytes are.

    python mbunpack.py --self-test
    python mbunpack.py tiles.mbtiles
    python mbunpack.py tiles.mbtiles --out tiles_dir --apply
    python mbunpack.py tiles.mbtiles --out tiles_dir --decompress --apply
    python mbunpack.py tiles.mbtiles --scheme tms
    python mbunpack.py tiles_dir

Given an .mbtiles file it reports the formats it found, compares them with the
format row in the metadata table, and writes a z/x/y directory only with
--apply. Given a directory it only reads: every z/x/y.ext file whose name
disagrees with its bytes is listed. Nothing else writes. The MBTiles file is
opened read-only and immutable, so SQLite makes no -wal, -shm or journal file
beside it. A symbolic link or special file in a folder is listed and never
opened.

Exit codes: 0 every tile was identified from its bytes and none disagrees with
the declared format, 1 at least one tile is unknown, damaged, misnamed, off the
grid, duplicated or disagrees with the metadata, 2 a file could not be read or
written, 4 nothing was compared (no tile was identified), 64 usage error,
70 an unexpected crash, which is never a finding.
"""

from __future__ import print_function

import argparse
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import traceback
import urllib.request
import zlib

# =============================================================================
# The decision core. Pure functions over bytes and numbers: no file, no
# database and no clock. Everything below the "I/O" banner touches the world.
# =============================================================================

PNG = "png"
JPEG = "jpg"
WEBP = "webp"
MVT = "mvt"

# The tile kinds mbunpack can name, and the extension each one gets.
EXT = {PNG: "png", JPEG: "jpg", WEBP: "webp", MVT: "mvt"}

# What the MBTiles 1.3 metadata row "format" calls each kind. A Mapbox Vector
# Tile is "pbf" there, and the spec says pbf means gzip-compressed.
FAMILY = {PNG: "png", JPEG: "jpg", WEBP: "webp", MVT: "pbf"}
KNOWN_FORMATS = ("pbf", "png", "jpg", "webp")

OK = "OK"
EMPTY = "EMPTY"
UNKNOWN = "UNKNOWN"
CORRUPT = "CORRUPT"
BADCOORD = "BADCOORD"
DUPLICATE = "DUPLICATE"
MISNAMED = "MISNAMED"
SKIPPED = "SKIPPED"
STATUSES = (OK, EMPTY, UNKNOWN, CORRUPT, BADCOORD, DUPLICATE, MISNAMED,
            SKIPPED)
# A tile in one of these makes the run exit 1. EMPTY and SKIPPED do not.
FINDING_STATUSES = (UNKNOWN, CORRUPT, BADCOORD, DUPLICATE, MISNAMED)

MEANING = {
    EMPTY: "zero-byte tile, there is nothing to write",
    UNKNOWN: "bytes match no format mbunpack reads",
    CORRUPT: "the bytes cannot be read",
    BADCOORD: "tile address is not a cell of the tile grid",
    DUPLICATE: "a second tile for one address, the later copy is not used",
    MISNAMED: "file name disagrees with the bytes inside it",
    SKIPPED: "not z/x/y.ext, or a link or special file, so it was not read",
}

DEFAULT_SCHEME = "xyz"
SCHEMES = ("xyz", "tms")
DEFAULT_SAMPLE = 10

# Zoom 30 is a grid of about a billion tiles a side, far past any real
# tileset. The ceiling only stops a corrupt zoom_level from sizing a huge
# integer. A tile above it is BADCOORD.
MAX_ZOOM = 30

# The most one tile may inflate to, and the most one tile file may hold. It is
# the limit this tool chose. A gzip bomb is refused instead of inflated, and a
# larger file is refused instead of read whole.
MAX_INFLATED = 64 * 1024 * 1024

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
GZIP_MAGIC = b"\x1f\x8b"

# Field number to the wire type the vector tile proto gives it.
_LAYER_WIRE = {1: 2, 2: 2, 3: 2, 4: 2, 5: 0, 15: 0}
_TILE_LAYER_TAG = (3 << 3) | 2

# A grid cell has at most ten digits (2 ** 30 - 1). A longer run of digits
# cannot be a tile name, and int() on thousands of digits raises ValueError on
# Python 3.11 and later, so the run is bounded before int() sees it.
MAX_DIGITS = 20
_DIGITS = re.compile(r"[0-9]{1,%d}\Z" % MAX_DIGITS)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class Sniff(object):
    """What the bytes of one tile are. payload is the bytes after gzip."""

    def __init__(self, kind, gzipped, detail, payload):
        self.kind = kind
        self.gzip = gzipped
        self.detail = detail
        self.payload = payload


class Item(object):
    """The verdict on one tile or one file, and what to write for it."""

    def __init__(self, status, where, rel="", payload=b"", kind="",
                 gzipped=False, detail=""):
        self.status = status
        self.where = where
        self.rel = rel
        self.payload = payload
        self.kind = kind
        self.gzip = gzipped
        self.detail = detail


def clean(text):
    """text with each control character shown as \\xNN, so a hostile file name
    or metadata value cannot move the cursor or set a title in a terminal."""
    return _CONTROL.sub(lambda m: "\\x%02x" % ord(m.group()), text)


def _varint(buf, pos):
    """Read one protobuf varint. Returns (value, next position). A varint that
    runs off the end of buf, or past ten bytes, is a ValueError."""
    value = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("varint runs off the end of the data")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift >= 70:
            raise ValueError("varint is longer than ten bytes")


def _layer_ok(buf):
    """True when buf parses as a vector tile Layer message that names itself
    and, if it states a version, states 1 or 2."""
    pos = 0
    named = False
    while pos < len(buf):
        tag, pos = _varint(buf, pos)
        field = tag >> 3
        wire = tag & 7
        if field == 0:
            return False
        expected = _LAYER_WIRE.get(field)
        if expected is not None and expected != wire:
            return False
        if wire == 0:
            value, pos = _varint(buf, pos)
            if field == 15 and value not in (1, 2):
                return False
        elif wire == 2:
            length, pos = _varint(buf, pos)
            if field == 1 and length > 0:
                named = True
            pos += length
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            return False
        if pos > len(buf):
            return False
    return named


def is_mvt(data):
    """True when data is a protobuf Tile message of Mapbox Vector Tile layers.

    A Tile holds only repeated field 3, the layers. Every byte must belong to
    one, each layer must parse, and each must carry a name. There is no magic
    number in this format, so the structure is the signature."""
    pos = 0
    try:
        while pos < len(data):
            tag, pos = _varint(data, pos)
            if tag != _TILE_LAYER_TAG:
                return False
            length, pos = _varint(data, pos)
            end = pos + length
            if end > len(data) or not _layer_ok(data[pos:end]):
                return False
            pos = end
    except ValueError:
        return False
    return bool(data)


def _sniff_raw(data, gzipped):
    if not data:
        return Sniff(EMPTY, gzipped, "zero bytes", data)
    if data.startswith(PNG_SIGNATURE):
        return Sniff(PNG, gzipped, "", data)
    if data.startswith(JPEG_SIGNATURE):
        return Sniff(JPEG, gzipped, "", data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return Sniff(WEBP, gzipped, "", data)
    if is_mvt(data):
        return Sniff(MVT, gzipped, "", data)
    return Sniff(UNKNOWN, gzipped, "begins %s" % data[:8].hex(), data)


def sniff(data, limit=MAX_INFLATED):
    """Name the format of one tile from its bytes. A gzip stream is inflated,
    at most limit bytes of it, and the inner bytes are named. The result's kind
    is a tile kind, or EMPTY, UNKNOWN or CORRUPT."""
    if data[:2] != GZIP_MAGIC:
        return _sniff_raw(data, False)
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        inner = inflater.decompress(data, limit)
    except zlib.error as exc:
        return Sniff(CORRUPT, True, "gzip stream is damaged (%s)" % exc, data)
    if not inflater.eof:
        if inflater.unconsumed_tail:
            why = "gzip stream inflates past %d bytes" % limit
        else:
            why = "gzip stream is truncated"
        return Sniff(CORRUPT, True, why, data)
    if inflater.unused_data:
        return Sniff(CORRUPT, True,
                     "%d byte(s) follow the end of the gzip stream"
                     % len(inflater.unused_data), data)
    if inner[:2] == GZIP_MAGIC:
        return Sniff(UNKNOWN, True, "gzip inside gzip", inner)
    return _sniff_raw(inner, True)


def tile_ext(kind, gzipped, decompress):
    """The extension that tells the truth about the bytes written."""
    ext = EXT[kind]
    return ext + ".gz" if gzipped and not decompress else ext


def kind_label(kind, gzipped):
    return "%s (gzip)" % kind if gzipped else kind


def disagrees(declared, kind, gzipped):
    """True when a tile does not match the metadata format row. The spec says
    pbf means gzip-compressed vector tiles, so a raw one disagrees, and so does
    a gzipped image."""
    return FAMILY[kind] != declared or gzipped != (declared == "pbf")


def _address_problem(z, col, row):
    """Why (z, col, row) is not a cell of the tile grid, or '' when it is."""
    for name, value in (("zoom_level", z), ("tile_column", col),
                        ("tile_row", row)):
        if type(value) is not int:
            return "%s is %r, not an integer" % (name, value)
    if not 0 <= z <= MAX_ZOOM:
        return "zoom_level %d is outside 0..%d" % (z, MAX_ZOOM)
    span = 2 ** z
    if not (0 <= col < span and 0 <= row < span):
        return ("column %d, row %d is outside the %d x %d grid of zoom %d"
                % (col, row, span, span, z))
    return ""


def classify_tile(z, col, row, data, scheme=DEFAULT_SCHEME, decompress=False,
                  limit=MAX_INFLATED):
    """Decide one MBTiles row. MBTiles keeps tile_row in the TMS scheme, y
    counted from the south, so scheme xyz flips it to y counted from the north
    and scheme tms leaves it."""
    why = _address_problem(z, col, row)
    if why:
        return Item(BADCOORD, "%s/%s/%s" % (z, col, row), detail=why)
    if not isinstance(data, bytes):
        what = "NULL" if data is None else type(data).__name__
        return Item(CORRUPT, "%d/%d/%d" % (z, col, row),
                    detail="tile_data is %s, not a blob" % what)
    y = 2 ** z - 1 - row if scheme == "xyz" else row
    where = "%d/%d/%d" % (z, col, y)
    found = sniff(data, limit)
    if found.kind not in EXT:
        return Item(found.kind, where, kind=found.kind, gzipped=found.gzip,
                    detail=found.detail)
    return Item(OK, where,
                rel="%s.%s" % (where, tile_ext(found.kind, found.gzip,
                                               decompress)),
                payload=found.payload if decompress else data,
                kind=found.kind, gzipped=found.gzip)


def _tile_address(parts):
    """(z, x, y, extension) when parts is a z/x/y.ext path, else None. The
    extension is everything after the first dot, and '' when there is none."""
    if len(parts) != 3:
        return None
    stem, _, ext = parts[2].partition(".")
    if not all(_DIGITS.match(p) for p in (parts[0], parts[1], stem)):
        return None
    return int(parts[0]), int(parts[1]), int(stem), ext


def classify_file(parts, reader, limit=MAX_INFLATED):
    """Decide one file of an unpacked tree. parts is its path below the root,
    split into names. reader() returns its bytes, at most limit + 1 of them,
    and is not called for a path that is not a tile or is off the grid."""
    where = "/".join(parts)
    address = _tile_address(parts)
    if address is None:
        return Item(SKIPPED, where)
    z, x, y, ext = address
    why = _address_problem(z, x, y)
    if why:
        return Item(BADCOORD, where, detail=why)
    try:
        data = reader()
    except OSError as exc:
        return Item(CORRUPT, where, detail="could not be read (%s)"
                    % (exc.strerror or type(exc).__name__))
    if len(data) > limit:
        return Item(CORRUPT, where,
                    detail="file is larger than %d bytes, so it was not read "
                           "whole" % limit)
    found = sniff(data, limit)
    if found.kind not in EXT:
        return Item(found.kind, where, kind=found.kind, gzipped=found.gzip,
                    detail=found.detail)
    expected = tile_ext(found.kind, found.gzip, False)
    if ext == expected:
        return Item(OK, where, kind=found.kind, gzipped=found.gzip)
    return Item(MISNAMED, where, kind=found.kind, gzipped=found.gzip,
                detail="bytes are %s, the name ends %s, expected .%s"
                % (kind_label(found.kind, found.gzip),
                   "." + ext if ext else "with no extension", expected))


class Report(object):
    """Counts and a few examples. Every input row or file lands in exactly one
    status, so the counts add up to total."""

    def __init__(self, mode, source, declared, scheme, sample):
        self.mode = mode
        self.source = source
        self.declared = declared
        self.scheme = scheme
        self.sample = sample
        self.total = 0
        self.counts = dict((s, 0) for s in STATUSES)
        self.kinds = {}
        self.declared_bad = 0
        self.samples = {}

    def note(self, label, text):
        shown = self.samples.setdefault(label, [])
        if len(shown) < self.sample:
            shown.append(clean(text))

    def add(self, item):
        self.total += 1
        self.counts[item.status] += 1
        if item.status != OK:
            self.note(item.status, "%s  %s" % (item.where, item.detail)
                      if item.detail else item.where)
            return
        key = (item.kind, item.gzip)
        self.kinds[key] = self.kinds.get(key, 0) + 1
        if (self.mode == "tileset" and self.declared in KNOWN_FORMATS
                and disagrees(self.declared, item.kind, item.gzip)):
            self.declared_bad += 1
            self.note("DECLARED", "%s  bytes are %s, metadata says %s"
                      % (item.where, kind_label(item.kind, item.gzip),
                         self.declared))

    def findings(self):
        return (sum(self.counts[s] for s in FINDING_STATUSES)
                + self.declared_bad)


def unpack(rows, declared, scheme=DEFAULT_SCHEME, decompress=False,
           sink=None, sample=DEFAULT_SAMPLE, limit=MAX_INFLATED,
           source=""):
    """Classify tile rows. rows yields (zoom_level, tile_column, tile_row,
    tile_data) sorted by those three columns, so a duplicate address sits next
    to its twin. sink(rel, payload) is called for each tile worth writing and
    is None on a dry run."""
    report = Report("tileset", source, declared, scheme, sample)
    previous = None
    for z, col, row, data in rows:
        address = (z, col, row)
        if address == previous:
            item = Item(DUPLICATE, "%s/%s/%s" % address,
                        detail="a second row for one address")
        else:
            item = classify_tile(z, col, row, data, scheme, decompress, limit)
            if item.status == OK and sink is not None:
                sink(item.rel, item.payload)
        previous = address
        report.add(item)
    return report


def audit(entries, read, sample=DEFAULT_SAMPLE, limit=MAX_INFLATED,
          source=""):
    """Classify the files of an unpacked tree. entries yields (parts, path)
    and read(path) returns the file's bytes. A path of None marks a link or
    special file, which is listed and never read. A second file at one
    address, whatever its extension or zero padding, is a DUPLICATE."""
    report = Report("tree", source, None, "", sample)
    seen = set()
    for parts, path in entries:
        where = "/".join(parts)
        address = _tile_address(parts)
        key = address[:3] if address else None
        if path is None:
            item = Item(SKIPPED, where, detail="link or special file")
        elif key is not None and key in seen:
            item = Item(DUPLICATE, where,
                        detail="a second file for one address")
        else:
            seen.add(key)
            item = classify_file(parts, lambda p=path: read(p), limit)
        report.add(item)
    return report


def gate(report):
    """The exit status. A run that identified nothing is not a clean run."""
    findings = report.findings()
    if report.counts[OK] == 0 and not findings:
        return 4
    return 1 if findings else 0


def describe(report):
    """The report as a list of lines."""
    unit = "tile(s)" if report.mode == "tileset" else "file(s)"
    head = "mbunpack: %s: %d %s read" % (report.source, report.total, unit)
    if report.mode == "tileset":
        head += ", scheme %s" % report.scheme
    lines = [head]
    if report.mode == "tileset":
        if report.declared is None:
            lines.append("declared format: none, the metadata has no format "
                         "row")
        elif report.declared in KNOWN_FORMATS:
            lines.append("declared format: %s" % report.declared)
        else:
            lines.append("declared format: %s, which mbunpack does not read, "
                         "so no tile was compared with it"
                         % clean(report.declared))
    if report.kinds:
        lines.append("identified from the bytes:")
        for (kind, gzipped), count in sorted(report.kinds.items()):
            lines.append("  %-14s %d" % (kind_label(kind, gzipped), count))
    for status in STATUSES[1:]:
        count = report.counts[status]
        if count:
            lines.append("%s %d: %s" % (status, count, MEANING[status]))
            for text in report.samples.get(status, []):
                lines.append("    " + text)
    if report.declared_bad:
        lines.append("DECLARED %d: the bytes disagree with the metadata "
                     "format row" % report.declared_bad)
        for text in report.samples.get("DECLARED", []):
            lines.append("    " + text)
    code = gate(report)
    if code == 4:
        lines.append("VERDICT: nothing was compared. %d %s read and none was "
                     "identified." % (report.total, unit))
    elif code == 1:
        lines.append("VERDICT: %d finding(s) in %d %s."
                     % (report.findings(), report.total, unit))
    else:
        agrees = report.declared in KNOWN_FORMATS
        unread = ["%d %s" % (report.counts[s], s) for s in (EMPTY, SKIPPED)
                  if report.counts[s]]
        if unread:
            lines.append("VERDICT: %d of %d %s were identified from their "
                         "bytes%s. Not identified, and not counted as a "
                         "finding: %s."
                         % (report.counts[OK], report.total, unit,
                            " and agree with the declared format"
                            if agrees else "", ", ".join(unread)))
        else:
            lines.append("VERDICT: every tile was identified from its "
                         "bytes%s." % (" and agrees with the declared format"
                                       if agrees else ""))
    return lines


# =============================================================================
# I/O. Everything that opens a file or a database.
# =============================================================================

def open_readonly(path):
    """Open an MBTiles file so that nothing can write to it, and so that
    SQLite makes no file beside it. mode=ro alone still creates a -wal and a
    -shm file next to a WAL-mode database. immutable=1 stops that, and it also
    makes SQLite ignore a -wal or -journal file, so a non-empty one is refused
    here: its changes would be missing from the read. The path is made a URI,
    so characters such as # and ? in a directory name stay in the path and do
    not start a fragment or a query."""
    full = os.path.abspath(path)
    for suffix in ("-wal", "-journal"):
        side = full + suffix
        if os.path.exists(side) and os.path.getsize(side):
            raise OSError("%s holds changes that are not in %s. mbunpack "
                          "reads the main file only and writes nothing, so "
                          "merge them first, with the sqlite3 shell on a "
                          "copy." % (side, full))
    uri = "file:%s?mode=ro&immutable=1" % urllib.request.pathname2url(full)
    return sqlite3.connect(uri, uri=True)


def read_metadata(con):
    """The metadata table as a dict. A file without one has no metadata."""
    try:
        return dict(con.execute("select name, value from metadata"))
    except sqlite3.OperationalError:
        return {}


def tile_rows(con):
    return con.execute("select zoom_level, tile_column, tile_row, tile_data "
                       "from tiles order by zoom_level, tile_column, "
                       "tile_row")


def declared_format(meta):
    value = meta.get("format")
    return None if value is None else str(value).strip().lower()


def may_read(mode, folder_real, top):
    """True when an entry is a regular file, not a link, in a folder whose
    real path is top or below it. mode is the st_mode of lstat, top ends in a
    separator. A folder reached through a Windows junction fails the second
    test, because its real path leaves the tree."""
    return stat.S_ISREG(mode) and (folder_real + os.sep).startswith(top)


def tree_entries(root):
    """(parts, path) for every entry below root, in a fixed order. path is
    None for a symbolic link, a special file or a file outside the tree, which
    must not be opened: a FIFO would block the read and a link would read a
    file the folder does not hold."""
    top = os.path.join(os.path.normcase(os.path.realpath(root)), "")
    for folder, dirs, files in os.walk(root):
        dirs.sort()
        real = os.path.normcase(os.path.realpath(folder))
        linked = [d for d in dirs
                  if os.path.islink(os.path.join(folder, d))]
        for name in sorted(files + linked):
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, root)
            mode = os.lstat(path).st_mode
            yield rel.split(os.sep), (path if may_read(mode, real, top)
                                      else None)


def read_file(path, cap=MAX_INFLATED + 1):
    """The first cap bytes of a file. The default is one byte more than a tile
    may hold, so classify_file can tell a file at the limit from one over it."""
    with open(path, "rb") as handle:
        return handle.read(cap)


def make_sink(out):
    """sink(rel, payload) for unpack. It makes each folder as it is needed, so
    a run that writes nothing leaves no directory behind."""
    def sink(rel, payload):
        path = os.path.join(out, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(payload)
    return sink


def write_metadata(out, meta):
    """metadata.json holds the metadata table. A NULL name is written as the
    text None: sort_keys would raise on a key that is not a string, and a
    crash there would exit 70 after the tiles were already written."""
    with open(os.path.join(out, "metadata.json"), "w") as handle:
        json.dump(dict((str(k), v) for k, v in meta.items()), handle,
                  indent=2, sort_keys=True, default=str)


def _tolerate_odd_characters():
    """Make print() unable to fail on a character the console cannot encode.
    A metadata value or a file name can hold one, and the UnicodeEncodeError
    would exit 1 before the VERDICT line, and 1 means a finding."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="replace")


# =============================================================================
# The self-test
# =============================================================================

def self_test():
    """Run the assertions in a temp directory that is removed even when one
    of them raises."""
    tmp = tempfile.mkdtemp(prefix="mbunpack-selftest-")
    try:
        return _self_test(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _enc(value):
    """Encode an int as a protobuf varint."""
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if value:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def _layer(name=b"roads", version=2, extra=b""):
    body = b"\x0a" + _enc(len(name)) + name
    if version is not None:
        body += b"\x78" + _enc(version)
    return body + b"\x28" + _enc(4096) + extra


def _tile(*layers):
    return b"".join(b"\x1a" + _enc(len(layer)) + layer for layer in layers)


def _gz(data):
    return gzip.compress(data, mtime=0)


FIXTURE_PNG = PNG_SIGNATURE + b"\x00\x00\x00\x0dIHDR" + b"\x00" * 17
FIXTURE_JPG = JPEG_SIGNATURE + b"\xe0" + b"\x00" * 16 + b"\xff\xd9"
FIXTURE_WEBP = b"RIFF" + struct.pack("<I", 12) + b"WEBPVP8 " + b"\x00" * 4
FIXTURE_MVT = _tile(_layer(b"roads"), _layer(b"water", 1))


def _make_mbtiles(path, tiles, meta=None, unique=True, metadata_table=True):
    con = sqlite3.connect(path)
    if metadata_table:
        con.execute("create table metadata (name text, value text)")
        for name, value in (meta or {}).items():
            con.execute("insert into metadata values (?, ?)", (name, value))
    con.execute("create table tiles (zoom_level integer, tile_column "
                "integer, tile_row integer, tile_data blob)")
    if unique:
        con.execute("create unique index tile_index on tiles "
                    "(zoom_level, tile_column, tile_row)")
    for z, col, row, data in tiles:
        con.execute("insert into tiles values (?, ?, ?, ?)",
                    (z, col, row, data))
    con.commit()
    con.close()


def _tree(root):
    """{relative path: bytes} for every file below root."""
    found = {}
    for parts, path in tree_entries(root):
        found["/".join(parts)] = read_file(path)
    return found


def _self_test(tmp):
    """Assertions over the decision core and the CLI. No network, no
    credentials. The only files are the ones written under tmp."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def run_cli(argv):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    counter = [0]

    def fresh(name):
        counter[0] += 1
        return os.path.join(tmp, "%d-%s" % (counter[0], name))

    print("mbunpack self-test: temporary files only, no network, no "
          "credentials")
    print("-" * 68)

    # ---- the format is read from the bytes
    check(sniff(FIXTURE_PNG).kind == PNG, "a PNG signature is a png")
    check(sniff(FIXTURE_JPG).kind == JPEG, "a JPEG signature is a jpg")
    check(sniff(FIXTURE_WEBP).kind == WEBP,
          "RIFF with WEBP at byte 8 is a webp")
    check(sniff(FIXTURE_MVT).kind == MVT and not sniff(FIXTURE_MVT).gzip,
          "a protobuf tile of named layers is a raw vector tile")
    gz_mvt = sniff(_gz(FIXTURE_MVT))
    check(gz_mvt.kind == MVT and gz_mvt.gzip and gz_mvt.payload == FIXTURE_MVT,
          "a gzipped vector tile is named a vector tile, and gzip is kept "
          "apart as its own fact  <-- pinned defect")
    check(sniff(_gz(FIXTURE_PNG)).kind == PNG and sniff(_gz(FIXTURE_PNG)).gzip,
          "a gzipped PNG is a png inside gzip")
    check(sniff(b"RIFF\x00\x00\x00\x00WAVEfmt ").kind == UNKNOWN,
          "RIFF without WEBP at byte 8 is not a webp")
    check(sniff(b"RIFF").kind == UNKNOWN, "a bare RIFF header is unknown")
    unknown = sniff(b"hello, tile")
    check(unknown.kind == UNKNOWN and unknown.detail == "begins "
          + b"hello, t".hex(),
          "bytes that match nothing are UNKNOWN and say how they begin")
    check(sniff(b"").kind == EMPTY and sniff(_gz(b"")).kind == EMPTY
          and sniff(_gz(b"")).gzip,
          "zero bytes, and gzip of zero bytes, are EMPTY")
    check(sniff(_gz(_gz(FIXTURE_PNG))).kind == UNKNOWN
          and sniff(_gz(_gz(FIXTURE_PNG))).detail == "gzip inside gzip",
          "gzip inside gzip is named, not unwrapped twice")

    # ---- gzip that cannot be trusted
    whole = _gz(FIXTURE_MVT)
    check(sniff(whole[:-5]).kind == CORRUPT
          and "truncated" in sniff(whole[:-5]).detail,
          "a truncated gzip stream is CORRUPT  <-- pinned defect")
    flipped = bytearray(whole)
    flipped[-8] ^= 0xFF
    check(sniff(bytes(flipped)).kind == CORRUPT
          and "damaged" in sniff(bytes(flipped)).detail,
          "a gzip stream with a wrong checksum is CORRUPT")
    check(sniff(b"\x1f\x8b\x00\x00\x00").kind == CORRUPT,
          "a gzip magic number with a bad header is CORRUPT")
    check(sniff(whole + b"junk").kind == CORRUPT
          and "4 byte(s) follow" in sniff(whole + b"junk").detail,
          "bytes after the end of the gzip stream are CORRUPT")
    bomb = _gz(b"\x00" * 100000)
    check(sniff(bomb, 1000).kind == CORRUPT
          and "past 1000" in sniff(bomb, 1000).detail
          and sniff(bomb).kind == UNKNOWN,
          "a gzip stream that inflates past the limit is refused, not "
          "inflated")
    refused = (sniff(whole[:-5]), sniff(bytes(flipped)),
               sniff(whole + b"junk"), sniff(bomb, 1000),
               sniff(_gz(_gz(FIXTURE_PNG))))
    check(all(r.gzip for r in refused),
          "every gzip refusal still says the bytes were gzip")
    block = b"".join(hashlib.sha256(b"%d" % i).digest() for i in range(640))
    far = sniff(_gz(PNG_SIGNATURE + block + block))
    check(far.kind == PNG and far.payload.endswith(block + block),
          "a gzip back-reference more than 16 KiB away inflates, the window "
          "is the full 32 KiB")
    members = sniff(whole + whole)
    check(members.kind == CORRUPT and "%d byte(s) follow" % len(whole) in members.detail,
          "two gzip members back to back are CORRUPT, only one is read")

    # ---- the vector tile structure test
    check(is_mvt(_tile(_layer())), "one named layer is a vector tile")
    check(is_mvt(_tile(_layer(version=None))),
          "a layer that states no version is accepted, the default is 1")
    check(is_mvt(_tile(_layer(extra=b"\x31" + b"\x00" * 8))),
          "a layer with an unknown fixed64 field still parses")
    check(is_mvt(_tile(_layer(extra=b"\x3d" + b"\x00" * 4))),
          "a layer with an unknown fixed32 field still parses")
    check(not is_mvt(b"\x0a\x03abc"),
          "a message whose first field is not the layer list is not a "
          "vector tile")
    check(not is_mvt(b"\x22" + _enc(len(_layer())) + _layer()),
          "a tile whose only field is not number 3 is not a vector tile, "
          "even when its bytes parse as a layer")
    check(not is_mvt(_tile(_layer(extra=b"\x2a\x01\x00"))),
          "a layer whose extent is a string, not a varint, is not a vector "
          "tile")
    check(not is_mvt(b"\x1a"), "a tile that ends inside a varint is not one")
    check(not is_mvt(b"\x1a" + b"\xff" * 11),
          "a varint longer than ten bytes is not one")
    check(not is_mvt(b"\x1a\x20" + _layer()),
          "a layer longer than the data is not a vector tile")
    check(not is_mvt(_tile(b"\x78\x02\x28\x80\x20")),
          "a layer with no name is not a vector tile")
    check(not is_mvt(_tile(b"\x0a\x00\x78\x02")),
          "a layer with an empty name is not a vector tile")
    check(not is_mvt(_tile(_layer(version=3))),
          "a layer version other than 1 or 2 is not a vector tile")
    check(not is_mvt(_tile(b"\x08\x01")),
          "a layer whose field 1 is a varint, not a string, is not one")
    check(not is_mvt(_tile(b"\x00\x01")),
          "field number zero is not a vector tile")
    check(not is_mvt(_tile(b"\x4b")) and not is_mvt(_tile(b"\x4e")),
          "a group or reserved wire type is not a vector tile")
    check(not is_mvt(_tile(b"\x0a\x01a\x31\x00")),
          "a fixed64 field that runs past the layer is not a vector tile")
    check(not is_mvt(_tile(b"\x0a\x05ab")),
          "a layer string that runs past the layer is not a vector tile")
    check(not is_mvt(_tile(b"\x0a\x01a\x78")),
          "a layer that ends inside a varint is not a vector tile")
    check(not is_mvt(b""), "zero bytes are not a vector tile")
    check(_varint(_enc(300), 0) == (300, 2)
          and _varint(_enc(2 ** 63), 0) == (2 ** 63, 10),
          "a varint of two bytes and one of ten bytes decode to their value  "
          "<-- pinned defect")
    raises(lambda: _varint(b"\x80" * 10 + b"\x00", 0),
           "an eleven byte varint is refused at the tenth byte  "
           "<-- pinned defect")
    big = _tile(_layer(name=b"x" * 300), _layer(name=b"y" * 130))
    check(sniff(big).kind == MVT and sniff(_gz(big)).kind == MVT,
          "layers whose lengths need two varint bytes are read  "
          "<-- pinned defect")
    check(is_mvt(_tile(_layer(extra=b"\x12\x00\x1a\x00\x22\x00"))),
          "a layer with a feature, a key and a value of length zero parses")
    wrong = all(not is_mvt(_tile(_layer(extra=bytes([(f << 3) | w, 0]))))
                for f, w in ((1, 0), (2, 0), (3, 0), (4, 0), (5, 2),
                             (15, 2)))
    check(wrong,
          "each layer field with the wrong wire type is not a vector tile  "
          "<-- pinned defect")
    check(is_mvt(_tile(_layer(name=b"a"))),
          "a layer named by one byte is a vector tile  <-- pinned defect")
    ext = _tile(_layer()) + b"\x82\x01\x00"
    check(not is_mvt(ext) and sniff(ext).kind == UNKNOWN,
          "a tile with a top-level field other than the layers is UNKNOWN")

    # ---- one MBTiles row
    def tile(z, col, row, data, **kw):
        return classify_tile(z, col, row, data, **kw)

    flat = tile(2, 1, 1, FIXTURE_PNG)
    check(flat.status == OK and flat.rel == "2/1/2.png"
          and flat.payload == FIXTURE_PNG,
          "the MBTiles row is flipped to xyz: row 1 at zoom 2 is y 2")
    check(tile(2, 1, 1, FIXTURE_PNG, scheme="tms").rel == "2/1/1.png",
          "scheme tms leaves the row alone")
    check(tile(0, 0, 0, FIXTURE_JPG).rel == "0/0/0.jpg"
          and tile(1, 1, 0, FIXTURE_WEBP).rel == "1/1/1.webp",
          "jpg and webp are named by their bytes, and zoom 0 has one cell")
    keep = tile(2, 1, 1, _gz(FIXTURE_MVT))
    check(keep.rel == "2/1/2.mvt.gz" and keep.payload == _gz(FIXTURE_MVT),
          "a gzipped vector tile is written as .mvt.gz, the bytes untouched, "
          "never under a bare extension  <-- pinned defect")
    inflate = tile(2, 1, 1, _gz(FIXTURE_MVT), decompress=True)
    check(inflate.rel == "2/1/2.mvt" and inflate.payload == FIXTURE_MVT,
          "--decompress writes the inflated bytes as .mvt")
    check(tile(2, 1, 1, FIXTURE_MVT, decompress=True).payload == FIXTURE_MVT,
          "--decompress leaves a tile that was never gzipped alone")
    check(tile(2, 1, 1, _gz(FIXTURE_PNG)).rel == "2/1/2.png.gz",
          "a gzipped image is named .png.gz")
    check(tile(2, 1, 1, b"hello").status == UNKNOWN
          and tile(2, 1, 1, b"").status == EMPTY
          and tile(2, 1, 1, whole[:-5]).status == CORRUPT,
          "unknown, empty and damaged tiles are not OK")
    check(tile(2, 1, 1, None).detail == "tile_data is NULL, not a blob"
          and tile(2, 1, 1, "text").detail == "tile_data is str, not a blob",
          "a NULL or text tile_data is CORRUPT and says which")
    for z, col, row, label in (
            (-1, 0, 0, "a negative zoom"), (31, 0, 0, "zoom 31"),
            (2, 4, 0, "a column past the grid"), (2, 0, 4, "a row past it"),
            (2, -1, 0, "a negative column"), (2, 0, -1, "a negative row"),
            (2.0, 0, 0, "a float zoom"), (2, "1", 0, "a text column"),
            (2, 0, True, "a boolean row"), (2, None, 0, "a NULL column")):
        check(tile(z, col, row, FIXTURE_PNG).status == BADCOORD,
              "%s is BADCOORD, so no path is built from it" % label)
    check(tile(30, 2 ** 30 - 1, 0, FIXTURE_PNG).status == OK,
          "zoom 30 and its last column are on the grid")
    check("outside the 4 x 4 grid of zoom 2"
          in tile(2, 4, 0, FIXTURE_PNG).detail,
          "an off-grid tile says what grid it missed")

    # ---- a tileset, row by row
    def rows(*items):
        return iter(items)

    seen = []
    report = unpack(rows((1, 0, 0, FIXTURE_PNG), (1, 0, 1, FIXTURE_PNG),
                         (1, 0, 1, FIXTURE_JPG), (1, 1, 0, b"nope"),
                         (1, 1, 1, b""), (1, 5, 0, FIXTURE_PNG)),
                    "png", sink=lambda rel, data: seen.append(rel))
    check(report.total == 6 and sum(report.counts.values()) == 6,
          "every input row lands in exactly one status, so the counts add "
          "up")
    check(seen == ["1/0/1.png", "1/0/0.png"]
          and report.counts[DUPLICATE] == 1,
          "the later row of a duplicate address is skipped, not written")
    check(report.counts[UNKNOWN] == 1 and report.counts[EMPTY] == 1
          and report.counts[BADCOORD] == 1 and report.findings() == 3,
          "unknown, off-grid and duplicate rows are findings, empty is not")
    check(gate(report) == 1, "a tileset with findings gates to exit 1")
    check(unpack(rows((0, 0, 0, FIXTURE_PNG)), "png").counts[OK] == 1,
          "a dry run with no sink writes nothing and still counts")

    def verdict(declared, *data):
        return unpack(rows(*[(1, 0, i, d) for i, d in enumerate(data)]),
                      declared)

    check(verdict("png", FIXTURE_PNG).declared_bad == 0
          and verdict("pbf", _gz(FIXTURE_MVT)).declared_bad == 0
          and verdict("jpg", FIXTURE_JPG).declared_bad == 0
          and verdict("webp", FIXTURE_WEBP).declared_bad == 0,
          "tiles that match the declared format do not disagree")
    check(verdict("png", _gz(FIXTURE_MVT)).declared_bad == 1
          and verdict("pbf", FIXTURE_PNG).declared_bad == 1,
          "vector tiles under png, and a png under pbf, disagree with the "
          "metadata  <-- pinned defect")
    check(verdict("pbf", FIXTURE_MVT).declared_bad == 1,
          "a raw vector tile under pbf disagrees, the spec says gzip  "
          "<-- pinned defect")
    check(verdict("png", _gz(FIXTURE_PNG)).declared_bad == 1,
          "a gzipped image under png disagrees")
    check(verdict(None, FIXTURE_PNG).declared_bad == 0
          and verdict("image/tiff", FIXTURE_PNG).declared_bad == 0,
          "a missing or unreadable declared format compares nothing")
    check(gate(verdict("png", _gz(FIXTURE_MVT))) == 1,
          "a disagreement with the metadata gates to exit 1")
    check(gate(unpack(rows(), "png")) == 4
          and gate(unpack(rows((0, 0, 0, b"")), "png")) == 4,
          "no tiles, or only empty ones, gate to exit 4, not 0")
    check(gate(verdict("png", FIXTURE_PNG)) == 0,
          "a clean tileset gates to exit 0")
    twin = unpack(rows((1, 1, 1, FIXTURE_PNG), (1, True, 1, FIXTURE_PNG)),
                  "png")
    check(twin.counts[DUPLICATE] == 1 and twin.counts[BADCOORD] == 0,
          "a True in tile_column compares equal to 1 and is DUPLICATE, not "
          "BADCOORD")
    check(declared_format({"format": b"png"}) == "b'png'"
          and verdict("b'png'", FIXTURE_PNG).declared_bad == 0
          and gate(verdict("jpeg", FIXTURE_PNG)) == 0,
          "a format stored as a BLOB, or spelled jpeg, is compared with "
          "nothing and the run still exits 0")

    # ---- the examples are capped
    many = unpack(rows(*[(3, 0, i, b"x") for i in range(5)]), None, sample=2)
    check(len(many.samples[UNKNOWN]) == 2 and many.counts[UNKNOWN] == 5,
          "--sample caps the examples and not the count")
    dozen = unpack(rows(*[(4, 0, i, b"x") for i in range(12)]), None)
    check(len(dozen.samples[UNKNOWN]) == DEFAULT_SAMPLE == 10
          and dozen.counts[UNKNOWN] == 12 and MAX_INFLATED == 64 * 2 ** 20,
          "ten examples are listed by default, and the inflate limit is "
          "64 MiB")
    kept = []
    unpack(rows((1, 0, 0, _gz(FIXTURE_MVT))), "pbf",
           sink=lambda rel, data: kept.append(rel))
    check(kept == ["1/0/1.mvt.gz"],
          "unpack keeps a gzipped tile gzipped unless it is told to inflate")
    none = unpack(rows((3, 0, 0, b"x")), None, sample=0)
    check(none.samples[UNKNOWN] == [] and none.counts[UNKNOWN] == 1,
          "--sample 0 lists no example")

    # ---- a tree of files
    def read_map(files):
        return lambda path: files[path]

    def tree(files, **kw):
        entries = [(p.split("/"), p) for p in files]
        return audit(entries, read_map(files), **kw)

    ok_tree = tree({"2/1/2.mvt.gz": _gz(FIXTURE_MVT),
                    "2/1/1.png": FIXTURE_PNG, "2/1/0.jpg": FIXTURE_JPG,
                    "2/2/0.webp": FIXTURE_WEBP, "2/2/1.mvt": FIXTURE_MVT})
    check(ok_tree.counts[OK] == 5 and gate(ok_tree) == 0,
          "files named by their bytes audit clean")
    bad_tree = tree({"2/1/2.png": _gz(FIXTURE_MVT)})
    check(bad_tree.counts[MISNAMED] == 1 and gate(bad_tree) == 1
          and "bytes are mvt (gzip), the name ends .png, expected .mvt.gz"
          in bad_tree.samples[MISNAMED][0],
          "a gzipped vector tile under .png is MISNAMED  <-- pinned defect")
    check(tree({"2/1/2.pbf": _gz(FIXTURE_MVT)}).counts[MISNAMED] == 1
          and tree({"2/1/2.pbf": FIXTURE_MVT}).counts[MISNAMED] == 1,
          "a bare .pbf is MISNAMED whether or not it is gzipped  "
          "<-- pinned defect")
    check(tree({"2/1/2": FIXTURE_PNG}).samples[MISNAMED][0].endswith(
        "the name ends with no extension, expected .png"),
        "a tile file with no extension is MISNAMED, not a crash  "
        "<-- pinned defect")
    check(tree({"2/1/2.pbf.gz": FIXTURE_PNG}).counts[MISNAMED] == 1,
          "the extension is everything after the first dot, so .pbf.gz is "
          "read whole  <-- pinned defect")
    skipped = tree({"metadata.json": b"{}", "2/x.png": b"", "a/1/2.png": b"",
                    "2/1/x.png": b"", "2/1/2/3.png": b""})
    check(skipped.counts[SKIPPED] == 5 and gate(skipped) == 4,
          "paths that are not z/x/y.ext are SKIPPED and compare nothing")
    check(tree({"2/x/3.png": b""}).counts[SKIPPED] == 1
          and tree({"2/x/3.png": b""}).counts[EMPTY] == 0,
          "a middle part that is not digits is SKIPPED, not a crash  "
          "<-- pinned defect")
    check(tree({"2/9/0.png": FIXTURE_PNG}).counts[BADCOORD] == 1
          and tree({"2/1/2.png": b"hello"}).counts[UNKNOWN] == 1
          and tree({"2/1/2.png": b""}).counts[EMPTY] == 1,
          "an off-grid file, an unknown file and an empty file are each "
          "their own status")
    unread = []
    audit([(["2", "9", "0.png"], "p")],
          lambda path: unread.append(path) or FIXTURE_PNG)
    check(unread == [], "a file off the grid is not read")
    check(tree({"2/1/2.PNG": FIXTURE_PNG}).counts[MISNAMED] == 1,
          "the extension is compared exactly, .PNG is not .png")
    dups = tree({"3/1/1.png": FIXTURE_PNG, "3/1/1.jpg": FIXTURE_JPG,
                 "3/1/01.png": FIXTURE_PNG, "3/1/2.png": FIXTURE_PNG})
    check(dups.counts[OK] == 2 and dups.counts[DUPLICATE] == 2
          and gate(dups) == 1,
          "a second file at one address is DUPLICATE whatever its extension "
          "or zero padding  <-- pinned defect")
    long_names = tree({"0/0/" + "9" * 5000 + ".png": FIXTURE_PNG,
                       "0/0/" + "9" * 21 + ".png": FIXTURE_PNG,
                       "0/0/" + "9" * 20 + ".png": FIXTURE_PNG})
    check(long_names.counts[SKIPPED] == 2
          and long_names.counts[BADCOORD] == 1,
          "a name of thousands of digits is SKIPPED and never reaches int(), "
          "twenty digits are BADCOORD  <-- pinned defect")

    def unreadable():
        raise OSError(13, "Permission denied")

    denied = classify_file(["2", "1", "2.png"], unreadable)
    check(denied.status == CORRUPT and "Permission denied" in denied.detail
          and "OSError" in classify_file(
              ["2", "1", "2.png"], lambda: (_ for _ in ()).throw(
                  OSError("boom"))).detail,
          "a file that cannot be read is CORRUPT and named, and the files "
          "before it still count  <-- pinned defect")
    over = classify_file(["2", "1", "2.png"], lambda: FIXTURE_PNG, limit=10)
    check(over.status == CORRUPT and "larger than 10 bytes" in over.detail
          and classify_file(["2", "1", "2.png"], lambda: FIXTURE_PNG,
                            limit=len(FIXTURE_PNG)).status == OK,
          "a file past the limit is refused, one at the limit is read  "
          "<-- pinned defect")
    check(may_read(stat.S_IFREG | 0o644, os.sep + "t", os.sep + "t" + os.sep)
          and may_read(stat.S_IFREG | 0o644, os.sep.join(["", "t", "a"]),
                       os.sep + "t" + os.sep),
          "a regular file in the tree may be read")
    check(not may_read(stat.S_IFLNK | 0o777, os.sep + "t", os.sep + "t" + os.sep)
          and not may_read(stat.S_IFIFO | 0o600, os.sep + "t",
                           os.sep + "t" + os.sep)
          and not may_read(stat.S_IFREG | 0o644, os.sep + "tx",
                           os.sep + "t" + os.sep)
          and not may_read(stat.S_IFREG | 0o644, os.sep + "other",
                           os.sep + "t" + os.sep),
          "a link, a FIFO, and a file in a folder outside the tree may not "
          "be read  <-- pinned defect")

    # ---- the report as text
    text = "\n".join(describe(unpack(
        rows((1, 0, 0, _gz(FIXTURE_MVT)), (1, 0, 1, FIXTURE_PNG),
             (1, 1, 0, b"nope")), "pbf", source="a.mbtiles")))
    check("mbunpack: a.mbtiles: 3 tile(s) read, scheme xyz" in text
          and "declared format: pbf" in text
          and "  mvt (gzip)     1" in text and "  png            1" in text,
          "the report names the source, the declared format and each kind "
          "found")
    check("UNKNOWN 1: bytes match no format mbunpack reads" in text
          and "    1/1/1  begins 6e6f7065" in text
          and "DECLARED 1: the bytes disagree" in text
          and "VERDICT: 2 finding(s) in 3 tile(s)." in text,
          "the report lists each finding with an example and a verdict")
    check("VERDICT: every tile was identified from its bytes and agrees "
          "with the declared format." in "\n".join(
              describe(verdict("png", FIXTURE_PNG)))
          and "bytes.\n" in "\n".join(describe(verdict(None, FIXTURE_PNG)))
          + "\n",
          "a clean run says so, and says less when no format was declared")
    check("none, the metadata has no format row"
          in "\n".join(describe(verdict(None, FIXTURE_PNG)))
          and "which mbunpack does not read" in "\n".join(
              describe(verdict("image/tiff", FIXTURE_PNG))),
          "a missing or unreadable declared format is said in words")
    gone = "\n".join(describe(unpack(
        rows((1, 0, 0, FIXTURE_PNG), (1, 0, 1, b"")), "png")))
    check("EMPTY 1: zero-byte tile, there is nothing to write" in gone
          and "VERDICT: 1 of 2 tile(s) were identified from their bytes and "
          "agree with the declared format. Not identified, and not counted "
          "as a finding: 1 EMPTY." in gone
          and "every tile was identified" not in gone,
          "the verdict counts an EMPTY row and no longer says every tile was "
          "identified  <-- pinned defect")
    holes = "\n".join(describe(tree({
        "2/1/2.png": FIXTURE_PNG, "2/1/3.png": b"",
        "2/1/4@2x.png": FIXTURE_PNG})))
    check("VERDICT: 1 of 3 file(s) were identified from their bytes. Not "
          "identified, and not counted as a finding: 1 EMPTY, 1 SKIPPED."
          in holes,
          "the verdict of a folder counts its EMPTY and SKIPPED files  "
          "<-- pinned defect")
    check(clean("a\x1b[31mb\x07\x9bc 2/1/2.png")
          == "a\\x1b[31mb\\x07\\x9bc 2/1/2.png",
          "clean() shows each control character as \\xNN and keeps the rest")
    hostile = "\n".join(describe(tree({
        "2/1/2.\x1b[2Jpng": FIXTURE_PNG, "2/1/x\x1b]0;t\x07": b""})))
    check("\x1b" not in hostile and "\x07" not in hostile
          and "the name ends .\\x1b[2Jpng" in hostile,
          "a file name with escape codes is printed with them shown, not "
          "obeyed  <-- pinned defect")
    check("\x1b" not in "\n".join(describe(verdict("\x1b[2J", FIXTURE_PNG))),
          "a metadata value with escape codes is printed with them shown  "
          "<-- pinned defect")
    check("VERDICT: nothing was compared. 0 tile(s) read"
          in "\n".join(describe(unpack(rows(), "png"))),
          "a run that compared nothing says so")
    check("mbunpack: x: 1 file(s) read" in "\n".join(describe(
        tree({"2/1/2.png": FIXTURE_PNG}, source="x")))
        and "scheme" not in describe(tree({"2/1/2.png": FIXTURE_PNG}))[0],
        "a tree report counts files and has no scheme")

    # ---- the CLI on real MBTiles files
    mixed = fresh("mixed.mbtiles")
    _make_mbtiles(mixed, [(2, 1, 1, _gz(FIXTURE_MVT)),
                          (2, 1, 2, _gz(FIXTURE_MVT)),
                          (2, 2, 1, _gz(FIXTURE_MVT))],
                  {"name": "synthetic", "format": "pbf"})
    before = hashlib.sha256(read_file(mixed)).hexdigest()
    listing = sorted(os.listdir(tmp))
    out = fresh("out")
    code, stdout, stderr = run_cli([mixed])
    check(code == 0 and "VERDICT: every tile" in stdout and stderr == ""
          and not os.path.exists(out),
          "a check of a clean vector tileset exits 0 and writes nothing")
    check(hashlib.sha256(read_file(mixed)).hexdigest() == before
          and sorted(os.listdir(tmp)) == listing,
          "reading a tileset changes no byte of it and leaves no journal")
    code, stdout, stderr = run_cli([mixed, "--out", out])
    check(code == 0 and "Check only." in stdout and "Re-run with --apply."
          in stdout and not os.path.exists(out),
          "--out without --apply writes nothing and says so")
    code, stdout, stderr = run_cli([mixed, "--out", out, "--apply"])
    written = _tree(out)
    check(code == 0 and sorted(written) == [
        "2/1/1.mvt.gz", "2/1/2.mvt.gz", "2/2/2.mvt.gz", "metadata.json"]
        and written["2/1/1.mvt.gz"] == _gz(FIXTURE_MVT),
        "--apply writes z/x/y.mvt.gz files holding the stored bytes, and "
        "metadata.json")
    check(written["metadata.json"].decode("utf-8").replace("\r\n", "\n")
          == '{\n  "format": "pbf",\n  "name": "synthetic"\n}'
          and json.loads(written["metadata.json"].decode("utf-8"))
          == {"name": "synthetic", "format": "pbf"}
          and "wrote 3 tile(s)" in stdout,
          "metadata.json holds the metadata table, and the run says how "
          "many tiles it wrote")
    code, stdout, stderr = run_cli([out])
    check(code == 0 and "SKIPPED 1" in stdout and "mvt (gzip)" in stdout
          and "MISNAMED" not in stdout,
          "an unpacked directory audits clean, and metadata.json is skipped "
          "without being a finding")

    def put(root, rel, data):
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)

    zeros = fresh("zeros")
    for rel, data in (("1/0/0.png", FIXTURE_PNG), ("1/0/1.png", b""),
                      ("1/1/0.png", b""), ("1/1/1@2x.png", FIXTURE_PNG)):
        put(zeros, rel, data)
    code, stdout, stderr = run_cli([zeros])
    check(code == 0 and "EMPTY 2" in stdout and "SKIPPED 1" in stdout
          and "VERDICT: 1 of 4 file(s) were identified from their bytes. "
          "Not identified, and not counted as a finding: 2 EMPTY, 1 SKIPPED."
          in stdout,
          "a folder of empty and retina-named files exits 0 and the verdict "
          "does not call it fully identified  <-- pinned defect")
    twins = fresh("twins")
    put(twins, "3/1/1.png", FIXTURE_PNG)
    put(twins, "3/1/1.jpg", FIXTURE_JPG)
    code, stdout, stderr = run_cli([twins])
    check(code == 1 and "DUPLICATE 1" in stdout,
          "a folder that holds one address twice exits 1  "
          "<-- pinned defect")

    outside = fresh("outside.txt")
    with open(outside, "w") as handle:
        handle.write("SECRET-OUTSIDE-THE-FOLDER")
    outdir = fresh("outdir")
    put(outdir, "0/0/0.png", FIXTURE_PNG)
    linked = fresh("linked")
    put(linked, "1/0/0.png", FIXTURE_PNG)
    made = []
    # The third entry names a file that exists, so os.symlink fails on every
    # host, as it does where links are not allowed. That run still audits.
    for rel, target, is_dir in (("1/0/1.png", outside, False),
                                ("2", outdir, True),
                                ("1/0/0.png", outside, False)):
        try:
            os.symlink(target, os.path.join(linked, *rel.split("/")),
                       target_is_directory=is_dir)
            made.append(rel)
        except (OSError, NotImplementedError):
            pass
    linked_report = audit(tree_entries(linked), read_file)
    listed = sorted(t.split("  ")[0]
                    for t in linked_report.samples.get(SKIPPED, []))
    check(linked_report.counts[OK] == 1 and listed == sorted(made)
          and linked_report.total == 1 + len(made)
          and "SECRET" not in "\n".join(describe(linked_report)),
          "a symbolic link to a file or a folder is listed and never read; "
          "this host made %d link(s)  <-- pinned defect" % len(made))

    # The mbutil defect, reproduced as a tree: gzip under .png and .pbf.
    legacy = fresh("legacy")
    for rel, data in (("2/1/1.png", _gz(FIXTURE_MVT)),
                      ("2/1/2.pbf", _gz(FIXTURE_MVT)),
                      ("2/2/1.mvt.gz", _gz(FIXTURE_MVT))):
        os.makedirs(os.path.join(legacy, os.path.dirname(rel)),
                    exist_ok=True)
        with open(os.path.join(legacy, rel), "wb") as handle:
            handle.write(data)
    code, stdout, stderr = run_cli([legacy])
    check(code == 1 and "MISNAMED 2" in stdout
          and "expected .mvt.gz" in stdout and "2/1/1.png" in stdout,
          "an audit finds gzip vector tiles under .png and .pbf and exits 1  "
          "<-- pinned defect")
    check(run_cli([fresh("nothing-here")])[0] == 2,
          "a path that does not exist exits 2")
    hollow = fresh("hollow-dir")
    os.mkdir(hollow)
    check(run_cli([hollow])[0] == 4,
          "a directory with no tiles exits 4, not 0")

    check(run_cli([mixed, "--sample", "0"])[0] == 0,
          "--sample 0 is accepted on the command line")
    tms = fresh("tms")
    code, stdout, stderr = run_cli([mixed, "--out", tms, "--scheme", "tms",
                                    "--apply"])
    check(code == 0 and sorted(_tree(tms)) == [
        "2/1/1.mvt.gz", "2/1/2.mvt.gz", "2/2/1.mvt.gz", "metadata.json"],
        "--scheme tms keeps the MBTiles row in the file name")
    inflated = fresh("inflated")
    code, stdout, stderr = run_cli([mixed, "--out", inflated, "--decompress",
                                    "--apply"])
    got = _tree(inflated)
    check(code == 0 and got["2/1/1.mvt"] == FIXTURE_MVT
          and run_cli([inflated])[0] == 0,
          "--decompress writes .mvt files of the inflated bytes, and they "
          "audit clean")

    # ---- the tileset that exports the bug
    wrong = fresh("wrong.mbtiles")
    _make_mbtiles(wrong, [(2, 1, 1, _gz(FIXTURE_MVT)),
                          (2, 1, 2, FIXTURE_PNG)], {"format": "png"})
    code, stdout, stderr = run_cli([wrong])
    check(code == 1 and "DECLARED 1" in stdout
          and "bytes are mvt (gzip), metadata says png" in stdout,
          "vector tiles in a tileset that says png exit 1 and name the tile")
    dup = fresh("dup.mbtiles")
    _make_mbtiles(dup, [(1, 0, 0, FIXTURE_PNG), (1, 0, 0, FIXTURE_JPG)],
                  {"format": "png"}, unique=False)
    code, stdout, stderr = run_cli([dup])
    check(code == 1 and "DUPLICATE 1" in stdout,
          "a tileset without a unique index and with a repeated address "
          "exits 1")
    nometa = fresh("nometa.mbtiles")
    _make_mbtiles(nometa, [(1, 0, 0, FIXTURE_PNG)], metadata_table=False)
    code, stdout, stderr = run_cli([nometa])
    check(code == 0 and "declared format: none" in stdout,
          "a file with no metadata table is read, and says it declared "
          "nothing")
    empty = fresh("empty.mbtiles")
    _make_mbtiles(empty, [], {"format": "png"})
    refused_out = fresh("refused")
    code, stdout, stderr = run_cli([empty, "--out", refused_out, "--apply"])
    check(code == 4 and "Refused." in stdout
          and not os.path.exists(refused_out),
          "a tileset with no tile exits 4, and --apply creates no directory")

    # ---- the files the CLI refuses
    text_file = fresh("text.mbtiles")
    with open(text_file, "w") as handle:
        handle.write("this is not a database, it is only long enough to be "
                     "read as one\n" * 4)
    code, stdout, stderr = run_cli([text_file])
    check(code == 2 and "not a database" in stderr and stdout == "",
          "a file that is not SQLite exits 2")
    no_tiles = fresh("no-tiles.mbtiles")
    con = sqlite3.connect(no_tiles)
    con.execute("create table metadata (name text, value text)")
    con.commit()
    con.close()
    code, stdout, stderr = run_cli([no_tiles])
    check(code == 2 and "no such table: tiles" in stderr,
          "a database with no tiles table exits 2")
    blocker = fresh("blocker")
    with open(blocker, "w") as handle:
        handle.write("a file where a folder is wanted")
    code, stdout, stderr = run_cli([mixed, "--out",
                                    os.path.join(blocker, "sub"), "--apply"])
    check(code == 2 and stderr.startswith("error:"),
          "a directory that cannot be made exits 2, not a traceback")

    # ---- a WAL-mode source gains no file beside it
    walled = fresh("walled.mbtiles")
    _make_mbtiles(walled, [(1, 0, 0, FIXTURE_PNG)], {"format": "png"})
    con = sqlite3.connect(walled)
    mode = con.execute("pragma journal_mode=wal").fetchone()[0]
    con.close()
    wal_before = hashlib.sha256(read_file(walled)).hexdigest()
    listing = sorted(os.listdir(tmp))
    code, stdout, stderr = run_cli([walled])
    check(mode == "wal" and code == 0 and "1 tile(s) read" in stdout
          and sorted(os.listdir(tmp)) == listing
          and hashlib.sha256(read_file(walled)).hexdigest() == wal_before,
          "a check of a WAL-mode tileset leaves no -wal or -shm file beside "
          "it  <-- pinned defect")
    for suffix in ("-wal", "-journal"):
        pending = fresh("pending.mbtiles")
        _make_mbtiles(pending, [(1, 0, 0, FIXTURE_PNG)], {"format": "png"})
        put(os.path.dirname(pending), os.path.basename(pending) + suffix,
            b"not empty")
        code, stdout, stderr = run_cli([pending])
        check(code == 2 and "holds changes that are not in" in stderr
              and stdout == "",
              "a non-empty %s file beside the source is refused, because an "
              "immutable read would miss it" % suffix)
    quiet_wal = fresh("quiet.mbtiles")
    _make_mbtiles(quiet_wal, [(1, 0, 0, FIXTURE_PNG)], {"format": "png"})
    put(os.path.dirname(quiet_wal), os.path.basename(quiet_wal) + "-wal", b"")
    check(run_cli([quiet_wal])[0] == 0,
          "an empty -wal file beside the source holds nothing and is "
          "accepted")

    # ---- usage errors, and only --apply writes
    stale = fresh("stale")
    for flag in ("--ap", "--appl", "--app"):
        code, stdout, stderr = run_cli([mixed, "--out", stale, flag])
        check(code == 64 and not os.path.exists(stale) and stdout == "",
              "%s is refused, it is not read as --apply  <-- pinned defect"
              % flag)
    check(run_cli([mixed, "--decomp"])[0] == 64
          and run_cli([mixed, "--sch", "tms"])[0] == 64,
          "no flag is accepted as a prefix of another")
    check(run_cli([])[0] == 64 and run_cli([mixed, "--sample", "-1"])[0] == 64
          and run_cli([mixed, "--scheme", "wms"])[0] == 64,
          "a missing source, a negative sample and an unknown scheme are "
          "usage errors")
    code, stdout, stderr = run_cli([mixed, "--apply"])
    check(code == 64 and "--apply needs --out" in stderr,
          "--apply without --out is a usage error")
    check(run_cli([legacy, "--out", fresh("x")])[0] == 64
          and run_cli([legacy, "--decompress"])[0] == 64,
          "a directory is audited only, so --out and --decompress are "
          "refused")
    occupied = fresh("occupied")
    os.mkdir(occupied)
    with open(os.path.join(occupied, "keep.txt"), "w") as handle:
        handle.write("mine")
    code, stdout, stderr = run_cli([mixed, "--out", occupied, "--apply"])
    check(code == 64 and os.listdir(occupied) == ["keep.txt"],
          "an --out directory that holds files is refused, even on --apply")
    check(run_cli([mixed, "--out", mixed, "--apply"])[0] == 64
          and hashlib.sha256(read_file(mixed)).hexdigest() == before,
          "--out naming the source file is refused and the source is "
          "intact")
    vacant = fresh("vacant")
    os.mkdir(vacant)
    check(run_cli([mixed, "--out", vacant, "--apply"])[0] == 0
          and len(_tree(vacant)) == 4,
          "an existing empty --out directory is filled")
    nullname = fresh("nullname.mbtiles")
    _make_mbtiles(nullname, [(1, 0, 0, FIXTURE_PNG)],
                  {None: "orphan", "format": "png", "name": "t"})
    nullname_out = fresh("nullname-out")
    code, stdout, stderr = run_cli([nullname, "--out", nullname_out,
                                    "--apply"])
    check(code == 0 and json.loads(_tree(nullname_out)[
        "metadata.json"].decode("utf-8")).get("None") == "orphan",
        "a metadata row with a NULL name is written, not a crash  "
        "<-- pinned defect")

    # ---- a path with characters a URI would read as syntax
    weird = os.path.join(tmp, "a #b %20 c")
    os.mkdir(weird)
    weird_file = os.path.join(weird, "t.mbtiles")
    _make_mbtiles(weird_file, [(1, 0, 0, FIXTURE_PNG)], {"format": "png"})
    code, stdout, stderr = run_cli([weird_file])
    check(code == 0 and "1 tile(s) read" in stdout,
          "a directory name with #, a space and %20 in it opens  "
          "<-- pinned defect")
    con = open_readonly(weird_file)
    refusal = ""
    try:
        con.execute("insert into tiles values (1, 1, 1, x'00')")
    except sqlite3.OperationalError as exc:
        refusal = str(exc)
    con.close()
    check("readonly" in refusal,
          "the connection mbunpack opens refuses a write")

    # ---- a write that fails part-way keeps what it wrote
    real_write_metadata = globals()["write_metadata"]

    def disk_full(*args):
        raise OSError(28, "No space left on device")

    partial = fresh("partial")
    try:
        globals()["write_metadata"] = disk_full
        code, stdout, stderr = run_cli([mixed, "--out", partial, "--apply"])
    finally:
        globals()["write_metadata"] = real_write_metadata
    check(code == 2 and "No space left on device" in stderr
          and sorted(_tree(partial)) == ["2/1/1.mvt.gz", "2/1/2.mvt.gz",
                                         "2/2/2.mvt.gz"],
          "a write that fails after the tiles exits 2 and leaves the files "
          "already written")
    got = fresh("cap.bin")
    with open(got, "wb") as handle:
        handle.write(b"0123456789")
    check(read_file(got, 4) == b"0123" and read_file(got) == b"0123456789",
          "read_file stops at its cap and reads a smaller file whole")

    # ---- the console
    calls = []

    class Reconfigurable(io.StringIO):
        def reconfigure(self, **kw):
            calls.append(kw)

    with contextlib.redirect_stdout(Reconfigurable()), \
            contextlib.redirect_stderr(Reconfigurable()):
        helped = main(["--help"])
    check(helped == 0 and calls == [{"errors": "replace"}] * 2,
          "main reconfigures stdout and stderr to replace what they cannot "
          "encode, and --help exits 0")
    odd = fresh("odd.mbtiles")
    _make_mbtiles(odd, [(1, 0, 0, FIXTURE_PNG)], {"format": chr(321)})
    child = subprocess.run(
        [sys.executable, os.path.abspath(__file__), odd],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=dict(os.environ, PYTHONIOENCODING="ascii"))
    check(child.returncode == 0 and b"declared format: ?," in child.stdout
          and b"Traceback" not in child.stderr,
          "a metadata value the console cannot encode prints as ? and the "
          "verdict still arrives  <-- pinned defect")

    # ---- a crash is not a finding
    real_unpack = globals()["unpack"]

    def boom(*args, **kw):
        raise RuntimeError("probe")

    try:
        globals()["unpack"] = boom
        code, stdout, stderr = run_cli([mixed])
    finally:
        globals()["unpack"] = real_unpack
    check(code == 70 and "unexpected RuntimeError: probe" in stderr
          and "Traceback" in stderr and "VERDICT" not in stdout,
          "an unexpected exception exits 70, not the 1 that means a finding  "
          "<-- pinned defect")

    real_self_test = globals()["self_test"]
    try:
        globals()["self_test"] = lambda: 7
        returned = main(["--self-test"])
    finally:
        globals()["self_test"] = real_self_test
    check(returned == 7,
          "main returns the exit status of --self-test, so a red self-test "
          "fails CI  <-- pinned defect")

    # ---- the temp directory goes even when the body raises
    made = []

    def exploding(path):
        made.append(path)
        raise RuntimeError("probe")

    real_body = globals()["_self_test"]
    try:
        globals()["_self_test"] = exploding
        try:
            self_test()
        except RuntimeError:
            made.append("raised")
    finally:
        globals()["_self_test"] = real_body
    check(len(made) == 2 and made[1] == "raised"
          and not os.path.exists(made[0]),
          "a self-test that raises still removes its temp directory  "
          "<-- pinned defect")

    # ---- the harness itself. A check() that cannot record a failure would
    # report every defect above as a pass.
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded as a failure")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
    finally:
        sys.stdout = quiet
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 3,
          "check() and raises() really do record a failure  <-- pinned "
          "defect")
    raises(lambda: _varint(b"\x80", 0),
           "and raises() accepts the ValueError it is looking for")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        red = summary(1, ["probe one", "probe two"])
    check(red == 1 and "3 assertions, 2 failed" in buf.getvalue()
          and "  FAILED: probe two" in buf.getvalue(),
          "the footer reports failures by count and by name, and exits 1  "
          "<-- pinned defect")

    # ---- importing the module runs nothing
    spec = importlib.util.spec_from_file_location(
        "mbunpack_imported", os.path.abspath(__file__))
    imported = importlib.util.module_from_spec(spec)
    buf = io.StringIO()
    cache_before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with contextlib.redirect_stdout(buf):
            spec.loader.exec_module(imported)
    finally:
        sys.dont_write_bytecode = cache_before
    check(buf.getvalue() == "" and imported.sniff(FIXTURE_PNG).kind == PNG,
          "importing the module prints nothing and exposes the core")

    return summary(passed[0], failed)


def summary(passed_count, failures):
    print("-" * 68)
    total = passed_count + len(failures)
    if failures:
        print("%d assertions, %d failed" % (total, len(failures)))
        for f in failures:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# =============================================================================
# The command line
# =============================================================================

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="mbunpack.py",
        description="Unpack an MBTiles file, naming every tile by what its "
                    "bytes are.",
        epilog="Nothing is written without --apply. The MBTiles file is "
               "opened read-only.",
        allow_abbrev=False,
    )
    ap.add_argument("source", nargs="?", metavar="SOURCE",
                    help="an .mbtiles file to check or unpack, or a z/x/y "
                         "directory to audit")
    ap.add_argument("--out", dest="out",
                    help="the directory to unpack into. It must not exist, "
                         "or must be empty. Needs --apply.")
    ap.add_argument("--scheme", dest="scheme", choices=SCHEMES,
                    default=DEFAULT_SCHEME,
                    help="xyz writes y counted from the north, tms keeps the "
                         "MBTiles row (default %s)" % DEFAULT_SCHEME)
    ap.add_argument("--decompress", dest="decompress", action="store_true",
                    help="write gzipped tiles inflated, under the plain "
                         "extension")
    ap.add_argument("--sample", dest="sample", type=int,
                    default=DEFAULT_SAMPLE,
                    help="how many examples to list under each heading "
                         "(default %d, 0 lists none)" % DEFAULT_SAMPLE)
    ap.add_argument("--apply", dest="apply", action="store_true",
                    help="actually write the directory")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the assertions and exit")
    return ap.parse_args(argv)


def _unpack_file(args):
    con = open_readonly(args.source)
    try:
        meta = read_metadata(con)
        sink = make_sink(args.out) if args.apply else None
        report = unpack(tile_rows(con), declared_format(meta), args.scheme,
                        args.decompress, sink, args.sample,
                        source=args.source)
    finally:
        con.close()
    if args.apply and report.counts[OK]:
        write_metadata(args.out, meta)
    return report


def main(argv=None):
    """Run the CLI. An error this code did not anticipate exits 70, because 1
    is the code a scheduled job reads as 'a tile has a finding'."""
    _tolerate_odd_characters()
    try:
        return _run(argv)
    except Exception as exc:
        print("error: unexpected %s: %s. This is a fault in the tool, not a "
              "finding about the tiles." % (type(exc).__name__, exc),
              file=sys.stderr)
        traceback.print_exc()
        return 70


def _run(argv):
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        # argparse exits 2 on a flag it refuses, and 2 here means a file
        # could not be read. A mistyped --ap is a usage error, so it gets 64.
        return 64 if exc.code == 2 else exc.code

    if args.self_test:
        return self_test()

    if not args.source:
        print("error: a SOURCE is required. Use --self-test to verify the "
              "tool without one.", file=sys.stderr)
        return 64
    if args.sample < 0:
        print("error: --sample cannot be negative.", file=sys.stderr)
        return 64
    if args.apply and not args.out:
        print("error: --apply needs --out.", file=sys.stderr)
        return 64
    is_tree = os.path.isdir(args.source)
    if is_tree and (args.out or args.decompress):
        print("error: a directory is audited, never written. --out and "
              "--decompress go with an .mbtiles file.", file=sys.stderr)
        return 64
    if not is_tree and not os.path.isfile(args.source):
        print("error: %s is not a file or a directory." % args.source,
              file=sys.stderr)
        return 2
    if args.out and os.path.exists(args.out) and (
            not os.path.isdir(args.out) or os.listdir(args.out)):
        # Checked on a dry run too, so the run that would mix into a folder
        # says so before --apply is added.
        print("error: --out %s exists and is not an empty directory. Name a "
              "new one." % args.out, file=sys.stderr)
        return 64

    try:
        if is_tree:
            report = audit(tree_entries(args.source), read_file, args.sample,
                           source=args.source)
        else:
            report = _unpack_file(args)
    except (sqlite3.Error, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    for line in describe(report):
        print(line)
    if args.out:
        if not args.apply:
            print("\nCheck only. %s was not written. Re-run with --apply."
                  % args.out)
        elif report.counts[OK]:
            print("\nwrote %d tile(s) and metadata.json under %s"
                  % (report.counts[OK], args.out))
        else:
            print("\nRefused. %s was not written, because no tile was "
                  "identified." % args.out)
    return gate(report)


if __name__ == "__main__":
    sys.exit(main())
