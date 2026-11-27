"""Charts of archived PVs, computed and drawn by ARGUS - not re-typed by a model.

A model that draws a chart itself must copy every data point into its answer:
slow (minutes for a few hundred points), lossy (it thins them by hand) and
capped by the chat. Here the model only names the PVs, the range and the kind
of chart; ARGUS fetches the data from the Archiver Appliance, aligns the PVs
on one time grid, does the arithmetic with numpy, and returns:

- the numbers behind the chart (statistics, correlation, fit, spectral peaks)
  for the model to explain, and
- the chart itself as a self-contained HTML page (Plotly) that the chat
  client shows inline - an MCP "ui://" resource. The model never sees the
  page, only a marker, so the data costs it nothing.

Archived samples are stored when the value changes (monitor) or at a fixed
period (scan). Between two samples the PV held the earlier value, so the
alignment holds the last value ("step") rather than interpolating.
"""

from __future__ import annotations

import asyncio
import html
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np

from argus.core.errors import ValidationError
from argus.providers.archiver.archiver_appliance import ArchiverApplianceProvider
from argus.providers.archiver.models import ArchiverSeries

KINDS = ("trend", "difference", "scatter", "histogram", "fft")
MAX_PVS = 6
# Points fetched per PV. The appliance down-samples longer ranges into bins
# (mean/min/max/count); a range with fewer samples comes back raw.
FETCH_POINTS = 2000
# Uniform grid for difference/scatter/fft (FFT needs uniform sampling).
GRID_POINTS = 2048
PLOTLY_JS = "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"


@dataclass
class _Series:
    pv: str
    units: str | None
    binned: bool
    samples_in_range: int
    t: np.ndarray  # epoch seconds
    v: np.ndarray  # mean for bins, value for raw samples
    lo: np.ndarray
    hi: np.ndarray


@dataclass
class PlotResult:
    summary: dict[str, Any]
    html: str
    uri: str


def _to_series(series: ArchiverSeries) -> _Series:
    t, v, lo, hi, total = [], [], [], [], 0
    for s in series.samples:
        if not isinstance(s.value, (int, float)) or isinstance(s.value, bool) or not math.isfinite(s.value):
            continue  # waveforms, disconnected/NaN samples
        t.append(s.timestamp.timestamp())
        v.append(float(s.value))
        lo.append(float(s.min if s.min is not None else s.value))
        hi.append(float(s.max if s.max is not None else s.value))
        total += s.count if s.count is not None else 1
    return _Series(series.pv_name, series.units, series.binned, total,
                   np.array(t), np.array(v), np.array(lo), np.array(hi))


def _num(x: float) -> float | None:
    return float(f"{x:.6g}") if x is not None and math.isfinite(x) else None


def _stats(v: np.ndarray) -> dict[str, Any]:
    if v.size == 0:
        return {"points": 0}
    return {
        "points": int(v.size),
        "min": _num(float(v.min())),
        "max": _num(float(v.max())),
        "mean": _num(float(v.mean())),
        "std": _num(float(v.std())),
        "first": _num(float(v[0])),
        "last": _num(float(v[-1])),
    }


def _hold(series: _Series, grid: np.ndarray) -> np.ndarray:
    """Value of the series at each grid time, holding the last sample; NaN before the first."""
    idx = np.searchsorted(series.t, grid, side="right") - 1
    out = np.full(grid.shape, np.nan)
    ok = idx >= 0
    out[ok] = series.v[idx[ok]]
    return out


def _iso(epoch: np.ndarray) -> list[str]:
    return [datetime.fromtimestamp(x, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") for x in epoch]


def _label(s: _Series) -> str:
    return f"{s.pv} [{s.units}]" if s.units else s.pv


class PlotService:
    def __init__(self, archiver: ArchiverApplianceProvider) -> None:
        self._archiver = archiver

    async def plot(
        self,
        pvs: list[str],
        kind: str,
        start: datetime,
        end: datetime,
        *,
        bins: int = 50,
        title: str | None = None,
    ) -> PlotResult:
        if kind not in KINDS:
            raise ValidationError(f"kind must be one of {', '.join(KINDS)}.")
        if not pvs or len(pvs) > MAX_PVS or not all(isinstance(p, str) and p.strip() for p in pvs):
            raise ValidationError(f"pvs must be a list of 1 to {MAX_PVS} PV names.")
        if kind in ("difference", "scatter") and len(pvs) != 2:
            raise ValidationError(f"kind '{kind}' needs exactly 2 PVs (A and B).")
        if start >= end:
            raise ValidationError("start must be before end.")

        fetched = await asyncio.gather(
            *(self._archiver.get_data(pv, start, end, max_points=FETCH_POINTS) for pv in pvs)
        )
        series = [_to_series(s) for s in fetched]
        empty = [s.pv for s in series if s.t.size == 0]
        if empty:
            raise ValidationError(
                f"No archived numeric samples for {', '.join(empty)} in this range: not archived, or no data."
            )

        builder = getattr(self, f"_{kind}")
        traces, layout, numbers = builder(series, start, end, bins)
        span = (end - start).total_seconds()
        summary: dict[str, Any] = {
            "kind": kind,
            "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pvs": [
                {"pv": s.pv, "units": s.units, "samples_in_range": s.samples_in_range,
                 "points_used": int(s.t.size), "binned": s.binned}
                for s in series
            ],
            **numbers,
        }
        if any(s.binned for s in series) and kind in ("histogram", "fft", "scatter", "difference"):
            summary["note"] = (
                "Long range: the archiver returned time-bin means, so the analysis sees averaged data "
                f"(about {span / FETCH_POINTS:.0f} s per bin). Narrow the range for raw samples."
            )
        heading = title or f"{kind}: {', '.join(pvs)}"
        uri = f"ui://argus/plot/{uuid.uuid4().hex}"
        return PlotResult(summary=summary, html=_page(heading, traces, layout), uri=uri)

    # --- one builder per kind: (traces, layout, numbers) ---------------------

    def _trend(self, series: list[_Series], start, end, bins):
        traces, numbers = [], {"stats": {}}
        units = list(dict.fromkeys(s.units for s in series))
        for s in series:
            axis = "y2" if len(units) > 1 and s.units != units[0] else "y"
            x = _iso(s.t)
            if s.binned:
                traces.append({"x": x + x[::-1], "y": list(s.hi) + list(s.lo[::-1]), "fill": "toself",
                               "line": {"width": 0}, "opacity": 0.25, "hoverinfo": "skip",
                               "showlegend": False, "yaxis": axis, "type": "scatter", "legendgroup": s.pv})
            traces.append({"x": x, "y": list(s.v), "name": _label(s), "yaxis": axis, "type": "scatter",
                           "mode": "lines", "line": {"shape": "hv", "width": 1.5}, "legendgroup": s.pv})
            numbers["stats"][s.pv] = _stats(s.v) | (
                {"min": _num(float(s.lo.min())), "max": _num(float(s.hi.max()))} if s.binned else {}
            )
        layout = {"xaxis": {"type": "date", "title": "time (UTC)"}, "yaxis": {"title": units[0] or ""}}
        if len(units) > 1:
            layout["yaxis2"] = {"title": " / ".join(u or "" for u in units[1:]), "overlaying": "y",
                                "side": "right"}
        return traces, layout, numbers

    def _aligned(self, series: list[_Series], start: datetime, end: datetime):
        lo = max(start.timestamp(), max(s.t[0] for s in series))
        if lo >= end.timestamp():
            raise ValidationError("The PVs have no overlapping archived data in this range.")
        grid = np.linspace(lo, end.timestamp(), GRID_POINTS)
        values = [_hold(s, grid) for s in series]
        ok = np.all([np.isfinite(v) for v in values], axis=0)
        return grid[ok], [v[ok] for v in values]

    def _difference(self, series, start, end, bins):
        grid, (a, b) = self._aligned(series, start, end)
        d = a - b
        name = f"{series[0].pv} - {series[1].pv}"
        traces = [{"x": _iso(grid), "y": list(d), "name": name, "type": "scatter", "mode": "lines",
                   "line": {"shape": "hv", "width": 1.5}}]
        layout = {"xaxis": {"type": "date", "title": "time (UTC)"},
                  "yaxis": {"title": series[0].units or "A - B"}, "showlegend": True}
        stats = _stats(d)
        stats["max_abs"] = _num(float(np.abs(d).max())) if d.size else None
        return traces, layout, {"difference": stats}

    def _scatter(self, series, start, end, bins):
        grid, (a, b) = self._aligned(series, start, end)
        numbers: dict[str, Any] = {"aligned_points": int(a.size)}
        traces = [{"x": list(a), "y": list(b), "mode": "markers", "type": "scatter", "name": "samples",
                   "marker": {"size": 5, "color": list(grid - grid[0]) if grid.size else [],
                              "colorscale": "Viridis", "showscale": True,
                              "colorbar": {"title": "s from start"}},
                   "text": _iso(grid), "hovertemplate": "%{text}<br>x=%{x}<br>y=%{y}<extra></extra>"}]
        if a.size >= 3 and np.ptp(a) > 0 and np.ptp(b) > 0:
            r = float(np.corrcoef(a, b)[0, 1])
            slope, intercept = np.polyfit(a, b, 1)
            numbers.update(pearson_r=_num(r), r_squared=_num(r * r),
                           fit={"slope": _num(float(slope)), "intercept": _num(float(intercept))})
            xs = np.array([a.min(), a.max()])
            traces.append({"x": list(xs), "y": list(slope * xs + intercept), "mode": "lines", "type": "scatter",
                           "name": f"fit (r={r:.3f})", "line": {"dash": "dash"}})
        else:
            numbers["pearson_r"] = None
            numbers["note_correlation"] = "One of the PVs is constant in this range: correlation is undefined."
        layout = {"xaxis": {"title": _label(series[0])}, "yaxis": {"title": _label(series[1])}}
        return traces, layout, numbers

    def _histogram(self, series, start, end, bins):
        traces, numbers = [], {"stats": {}}
        for s in series:
            traces.append({"x": list(s.v), "type": "histogram", "name": _label(s), "nbinsx": bins,
                           "opacity": 0.6 if len(series) > 1 else 0.9})
            st = _stats(s.v)
            if s.v.size:
                st["p05"], st["p50"], st["p95"] = (_num(float(x)) for x in np.percentile(s.v, [5, 50, 95]))
            numbers["stats"][s.pv] = st
        units = {s.units for s in series}
        layout = {"barmode": "overlay", "xaxis": {"title": units.pop() if len(units) == 1 else "value"},
                  "yaxis": {"title": "count"}}
        return traces, layout, numbers

    def _fft(self, series, start, end, bins):
        traces, numbers = [], {"spectra": {}}
        for s in series:
            grid = np.linspace(max(start.timestamp(), s.t[0]), end.timestamp(), GRID_POINTS)
            v = _hold(s, grid)
            v = v[np.isfinite(v)]
            dt = (grid[-1] - grid[0]) / (GRID_POINTS - 1)
            if v.size < 16 or dt <= 0:
                numbers["spectra"][s.pv] = {"note": "too few samples for a spectrum"}
                continue
            # Remove the linear drift: otherwise the slowest bins (the window itself) dominate.
            x = np.arange(v.size)
            v = v - np.polyval(np.polyfit(x, v, 1), x)
            amp = np.abs(np.fft.rfft(v * np.hanning(v.size))) * 4 / v.size  # x2 one-sided, x2 Hann gain
            freq = np.fft.rfftfreq(v.size, dt)
            # Peaks: local maxima only (one per line, not its neighbouring bins), and periods
            # shorter than half the window - a slower "period" is not resolved.
            window = v.size * dt
            inner = np.arange(1, amp.size - 1)
            is_peak = (amp[inner] > amp[inner - 1]) & (amp[inner] >= amp[inner + 1]) & (freq[inner] >= 2 / window)
            candidates = inner[is_peak]
            top = candidates[np.argsort(amp[candidates])[::-1][:3]]
            amp, freq = amp[1:], freq[1:]  # drop DC from the plotted spectrum
            top = top - 1
            floor = float(np.median(amp)) or 1e-300
            peaks = [{"freq_hz": _num(float(freq[i])), "period_s": _num(float(1 / freq[i])),
                      "amplitude": _num(float(amp[i])), "x_median": _num(float(amp[i]) / floor)}
                     for i in sorted(top, key=lambda i: -amp[i])]
            strongest = peaks[0]["x_median"] if peaks else 0
            numbers["spectra"][s.pv] = {
                "sample_period_s": _num(dt),
                "nyquist_hz": _num(1 / (2 * dt)),
                "resolution_hz": _num(1 / (v.size * dt)),
                "peaks": peaks,
                # x_median: peak height over the spectrum's median level. A real periodicity
                # stands well above it; noise peaks are a few times the median at most.
                "verdict": "dominant periodicity" if strongest >= 10
                else "no dominant periodicity (noise-like spectrum)",
            }
            traces.append({"x": list(freq), "y": list(amp), "type": "scatter", "mode": "lines",
                           "name": _label(s)})
        layout = {"xaxis": {"title": "frequency (Hz)", "type": "log"}, "yaxis": {"title": "amplitude"}}
        numbers["note_fft"] = (
            "Spectrum of the archived signal resampled on a uniform grid (last value held between samples). "
            "The archiver's own sampling limits it: nothing above the Nyquist frequency is real."
        )
        return traces, layout, numbers


def _clean(value: Any) -> Any:
    """Floats to 6 significant digits, NaN/Inf to null: smaller page, valid JSON."""
    if isinstance(value, float):
        return _num(value)
    if isinstance(value, np.floating):
        return _num(float(value))
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _page(title: str, traces: list[dict], layout: dict) -> str:
    layout = {
        "title": {"text": title, "font": {"size": 14}},
        "margin": {"l": 70, "r": 70, "t": 50, "b": 60},
        "legend": {"orientation": "h", "y": -0.22},
        "hovermode": "closest",
        **layout,
    }
    data = json.dumps(_clean(traces), separators=(",", ":"), allow_nan=False)
    lay = json.dumps(_clean(layout), separators=(",", ":"), allow_nan=False)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<script src="{PLOTLY_JS}"></script>
<style>html,body{{margin:0;background:#fff;font-family:system-ui,sans-serif}}#p{{width:100%;height:460px}}
#err{{padding:12px;color:#a00;display:none}}</style></head>
<body><div id="p"></div><div id="err">The chart library could not load (no internet access from this browser?).</div>
<script>
function size(){{try{{parent.postMessage({{type:"ui-size-change",payload:{{height:document.body.scrollHeight,width:document.body.scrollWidth}}}},"*")}}catch(e){{}}}}
if(window.Plotly){{Plotly.newPlot("p",{data},{lay},{{responsive:true,displaylogo:false}}).then(size);}}
else{{document.getElementById("p").style.display="none";document.getElementById("err").style.display="block";}}
size();window.addEventListener("resize",size);
</script></body></html>"""
