"""Signal sources for points without a model: constant, ramp, sine and noise, added up."""
from __future__ import annotations

import math
import random

from .profile import Source


class Signal:
    def __init__(self, sources: list[Source], start: float, seed: int | None = None):
        self.sources = sources
        self.start = start
        self._rng = random.Random(seed)

    def value(self, t: float) -> float:
        elapsed = t - self.start
        total = 0.0
        for src in self.sources:
            p = src.params
            if src.type == "constant":
                total += p["value"]
            elif src.type == "ramp":
                frac = (elapsed % p["period"]) / p["period"]
                total += p["start"] + (p["stop"] - p["start"]) * frac
            elif src.type == "sine":
                total += p["offset"] + p["amplitude"] * math.sin(
                    2 * math.pi * elapsed / p["period"])
            elif src.type == "noise":
                total += self._rng.gauss(0.0, p["sigma"])
        return total
