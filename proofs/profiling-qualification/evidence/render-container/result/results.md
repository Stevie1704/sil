# Profiling qualification: offline-render

perf: none; failed checks: 0

| Scenario | Exit | Wall ms | Checks | Failed |
| --- | --- | --- | --- | --- |
| render-container-cap-perfmon-cpu-activation-limit | 0 | 440 | 8 | - |
| render-container-cap-perfmon-cpu-capture-limit | 0 | 432 | 8 | - |
| render-container-cap-perfmon-cpu-main-matching-activations | 0 | 454 | 8 | - |
| render-container-cap-perfmon-cpu-mixed-activations | 0 | 498 | 8 | - |
| render-container-cap-perfmon-heap-capture-limit | 0 | 322 | 8 | - |
| render-container-cap-perfmon-heap-mixed-group | 0 | 1903 | 8 | - |
| render-container-cap-perfmon-heap-runner-debug-matching | 0 | 363 | 8 | - |
| render-native-paranoid-2-cpu-activation-limit | 0 | 380 | 8 | - |
| render-native-paranoid-2-cpu-capture-limit | 0 | 364 | 8 | - |
| render-native-paranoid-2-cpu-main-matching-activations | 0 | 404 | 8 | - |
| render-native-paranoid-2-cpu-mixed-activations | 0 | 390 | 8 | - |
| render-native-paranoid-default-heap-capture-limit | 0 | 323 | 8 | - |
| render-native-paranoid-default-heap-mixed-group | 0 | 703 | 8 | - |
| render-native-paranoid-default-heap-runner-debug-matching | 0 | 363 | 8 | - |

## Observations

- render-container-cap-perfmon-cpu-activation-limit: {"html_bytes": 449405, "set_bytes": {"activation/correlation.json": 133129, "cpu/samples.json": 340418, "profile.json": 72808}, "wall_ms": 440}
- render-container-cap-perfmon-cpu-capture-limit: {"html_bytes": 661713, "set_bytes": {"cpu/samples.json": 151232, "profile.json": 55834}, "wall_ms": 432}
- render-container-cap-perfmon-cpu-main-matching-activations: {"html_bytes": 631992, "set_bytes": {"activation/correlation.json": 150713, "cpu/samples.json": 345740, "profile.json": 74282}, "wall_ms": 454}
- render-container-cap-perfmon-cpu-mixed-activations: {"html_bytes": 837819, "set_bytes": {"activation/correlation.json": 134967, "cpu/samples.json": 456809, "profile.json": 81091}, "wall_ms": 498}
- render-container-cap-perfmon-heap-capture-limit: {"html_bytes": 67975, "set_bytes": {"heap/allocations.json": 47771, "profile.json": 7556}, "wall_ms": 322}
- render-container-cap-perfmon-heap-mixed-group: {"html_bytes": 175907, "set_bytes": {"heap/allocations.json": 6050532, "profile.json": 21726}, "wall_ms": 1903}
- render-container-cap-perfmon-heap-runner-debug-matching: {"html_bytes": 79712, "set_bytes": {"heap/allocations.json": 175491, "profile.json": 11022}, "wall_ms": 363}
- render-native-paranoid-2-cpu-activation-limit: {"html_bytes": 271075, "set_bytes": {"activation/correlation.json": 115239, "cpu/samples.json": 182163, "profile.json": 64699}, "wall_ms": 380}
- render-native-paranoid-2-cpu-capture-limit: {"html_bytes": 368643, "set_bytes": {"cpu/samples.json": 79582, "profile.json": 55872}, "wall_ms": 364}
- render-native-paranoid-2-cpu-main-matching-activations: {"html_bytes": 455728, "set_bytes": {"activation/correlation.json": 132661, "cpu/samples.json": 181790, "profile.json": 68024}, "wall_ms": 404}
- render-native-paranoid-2-cpu-mixed-activations: {"html_bytes": 475616, "set_bytes": {"activation/correlation.json": 98977, "cpu/samples.json": 179642, "profile.json": 69798}, "wall_ms": 390}
- render-native-paranoid-default-heap-capture-limit: {"html_bytes": 68056, "set_bytes": {"heap/allocations.json": 47596, "profile.json": 7777}, "wall_ms": 323}
- render-native-paranoid-default-heap-mixed-group: {"html_bytes": 155491, "set_bytes": {"heap/allocations.json": 1491491, "profile.json": 15699}, "wall_ms": 703}
- render-native-paranoid-default-heap-runner-debug-matching: {"html_bytes": 81592, "set_bytes": {"heap/allocations.json": 176867, "profile.json": 11316}, "wall_ms": 363}
