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

```
$ python mbunpack.py self-test
```

This tool reads the first bytes of each tile. A PNG, a JPEG and a WebP have signatures. A gzip
stream starts with `1f 8b`. A Mapbox Vector Tile has no signature, so the tool parses its
protobuf structure. The extension it writes says what the bytes are: `.png`, `.jpg`, `.webp`,
`.mvt` or `.mvt.gz`. It also compares what it found with the `format` row in the metadata table,
and it exits 4 when it compared nothing, so that an empty result cannot pass for a clean one.

```
$ python mbunpack.py --self-test
@@SELFTEST@@
```

The full run prints all 127 assertions. Each `...` line above is where this block is cut.

## What already exists

[mbutil](https://github.com/mapbox/mbutil) is the tool most people reach for. It is archived on
GitHub, so it takes no more fixes. It does both directions, export and import. It reads and
writes UTFGrid data. It knows six directory layouts (`xyz`, `tms`, `wms`, `zyx`, `gwc` and
`ags`), and it can deduplicate identical tiles. Its licence is BSD 3-Clause. If you need to build
an MBTiles file from a folder, mbutil is still the tool that does it, and mbunpack does not.
mbutil has one habit this tool exists to remove: it never looks at the bytes of a tile.

Three more tools unpack MBTiles files. I read their READMEs and did not run them.
[amarant/mbutiles](https://github.com/amarant/mbutiles) is a Rust port of mbutil and documents
the same `--image-format` option with the default `png`.
[watergis/mbtiles2pbf](https://github.com/watergis/mbtiles2pbf) extracts tiles with a file
extension that the caller passes in. [Zverik/mbutil2](https://github.com/Zverik/mbutil2) is a
second rewrite, last pushed in 2017. In each README the extension comes from a setting, not from
the tile.

A search of GitHub for `mbunpack` on 2026-10-22 found no repository of that name. A search for
tools that unpack MBTiles to a folder found the four above. That shows only what the search
found.

## Requirements

Python 3.9 or newer and nothing else. The standard library covers SQLite, gzip and the
protobuf wire format. No third-party package, no network, no credentials, no ArcGIS. The only
formats it relies on are the two public specifications listed under "Specifications relied on".

The self-test prints the same 127 assertions on Windows (Python 3.13.2), on Python 3.9.25 and
on Ubuntu (Python 3.12.3). The three outputs are identical line for line.
`coverage run --branch mbunpack.py --self-test` reports 100 percent of lines and branches on
Windows and on Ubuntu.

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
The MBTiles file is opened read-only, so the run cannot change it or leave a journal beside it.

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
| `CORRUPT` | A gzip stream that is damaged, truncated, followed by extra bytes, or inflates past 64 MiB. Or `tile_data` is NULL or text. | No | 1 |
| `BADCOORD` | The zoom, column or row is not an integer, or is not a cell of the grid at that zoom. | No | 1 |
| `DUPLICATE` | A second row for an address. The later row is skipped. | No | 1 |
| `MISNAMED` | Folder audit only. The file name disagrees with its bytes. | n/a | 1 |
| `SKIPPED` | Folder audit only. The path is not `z/x/y.ext`, so it was not read. | n/a | 0 |

A tile that is `OK` can still disagree with the metadata. The MBTiles specification says the
`format` row `pbf` means gzip-compressed vector tiles. So a tileset that says `pbf` and holds
raw vector tiles disagrees, a tileset that says `png` and holds vector tiles disagrees, and a
tileset that says `png` and holds gzipped PNGs disagrees. Each is counted under `DECLARED` and
makes the run exit 1. A missing `format` row, or one that is not `pbf`, `png`, `jpg` or `webp`,
compares nothing, and the report says so.

**The folder audit.** A file at `z/x/y.ext` is read and named the same way. Its extension is
everything after the first dot, compared exactly with the extension this tool would write. So
`2/1/2.png` holding gzip is `MISNAMED`, a bare `2/1/2.pbf` is `MISNAMED` whether or not it is
gzipped, `2/1/2.pbf.gz` holding a PNG is `MISNAMED`, and a file with no extension at all is
`MISNAMED` too. A folder this tool wrote audits clean.

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
| 3 | `mbutil/util.py` 269 and 270 | The import logs "tiles (and grids) inserted." whatever the count. | The same run printed that line, and no warning. |
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
SKIPPED 1: path is not z/x/y.ext, so it was not read
    metadata.json
VERDICT: 1 finding(s) in 2 file(s).
```

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
  the row.
- [Mapbox Vector Tile 2.1](https://github.com/mapbox/vector-tile-spec/blob/master/2.1/README.md),
  checked at commit `c2bc4e1de1b5a75c59b35f3d72e8a358702629f3`. The file extension SHOULD be
  `mvt`, and the MIME type SHOULD be `application/vnd.mapbox-vector-tile`. The `vector_tile.proto`
  in the same folder gives `Tile.layers` field number 3, `Layer.name` 1, `Layer.version` 15 and
  `Layer.extent` 5. This is why `.mvt` is the extension, and the structure test follows the proto.
  The specification names no magic number, and the tool does not claim one.
- [SQLite URI filenames](https://www.sqlite.org/uri.html). The query parameter `mode=ro` opens
  the database read-only. The fragment after `#` is ignored, and `%HH` escapes may appear in the
  path. This is why the path is percent-encoded before it becomes a URI. A directory name that
  holds `#` is otherwise cut short at the `#`.
- [Python zlib](https://docs.python.org/3/library/zlib.html). `decompressobj.decompress` with a
  `max_length` returns at most that many bytes and keeps the rest in `unconsumed_tail`. This is
  how a gzip bomb is refused and not inflated.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | At least one tile was identified, and no tile has a finding. |
| 1 | At least one finding: `UNKNOWN`, `CORRUPT`, `BADCOORD`, `DUPLICATE`, `MISNAMED`, or a `DECLARED` disagreement. |
| 2 | A file could not be read or written: a missing path, a file that is not SQLite, no `tiles` table, or a folder that cannot be made. |
| 4 | Nothing was compared. No tile was identified, or the tileset or folder holds no tile. |
| 64 | A usage error. This includes an abbreviated flag. |
| 70 | An unexpected crash. This is a fault in the tool and never a finding. |

Exit 4 matters to a scheduled job. A run that read zero tiles has found nothing wrong, and it
has also proved nothing.

## Limits

- The format is read from signatures. A PNG, JPEG or WebP is not decoded, so a truncated PNG with
  a good signature is named `png`. A vector tile whose layers parse but whose geometry is damaged
  is named `mvt`. The tool answers "what is this file", and not "can a renderer draw it".
- It names four formats: PNG, JPEG, WebP and Mapbox Vector Tile, each plain or in gzip. A GIF,
  a TIFF, a zlib-compressed tile or a vector tile of another kind is `UNKNOWN`. The `grids` and
  `grid_data` tables of UTFGrid data are not read.
- An `UNKNOWN`, `CORRUPT`, `BADCOORD` or `DUPLICATE` tile is counted and listed and not written.
  There is no option to write it under a guessed name.
- The folder audit is strict by design. The extension must equal what this tool would write,
  exactly and in lower case. A folder from any tool that keeps gzip under a bare `.pbf` is
  flagged, because a file host must then send `Content-Encoding: gzip` for it to work. If your
  host does send it, the flag is a note and not a fault.
- The ceiling of zoom 30 is this tool's own limit, and the specification sets none. It stops a
  corrupt `zoom_level` from sizing a huge grid. A gzip stream that inflates past 64 MiB is
  `CORRUPT`. No real tile does.
- Duplicate addresses are found when two rows sit next to each other in the sorted order. The
  query sorts by zoom, column and row. A tileset with the unique index from the specification
  cannot hold a duplicate. A view or a table without the index can.
- The sort reads the whole `tiles` table once. Timing from one run on one Windows workstation:
  20,000 synthetic tiles took 5.5 seconds to check and 17 seconds to unpack. Your disk will
  differ. A tileset of millions of tiles is slow and the tool prints no progress.
- A write that fails part-way, such as a full disk, exits 2 and leaves the files written so far.
  Delete the folder and run again.
- Only the `tiles` and `metadata` tables are read. A file with no `metadata` table is read and
  reports no declared format. The MBTiles specification requires that table, so such a file is
  not valid, and the tool does not say so.
- A metadata row with a NULL name is written to `metadata.json` under the key `None`.
- No Esri behaviour is relied on, and none is tested. Vector tile packages from ArcGIS are a
  different format and are not read.
- The `metadata.json` it writes is plain JSON of the table. It is not a promise that another
  importer will read it.
- Nothing in this tool needs the network or a credential, so there is no network path to test
  against a stub, and no credential can reach its output.

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
