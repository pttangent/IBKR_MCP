"""Local, dependency-free dashboard routes for the tick radar."""

from __future__ import annotations

from starlette.responses import HTMLResponse, JSONResponse

from .tick_radar import get_default_engine


DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-Hans">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>IBKR Tick Radar</title>
<style>
:root{color-scheme:dark;--bg:#0b0d10;--panel:#15191f;--panel2:#1b2028;--text:#edf2f7;--muted:#8e9baa;--line:#29313b;--up:#31d07d;--down:#ff6673;--warn:#f0b44d;--accent:#67a7ff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,-apple-system,Segoe UI,sans-serif}.wrap{max-width:1600px;margin:auto;padding:18px}.head{display:flex;gap:16px;justify-content:space-between;align-items:flex-end;margin-bottom:14px}.title{font-size:26px;font-weight:750}.sub{color:var(--muted);margin-top:4px}.status{padding:6px 10px;border:1px solid var(--line);border-radius:999px;background:var(--panel)}.grid{display:grid;grid-template-columns:minmax(0,2.3fr) minmax(320px,.8fr);gap:14px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}.panel h2{font-size:14px;margin:0;padding:12px 14px;border-bottom:1px solid var(--line);background:var(--panel2)}table{width:100%;border-collapse:collapse;min-width:1120px}th,td{padding:9px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th{position:sticky;top:0;background:var(--panel2);z-index:1;color:var(--muted);font-size:12px}th:first-child,td:first-child{text-align:left}.scroll{overflow:auto;max-height:72vh}.symbol{font-weight:750;font-size:15px}.tag{font-size:11px;border:1px solid var(--line);border-radius:6px;padding:2px 5px;color:var(--muted)}.live{color:var(--up)}.bad{color:var(--down)}.warn{color:var(--warn)}.score{font-variant-numeric:tabular-nums;font-weight:700}.up{color:var(--up)}.down{color:var(--down)}.neutral{color:var(--muted)}.bar{height:5px;background:#252c35;border-radius:4px;overflow:hidden;margin-top:4px;min-width:80px}.fill{height:100%;width:0;background:var(--accent)}.alerts{max-height:72vh;overflow:auto}.alert{padding:11px 12px;border-bottom:1px solid var(--line)}.alert-top{display:flex;justify-content:space-between;gap:10px}.alert-kind{font-weight:700}.alert-meta{color:var(--muted);font-size:12px;margin-top:5px}.empty{padding:20px;color:var(--muted)}.foot{color:var(--muted);font-size:12px;margin-top:12px}.metric{display:flex;flex-direction:column;align-items:flex-end}.metric small{color:var(--muted)}@media(max-width:1000px){.grid{grid-template-columns:1fr}.alerts{max-height:360px}}
</style>
</head>
<body><div class="wrap">
<div class="head"><div><div class="title">IBKR Tick-by-Tick Radar</div><div class="sub">确定性微观结构信号 · 1/3/5分钟衰减投影 · 在线前瞻校准</div></div><div id="status" class="status">等待数据</div></div>
<div class="grid">
<section class="panel"><h2>实时信号</h2><div class="scroll"><table><thead><tr>
<th>Symbol</th><th>Last</th><th>Data</th><th>Composite</th><th>Activity</th><th>Large trade</th><th>Volume burst</th><th>Flow</th><th>Price impulse</th><th>Half-life</th><th>有效期限</th><th>1m</th><th>3m</th><th>5m</th><th>经验1m</th>
</tr></thead><tbody id="rows"></tbody></table></div></section>
<section class="panel"><h2>关键警报</h2><div id="alerts" class="alerts"><div class="empty">暂无警报</div></div></section>
</div>
<div class="foot">衰减投影表示：没有新成交确认时，当前信号按半衰期模型还剩多少；它不是收益保证。经验收益只有达到最小样本数后才显示为 calibrated。</div>
</div>
<script>
const fmt=(v,d=1)=>Number.isFinite(Number(v))?Number(v).toFixed(d):'—';
const cls=v=>Number(v)>3?'up':Number(v)<-3?'down':'neutral';
function scoreCell(signal){const s=Number(signal?.score||0);return `<div class="metric"><span class="score ${cls(s)}">${fmt(s)}</span><div class="bar"><div class="fill" style="width:${Math.min(100,Math.abs(s))}%;background:${s>0?'var(--up)':s<0?'var(--down)':'var(--accent)'}"></div></div></div>`}
function dataCell(item){let c=item.trade_eligible?'live':item.halted?'warn':'bad';let label=item.halted?'HALTED':item.tick_active?(item.market_data_type||'UNKNOWN'):'QUOTE';return `<span class="tag ${c}">${label}</span>`}
function empirical(signal){const p=signal?.empirical_forward_performance?.['1m'];if(!p)return '—';if(!p.calibrated)return `n=${p.sample_count}`;return `${fmt(p.ewma_signed_return_bps,2)}bp / ${fmt((p.hit_rate||0)*100,0)}%`}
function renderRows(items){const body=document.getElementById('rows');body.innerHTML=items.map(item=>{const s=item.signals||{},c=s.composite||{},proj=c.projected_score_if_unconfirmed||{};return `<tr><td><span class="symbol">${item.symbol}</span></td><td>${fmt(item.last_trade_price,2)}</td><td>${dataCell(item)}</td><td>${scoreCell(c)}</td><td>${scoreCell(s.activity)}</td><td>${scoreCell(s.large_trade)}</td><td>${scoreCell(s.volume_burst)}</td><td>${scoreCell(s.signed_flow)}</td><td>${scoreCell(s.price_impulse)}</td><td>${fmt(c.half_life_seconds,0)}s</td><td>${fmt(c.effective_horizon_minutes,2)}m</td><td class="${cls(proj['1m'])}">${fmt(proj['1m'])}</td><td class="${cls(proj['3m'])}">${fmt(proj['3m'])}</td><td class="${cls(proj['5m'])}">${fmt(proj['5m'])}</td><td>${empirical(c)}</td></tr>`}).join('')||'<tr><td colspan="15" class="empty">尚未启动 radar</td></tr>'}
function renderAlerts(values){const box=document.getElementById('alerts');box.innerHTML=(values||[]).slice().reverse().map(a=>`<div class="alert"><div class="alert-top"><span><b>${a.symbol}</b> <span class="alert-kind">${a.kind}</span></span><span class="score ${cls(a.score)}">${fmt(a.score)}</span></div><div class="alert-meta">${a.emitted_at_utc||''} · 起点 ${fmt(a.start_price,2)} · half-life ${fmt(a.half_life_seconds,0)}s · horizon ${fmt(a.effective_horizon_minutes,2)}m</div></div>`).join('')||'<div class="empty">暂无警报</div>'}
async function refresh(){try{const r=await fetch('/api/v1/radar/snapshot',{cache:'no-store'});const d=await r.json();renderRows(d.symbols||[]);renderAlerts(d.alerts||[]);document.getElementById('status').textContent=`${d.active_tick_streams||0} tick streams · ${d.symbol_count||0} symbols · ${new Date(d.generated_at_utc).toLocaleTimeString()}`;}catch(e){document.getElementById('status').textContent='连接失败';}}
refresh();setInterval(refresh,1000);
</script></body></html>"""


async def tick_radar_dashboard(request):
    return HTMLResponse(DASHBOARD_HTML)


async def tick_radar_snapshot(request):
    return JSONResponse(get_default_engine().snapshot())
