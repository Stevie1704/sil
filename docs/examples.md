# Installed examples

The release wheel and runtime image carry the editable example sources used
by the user guides. List them with `sil examples`, or find their installed
directory with `sil examples --path`. They match the installed SiL version
and need no network access or SiL source checkout.

Copy them into a new or empty directory before editing or building:

```sh
sil examples --dir examples
```

Run the guides' `examples/...` commands from the parent of that directory.
All examples are copied together because bundle preparation uses the library,
FMU replay and coupling examples as siblings. An existing nonempty directory
is refused before copying; choose a new directory to retain your edits.

| Directory | Workflow |
| --- | --- |
| `library/` | Shared-library adapter, simple and multi-port bindings, sample C libraries, recorded inputs and replay windows: `sil docs library`, `sil docs replay` |
| `bundle/` | Preparation script for library, FMU replay and coupling regression bundles: `sil docs regression-bundles` |
| `csv/` | CSV mapping, signals, observer and replay Manifest: `sil docs recorded-input` |
| `fmu/` | Single-FMU Schemas, mapping, stimulus and Manifest; pass your archive with `manifest.py --fmu PATH`: `sil docs fmi` |
| `fmu-replay/` | Small C FMU, packaging script, authoring document and independently enumerated reference: `sil docs recorded-input` |
| `fmu-coupling/` | Coupling and substitution documents: `sil docs fmu-coupling` |
| `fmu-numeric/`, `fmu-array/` | Numeric and array authoring documents, input signals and references: `sil docs fmi` |
| `compare/` | Comparison contract and reference signals: `sil docs comparison` |
| `adas-reference/` | Native and Process adapters, C application, maneuvers, expected outputs and FMU sources: `sil docs adas-reference` |

The packaged ACC example also runs directly with
`python -m sil.examples.acc.manifest acc.json`; it needs no export.

Install the wheel and the native development archive to author and run Native
participants: the archive supplies `sil-run`, `silschema` and `include/sil`.
Building C examples needs a compiler. A supplier FMU is supplied separately;
SiL does not bundle the repository's reference FMU fixtures. The runtime
image provides execution, docs and example export; its minimal environment
does not include a compiler or the native development headers.

`sil examples` exits `0` after listing, locating or copying, `1` when examples
are not installed, and `2` for invalid arguments or a refused export. An
unstamped source checkout uses its own `examples/` directory instead.

The command and export layout are supported interfaces. Example code is an
editable starting point; its functions are not public Python APIs.
Contributor instructions, architecture decisions and retained acceptance
evidence remain in the source repository. Packaged guides link to that
optional background at the matching release tag.
