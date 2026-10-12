"""Styles, scripts and small HTML helpers of the triage report."""
from __future__ import annotations

import html
from typing import Any


CSS = """
:root{--bg:#fdfdfc;--fg:#1c2530;--muted:#6b7785;--card:#f7f8f9;--card2:#f0f2f4;--line:#e3e7eb;
--red:#c62828;--amber:#a3660a;--green:#2e7d32;--blue:#1565c0;--grey:#8593a3;
--pass:#cfe8d2;--fail:#e05a5a;--walk:#f0b64a;--err:#8593a3}
@media(prefers-color-scheme:dark){:root{--bg:#12151a;--fg:#e7ebee;--muted:#a3aeba;--card:#1b2027;--card2:#212730;
--line:#2c333c;--red:#ff8b83;--amber:#f0b64a;--green:#82d29a;--blue:#7fb4ff;--pass:#2c4a33;--fail:#e05a5a;--walk:#b98520}}
*{box-sizing:border-box}
body{margin:0;padding:32px 20px;background:var(--bg);color:var(--fg);
font:16px/1.6 -apple-system,"Segoe UI",system-ui,Roboto,sans-serif;max-width:900px;margin-inline:auto}
h1{font-size:23px;font-weight:650;margin:0 0 6px;letter-spacing:-.01em}
h2{font-size:16px;font-weight:650;margin:40px 0 12px;letter-spacing:-.005em}
h3{font-size:14px;font-weight:650;margin:20px 0 8px;color:var(--fg)}
p{margin:8px 0}
.muted{color:var(--muted)}.small{font-size:13px}
.verdict{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--grey);
border-radius:10px;padding:18px 20px;margin:18px 0}
.verdict.bad{border-left-color:var(--red)}.verdict.ok{border-left-color:var(--green)}.verdict.mid{border-left-color:var(--amber)}
.verdict .headline{font-size:18px;font-weight:650;margin-bottom:4px}
.start{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--blue);
border-radius:10px;padding:6px 20px 14px;margin:18px 0}
.counts{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 2px}
.count{background:var(--card2);border-radius:8px;padding:10px 16px;min-width:100px}
.count b{display:block;font-size:21px;font-weight:650}
details.card{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:14px 0;overflow:hidden}
details.card>summary{cursor:pointer;padding:14px 18px;font-weight:600;font-size:15px;list-style:none;
display:flex;align-items:baseline;gap:2px}
details.inner{margin:16px 0;padding-top:12px;border-top:1px solid var(--line)}
details.inner>summary{cursor:pointer;padding:2px 0 8px;font-weight:550;font-size:13.5px;color:var(--muted);list-style:none}
details>summary::-webkit-details-marker{display:none}
details.card>summary:before{content:"▸  ";color:var(--muted);font-size:12px}
details.card[open]>summary:before{content:"▾  "}
details.inner>summary:before{content:"▸  ";margin-right:2px}
details.inner[open]>summary:before{content:"▾  "}
.card .body{padding:2px 20px 20px}
.badge{display:inline-block;font-size:11px;font-weight:700;padding:2px 9px;border-radius:20px;margin-right:10px;
color:#fff;vertical-align:2px;letter-spacing:.02em;text-transform:uppercase}
.badge.likely{background:var(--red)}.badge.known{background:var(--blue)}.badge.watch{background:var(--amber)}
ol.repro{padding-left:22px}ol.repro li{margin:5px 0}
.hit{background:rgba(224,90,90,.16);border-radius:4px;padding:1px 6px;font-weight:600}
.tag{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:20px;padding:1px 8px;margin-left:6px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th{text-align:left;padding:6px 10px;color:var(--muted);font-weight:600;font-size:12px;
border-bottom:1px solid var(--line);text-transform:uppercase;letter-spacing:.02em}
td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
code{font:12.5px ui-monospace,Consolas,monospace;background:var(--card2);border-radius:4px;padding:1px 5px}
.timeline{display:flex;flex-wrap:wrap;gap:1px;margin:6px 0 16px}
.timeline a,.timeline span{display:block;width:7px;height:18px;border-radius:1px;background:var(--pass)}
.timeline .f{background:var(--fail)}.timeline .w{background:var(--walk)}.timeline .e{background:var(--err)}
.timeline a.onset{outline:2px solid var(--fg);outline-offset:1px}
.legend span{display:inline-block;width:10px;height:10px;margin:0 4px 0 12px;vertical-align:-1px;border-radius:2px}
img.shot{max-width:100%;border:1px solid var(--line);border-radius:8px;margin-top:8px}
.note{background:var(--card2);border-left:4px solid var(--amber);padding:10px 14px;border-radius:6px;margin:12px 0}
.see{background:var(--card2);border-radius:8px;padding:12px 16px;margin:10px 0}
textarea{width:100%;min-height:220px;font:12.5px Consolas,monospace;background:var(--card2);color:var(--fg);
border:1px solid var(--line);border-radius:8px;padding:10px}
.shots{display:flex;flex-wrap:wrap;gap:12px;margin:8px 0}
figure{margin:0;flex:1 1 280px;max-width:100%}
figure img.shot{width:100%;margin-top:0;cursor:zoom-in}
figure.big{flex-basis:100%}
figcaption{font-size:12px;color:var(--muted);margin-top:4px}
tr.onset td{background:rgba(224,90,90,.12)}
ul.seq{padding-left:20px}ul.seq li{margin:4px 0}
button,a.btn{font:inherit;font-size:13px;font-weight:550;padding:6px 14px;border:1px solid var(--blue);
background:transparent;color:var(--blue);border-radius:20px;cursor:pointer;text-decoration:none;
display:inline-block;margin-right:8px}
.whatchanged{background:var(--card2);border-left:4px solid var(--red);border-radius:8px;padding:14px 18px;margin:14px 0 18px}
.whatchanged h3{margin:0 0 10px;color:var(--red);font-size:13.5px;text-transform:uppercase;letter-spacing:.02em}
ul.difflist{list-style:none;padding:0;margin:2px 0 12px}
ul.difflist li{padding:6px 10px;border-radius:6px;margin:3px 0;font-size:13.5px;line-height:1.5}
ul.difflist li.diff-change{background:rgba(163,102,10,.10)}
ul.difflist li.diff-add{background:rgba(46,125,50,.10)}
ul.difflist li.diff-remove{background:rgba(198,40,40,.10)}
ul.difflist code{background:rgba(127,127,127,.12)}
ul.leads{list-style:none;padding:0;margin:8px 0 0}
ul.leads li{padding:8px 12px;border-radius:6px;margin:4px 0;font-size:13.5px;line-height:1.5;background:rgba(21,101,192,.08)}
.explain{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 20px;margin:12px 0}
.explain h3{margin:0 0 2px;font-size:14.5px}
.explain p.muted{margin-top:0}
"""


def esc(x: Any) -> str:
    return html.escape(str(x), quote=True)
