"""Dark theme shared by the HTML reports (issue report, run log, corpus report, review page). Each report appends
its override to its own stylesheet; the corpus report and the review page use the same colours through CSS
variables (DARK_VARS).
"""

# CSS variables used by the corpus report and the review page
DARK_VARS = ("--bg:#14161b;--panel:#1c1f26;--ink:#e8eaf0;--mute:#98a2b3;--line:#2c313b;--accent:#8aa4ff;"
             "--good:#53c58a;--warn:#f0a85a;--bad:#f58a80;--chip:#262b35;--ok:#53c58a;--no:#f58a80;"
             "--triv:#98a2b3;--bug:#f0a85a;color-scheme:dark")

ISSUE_REPORT_DARK = """
:root{color-scheme:dark}
body{color:#e8eaf0;background:#14161b}
.chip{background:#1c1f26;border-color:#2c313b;color:#c9ced8}
.chip.strong{background:#2f5bea;border-color:#2f5bea;color:#fff}
.hint,.muted,.t .sub,.lbl,.row.head,figcaption{color:#98a2b3}
.list{background:#1c1f26;border-color:#2c313b}
.row.head{background:#181b21;border-bottom-color:#2c313b}
details.issue{border-bottom-color:#2c313b}
details.issue>summary:hover{background:#222733}details[open].issue>summary{background:#212a3d}
.n{color:#7b8494}.c{color:#c9ced8}
.badge{border-color:#3a4150;color:#98a2b3}
.badge.known{border-color:#7a6320;color:#f0c76a;background:#2d2711}
.badge.inf{border-color:#5a4d8f;color:#b9a8f0;background:#251f3d}
.body{background:#171a20;border-top-color:#2c313b}
details.ex{background:#1c1f26;border-color:#2c313b}
details.fold{background:#1c1f26;border-color:#2c313b;color:#e8eaf0}
.exbody{border-top-color:#2c313b}
a.log{color:#8aa4ff;border-color:#34406a;background:#1d2440}a.log:hover{background:#262f55}
li.fail{background:#3a1c1c;color:#f58a80}
.btns button{border-color:#3a4150;background:#1c1f26;color:#e8eaf0}
.btns button:hover{background:#262b35;border-color:#5a6a9a}
details.sub>summary{color:#8aa4ff}
.tech{border-left-color:#2c313b}figure img{border-color:#2c313b}
table{color:#e8eaf0}td,th{border-color:#2c313b}th{background:#262b35}
code{background:#262b35;color:#e8eaf0}
pre{background:#14161b;border-color:#2c313b;color:#e8eaf0}
.rep{background:#1c1f26;border-color:#5a2a2a;border-left-color:#d64545}.repname{color:#f58a80}
.ev{border-left-color:#3a4150;color:#c9ced8}
"""

RUN_LOG_DARK = """
body{background:#14161b;color:#e8eaf0}
.meta{color:#98a2b3}
.sbox-total{background:#1f2540;color:#9fb0ff;border-color:#2f3a66}
.sbox-pass{background:#14281d;color:#6fd39a;border-color:#1f4a31}
.sbox-fail{background:#33181c;color:#f58a80;border-color:#5a2a30}
.sbox-error{background:#33250f;color:#f0a85a;border-color:#5a4018}
.step{background:#1c1f26;box-shadow:none}
.step-skip{border-left-color:#4b5262}
.step-num{color:#b6bdcb}
.badge-pass,.badge-pass-sm{background:#14281d;color:#6fd39a}
.badge-fail,.badge-fail-sm{background:#33181c;color:#f58a80}
.badge-error{background:#33250f;color:#f0a85a}
.badge-skip{background:#262b35;color:#98a2b3}
.badge-known,.known-tag{background:#17263a;color:#7fb2f0}
.sbox-known{background:#17263a;color:#7fb2f0;border-color:#27415f}
.step-known{border-left-color:#3b7dc4}
.mode-tag{background:#1f2a45;color:#8aa4ff}
.inv-line,.failure-msg{color:#98a2b3}
.ev-item{border-left-color:#3a4150;color:#c9ced8;background:#181b21}
.inv-evidence-name{color:#b6bdcb}
.failure{background:#2a1a1d;border-color:#5a2a30}
.failure-name{color:#f58a80}
.mismatch{background:#1c1f26;border-color:#2c313b}
.label{color:#98a2b3}
.state-diff{background:#1a2234;border-color:#2f3f66}.state-diff-title{color:#8aa4ff}
.action-error{background:#33250f;border-color:#5a4018;color:#f0a85a}
summary{color:#8aa4ff}summary:hover{color:#b3c4ff}
pre{background:#14161b;border-color:#2c313b;color:#e8eaf0}
.screenshot{border-color:#2c313b}
.action-selected{background:#1f2a45}.action-cat{color:#98a2b3}.action-testid{color:#7b8494}
.action-group{background:#181b21}.action-group-title{color:#98a2b3}.action-group div{color:#c9ced8}
.action-selected{color:#8aa4ff !important}.action-more{color:#7b8494 !important}
code{background:#262b35;color:#e8eaf0}
.nav-bar{background:#1c1f26;box-shadow:0 2px 12px rgba(0,0,0,.5)}
.nav-btn-fail{background:#33181c;color:#f58a80}.nav-btn-error{background:#33250f;color:#f0a85a}
.nav-label{color:#98a2b3}
"""