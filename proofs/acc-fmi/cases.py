"""Authored inputs, communication grid and tolerances, shared by both drivers."""
STEP_NS = 100_000_000
STEPS = 10
# Fixed before execution; SI absolute tolerance plus a small relative allowance.
ABS_TOL = 1e-10
REL_TOL = 1e-12
INPUTS = {
    "AccController": ["gap_m", "relative_speed_mps", "ego_speed_mps"],
    "AccPlant": ["accel_mps2"],
}
OUTPUTS = {
    "AccController": ["accel_mps2"],
    "AccPlant": ["ego_position_m", "ego_speed_mps", "lead_position_m",
                 "lead_speed_mps", "gap_m", "relative_speed_mps"],
}
# Changes at communication points; absent updates deliberately exercise hold.
CASES = {
    "controller": {"model": "AccController", "instances": [
        {"start": [60.0, 0.0, 25.0], "updates": {
            "0": [60.0, 0.0, 25.0], "2": [10.0, -5.0, 25.0],
            "4": [43.5, 0.0, 25.0], "6": [42.5, 0.0, 25.0],
            "8": [42.5, 1.0, 25.0]}},
        {"start": [42.5, -1.0, 25.0], "updates": {
            "0": [42.5, -1.0, 25.0], "3": [100.0, 0.0, 25.0],
            "7": [0.0, 0.0, 25.0]}},
    ]},
    "plant-accelerate": {"model": "AccPlant", "instances": [
        {"start": [1.5], "updates": {"0": [1.5]}},
        {"start": [-3.0], "updates": {"0": [-3.0]}},
    ]},
    "plant-coast": {"model": "AccPlant", "instances": [
        {"start": [0.0], "updates": {"0": [0.0]}},
        {"start": [0.5], "updates": {"0": [0.5]}},
    ]},
}
