# Profiling qualification: offline-render

perf: none; failed checks: 0

| Scenario | Exit | Wall ms | Checks | Failed |
| --- | --- | --- | --- | --- |
| render-container-cap-perfmon-cpu-activation-limit | 0 | 437 | 8 | - |
| render-container-cap-perfmon-cpu-capture-limit | 0 | 429 | 8 | - |
| render-container-cap-perfmon-cpu-main-matching-activations | 0 | 457 | 8 | - |
| render-container-cap-perfmon-cpu-mixed-activations | 0 | 506 | 8 | - |
| render-container-cap-perfmon-heap-capture-limit | 0 | 321 | 8 | - |
| render-container-cap-perfmon-heap-mixed-group | 0 | 1883 | 8 | - |
| render-container-cap-perfmon-heap-runner-debug-matching | 0 | 369 | 8 | - |
| render-native-paranoid-2-cpu-activation-limit | 0 | 391 | 8 | - |
| render-native-paranoid-2-cpu-capture-limit | 0 | 372 | 8 | - |
| render-native-paranoid-2-cpu-main-matching-activations | 0 | 414 | 8 | - |
| render-native-paranoid-2-cpu-mixed-activations | 0 | 403 | 8 | - |
| render-native-paranoid-default-heap-capture-limit | 0 | 328 | 8 | - |
| render-native-paranoid-default-heap-mixed-group | 0 | 728 | 8 | - |
| render-native-paranoid-default-heap-runner-debug-matching | 0 | 378 | 8 | - |

## Observations

- render-container-cap-perfmon-cpu-activation-limit: {"html_bytes": 492160, "set_bytes": {"activation/correlation.json": 135590, "cpu/samples.json": 358627, "profile.json": 74429}, "wall_ms": 437}
- render-container-cap-perfmon-cpu-capture-limit: {"html_bytes": 592896, "set_bytes": {"cpu/samples.json": 123191, "profile.json": 43637}, "wall_ms": 429}
- render-container-cap-perfmon-cpu-main-matching-activations: {"html_bytes": 625677, "set_bytes": {"activation/correlation.json": 149849, "cpu/samples.json": 340743, "profile.json": 71568}, "wall_ms": 457}
- render-container-cap-perfmon-cpu-mixed-activations: {"html_bytes": 856294, "set_bytes": {"activation/correlation.json": 136829, "cpu/samples.json": 474857, "profile.json": 81117}, "wall_ms": 506}
- render-container-cap-perfmon-heap-capture-limit: {"html_bytes": 67976, "set_bytes": {"heap/allocations.json": 47771, "profile.json": 7557}, "wall_ms": 321}
- render-container-cap-perfmon-heap-mixed-group: {"html_bytes": 175912, "set_bytes": {"heap/allocations.json": 6050532, "profile.json": 21727}, "wall_ms": 1883}
- render-container-cap-perfmon-heap-runner-debug-matching: {"html_bytes": 79717, "set_bytes": {"heap/allocations.json": 175491, "profile.json": 11023}, "wall_ms": 369}
- render-native-paranoid-2-cpu-activation-limit: {"html_bytes": 279100, "set_bytes": {"activation/correlation.json": 115091, "cpu/samples.json": 182081, "profile.json": 64997}, "wall_ms": 391}
- render-native-paranoid-2-cpu-capture-limit: {"html_bytes": 368641, "set_bytes": {"cpu/samples.json": 80850, "profile.json": 55542}, "wall_ms": 372}
- render-native-paranoid-2-cpu-main-matching-activations: {"html_bytes": 452392, "set_bytes": {"activation/correlation.json": 132518, "cpu/samples.json": 181302, "profile.json": 66510}, "wall_ms": 414}
- render-native-paranoid-2-cpu-mixed-activations: {"html_bytes": 494340, "set_bytes": {"activation/correlation.json": 100094, "cpu/samples.json": 194603, "profile.json": 70288}, "wall_ms": 403}
- render-native-paranoid-default-heap-capture-limit: {"html_bytes": 68057, "set_bytes": {"heap/allocations.json": 47596, "profile.json": 7778}, "wall_ms": 328}
- render-native-paranoid-default-heap-mixed-group: {"html_bytes": 155496, "set_bytes": {"heap/allocations.json": 1491491, "profile.json": 15700}, "wall_ms": 728}
- render-native-paranoid-default-heap-runner-debug-matching: {"html_bytes": 81597, "set_bytes": {"heap/allocations.json": 176867, "profile.json": 11317}, "wall_ms": 378}
