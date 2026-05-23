from __future__ import annotations

from math import ceil, log10


def required_votes(n: int) -> int:
    if n <= 0:
        return 0

    if n < 100:
        error_margin = 0.10 - (0.03 * (n - 1) / 99)
    elif n < 500:
        error_margin = 0.07 - (0.02 * (n - 100) / 400)
    else:
        error_margin = max(0.02, 0.05 - 0.03 * log10(n / 500) / log10(2000))

    base_sample_size = 0.9604 / (error_margin ** 2)
    cochran = ceil(base_sample_size / (1 + (base_sample_size - 1) / n))
    return min(ceil(0.75 * n), cochran)
