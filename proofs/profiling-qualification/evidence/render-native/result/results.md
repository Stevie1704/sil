# Profiling qualification: offline-render

perf: none; failed checks: 0

| Scenario | Exit | Wall ms | Checks | Failed |
| --- | --- | --- | --- | --- |
| render-container-cap-perfmon-cpu-activation-limit | 0 | 179 | 8 | - |
| render-container-cap-perfmon-cpu-capture-limit | 0 | 180 | 8 | - |
| render-container-cap-perfmon-cpu-main-matching-activations | 0 | 195 | 8 | - |
| render-container-cap-perfmon-cpu-mixed-activations | 0 | 226 | 8 | - |
| render-container-cap-perfmon-heap-capture-limit | 0 | 85 | 8 | - |
| render-container-cap-perfmon-heap-mixed-group | 0 | 1348 | 8 | - |
| render-container-cap-perfmon-heap-runner-debug-matching | 0 | 104 | 8 | - |
| render-native-paranoid-2-cpu-activation-limit | 0 | 137 | 8 | - |
| render-native-paranoid-2-cpu-capture-limit | 0 | 124 | 8 | - |
| render-native-paranoid-2-cpu-main-matching-activations | 0 | 152 | 8 | - |
| render-native-paranoid-2-cpu-mixed-activations | 0 | 147 | 8 | - |
| render-native-paranoid-default-heap-capture-limit | 0 | 87 | 8 | - |
| render-native-paranoid-default-heap-mixed-group | 0 | 400 | 8 | - |
| render-native-paranoid-default-heap-runner-debug-matching | 0 | 105 | 8 | - |

## Observations

- render-container-cap-perfmon-cpu-activation-limit: {"html_bytes": 449405, "set_bytes": {"activation/correlation.json": 133129, "cpu/samples.json": 340418, "profile.json": 72808}, "wall_ms": 179}
- render-container-cap-perfmon-cpu-capture-limit: {"html_bytes": 661713, "set_bytes": {"cpu/samples.json": 151232, "profile.json": 55834}, "wall_ms": 180}
- render-container-cap-perfmon-cpu-main-matching-activations: {"html_bytes": 631992, "set_bytes": {"activation/correlation.json": 150713, "cpu/samples.json": 345740, "profile.json": 74282}, "wall_ms": 195}
- render-container-cap-perfmon-cpu-mixed-activations: {"html_bytes": 837819, "set_bytes": {"activation/correlation.json": 134967, "cpu/samples.json": 456809, "profile.json": 81091}, "wall_ms": 226}
- render-container-cap-perfmon-heap-capture-limit: {"html_bytes": 67975, "set_bytes": {"heap/allocations.json": 47771, "profile.json": 7556}, "wall_ms": 85}
- render-container-cap-perfmon-heap-mixed-group: {"html_bytes": 175907, "set_bytes": {"heap/allocations.json": 6050532, "profile.json": 21726}, "wall_ms": 1348}
- render-container-cap-perfmon-heap-runner-debug-matching: {"html_bytes": 79712, "set_bytes": {"heap/allocations.json": 175491, "profile.json": 11022}, "wall_ms": 104}
- render-native-paranoid-2-cpu-activation-limit: {"html_bytes": 271075, "set_bytes": {"activation/correlation.json": 115239, "cpu/samples.json": 182163, "profile.json": 64699}, "wall_ms": 137}
- render-native-paranoid-2-cpu-capture-limit: {"html_bytes": 368643, "set_bytes": {"cpu/samples.json": 79582, "profile.json": 55872}, "wall_ms": 124}
- render-native-paranoid-2-cpu-main-matching-activations: {"html_bytes": 455728, "set_bytes": {"activation/correlation.json": 132661, "cpu/samples.json": 181790, "profile.json": 68024}, "wall_ms": 152}
- render-native-paranoid-2-cpu-mixed-activations: {"html_bytes": 475616, "set_bytes": {"activation/correlation.json": 98977, "cpu/samples.json": 179642, "profile.json": 69798}, "wall_ms": 147}
- render-native-paranoid-default-heap-capture-limit: {"html_bytes": 68056, "set_bytes": {"heap/allocations.json": 47596, "profile.json": 7777}, "wall_ms": 87}
- render-native-paranoid-default-heap-mixed-group: {"html_bytes": 155491, "set_bytes": {"heap/allocations.json": 1491491, "profile.json": 15699}, "wall_ms": 400}
- render-native-paranoid-default-heap-runner-debug-matching: {"html_bytes": 81592, "set_bytes": {"heap/allocations.json": 176867, "profile.json": 11316}, "wall_ms": 105}
