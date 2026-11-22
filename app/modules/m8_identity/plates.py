import re
import unicodedata

# 车牌识别常见的分隔符与空白，规范化时一律剔除。
SEPARATORS = frozenset("·•●-‐‑‒–—―_ ")
PLATE_PATTERN = re.compile(r"[一-鿿][A-Z][A-Z0-9]{5,6}")


def normalize_plate(raw):
    cleaned = unicodedata.normalize("NFKC", raw).upper()
    return "".join(char for char in cleaned if char not in SEPARATORS and not char.isspace())


def wellformed(plate):
    return bool(PLATE_PATTERN.fullmatch(plate))


def plate_distance(left, right):
    """车牌编辑距离，覆盖换字、漏字、多字和相邻换位，超过 2 一律记 2。"""
    if left == right:
        return 0
    rows, cols = len(left), len(right)
    if abs(rows - cols) >= 2:
        return 2
    before2 = None
    before1 = list(range(cols + 1))
    for i in range(1, rows + 1):
        current = [i] + [0] * cols
        for j in range(1, cols + 1):
            cost = 0 if left[i - 1] == right[j - 1] else 1
            current[j] = min(before1[j] + 1, current[j - 1] + 1, before1[j - 1] + cost)
            if i > 1 and j > 1 and left[i - 1] == right[j - 2] and left[i - 2] == right[j - 1]:
                current[j] = min(current[j], before2[j - 2] + 1)
        before2, before1 = before1, current
    return min(before1[cols], 2)
