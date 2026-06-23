"""Dashboard HTML for the MCP Apps UI resource.

Data is injected server-side and the weight graph is hand-drawn inline SVG — no
external styles or fonts. When embedded as an MCP Apps resource, the only added
dependency is the app-bridge script (so the host completes the handshake and
sizes the iframe); the plain web page omits even that.
"""

from html import escape

from .models import FoodLog, Progress, WeightEntry

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 20px;
  font: 15px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
  background: #0f1115; color: #e6e8eb;
}
h1 { font-size: 18px; margin: 0 0 16px; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .05em;
     color: #9aa3ad; margin: 24px 0 8px; }
.cards { display: flex; gap: 12px; flex-wrap: wrap; }
.card { background: #181b21; border: 1px solid #262a31; border-radius: 12px;
        padding: 14px 16px; flex: 1 1 180px; }
.metric { font-size: 26px; font-weight: 600; }
.metric small { font-size: 13px; font-weight: 400; color: #9aa3ad; }
.bar { height: 8px; background: #262a31; border-radius: 999px; margin-top: 10px; overflow: hidden; }
.bar > span { display: block; height: 100%; border-radius: 999px; background: #4f9dff; }
.bar.met > span { background: #3fb950; }
.goal { color: #9aa3ad; font-size: 12px; margin-top: 6px; }
svg { width: 100%; height: auto; display: block; }
.chart { background: #181b21; border: 1px solid #262a31; border-radius: 12px; padding: 12px; }
ul.meals { list-style: none; margin: 0; padding: 0; }
ul.meals li { display: flex; justify-content: space-between; gap: 12px;
  padding: 8px 0; border-bottom: 1px solid #20242b; }
ul.meals li:last-child { border-bottom: 0; }
.meal-name { color: #e6e8eb; }
.meal-meta { color: #9aa3ad; font-size: 13px; white-space: nowrap; }
.empty { color: #6b7280; font-style: italic; }
"""


def _bar(value: float, target: float, *, met_is_good_above: bool) -> str:
    pct = 0.0 if target <= 0 else min(100.0, value / target * 100.0)
    met = value >= target if met_is_good_above else value <= target
    cls = "bar met" if met else "bar"
    return f'<div class="{cls}"><span style="width:{pct:.0f}%"></span></div>'


def _card(
    value: float, target: int, unit: str, remaining: float, goal_word: str, *, above: bool
) -> str:
    return (
        f'<div class="card">'
        f'<div class="metric">{value:.0f} <small>/ {target} {unit}</small></div>'
        f"{_bar(value, target, met_is_good_above=above)}"
        f'<div class="goal">{goal_word} {target} {unit} · {remaining:+.0f} to go</div>'
        f"</div>"
    )


def _weight_svg(weights: list[WeightEntry]) -> str:
    if len(weights) < 2:
        return '<p class="empty">Not enough weight entries yet to plot a trend.</p>'

    w, h, pad = 600.0, 200.0, 28.0
    kgs = [e.weight_kg for e in weights]
    lo, hi = min(kgs), max(kgs)
    if hi == lo:
        hi += 0.5
        lo -= 0.5
    n = len(weights)

    def x(i: int) -> float:
        return pad + (w - 2 * pad) * (i / (n - 1))

    def y(kg: float) -> float:
        return pad + (h - 2 * pad) * (1 - (kg - lo) / (hi - lo))

    points = " ".join(f"{x(i):.1f},{y(e.weight_kg):.1f}" for i, e in enumerate(weights))
    dots = "".join(
        f'<circle cx="{x(i):.1f}" cy="{y(e.weight_kg):.1f}" r="2.5" fill="#4f9dff"/>'
        for i, e in enumerate(weights)
    )
    return (
        f'<div class="chart"><svg viewBox="0 0 {w:.0f} {h:.0f}" '
        f'preserveAspectRatio="none" role="img" aria-label="Weight over time">'
        f'<text x="{pad:.0f}" y="16" fill="#9aa3ad" font-size="11">{hi:.1f} kg</text>'
        f'<text x="{pad:.0f}" y="{h - 8:.0f}" fill="#9aa3ad" font-size="11">{lo:.1f} kg</text>'
        f'<polyline fill="none" stroke="#4f9dff" stroke-width="2" '
        f'stroke-linejoin="round" points="{points}"/>{dots}</svg></div>'
    )


def _meals_html(logs: list[FoodLog]) -> str:
    if not logs:
        return '<p class="empty">Nothing logged yet.</p>'
    items = []
    for log in logs:
        when = log.eaten_at.strftime("%a %H:%M")
        qty = f"{log.quantity_g:.0f} g · " if log.quantity_g else ""
        items.append(
            f'<li><span class="meal-name">{escape(log.name)}</span>'
            f'<span class="meal-meta">{qty}{log.kcal:.0f} kcal · '
            f"{log.protein_g:.0f} g protein · {when}</span></li>"
        )
    return f'<ul class="meals">{"".join(items)}</ul>'


# The MCP Apps bridge: connecting completes the ui/initialize handshake and turns
# on the ResizeObserver that reports content height to the host — without it the
# host renders the iframe at zero height (it "blinks" then vanishes). Loaded from
# the self-contained build so no bundler/import-map is needed; the resource's
# _meta.ui.csp must allow this origin. Harmless when opened as a plain web page
# (connect() just never resolves; the server-rendered content is already visible).
APP_BRIDGE_ORIGIN = "https://unpkg.com"
_APP_BRIDGE_URL = (
    f"{APP_BRIDGE_ORIGIN}/@modelcontextprotocol/ext-apps@1.7.4/dist/src/app-with-deps.js"
)
_APP_BRIDGE = f"""
<script type="module">
  import {{ App }} from "{_APP_BRIDGE_URL}";
  try {{ await new App({{ name: "weight-mcp dashboard", version: "1.0.0" }}).connect(); }}
  catch (err) {{ /* not inside an MCP host — content is already rendered */ }}
</script>"""


def render_dashboard(
    weights: list[WeightEntry],
    logs: list[FoodLog],
    progress: Progress,
    *,
    embed_app_bridge: bool = False,
) -> str:
    goal_word = "min" if progress.goal_mode == "floor" else "max"
    above = progress.goal_mode == "floor"
    bridge = _APP_BRIDGE if embed_app_bridge else ""
    kcal_card = _card(
        progress.kcal, progress.kcal_target, "kcal", progress.kcal_remaining, goal_word, above=above
    )
    protein_card = _card(
        progress.protein_g,
        progress.protein_target_g,
        "g protein",
        progress.protein_remaining_g,
        goal_word,
        above=above,
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>weight-mcp dashboard</title><style>{_CSS}</style></head>
<body>
<h1>Today — {progress.day.isoformat()}</h1>
<div class="cards">{kcal_card}{protein_card}</div>
<h2>Weight</h2>
{_weight_svg(weights)}
<h2>Recently eaten</h2>
{_meals_html(logs)}
{bridge}
</body></html>"""
