# mbunpack

Unpack an MBTiles file into a z/x/y folder and name every tile by what its bytes are, not by a
flag. Audit a folder that some other tool already unpacked and list every file whose name
disagrees with its contents.

An MBTiles file holds each tile as a blob, and a blob has no file name. Whatever unpacks the
file has to invent the extension, and some tools take it from a command line flag whose default
is `png`. Picture a vector basemap exported for a static file host. The export prints nothing
wrong and exits 0. The folder holds tens of thousands of files, every one named `.png`, and no
file is an image. Each is a gzip-compressed vector tile. The viewer draws nothing and reports no
error. Someone renames the files to `.pbf` and the name now looks right, but the bytes are still
gzip, and the server must send a `Content-Encoding` header that nobody set. Someone tries to
rebuild the MBTiles file from the folder to start again. The import logs "tiles (and grids)
inserted.", exits 0, and the new file holds no tiles at all.

Nobody catches it, because no step fails. Every tool did what it was told. The name was a
guess, and the guess was never compared with the file. The section "What mbutil does" reproduces
each step of this on a one-tile file, with the line of code behind it.

This tool reads the first bytes of each tile. A PNG, a JPEG and a WebP have signatures. A gzip
stream starts with `1f 8b`. A Mapbox Vector Tile has no signature, so the tool parses its
protobuf structure. The extension it writes says what the bytes are: `.png`, `.jpg`, `.webp`,
`.mvt` or `.mvt.gz`. It also compares what it found with the `format` row in the metadata table.
It exits 4 when no tile was identified, or when the `format` row names a format it does not
read, so that a run that compared nothing cannot pass for a clean one.

```
$ python mbunpack.py --self-test
mbunpack self-test: temporary files only, no network, no credentials
--------------------------------------------------------------------
PASS  a PNG signature is a png
PASS  a JPEG signature is a jpg
PASS  RIFF with WEBP at byte 8 is a webp
...
PASS  a group or reserved wire type is refused even when four bytes follow it  <-- pinned defect
...
PASS  the footer reports failures by count and by name, and exits 1  <-- pinned defect
PASS  importing the module prints nothing and exposes the core
--------------------------------------------------------------------
185 assertions, 0 failed
```

The full run prints all 185 assertions. Each `...` line above is where this block is cut. The
lines before and after a cut are copied from the run without change.

## What already exists

[mbutil](https://github.com/mapbox/mbutil) is the tool most people reach for. It is archived on
GitHub, so it takes no more fixes. It does both directions, export and import. It reads and
writes UTFGrid data. It knows six directory layouts (`xyz`, `tms`, `wms`, `zyx`, `gwc` and
`ags`), and it can deduplicate identical tiles. Its licence is BSD 3-Clause. If you need to build
an MBTiles file from a folder, mbutil is still the tool that does it, and mbunpack does not.
mbutil has one habit this tool exists to remove: it never looks at the bytes of a tile.

Four more tools read MBTiles files. I read their READMEs on 2026-10-23 and did not run them. I
did not read their code.

- [amarant/mbutiles](https://github.com/amarant/mbutiles) is a Rust tool that exports tiles from
  an MBTiles file. Its README says "Inspired by mbutil" and documents an `--image-format` option
  with the default `png`.
- [watergis/mbtiles2pbf](https://github.com/watergis/mbtiles2pbf) extracts tiles with a file
  extension that the caller passes in, `FileExtension.MVT` in its example.
- [Zverik/mbutil2](https://github.com/Zverik/mbutil2) says it is "loosely based on original
  mapbox-mbutil". Its README is two lines, so I know nothing more about it. GitHub gives its
  last push as 2011-11-19.
- [maplibre/martin](https://github.com/maplibre/martin) is a tile server, licensed Apache-2.0.
  Its README says its `mbtiles` tool can examine, copy, validate, compare and apply diffs between
  MBTiles files. That is a different job from this one, and I do not know what its `validate`
  checks.

On 2026-10-30 at 02:10 UTC, `gh search repos mbunpack` returned one repository, this one. The
search `gh search repos "mbtiles unpack"` returned none. I found the tools above by reading the
results for `mbtiles`. That is not a census, and it shows only what the search found.

## Requirements

Python 3.9 or newer and nothing else. The standard library covers SQLite, gzip and the
protobuf wire format. No third-party package, no network, no credentials, no ArcGIS. The only
formats it relies on are the public specifications listed under "Specifications relied on".

The self-test prints the same 185 assertions on Windows (Python 3.13.2), on Windows Python
3.9.25 and on Ubuntu (Python 3.12.3). The outputs are identical line for line, apart from the
carriage return that Windows text mode adds to each line when the output goes to a file. To
repeat the 3.9 run, install that version first, for example with `uv python install 3.9`.
`coverage run --branch mbunpack.py --self-test` reports 100 percent of lines and branches (1049
statements, 218 branches) on Windows (Python 3.13.2, coverage 7.16.1) and on Ubuntu (Python
3.12.3, coverage 7.17.0).

```
git clone https://github.com/uhsear/mbunpack.git
```

## Usage

```
python mbunpack.py --self-test
python mbunpack.py tiles.mbtiles
python mbunpack.py tiles.mbtiles --out tiles_dir --apply
python mbunpack.py tiles.mbtiles --out tiles_dir --decompress --apply
python mbunpack.py tiles.mbtiles --out tiles_dir --scheme tms --apply
python mbunpack.py tiles_dir
```

| Flag | Default | What it does |
|---|---|---|
| `SOURCE` | none | An `.mbtiles` file to check or unpack, or a folder to audit. Required unless `--self-test`. |
| `--out` | off | The folder to unpack into. It must not exist, or must be empty. Needs `--apply`. |
| `--scheme` | `xyz` | `xyz` writes y counted from the north. `tms` keeps the MBTiles row as it is. |
| `--decompress` | off | Write gzipped tiles inflated, under the plain extension. |
| `--sample` | `10` | How many examples to list under each heading. `0` lists none. |
| `--apply` | off | Actually write. Without it nothing is written. |
| `--self-test` | off | Run the assertions and exit. Other flags are ignored. |

Every flag must be spelled in full. The parser sets `allow_abbrev=False`, so `--ap`, `--app` and
`--appl` are refused with exit 64 instead of being read as `--apply`. A run with `--out` and
without `--apply` is a dry run: it reads every tile, prints the full report and writes nothing.

`--out` must name a folder that does not exist or is empty. The check runs on a dry run too. A
folder that holds files is refused with exit 64, and so is a path that is also the source file.

The MBTiles file is opened read-only and immutable, so SQLite writes no file beside it. That
holds for a file in WAL journal mode too. A file with a non-empty `-wal` or `-journal` file next
to it is refused with exit 2, because an immutable open ignores those files and the read would
miss their changes. Merge them first, on a copy.

A folder source is audited and never written. `--out` and `--decompress` go with an `.mbtiles`
file, and a folder source with either one is refused with exit 64.

## What it checks

**The tile.** The first bytes decide the format. The order is: zero bytes, gzip magic, PNG
signature, JPEG signature, `RIFF` and `WEBP`, and last the vector tile structure test.

| Found | Extension written | How it is known |
|---|---|---|
| PNG | `.png` | The 8-byte PNG signature. |
| JPEG | `.jpg` | `ff d8 ff`. |
| WebP | `.webp` | `RIFF`, four size bytes, then `WEBP`. |
| Vector tile | `.mvt` | The whole blob parses as a protobuf `Tile` of layers, and each layer has a name. |
| Any of the above inside gzip | `.png.gz`, `.mvt.gz` and so on | gzip magic `1f 8b`, then the inner bytes are named. |

The vector tile test has no signature to use, because the format has none. It accepts a blob
only when every byte belongs to a layer (field number 3), each layer parses, each layer has a
non-empty name, and a stated version is 1 or 2. A random blob that passes all of that is very
unlikely, but the test cannot prove the geometry inside is sound. See Limits.

A gzip tile is written as it is stored, under `.mvt.gz`, unless you pass `--decompress`. That
writes the inflated bytes as `.mvt`. The extension always matches the bytes written.

**Every row lands in exactly one status.** The counts add up to the number of rows read.

| Status | Meaning | Written | Exit |
|---|---|---|---|
| `OK` | The bytes are a format this tool names. | Yes | 0 |
| `EMPTY` | Zero bytes, or gzip of zero bytes. | No | 0 |
| `UNKNOWN` | The bytes match no format this tool reads, or are gzip inside gzip. | No | 1 |
| `CORRUPT` | A gzip stream that is damaged, truncated, followed by extra bytes, or inflates past 64 MiB. Or `tile_data` is NULL or text. Or, in a folder, a file larger than 64 MiB or one that cannot be read. | No | 1 |
| `BADCOORD` | The zoom, column or row is not an integer, or is not a cell of the grid at that zoom. | No | 1 |
| `DUPLICATE` | A second tile for one address. Only the first copy is used. | No | 1 |
| `MISNAMED` | Folder audit only. The file name disagrees with its bytes. | n/a | 1 |
| `SKIPPED` | Folder audit only. The path is not `z/x/y.ext`, or the entry is a link or special file. It was not read. | n/a | 0 |

`EMPTY` and `SKIPPED` do not make the run exit 1. The report counts them, and the verdict line
no longer says that every tile was identified when some were not. A folder with one good tile,
two zero-byte files and one retina-named file ends like this:

```
EMPTY 2: zero-byte tile, there is nothing to write
    1/0/1.png  zero bytes
    1/1/0.png  zero bytes
SKIPPED 1: not z/x/y.ext, or a link or special file, so it was not read
    1/1/1@2x.png
VERDICT: 1 of 4 file(s) were identified from their bytes. Not identified, and not counted as a finding: 2 EMPTY, 1 SKIPPED.
```

A scheduled job that reads only the exit code does not see that line. Read the report, or watch
for the word `SKIPPED` or `EMPTY` in it.

A tile that is `OK` can still disagree with the metadata. The MBTiles specification says the
`format` row `pbf` means gzip-compressed vector tiles. So a tileset that says `pbf` and holds
raw vector tiles disagrees, a tileset that says `png` and holds vector tiles disagrees, and a
tileset that says `png` and holds gzipped PNGs disagrees. Each is counted under `DECLARED` and
makes the run exit 1.

The `format` row is read as `pbf`, `png`, `jpg` or `webp`. Four more spellings are read as the
format they name: `jpeg` as `jpg`, and the media types `image/png`, `image/jpeg` and
`image/webp`. The MBTiles specification allows an IETF media type there. The table of spellings
is this tool's choice and not the specification's. Any other value, such as `gif`, `image/tiff`
or a BLOB, compares nothing: the report says that the format is not read, and the run exits 4
even when every tile was identified. A tileset with no `format` row at all also compares
nothing, but it exits 0 when every tile was identified, because there is no declared format to
disagree with. The two cases differ on purpose. A row that names a format is a claim the tool
cannot check. A missing row makes no claim.

**The folder audit.** A file at `z/x/y.ext` is read and named the same way. Its extension is
everything after the first dot, compared with the extension this tool would write. One
spelling is also accepted: a JPEG kept under `.jpeg`, or `.jpeg.gz` when gzipped. So
`2/1/2.png` holding gzip is `MISNAMED`, a bare `2/1/2.pbf` is `MISNAMED` whether or not it is
gzipped, `2/1/2.pbf.gz` holding a PNG is `MISNAMED`, and a file with no extension at all is
`MISNAMED` too. A folder this tool wrote audits clean.

A second file at one address is a `DUPLICATE`, whatever its extension or zero padding. So
`3/1/1.png` and `3/1/1.jpg` in one folder exit 1, and so do `2/1/2.png` and `2/1/02.png`.

The audit opens only regular files. A symbolic link, a FIFO or other special file, and any file
whose folder resolves outside the audited folder (a Windows junction does this) is listed under
`SKIPPED` and is never opened. A link to a folder is listed and not entered. Audit the target as
its own folder if you want its tiles read. A FIFO named like a tile does not stop the run, and a
link to a file outside the folder cannot put that file's bytes in the report.

A file that cannot be read is `CORRUPT` and named, and the files before it still count. A file
larger than 64 MiB is `CORRUPT` and is not read past that size.

Every name, every path on the command line and every metadata value is printed with its control
characters shown as `\xNN`. A folder that holds a file named with escape codes cannot move the
cursor or set the title of your terminal. Text that SQLite or the operating system puts in an
error message is printed as it came.

## What mbutil does

Every row below was run against a fresh clone of mbutil at commit
`544c76eea925e3c1bc129f601e314ea9701bfc79`, the head of its default branch on 2026-10-22. The
line numbers are those of that commit. I ran mbutil to watch it. I copied no code from it.

Build a one-tile vector file with the standard library, then run both tools on it:

```
import gzip, sqlite3
layer = b"\x0a\x05roads\x78\x02\x28\x80\x20"
tile = gzip.compress(b"\x1a" + bytes([len(layer)]) + layer)
con = sqlite3.connect("roads.mbtiles")
con.execute("create table metadata (name text, value text)")
con.execute("create table tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob)")
con.execute("insert into metadata values ('name', 'roads'), ('format', 'pbf')")
con.execute("insert into tiles values (2, 1, 1, ?)", (tile,))
con.commit()
```

| # | Where | What the code does | What I saw |
|---|---|---|---|
| 1 | `mbutil/util.py` 334 and 336, `mb-util` 41 to 44 | The export extension is `kwargs.get('format', 'png')`. The tile bytes are never read. | `python mbutil/mb-util roads.mbtiles out` wrote `out/2/1/2.png`, exit 0. With `--image_format pbf` it wrote `2.pbf`, and the file starts `1f 8b`. |
| 2 | `mbutil/util.py` 241 to 251 | The import keeps a file only when `ext == image_format`. There is no `else` for tiles. | `python mbutil/mb-util out_pbf back.mbtiles` exited 0. `back.mbtiles` holds 0 tiles. |
| 3 | `mbutil/util.py` 269 and 270 | The import logs "tiles (and grids) inserted." whatever the count. The call is `logger.debug`, and `mb-util` line 17 sets the level to `DEBUG`, so the line prints. | The same run printed `DEBUG:mbutil.util:tiles (and grids) inserted.`, and no warning. |
| 4 | `mbutil/util.py` 224 | `current_file.split('.',1)` makes the extension everything after the first dot. | A file `2.pbf.gz` was dropped without a message, exit 0, 0 tiles. A file `2` with no dot ended in `ValueError: not enough values to unpack`, exit 1. |
| 5 | `mb-util` 73 to 76 | The `dumps` command calls `sys.exit(1)` after it succeeds. | `python mbutil/mb-util roads.mbtiles dumps` printed the metadata and exited 1. |

Row 1 is the one this tool answers directly. `python mbunpack.py roads.mbtiles --out unpacked
--apply` wrote `unpacked/2/1/2.mvt.gz`, and the bytes in it are the stored bytes. Run on the
folder mbutil wrote, the audit says:

```
$ python mbunpack.py out_default
mbunpack: out_default: 2 file(s) read
MISNAMED 1: file name disagrees with the bytes inside it
    2/1/2.png  bytes are mvt (gzip), the name ends .png, expected .mvt.gz
SKIPPED 1: not z/x/y.ext, or a link or special file, so it was not read
    metadata.json
VERDICT: 1 finding(s) in 2 file(s).
```

The `identified from the bytes:` block counts only files that are named correctly. The one tile
here is misnamed, so the block is absent. `metadata.json` is the file that mbutil writes beside
the tiles, and it is `SKIPPED` because it is not a tile.

That run exits 1. The audit is the check to run before a folder goes back into mbutil's import,
or onto a file host. Rows 2 to 4 are why: the import cannot tell a name it rejects from a tile
that is not there.

mbunpack has no import. It does not build MBTiles files.

## Specifications relied on

- [MBTiles 1.3](https://github.com/mapbox/mbtiles-spec/blob/master/1.3/spec.md), checked at
  commit `1970e38abe35bebf8fd341e71b6dbf906d420565`. The `format` row is `pbf`, `jpg`, `png`,
  `webp` or an IETF media type. `pbf` "refers to gzip-compressed vector tile data". `tile_row` is
  in the TMS scheme, so tile 11/327/791 is stored with `tile_row` 1256, which is 2^11 - 1 - 791.
  `tile_data` is the raw binary image or vector tile data. This is why the default scheme flips
  the row. The specification also says that a `pbf` tileset MUST carry a `json` metadata row.
  This tool does not check for it.
- [Mapbox Vector Tile 2.1](https://github.com/mapbox/vector-tile-spec/blob/master/2.1/README.md),
  checked at commit `c2bc4e1de1b5a75c59b35f3d72e8a358702629f3`. The file extension SHOULD be
  `mvt`, and the MIME type SHOULD be `application/vnd.mapbox-vector-tile`. The `vector_tile.proto`
  in the same folder gives `Tile.layers` field number 3, `Layer.name` 1, `Layer.version` 15 and
  `Layer.extent` 5. This is why `.mvt` is the extension, and the structure test follows the proto.
  The specification names no magic number, and the tool does not claim one.
- [SQLite URI filenames](https://www.sqlite.org/uri.html). The query parameter `mode=ro` opens
  the database read-only. The query parameter `immutable=1` tells SQLite that the file cannot
  change, and SQLite then "skips all file locking and change detection". The fragment after `#`
  is ignored, and `%HH` escapes may appear in the path. This is why the path is percent-encoded
  before it becomes a URI. A directory name that holds `#` is otherwise cut short at the `#`.
  The same page says "The authority may be omitted, may be blank, or may be localhost. Any other
  authority results in an error." The tool therefore always writes a blank authority, which is
  `file://` and then the path. A network share `\\host\share\x.mbtiles` becomes
  `file:////host/share/x.mbtiles`, and a POSIX path that starts with two slashes becomes
  `file:////tmp/x`. The standard library function `urllib.request.pathname2url` returns
  `//host/share/x` for a UNC path on Python 3.12 and 3.13, and SQLite reads `host` as an
  authority and refuses it. It returns `////host/share/x` on Python 3.9. The self-test pins the
  URI strings, and it opens a file through the `localhost` administrative share on Windows and
  through a path with two leading slashes on Linux.
- [SQLite write-ahead logging, section 5](https://www.sqlite.org/wal.html). A WAL-mode database
  can be read without write permission when its `-shm` and `-wal` files exist, when the folder
  is writable so they can be created, or when the connection uses `immutable`. A plain read-only
  open therefore creates those two files beside the database. The immutable open does not, and
  it does not read the `-wal` file, which is why a non-empty one is refused.
- [RFC 1952](https://www.rfc-editor.org/rfc/rfc1952.txt), section 2.2. "A gzip file consists
  of a series of members". This tool reads one member, so a second member is reported as bytes
  after the end of the stream. See Limits.
- [Python zlib](https://docs.python.org/3/library/zlib.html). `decompressobj.decompress` with a
  `max_length` returns at most that many bytes and keeps the rest in `unconsumed_tail`. This is
  how a gzip bomb is refused and not inflated.
- [Python integer string conversion limit](https://docs.python.org/3/library/stdtypes.html#int-max-str-digits).
  From Python 3.11, `int()` of a string of more than 4300 digits raises `ValueError`. This is why
  a run of more than 20 digits in a tile name is never passed to `int()`.
- [Python os.walk](https://docs.python.org/3/library/os.html#os.walk). By default it does not
  walk down into symbolic links that resolve to directories. They still appear in the list of
  directories, which is how the audit finds and reports them.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | At least one tile was identified, no tile has a finding, and the `format` row, if there is one, is a format this tool reads. `EMPTY` and `SKIPPED` files are not findings, and the verdict line counts them. |
| 1 | At least one finding: `UNKNOWN`, `CORRUPT`, `BADCOORD`, `DUPLICATE`, `MISNAMED`, or a `DECLARED` disagreement. |
| 2 | A file could not be read or written: a missing path, a file that is not SQLite, no `tiles` table, a `-wal` or `-journal` file with changes in it, or a folder that cannot be made. |
| 4 | Nothing was compared. No tile was identified, the tileset or folder holds no tile, or the `format` row names a format this tool does not read. |
| 64 | A usage error. This includes an abbreviated flag. |
| 70 | An unexpected crash. This is a fault in the tool and never a finding. |

Exit 4 matters to a scheduled job. A run that read zero tiles has found nothing wrong, and it
has also proved nothing. A finding takes priority: a run with a finding exits 1 even when the
`format` row is not one this tool reads.

## Limits

- The format is read from signatures. A PNG, JPEG or WebP is not decoded, so a truncated PNG with
  a good signature is named `png`. A vector tile whose layers parse but whose geometry is damaged
  is named `mvt`. The tool answers "what is this file", and not "can a renderer draw it".
- It names four formats: PNG, JPEG, WebP and Mapbox Vector Tile, each plain or in gzip. A GIF,
  a TIFF, a zlib-compressed tile or a vector tile of another kind is `UNKNOWN`. The `grids` and
  `grid_data` tables of UTFGrid data are not read.
- A vector tile whose top-level message carries a field other than the layer list is named
  `UNKNOWN`, because every byte must belong to field 3.
- A gzip file made of two or more members joined together is `CORRUPT`, because only the first
  member is read and the rest counts as trailing bytes. RFC 1952 allows such a file, and
  browsers accept it.
- An `UNKNOWN`, `CORRUPT`, `BADCOORD` or `DUPLICATE` tile is counted and listed and not written.
  There is no option to write it under a guessed name.
- A `format` row spelled `jpeg` or `image/png`, `image/jpeg` or `image/webp` is read as the
  format it names. Any other media type, and a row stored as a BLOB, is not read, and the run
  exits 4. A row that is empty or only spaces prints `declared format: , which mbunpack does not
  read` and exits 4 too. A tileset with no `format` row exits 0.
- The folder audit is strict by design. The extension must equal what this tool would write, in
  lower case, apart from `.jpeg` and `.jpeg.gz` for a JPEG. A folder from any tool that keeps gzip under a bare `.pbf` is
  flagged, because a file host must then send `Content-Encoding: gzip` for it to work. If your
  host does send it, the flag is a note and not a fault.
- The folder audit accepts zero-padded names. `02/001/002.png` is read as the cell 2/1/2 and can
  be `OK`. It finds a duplicate only when a second file has the same cell.
- A part of a tile path with more than 20 digits is not a tile name, and the file is `SKIPPED`.
- The folder audit keeps every address it has seen in memory, and it reads each file whole, up to
  64 MiB. A folder of many millions of tiles needs memory to match.
- The audit does not follow a link. A folder that is reached only through a link, or a junction
  that points outside, is not read, and its tiles do not count. It exits 0 if nothing else is
  wrong. The report lists the link under `SKIPPED`.
- The zoom ceiling of 30 is this tool's own limit, and the specification sets none. It stops a
  corrupt `zoom_level` from sizing a huge grid. The 64 MiB inflate limit is a limit this tool
  chose too. A gzip stream that inflates past it is `CORRUPT`.
- Tiles in a tileset are checked for a duplicate when two rows sit next to each other in the
  sorted order. The query sorts by zoom, column and row. A tileset with the unique index from the
  specification cannot hold a duplicate. A view or a table without the index can. The check
  compares the raw values with `==`, so a `True` or `1.0` in `tile_column` equals `1` and is
  reported as `DUPLICATE`, not as `BADCOORD`.
- When a table without the unique index holds two rows for one address, SQLite's `ORDER BY` does
  not say which copy comes first. Which bytes are written, and which row is called the
  duplicate, is therefore not defined.
- The sort reads the whole `tiles` table once. Timing from two runs on one Windows workstation,
  with 20,000 synthetic gzipped vector tiles at zooms 8 to 10 (a 1.3 MB file): 0.27 and 0.38
  seconds to check, and 10.3 and 12.0 seconds to unpack. Writing 20,000 small files is the slow
  part. These are two runs and not a benchmark. Your disk will differ. A tileset of millions of
  tiles is slow and the tool prints no progress.
- A write that fails part-way, such as a full disk, exits 2 and leaves the files written so far.
  The self-test makes the metadata write fail and checks that the three tile files stay. Delete
  the folder and run again.
- Do not point the tool at an MBTiles file that another program is writing. SQLite documents that
  an immutable open of a file that changes can return wrong results.
- Only the `tiles` and `metadata` tables are read. A file with no `metadata` table is read and
  reports no declared format. The MBTiles specification requires that table, so such a file is
  not valid, and the tool does not say so.
- A metadata row with a NULL name is written to `metadata.json` under the key `None`. When two
  rows share a name, `metadata.json` keeps the last one.
- `metadata.json` is a copy of the `metadata` table. A credential that someone stored in a
  metadata row is copied into the output folder with it.
- A closed pipe is not handled. `python mbunpack.py --self-test | head -3` ended in a traceback
  with exit 70 on Ubuntu (Python 3.12.3), and in `OSError: [Errno 22]` with exit 120 on Windows
  (Python 3.13.2). Whether a report cut by `head` fails depends on its size and timing. Read the
  whole report, and do not use the exit code of a cut pipe.
- A tile folder scanned from its parent, with the tiles nested one level down, and a folder
  of retina names such as `1/0/1@2x.png`, report every such file as `SKIPPED`. The run exits 4
  if nothing at all was identified, and exits 0 if even one `z/x/y.ext` file sits at the root.
  Read the `SKIPPED` line and not only the exit code.
- A folder that stores raw (not gzipped) vector tiles under `.pbf` is reported `MISNAMED`, with
  `.mvt` as the expected name. The audit follows the `.mvt` and `.mvt.gz` names that this tool
  writes, and not the `.pbf` convention.
- The `-wal` and `-journal` check looks beside the path as typed, not beside the target of a
  symbolic link. A symlinked `.mbtiles` file whose real file has a non-empty `-wal` is read
  without it, and the read can miss recent changes.
- An `--out` folder that exists but cannot be listed, for lack of permission, ends in exit 70
  and not 2 or 64.
- A `NULL` or text `tile_data` is listed under its stored `tile_row`, and every other status is
  listed under the flipped `y`. Under the default `xyz` scheme that one label can point at a
  different tile.
- A gzip tile with zero padding after the stream is `CORRUPT`.
- The tool ignores a `scheme` metadata row. The MBTiles specification stores `tile_row` from the
  bottom, so a file that stores it from the top needs `--scheme tms`.
- A hard link inside the audited folder is a regular file and is read like any other. If its name
  looks like a tile and its bytes match nothing, the first eight bytes are printed as hex.
- The self-test cannot detect a fault in `summary()`, the function that prints its own footer. A
  mutant that always printed `0 failed` would hide itself. Only a second process that parses the
  footer could close that gap, and there is none.
- No Esri behaviour is relied on, and none is tested. Vector tile packages from ArcGIS are a
  different format and are not read.
- The `metadata.json` it writes is plain JSON of the table. It is not a promise that another
  importer will read it.
- The link assertion in the self-test makes a symbolic link to a file and one to a folder. A
  host that does not allow links runs the same assertion with no links, and it proves less. The
  Windows host that made the quoted run allowed links. The FIFO case is not in the self-test,
  because a regression would hang the run. It is covered by the pure `may_read` function, and I
  ran it once by hand on Ubuntu with a FIFO named `1/0/1.png`, under `timeout 5`.
- Nothing in this tool needs the network or a credential, so there is no network path to test
  against a stub. The only file bytes it can print are the first eight bytes of an `UNKNOWN` tile
  inside the folder you named.

## Contributing

Open an issue with a tile that this tool names wrongly. Say what produced it. A fixture is
better than a description. Every fix gets an assertion in `--self-test`.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

- [jobharness](https://github.com/uhsear/jobharness) - a scheduled script's logging, retry and
  safe unzip. Both tools write files only to a place the caller named, and refuse a name that
  could lead outside it.
- [sdcheck](https://github.com/uhsear/sdcheck) - the same refusal to let a clean exit code stand
  for a check that compared nothing.
