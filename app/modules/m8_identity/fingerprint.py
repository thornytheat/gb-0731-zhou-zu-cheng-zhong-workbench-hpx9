from app.config import FINGERPRINT_TOLERANCE_M


def fingerprint_of(axes):
    """车辙指纹：确认车轴数加相邻轴距序列。少于两根确认车轴或轴距有缺口时不可测量。"""
    if len(axes) < 2:
        return None
    gaps = [axis["gap_m"] for axis in axes[1:]]
    if any(gap is None for gap in gaps):
        return None
    return (len(axes), tuple(gaps))


def fingerprint_match(left, right, tolerance=FINGERPRINT_TOLERANCE_M):
    if left is None or right is None or left[0] != right[0] or len(left[1]) != len(right[1]):
        return False
    return all(abs(a - b) <= tolerance for a, b in zip(left[1], right[1]))


def describe(fingerprint):
    if fingerprint is None:
        return "不可测"
    gaps = "、".join(str(gap) for gap in fingerprint[1])
    return f"{fingerprint[0]} 轴 · 轴距 {gaps} m"
