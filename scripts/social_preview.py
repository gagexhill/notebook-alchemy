"""Render the repository's social preview image (1280x640 PNG) with Playwright.

Playwright comes with the `notebooklm-py[browser]` dependency. GitHub has no API for the social
preview, so the upload is a manual step in the repository settings (see OPERATIONS.md).

Usage: python scripts/social_preview.py [--videos 52 --domains 6 --sources 8 --example @Hyperplexed]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "docs" / "images" / "social-preview.png"

TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><style>
:root{--bg:#0f1115;--ink:#f2f3f5;--mute:#9aa1ad;--line:#2a2f3a;--accent:#8b7cf6}
*{box-sizing:border-box;margin:0}
body{width:1280px;height:640px;background:var(--bg);color:var(--ink);
 font-family:"Segoe UI",system-ui,-apple-system,Arial,sans-serif;padding:56px 84px;position:relative;overflow:hidden}
body:before{content:"";position:absolute;inset:0;
 background:radial-gradient(900px 420px at 85% -10%,#2a2450 0%,transparent 70%)}
.wrap{position:relative;height:100%;display:flex;flex-direction:column;justify-content:space-between}
.eyebrow{font-size:22px;letter-spacing:.18em;text-transform:uppercase;color:var(--accent);font-weight:600}
h1{font-size:84px;line-height:1.04;font-weight:700;letter-spacing:-.02em;margin-top:18px}
.sub{font-size:32px;color:var(--mute);margin-top:18px;font-weight:400}
.flow{display:flex;align-items:center;gap:26px;margin-top:6px}
.node{border:1px solid var(--line);background:#161923;border-radius:14px;padding:18px 28px}
.n{font-size:48px;font-weight:700;line-height:1}
.l{font-size:21px;color:var(--mute);margin-top:6px}
.arrow{font-size:40px;color:var(--accent)}
.foot{display:flex;justify-content:space-between;font-size:24px;color:var(--mute)}
code{font-family:Consolas,"Cascadia Mono",monospace;color:var(--ink)}
</style></head><body><div class="wrap">
<div><div class="eyebrow">Notebook Alchemy</div>
<h1>A channel in,<br>a notebook out.</h1>
<div class="sub">Fit a whole YouTube channel in a few NotebookLM sources.</div></div>
<div class="flow">
 <div class="node"><div class="n">{videos}</div><div class="l">videos</div></div><div class="arrow">&rarr;</div>
 <div class="node"><div class="n">{domains}</div><div class="l">domains</div></div><div class="arrow">&rarr;</div>
 <div class="node"><div class="n">{sources}</div><div class="l">sources</div></div>
 <div class="l" style="margin-left:18px;font-size:22px">example: {example}</div>
</div>
<div class="foot"><code>github.com/gagexhill/notebook-alchemy</code><span>MIT</span></div>
</div></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--videos", type=int, default=52)
    parser.add_argument("--domains", type=int, default=6)
    parser.add_argument("--sources", type=int, default=8)
    parser.add_argument("--example", default="@Hyperplexed")
    args = parser.parse_args()

    html = (
        TEMPLATE.replace("{videos}", str(args.videos))
        .replace("{domains}", str(args.domains))
        .replace("{sources}", str(args.sources))
        .replace("{example}", args.example)
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 640})
        page.set_content(html)
        page.screenshot(path=str(args.out))
        browser.close()
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
