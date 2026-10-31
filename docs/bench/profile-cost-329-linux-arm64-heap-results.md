# Profile command cost: 329-linux-arm64-heap

- Machine: Debian arm64 container on Apple M1 Pro (Colima), not the supported machine class
- Revision: `8d9981678a318a4fa7915ad40070d56aafec064e`
- Tools: real collector. The real collector on PATH, as the capture records.
- Load: One coding agent. Linux VM with the Docker disk 100% full; workspace on a bound host directory.
- Repetitions: 5
- Runner end: Modification time of the side-car, the last file of the runner. It is on the same clock as the start of the command, with file-system resolution. The end of perf or Heaptrack is in post-Run time.

Values in ms: min / median / max.

| Case | Mode | End to end | Capture and Run | Post-Run | Profile hash | Profile hash ops | Profile hash MiB | Runner input digests | Runner Recording |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| heap-runner | full | 1715.3 / 1846.4 / 1861.2 | 757.9 / 903.4 / 911.8 | 943.0 / 949.4 / 957.5 | 11.6 / 12.6 / 13.5 | 5 | 12.0 | 2 | calculated |
| heap-runner | external | 1698.4 / 1795.1 / 1838.0 | 728.8 / 827.7 / 858.6 | 964.0 / 969.6 / 979.5 | 11.2 / 12.9 / 13.1 | 5 | 12.0 | 0 | omitted |
| heap-process | full | 1158.9 / 1163.3 / 1173.7 | 728.1 / 731.0 / 747.9 | 425.7 / 430.0 / 434.0 | 4.3 / 5.2 / 5.2 | 5 | 3.7 | 3 | calculated |
| heap-process | external | 1148.3 / 1154.6 / 1168.9 | 711.8 / 722.0 / 725.7 | 426.5 / 431.6 / 457.1 | 4.4 / 4.9 / 5.1 | 5 | 3.7 | 0 | omitted |
