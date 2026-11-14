# Camera frame with record mode digest

This capture shows what `record="digest"` ([issue #398](https://github.com/Stevie1704/sil/issues/398))
changes on the camera path of the [routing baseline](large-message-routing-baseline.md).
It is evidence, not a threshold: no test or gate reads a value from it.

## Procedure

`make bench` now has the rows `native-camera-fanout0-recon-digest` and
`native-camera-fanout1-recon-digest`. Each row is one Native publisher of
`bench.CameraFrame` (2 764 808 bytes, 1280×720 RGB8) and 0 or 1 Native
subscribers. Recording is on, and the payload Channel has record mode `digest`.
The rows were captured with:

```sh
python tools/bench_routing.py --build-dir build --out-dir <dir> \
  --only native-camera-fanout0 --repeats 5
python tools/bench_routing.py --build-dir build --out-dir <dir> \
  --only native-camera-fanout1 --repeats 5
```

- Build: `a1442f0`, Release, OpenSSL SHA-256 backend, Full provenance mode.
- Machine: Apple M1 Pro, macOS 27.0.1 arm64, warm page cache. This is not the
  supported Linux x86-64 machine class.
- Load: the 1-minute load average was about 7. Do not use these values to
  compare small differences.
- 50 frames per Run, 5 timed Runs per row. The values are medians.

## Results

Per frame, from the medians of the whole Run.

| Subscribers | Recording | Wall µs | User µs | System µs | Recorded bytes |
| --- | --- | --- | --- | --- | --- |
| 0 | on (full) | 3485 | 2846 | 550 | 2 764 808 |
| 0 | on, digest | 1518 | 1333 | 95 | 32 |
| 0 | off | 369 | 219 | 89 | 0 |
| 1 | on (full) | 3473 | 2844 | 518 | 2 764 808 |
| 1 | on, digest | 1615 | 1403 | 118 | 32 |
| 1 | off | 486 | 313 | 106 | 0 |

## What this shows

- **Digest removes most of the Recording cost.** Against `full`, a digest frame
  costs about 1970 µs less wall time and about 1500 µs less user time. The
  Messages of 50 frames hold 1600 payload bytes, not 132 MiB.
- **Digest does not remove all of it.** A digest frame still costs about
  1150 µs more than a frame with Recording off. That is the SHA-256 of
  2.76 MB per frame. In Full provenance mode, a `full` Recording also pays a
  SHA-256 of the same bytes, because the runner digests the file while it
  writes it. So digest removes the write and the I/O, not the hash.
- **The subscriber copy is unchanged.** The `subscriber_copy` count and the
  difference between 0 and 1 subscribers are the same in each mode.
- `--no-recording` stays the fastest option, but it gives no evidence of the
  Run. Digest keeps the time, the Channel, the publish order and a proof of
  each payload.
