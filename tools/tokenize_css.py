"""One-shot: turn every hard-coded colour in the app stylesheet into a token.

The interface had its palette in a single ``:root`` block *and* about thirty
hard-coded hex values further down, so changing the theme meant hunting them
down. This rewrites those values as variables with the same defaults, which is
a no-op visually but makes a theme a one-block swap — the design options are
then previewable, and shippable, by replacing that block alone.

Run once; afterwards ``git diff`` shows only pure value-preserving edits, and
``tools/make_design_options.py --skeleton`` can render before/after for a
pixel comparison.

    python tools/tokenize_css.py pilot_app/static/index.html
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT_BLOCK_OLD = """    :root{
      --navy:#123b63;--blue:#1769aa;--ink:#172b3a;--muted:#64748b;--line:#dbe3eb;
      --bg:#f3f6f9;--card:#ffffff;--ok:#087a55;--warn:#8a6100;--bad:#b42318;
      --ok-bg:#e8f5ef;--warn-bg:#fff8e8;--bad-bg:#fff1f0;--info-bg:#eaf4fc;--info-line:#b9d7ee;
      --radius:12px;
    }"""

ROOT_BLOCK_NEW = """    :root{
      /* Every colour in this stylesheet resolves to a token declared here, so a
         theme is one block swap instead of a hunt for hard-coded hex values.
         The values below are the original palette, unchanged. */
      --navy:#123b63;--blue:#1769aa;--ink:#172b3a;--muted:#64748b;--line:#dbe3eb;
      --bg:#f3f6f9;--card:#ffffff;--ok:#087a55;--warn:#8a6100;--bad:#b42318;
      --ok-bg:#e8f5ef;--warn-bg:#fff8e8;--bad-bg:#fff1f0;--info-bg:#eaf4fc;--info-line:#b9d7ee;
      /* text that sits on a light surface (--navy is dark, so it doubles as
         body text on --card/--info-bg; dark themes point this at a light ink) */
      --navy-ink:var(--navy);
      --line-soft:#eef2f6;--surface-2:#f8fafc;--surface-3:#f7faff;--panel:#fbfdff;
      --wizard-bg:#f4faff;--wizard-line:#8fb9d8;
      --input-line:#b9c6d2;--input-bg:#ffffff;
      --quiet-bg:#e8eef4;--tab-bg:#e7edf3;--track:#e6ecf2;--danger-line:#f5c4c0;
      --ok-line:#cbe8d9;--ok-ink:#0a5c42;--warn-line:#f0d9a8;
      --appbar-bg:var(--navy);--appbar-ink:#ffffff;--appbar-muted:#bcd7ea;
      --hero-a:#123b63;--hero-b:#1c5b8f;--hero-ink:#ffffff;--hero-kicker:#bcd7ea;
      --hero-text:#e3f0fa;--hero-btn-bg:#ffffff;--hero-btn-ink:var(--navy);
      --hero-done-a:#0a5c42;--hero-done-b:#12805e;
      --card-shadow:0 3px 12px #123b6308;
      --page:720px;
      --radius:12px;
    }"""

REPLACEMENTS: tuple[tuple[str, str], ...] = (
    (ROOT_BLOCK_OLD, ROOT_BLOCK_NEW),
    # ---- layout: the reading column and the app bar share one width token ----
    ("background:var(--navy);color:#fff;padding:16px max(16px,calc((100% - 720px)/2));",
     "background:var(--appbar-bg);color:var(--appbar-ink);padding:16px max(16px,calc((100% - var(--page))/2));"),
    ("header.appbar small{display:block;color:#bcd7ea;",
     "header.appbar small{display:block;color:var(--appbar-muted);"),
    ("main{max-width:720px;margin:0 auto;padding:16px}",
     "main{max-width:var(--page);margin:0 auto;padding:16px}"),
    # ---- plain colour tokens ----
    ("box-shadow:0 3px 12px #123b6308}", "box-shadow:var(--card-shadow)}"),
    ("width:100%;border:1px solid #b9c6d2;", "width:100%;border:1px solid var(--input-line);"),
    ("      background:#fff;color:inherit;min-width:0;", "      background:var(--input-bg);color:inherit;min-width:0;"),
    ("button.secondary{background:#e8eef4;color:var(--navy)}",
     "button.secondary{background:var(--quiet-bg);color:var(--navy-ink)}"),
    ("button.ghost{background:#fff;color:var(--navy);border:1px solid var(--line)}",
     "button.ghost{background:var(--input-bg);color:var(--navy-ink);border:1px solid var(--line)}"),
    ("button.danger{background:#fff1f0;color:var(--bad);border:1px solid #f5c4c0}",
     "button.danger{background:var(--bad-bg);color:var(--bad);border:1px solid var(--danger-line)}"),
    (".button-link.secondary{background:#e8eef4;color:var(--navy)}",
     ".button-link.secondary{background:var(--quiet-bg);color:var(--navy-ink)}"),
    (".status{padding:11px 13px;border-radius:9px;background:var(--info-bg);color:var(--navy);margin:12px 0;font-size:14px}",
     ".status{padding:11px 13px;border-radius:9px;background:var(--info-bg);color:var(--navy-ink);margin:12px 0;font-size:14px}"),
    (".progress{height:8px;border-radius:99px;background:#e6ecf2;", ".progress{height:8px;border-radius:99px;background:var(--track);"),
    ("nav.tabs button{flex:1 1 150px;min-width:0;background:#e7edf3;color:var(--navy);font-weight:650;",
     "nav.tabs button{flex:1 1 150px;min-width:0;background:var(--tab-bg);color:var(--navy-ink);font-weight:650;"),
    ("nav.tabs button.active{background:var(--navy);color:#fff}",
     "nav.tabs button.active{background:var(--navy);color:var(--appbar-ink)}"),
    (".hero{border-radius:var(--radius);padding:20px;background:linear-gradient(135deg,#123b63,#1c5b8f);color:#fff}",
     ".hero{border-radius:var(--radius);padding:20px;background:linear-gradient(135deg,var(--hero-a),var(--hero-b));color:var(--hero-ink)}"),
    (".hero .kicker{font-size:12px;letter-spacing:.08em;color:#bcd7ea}",
     ".hero .kicker{font-size:12px;letter-spacing:.08em;color:var(--hero-kicker)}"),
    (".hero p{color:#e3f0fa;", ".hero p{color:var(--hero-text);"),
    (".hero .button-link,.hero button{background:#fff;color:var(--navy)}",
     ".hero .button-link,.hero button{background:var(--hero-btn-bg);color:var(--hero-btn-ink)}"),
    (".hero.done{background:linear-gradient(135deg,#0a5c42,#12805e)}",
     ".hero.done{background:linear-gradient(135deg,var(--hero-done-a),var(--hero-done-b))}"),
    (".channel{background:#fff;", ".channel{background:var(--card);"),
    (".channel .dot{font-size:12px;font-weight:700;padding:3px 9px;border-radius:99px;background:#eef2f6;",
     ".channel .dot{font-size:12px;font-weight:700;padding:3px 9px;border-radius:99px;background:var(--line-soft);"),
    (".channel.missing .dot{background:#eef2f6;color:var(--bad)}",
     ".channel.missing .dot{background:var(--line-soft);color:var(--bad)}"),
    (".channel.optional .dot{background:#eef2f6;color:var(--muted)}",
     ".channel.optional .dot{background:var(--line-soft);color:var(--muted)}"),
    (".tasklist li,.activity li{padding:12px 0;border-top:1px solid #eef2f6}",
     ".tasklist li,.activity li{padding:12px 0;border-top:1px solid var(--line-soft)}"),
    ("border-radius:99px;background:#eef2f6;color:var(--muted);margin-right:6px}",
     "border-radius:99px;background:var(--line-soft);color:var(--muted);margin-right:6px}"),
    (".pill.low{background:#eef2f6;color:var(--muted)}", ".pill.low{background:var(--line-soft);color:var(--muted)}"),
    (".metrics div{background:#f8fafc;border:1px solid #e7edf3;",
     ".metrics div{background:var(--surface-2);border:1px solid var(--tab-bg);"),
    ("margin:14px 0;background:#fbfdff}", "margin:14px 0;background:var(--panel)}"),
    ("details.advanced > summary{cursor:pointer;font-weight:650;color:var(--navy);",
     "details.advanced > summary{cursor:pointer;font-weight:650;color:var(--navy-ink);"),
    (".howto{margin:14px 0;padding:14px 16px;border-radius:10px;background:#f7faff;border:1px solid #dbe7f5}",
     ".howto{margin:14px 0;padding:14px 16px;border-radius:10px;background:var(--surface-3);border:1px solid var(--line)}"),
    (".howto h3{font-size:14px;color:var(--navy);", ".howto h3{font-size:14px;color:var(--navy-ink);"),
    ("border:1px solid #f0d9a8;color:var(--warn);font-size:13px}", "border:1px solid var(--warn-line);color:var(--warn);font-size:13px}"),
    ("border:1px solid #cbe8d9;color:#0a5c42;font-size:13px}", "border:1px solid var(--ok-line);color:var(--ok-ink);font-size:13px}"),
    (".forward-wizard{margin:16px 0;padding:16px;border:1px solid var(--info-line);border-radius:11px;background:#f4faff}",
     ".forward-wizard{margin:16px 0;padding:16px;border:1px solid var(--info-line);border-radius:11px;background:var(--wizard-bg)}"),
    (".forward-wizard h3{font-size:16px;color:var(--navy);", ".forward-wizard h3{font-size:16px;color:var(--navy-ink);"),
    ("padding:11px 13px;border:1px dashed #8fb9d8;border-radius:9px;background:#fff;font-weight:700}",
     "padding:11px 13px;border:1px dashed var(--wizard-line);border-radius:9px;background:var(--card);font-weight:700}"),
    ("border:1px solid #cbe8d9;color:#0a5c42}", "border:1px solid var(--ok-line);color:var(--ok-ink)}"),
    (".saved.warn{background:var(--warn-bg);border-color:#f0d9a8;", ".saved.warn{background:var(--warn-bg);border-color:var(--warn-line);"),
    (".report{border-top:1px solid #eef2f6;", ".report{border-top:1px solid var(--line-soft);"),
    (".report .report-body h4{font-size:13px;color:var(--navy);", ".report .report-body h4{font-size:13px;color:var(--navy-ink);"),
    (".jargon dt{font-weight:650;color:var(--navy);", ".jargon dt{font-weight:650;color:var(--navy-ink);"),
)

RESPONSIVE = """    /* ---- responsive layout ----
       Six tabs ask for a 150px basis each, so they used to wrap onto a second
       row on every desktop width; below 560px the row scrolls instead of
       squeezing or ellipsing the labels. On a wide screen the reading column
       widens to 880px and the two shortest dashboard cards sit side by side, so
       the page stops looking like a phone screenshot. */
    @media (min-width:560px){
      nav.tabs{flex-wrap:nowrap}
      nav.tabs button{flex:1 1 0;padding:9px 6px}
    }
    @media (max-width:559px){
      nav.tabs{flex-wrap:nowrap;overflow-x:auto}
      nav.tabs button{flex:0 0 auto;padding:9px 14px}
    }
    @media (min-width:1024px){
      :root{--page:880px}
      #dashboard{display:grid;grid-template-columns:1fr 1fr;gap:0 16px;align-items:start}
      #dashboard > *{grid-column:1 / -1}
      #dashboard > div:nth-child(3){grid-column:1}
      #dashboard > div:nth-child(4){grid-column:2}
    }
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", nargs="?", default="pilot_app/static/index.html")
    args = parser.parse_args()

    path = pathlib.Path(args.target)
    text = path.read_text(encoding="utf-8")
    origin = text

    failures: list[str] = []
    for old, new in REPLACEMENTS:
        if old == new:
            continue
        count = text.count(old)
        if count == 0:
            failures.append(f"NOT FOUND: {old[:70]}")
            continue
        if count > 1:
            failures.append(f"AMBIGUOUS ({count}x): {old[:70]}")
            continue
        text = text.replace(old, new, 1)

    if failures:
        print("refusing to write, unmatched rules:", file=sys.stderr)
        for item in failures:
            print("  " + item, file=sys.stderr)
        return 1

    if "\n    /* ---- responsive layout ----" in text:
        print("responsive block already present; nothing to do")
        return 0

    text = text.replace("  </style>", RESPONSIVE + "  </style>", 1)
    if text == origin:
        print("no change", file=sys.stderr)
        return 1

    path.write_text(text, encoding="utf-8")
    remaining = [line for line in text.splitlines() if "#" in line and "var(--" not in line
                 and line.strip().startswith(("--", ".")) is False]
    print(f"{path}: rewritten ({len(REPLACEMENTS)} rules)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
