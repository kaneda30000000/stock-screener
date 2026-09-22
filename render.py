"""結果を1枚のHTMLページに書き出す部分。スマホで見ることを前提にしています。"""

import datetime as dt
import html
import json
import math

import numpy as np

JST = dt.timezone(dt.timedelta(hours=9))


def _f(v, digits=1, suffix="", dash="—"):
    if v is None:
        return dash
    try:
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return dash
        return f"{float(v):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return dash


def _sparkline(values, w=132, h=34):
    vals = [v for v in (values or []) if v is not None and not math.isnan(v)]
    if len(vals) < 3:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    step = (w - 4) / (len(vals) - 1)
    pts = " ".join(
        f"{2 + i * step:.1f},{h - 3 - (v - lo) / rng * (h - 6):.1f}"
        for i, v in enumerate(vals)
    )
    last_x = 2 + (len(vals) - 1) * step
    last_y = h - 3 - (vals[-1] - lo) / rng * (h - 6)
    return (
        f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        f'aria-hidden="true"><polyline points="{pts}" fill="none" '
        f'stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" '
        f'stroke-linecap="round"/>'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.6" fill="currentColor"/></svg>'
    )


def _card(row, rank):
    code = html.escape(str(row.name))
    name = html.escape(str(row.get("name") or ""))
    sector = html.escape(str(row.get("sector") or ""))
    score = int(row.get("score") or 0)
    smax = int(row.get("score_max") or 100)
    pct = max(4, min(100, round(score / smax * 100)))

    chips = "".join(
        f'<span class="chip">{html.escape(str(d["label"]))}'
        f'<b>+{int(d["points"])}</b></span>'
        for d in (row.get("score_detail") or []) if d.get("label")
    )

    def cell(label, value):
        return f'<div class="cell"><span>{label}</span><b>{value}</b></div>'

    metrics = "".join([
        cell("株価", _f(row.get("price"), 0, "円")),
        cell("PER", _f(row.get("per"), 1, "倍")),
        cell("PBR", _f(row.get("pbr"), 2, "倍")),
        cell("配当利回り", _f(row.get("div_yield"), 2, "%")),
        cell("配当性向", _f(row.get("payout"), 0, "%")),
        cell("自己資本比率", _f(row.get("equity_ratio"), 0, "%")),
        cell("営業利益率", _f(row.get("op_margin"), 1, "%")),
        cell("高値から", "−" + _f(row.get("drawdown"), 1, "%")),
        cell("レンジ日数", _f(row.get("range_days"), 0, "日")),
    ])

    streak = int(row.get("growth_streak") or 0)
    cagr = _f(row.get("profit_cagr"), 1, "%")
    lo, hi = _f(row.get("range_low"), 0), _f(row.get("range_high"), 0)

    return f"""
<article class="card">
  <header>
    <div class="ident">
      <span class="rank">{rank}</span>
      <div>
        <h2><span class="code">{code}</span> {name}</h2>
        <p class="sector">{sector}</p>
      </div>
    </div>
    <div class="score">
      <div class="score-num">{score}<span>/{smax}</span></div>
      <div class="bar"><i style="width:{pct}%"></i></div>
    </div>
  </header>
  <div class="grid">{metrics}</div>
  <div class="foot">
    <div class="note">{streak}期連続 増収増益 ・ 営業利益 年{cagr}成長<br>
      レンジ {lo}〜{hi}円</div>
    <div class="sparkwrap">{_sparkline(row.get("spark"))}<span>直近3か月</span></div>
  </div>
  <div class="chips">{chips}</div>
</article>"""


CSS = """
:root{
  --bg:#f6f7f9; --surface:#ffffff; --line:#e3e6ea;
  --ink:#16191d; --ink2:#5a616b; --ink3:#878e99;
  --accent:#1f6feb; --accent-soft:#e8f0fe;
  --good:#1a7f5a; --warn:#b06b00;
  --radius:14px;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#0f1216; --surface:#171b21; --line:#282e37;
    --ink:#e9edf2; --ink2:#a3acb9; --ink3:#767f8c;
    --accent:#5a9bff; --accent-soft:#1b2940;
    --good:#4ec08d; --warn:#e0a23c;
  }
}
:root[data-theme="dark"]{
  --bg:#0f1216; --surface:#171b21; --line:#282e37;
  --ink:#e9edf2; --ink2:#a3acb9; --ink3:#767f8c;
  --accent:#5a9bff; --accent-soft:#1b2940;
  --good:#4ec08d; --warn:#e0a23c;
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--bg); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",
    "Yu Gothic UI",sans-serif;
  font-size:15px; line-height:1.55;
  -webkit-text-size-adjust:100%;
}
.wrap{max-width:1180px; margin:0 auto; padding:20px 16px 64px}
.top{margin-bottom:18px}
.top h1{font-size:19px; margin:0 0 4px; letter-spacing:.01em}
.top .sub{color:var(--ink2); font-size:13px; margin:0}
.stats{
  display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin:14px 0 6px;
  max-width:520px;
}
.stat{
  background:var(--surface); border:1px solid var(--line); border-radius:10px;
  padding:8px 10px;
}
.stat span{display:block; font-size:10.5px; color:var(--ink3); white-space:nowrap}
.stat b{font-size:17px; font-variant-numeric:tabular-nums}
.cards{display:grid; gap:12px; grid-template-columns:1fr}
@media(min-width:760px){.cards{grid-template-columns:repeat(2,1fr)}}
@media(min-width:1140px){.cards{grid-template-columns:repeat(3,1fr)}}
.card{
  background:var(--surface); border:1px solid var(--line);
  border-radius:var(--radius); padding:14px 14px 12px;
}
.card header{display:flex; justify-content:space-between; gap:12px; align-items:flex-start}
.ident{display:flex; gap:9px; min-width:0}
.rank{
  flex:none; width:22px; height:22px; border-radius:6px; margin-top:2px;
  background:var(--accent-soft); color:var(--accent);
  font-size:11px; font-weight:700; display:grid; place-items:center;
  font-variant-numeric:tabular-nums;
}
.card h2{font-size:14.5px; margin:0; font-weight:650; line-height:1.35}
.code{
  font-variant-numeric:tabular-nums; color:var(--ink2);
  font-size:12.5px; margin-right:4px;
}
.sector{margin:1px 0 0; font-size:11.5px; color:var(--ink3)}
.score{flex:none; width:92px; text-align:right}
.score-num{font-size:22px; font-weight:700; font-variant-numeric:tabular-nums; line-height:1.1}
.score-num span{font-size:11px; color:var(--ink3); font-weight:500}
.bar{height:5px; border-radius:3px; background:var(--line); margin-top:5px; overflow:hidden}
.bar i{display:block; height:100%; border-radius:3px; background:var(--accent)}
.grid{
  display:grid; grid-template-columns:repeat(3,1fr); gap:1px;
  background:var(--line); border:1px solid var(--line); border-radius:10px;
  overflow:hidden; margin:12px 0 10px;
}
.cell{background:var(--surface); padding:6px 8px}
.cell span{display:block; font-size:10.5px; color:var(--ink3)}
.cell b{font-size:13.5px; font-weight:600; font-variant-numeric:tabular-nums}
.foot{display:flex; justify-content:space-between; align-items:flex-end; gap:10px}
.note{font-size:11.5px; color:var(--ink2)}
.sparkwrap{flex:none; text-align:right; color:var(--accent)}
.sparkwrap span{display:block; font-size:10px; color:var(--ink3); margin-top:-2px}
.chips{display:flex; flex-wrap:wrap; gap:5px; margin-top:10px}
.chip{
  font-size:10.5px; color:var(--ink2); background:var(--bg);
  border:1px solid var(--line); border-radius:999px; padding:2px 8px;
}
.chip b{color:var(--accent); margin-left:4px; font-variant-numeric:tabular-nums}
.empty{
  background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
  padding:28px 18px; text-align:center; color:var(--ink2);
}
details.about{margin-top:26px; font-size:12.5px; color:var(--ink2)}
details.about summary{cursor:pointer; color:var(--ink); font-weight:600}
details.about table{border-collapse:collapse; margin-top:10px; width:100%}
details.about th,details.about td{
  border-bottom:1px solid var(--line); padding:5px 8px; text-align:left; font-weight:400;
}
details.about th{color:var(--ink3); font-size:11px}
footer{margin-top:26px; font-size:11px; color:var(--ink3); line-height:1.7}
"""


def render(passed, allrows, cfg, meta, out_path):
    rows = passed.head(int(cfg["display"]["max_rows"]))
    cards = "\n".join(_card(r, i + 1) for i, (_, r) in enumerate(rows.iterrows()))
    if not len(rows):
        cards = ('<div class="empty">今日は必須条件をすべて満たす銘柄がありませんでした。'
                 '<br>条件をゆるめたい場合は config.yml の数字を調整してください。</div>')

    fails = [
        ("業績（連続増収増益）", int(allrows["ok_growth"].sum())),
        ("PER15倍以下", int(allrows["ok_per"].sum())),
        ("高値から下落", int(allrows["ok_drawdown"].sum())),
        ("レンジ形成", int(allrows["ok_range"].sum())),
        ("売買代金", int(allrows["ok_liquidity"].sum())),
    ]
    fail_rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{v:,} 銘柄</td></tr>" for k, v in fails
    )

    req = cfg["required"]
    cond_rows = "".join([
        f"<tr><td>業績</td><td>直近{req['consecutive_growth_years']}期連続で増収かつ増益</td></tr>",
        f"<tr><td>PER</td><td>{req['per_max']}倍以下（会社予想EPS基準）</td></tr>",
        f"<tr><td>下落</td><td>直近{req['high_lookback_days']}営業日の高値から{req['drawdown_min_pct']}%以上下落</td></tr>",
        f"<tr><td>レンジ</td><td>直近{req['range_window_days']}営業日の値幅が平均株価の{req['range_width_max_pct']}%以内、かつ高値を更新していない</td></tr>",
        f"<tr><td>流動性</td><td>平均売買代金 {int(req['min_avg_turnover_yen']):,}円以上</td></tr>",
    ])

    now = dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    return_html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>買い時スクリーニング</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <h1>買い時スクリーニング</h1>
    <p class="sub">最終更新 {now}（日本時間）・株価基準 {html.escape(str(meta.get('price_date','—')))}{html.escape(meta.get('price_note',''))}</p>
    <div class="stats">
      <div class="stat"><span>調べた銘柄</span><b>{meta.get('universe',0):,}</b></div>
      <div class="stat"><span>条件通過</span><b>{len(passed):,}</b></div>
      <div class="stat"><span>表示</span><b>{len(rows):,}</b></div>
      <div class="stat"><span>最高点</span><b>{int(passed['score'].max()) if len(passed) else 0}</b></div>
    </div>
  </div>

  <div class="cards">
{cards}
  </div>

  <details class="about">
    <summary>条件と配点について</summary>
    <table>
      <tr><th>必須条件</th><th>内容</th></tr>
      {cond_rows}
    </table>
    <table>
      <tr><th>各条件を単独で満たした銘柄数</th><th></th></tr>
      {fail_rows}
    </table>
  </details>

  <footer>
    このページは自動生成です。数値はJPX公式のJ-Quants APIと、日中は遅延株価をもとに機械的に計算しています。<br>
    決算の実数値・会社予想は必ずご自身で確認してください。投資判断の責任は利用者にあります。
  </footer>
</div>
</body>
</html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(return_html)
    return out_path


def dump_json(passed, meta, path):
    recs = []
    for code, r in passed.iterrows():
        recs.append({
            "code": str(code),
            "name": r.get("name"),
            "score": int(r.get("score") or 0),
            "price": None if r.get("price") is None or np.isnan(r.get("price")) else float(r["price"]),
            "per": None if np.isnan(r.get("per", np.nan)) else float(r["per"]),
            "div_yield": None if np.isnan(r.get("div_yield", np.nan)) else float(r["div_yield"]),
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "stocks": recs}, f, ensure_ascii=False, indent=1)
