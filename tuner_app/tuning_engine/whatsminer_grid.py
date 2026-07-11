from __future__ import annotations


def build_power_limit_axis(engine) -> list[int]:
    """Descending evenly-spaced integer axis from POWER_LIMIT_W..WHATSMINER_PL_MIN_W,
    count=WHATSMINER_PL_COUNT. Reads engine.config["POWER_LIMIT_W"] (shared fleet-only
    upper cap), ["WHATSMINER_PL_MIN_W"], ["WHATSMINER_PL_COUNT"]. Endpoints inclusive.
    Returns list[int]."""
    max_w = engine.config["POWER_LIMIT_W"]
    min_w = engine.config["WHATSMINER_PL_MIN_W"]
    count = engine.config["WHATSMINER_PL_COUNT"]

    if count == 1:
        return [max_w]

    step = (max_w - min_w) / (count - 1)
    return [int(round(max_w - i * step)) for i in range(count)]


def build_freq_axis(engine) -> list[float]:
    """Descending freq axis from FREQ_MAX..FREQ_MIN, count=FREQ_COUNT, snapped to 1 MHz.
    Reads engine.config["WHATSMINER_FREQ_MAX_MHZ"], ["WHATSMINER_FREQ_MIN_MHZ"],
    ["WHATSMINER_FREQ_COUNT"]. Each freq snapped to nearest int MHz via int(round(x)).
    Endpoints inclusive."""
    max_mhz = engine.config["WHATSMINER_FREQ_MAX_MHZ"]
    min_mhz = engine.config["WHATSMINER_FREQ_MIN_MHZ"]
    count = engine.config["WHATSMINER_FREQ_COUNT"]

    if count == 1:
        return [float(max_mhz)]

    step = (max_mhz - min_mhz) / (count - 1)
    return [float(int(round(max_mhz - i * step))) for i in range(count)]


def freq_to_mode_and_percent(
    target_mhz: float, mode_baselines: dict, anchor: str
) -> tuple[str, float]:
    """Pick the supported mode whose baseline is closest to target_mhz; compute
    percent = 100*(target/baseline - 1); clamp percent to [-100, +100]. On
    out-of-range (clamped), try the next-nearest mode and re-compute. Skips
    modes with supported=False. anchor is "current_mode" or "normal_only";
    when "normal_only", percent is computed against the "normal" baseline
    instead of the picked-mode baseline."""
    supported_modes = {name: info for name, info in mode_baselines.items() if info.get("supported")}

    if not supported_modes:
        raise ValueError("No supported modes found")

    sorted_modes = sorted(
        supported_modes.items(), key=lambda x: abs(target_mhz - x[1]["target_freq"])
    )

    for mode_name, mode_info in sorted_modes:
        if anchor == "current_mode":
            baseline_freq = mode_info["target_freq"]
        else:
            baseline_freq = mode_baselines["normal"]["target_freq"]

        percent_raw = 100 * (target_mhz / baseline_freq - 1)
        clamped_percent = max(-100.0, min(100.0, percent_raw))

        if -100.0 <= percent_raw <= 100.0:
            return (mode_name, clamped_percent)

    # If no mode fits in range, return the closest one clamped
    closest_mode_name, closest_mode_info = sorted_modes[0]
    if anchor == "current_mode":
        baseline_freq = closest_mode_info["target_freq"]
    else:
        baseline_freq = mode_baselines["normal"]["target_freq"]

    percent_raw = 100 * (target_mhz / baseline_freq - 1)
    clamped_percent = max(-100.0, min(100.0, percent_raw))
    return (closest_mode_name, clamped_percent)


def mode_and_percent_to_freq(mode: str, percent: float, mode_baselines: dict, anchor: str) -> float:
    """Inverse: returns the freq the firmware would actually run at given mode
    + percent. anchor=="current_mode" -> uses mode_baselines[mode]["target_freq"];
    anchor=="normal_only" -> uses mode_baselines["normal"]["target_freq"]."""
    if anchor == "current_mode":
        baseline_freq = mode_baselines[mode]["target_freq"]
    else:
        baseline_freq = mode_baselines["normal"]["target_freq"]

    return baseline_freq * (1 + percent / 100)
