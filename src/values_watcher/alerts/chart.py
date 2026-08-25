"""Renderiza gráficos de FVG: 3 velas iniciales y mitigación."""

from __future__ import annotations

import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from values_watcher.core.fvg import Candle, Fvg


def render_fvg_chart(fvg: Fvg, candles: list[Candle]) -> bytes:
    """3 candlesticks + rectángulo del gap. Devuelve PNG como bytes."""
    if len(candles) != 3:
        raise ValueError("Se esperan exactamente 3 velas")

    c1, c2, c3 = candles
    fig, ax = plt.subplots(figsize=(4, 3), dpi=120)
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#1a1a2e")

    color_up = "#26a69a"
    color_dn = "#ef5350"
    color_gap = "#ffd54f"

    all_highs = [c.high for c in candles]
    all_lows = [c.low for c in candles]
    y_min = min(all_lows)
    y_max = max(all_highs)
    y_range = y_max - y_min
    y_min -= y_range * 0.15
    y_max += y_range * 0.15

    for i, c in enumerate(candles):
        body_color = color_up if c.close >= c.open else color_dn
        ax.plot([i, i], [c.low, c.high], color=body_color, linewidth=1.2, zorder=2)
        body_bottom = min(c.open, c.close)
        body_height = abs(c.close - c.open)
        if body_height < y_range * 0.005:
            body_height = y_range * 0.005
        rect = Rectangle(
            (i - 0.28, body_bottom), 0.56, body_height,
            facecolor=body_color, edgecolor=body_color, zorder=3
        )
        ax.add_patch(rect)

    gap_rect = Rectangle(
        (-0.45, fvg.bottom), 2.9, fvg.top - fvg.bottom,
        facecolor=color_gap, alpha=0.35, edgecolor=color_gap,
        linewidth=1, linestyle="--", zorder=1
    )
    ax.add_patch(gap_rect)

    direction_es = "ALCISTA" if fvg.direction.value == "bullish" else "BAJISTA"
    ax.set_title(
        f"{fvg.symbol} {fvg.timeframe} — FVG {direction_es}\n"
        f"{fvg.bottom:,.1f} – {fvg.top:,.1f}",
        color="white", fontsize=9, pad=8
    )
    ax.set_xlim(-0.6, 2.6)
    ax.set_ylim(y_min, y_max)
    ax.set_xticks([0, 1, 2])
    ax.set_xticklabels(["Vela 1", "Vela 2", "Vela 3"], color="#888", fontsize=7)
    ax.tick_params(axis="y", colors="#888", labelsize=7)
    for spine in ax.spines.values():
        spine.set_color("#333")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def render_fvg_mitigated(fvg: Fvg, candles_3: list[Candle], mitigating_candle: Candle) -> bytes:
    """3 velas originales + vela que mitigó. Gap sombreado y flecha a la vela de relleno."""
    candles = list(candles_3) + [mitigating_candle]
    fig, ax = plt.subplots(figsize=(5, 3), dpi=120)
    fig.patch.set_facecolor("#1a1a2e")
    ax.set_facecolor("#1a1a2e")

    color_up = "#26a69a"
    color_dn = "#ef5350"
    color_gap = "#ffd54f"
    color_mit = "#ff9800"  # naranja para la vela que rellena

    all_highs = [c.high for c in candles]
    all_lows = [c.low for c in candles]
    y_min = min(all_lows)
    y_max = max(all_highs)
    y_range = y_max - y_min
    y_min -= y_range * 0.15
    y_max += y_range * 0.15

    for i, c in enumerate(candles):
        is_mitigating = (i == 3)
        body_color = color_mit if is_mitigating else (color_up if c.close >= c.open else color_dn)
        edge_color = "#ff5722" if is_mitigating else body_color
        linewidth = 2.5 if is_mitigating else 1.2

        ax.plot([i, i], [c.low, c.high], color=body_color, linewidth=linewidth, zorder=2)
        body_bottom = min(c.open, c.close)
        body_height = abs(c.close - c.open)
        if body_height < y_range * 0.005:
            body_height = y_range * 0.005
        rect = Rectangle(
            (i - 0.28, body_bottom), 0.56, body_height,
            facecolor=body_color, edgecolor=edge_color, linewidth=2 if is_mitigating else 1, zorder=3
        )
        ax.add_patch(rect)

    # Gap: solo sobre las 3 primeras velas, no sobre la mitigadora
    gap_rect = Rectangle(
        (-0.45, fvg.bottom), 2.9, fvg.top - fvg.bottom,
        facecolor=color_gap, alpha=0.25, edgecolor=color_gap,
        linewidth=1, linestyle="--", zorder=1
    )
    ax.add_patch(gap_rect)

    # Flecha desde la vela mitigadora hacia el gap
    mit = mitigating_candle
    arrow_y = (mit.high + mit.low) / 2
    ax.annotate(
        "", xy=(2.5, (fvg.top + fvg.bottom) / 2), xytext=(3.5, arrow_y),
        arrowprops=dict(arrowstyle="->", color="#ff9800", lw=2)
    )

    direction_es = "ALCISTA" if fvg.direction.value == "bullish" else "BAJISTA"
    ax.set_title(
        f"{fvg.symbol} {fvg.timeframe} — FVG {direction_es} MITIGADO\n"
        f"{fvg.bottom:,.1f} – {fvg.top:,.1f} rellenado",
        color="white", fontsize=9, pad=8
    )
    ax.set_xlim(-0.6, 3.6)
    ax.set_ylim(y_min, y_max)
    ax.set_xticks([0, 1, 2, 3])
    ax.set_xticklabels(["Vela 1", "Vela 2", "Vela 3", "Rellena"], color="#888", fontsize=7)
    ax.tick_params(axis="y", colors="#888", labelsize=7)
    for spine in ax.spines.values():
        spine.set_color("#333")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()
