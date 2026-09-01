#!/usr/bin/env python3
"""Build a self-contained HTML copy of the decision card (data inlined).

The normal index.html fetches data/market-card.json at runtime, which fails
under file:// (CORS) and behind a proxy. This emits market-card-view.html with
the payload inlined into window.__CARD_DATA__, so it renders on a plain
double-click with zero network. Styles (styles.css) and logic (app.js) are
still referenced relatively, so keep this file next to them.

Run:  python -X utf8 scripts/build_standalone.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CARD = ROOT / "data" / "market-card.json"
INDEX = ROOT / "index.html"
OUT = ROOT / "market-card-view.html"


def main() -> None:
    if not CARD.exists():
        raise SystemExit("data/market-card.json 不存在，请先运行 scripts/build_data.py")
    payload = json.loads(CARD.read_text(encoding="utf-8"))
    html = INDEX.read_text(encoding="utf-8")
    data_script = (
        "<script>window.__CARD_DATA__ = "
        f"{json.dumps(payload, ensure_ascii=False)};\n</script>\n"
    )
    # Inject the inline payload right before the app.js <script> tag.
    new_html, n = re.subn(
        r'<script src="app\.js[^>]*></script>',
        data_script + r'<script src="app.js?v=20260901b" defer></script>',
        html, count=1,
    )
    if n != 1:
        raise SystemExit("未能在 index.html 中定位 app.js 脚本标签")
    OUT.write_text(new_html, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.name} ({len(new_html):,} bytes, inlined market_date={payload['meta']['market_date']})")


if __name__ == "__main__":
    main()
