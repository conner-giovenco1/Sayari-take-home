"""Step 4: the report's chart.

  output/links_by_supplier.png : for every supplier with a direct link, or a two-hop link since 24 Feb 2022 or of
     unknown date, how many US/EU/UK-listed parties it is linked to,
     split into direct links (after the invasion, before it, latest shipment unknown) and two-hop links (after the
     invasion or a current stake; latest shipment unknown). Series with no links are left off.
     Suppliers are ordered by risk level first (high, then medium), then by direct links, so a supplier with many
     weak two-hop links never looks riskier than one with a recent direct link.

Run from take_home_code/:  .venv/bin/python src/figures.py
"""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

RECENT, OLDER, UNDATED, TWO_HOP = "#c0392b", "#f0b27a", "#a569bd", "#d5d8dc"
SERIES = {  # column -> (legend label, color); series with no links at all are left off the chart
    "direct_links_since_invasion": ("Direct link, after 24 Feb 2022", RECENT),
    "direct_links_before_invasion": ("Direct link, before 24 Feb 2022", OLDER),
    "direct_links_undated": ("Direct link, latest shipment unknown", UNDATED),
    "two_hop_links_since_invasion": ("Two hops away, after 24 Feb 2022 or current stake (weaker evidence)", TWO_HOP),
    "two_hop_links_undated": ("Two hops away, latest shipment unknown", "#eceef0"),
}
LEVEL_ORDER = {"high": 0, "medium": 1, "low": 2, "incomplete": 3}
LEVEL_COLOR = {"high": RECENT, "medium": "#b9770e", "low": "#566573", "incomplete": "#566573"}


def links_chart(table):
    """Draw the stacked bar chart of listed parties per supplier and save it to output/links_by_supplier.png."""
    data = table.set_index("supplier")
    cols = [c for c in SERIES if data[c].sum() > 0]
    data = data[data[cols].sum(axis=1) > 0].copy()
    if data.empty:
        print("no supplier has a link to a listed party; no chart drawn")
        return
    data["level"] = data.exposure.str.split().str[0]  # "incomplete - rerun collect.py" -> "incomplete"
    data["level_rank"] = data.level.map(LEVEL_ORDER)
    data["direct"] = data[[c for c in cols if c.startswith("direct")]].sum(axis=1)
    data["total"] = data[cols].sum(axis=1)
    # Top of the chart = riskiest: by level, then recent direct links, then all direct links, then all links, then
    # name (for a stable order).
    data = data.reset_index().sort_values(
        ["level_rank", "direct_links_since_invasion", "direct", "total", "supplier"],
        ascending=[True, False, False, False, True]).set_index("supplier")
    data = data.iloc[::-1]  # barh draws the first row at the bottom
    fig, ax = plt.subplots(figsize=(7, 0.17 * len(data) + 1.2))
    data[cols].plot.barh(stacked=True, ax=ax, color=[SERIES[c][1] for c in cols], width=0.75)
    ax.set_yticks(range(len(data)))
    ax.set_yticklabels([f"{name}  ({level})" for name, level in zip(data.index, data.level)])
    for tick, level in zip(ax.get_yticklabels(), data.level):
        tick.set_color(LEVEL_COLOR[level])
        tick.set_fontweight("bold" if level == "high" else "normal")
    ax.set_xlabel("Linked parties on US / EU / UK sanctions or export-control lists")
    ax.set_ylabel("")
    ax.legend([SERIES[c][0] for c in cols], fontsize=7, loc="lower right")
    ax.set_title(f"{len(data)} of {len(table)} suppliers have a direct link to a US/EU/UK-listed party,\n"
                 "or a two-hop link since 24 Feb 2022 or of unknown date\n"
                 "(ordered by risk level, then by direct links)", fontsize=9)
    ax.tick_params(labelsize=7)
    ax.xaxis.get_major_locator().set_params(integer=True)
    fig.tight_layout()
    fig.savefig("output/links_by_supplier.png", dpi=200)
    plt.close(fig)


def main():
    """Draw the chart from output/supplier_risk.csv."""
    links_chart(pd.read_csv("output/supplier_risk.csv"))


if __name__ == "__main__":
    main()
