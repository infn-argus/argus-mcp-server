from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ArchiverSample:
    """One archived sample - or, for a down-sampled series, one time bin.

    A bin's ``value`` is the mean of the samples in it; ``min``/``max``/``count``
    say what the mean hides (a spike, an empty stretch). Raw samples leave them
    None.
    """

    timestamp: datetime
    value: float
    severity: int | None = None
    min: float | None = None
    max: float | None = None
    count: int | None = None


@dataclass
class ArchiverSeries:
    pv_name: str
    samples: list[ArchiverSample] = field(default_factory=list)
    units: str | None = None
    binned: bool = False
