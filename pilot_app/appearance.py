"""Theme colours the operating system needs *before* the app has painted.

Why this is not in the stylesheet: the address bar and the Android install
splash are drawn from the web app manifest, which the browser fetches before any
page CSS runs -- and the splash is painted by the OS at install time, long
before. The runtime copy in ``app.js`` therefore cannot be the source for it.

So this module is the server-side half of one fact that now lives in four files.
That is exactly the shape of bug this project keeps hitting, so it is not left to
care: ``test_appearance`` reads ``app.js``'s ``THEME_COLORS`` and the ``--bg``
line of every theme block in ``index.html`` and fails if any of them disagrees
with the table below.

Two colours per theme, and they are not the same colour:

* ``theme_color``    -- the browser chrome / Android status bar. Near-black for
  the light themes on purpose: the app's own header is dark in every theme.
* ``background_color`` -- the splash. This is the page background the app is
  about to paint, so it has to follow the light/dark split.
"""

from __future__ import annotations

import json

DEFAULT_THEME = "paper"

# theme -> (theme_color, background_color)
THEME_COLORS: dict[str, tuple[str, str]] = {
    "classic": ("#123b63", "#f3f6f9"),
    "paper": ("#1f1e1b", "#f6f4ef"),
    "dusk": ("#0a1120", "#0a1120"),
    "harbour": ("#243036", "#fbf4ea"),
    "night": ("#0a0c0e", "#08090a"),
}


def colors_for(theme: str) -> tuple[str, str]:
    """The two colours for ``theme``; an unknown theme gets the default's.

    An unknown value must not produce a broken manifest: the profile column has
    a default and the API validates against ``web.THEMES``, but a row written by
    an older version (or edited by hand) must degrade to a working splash rather
    than to a missing ``theme_color``.
    """
    return THEME_COLORS.get(theme or "", THEME_COLORS[DEFAULT_THEME])


def manifest_document(theme: str = DEFAULT_THEME) -> dict:
    """The install metadata, with the colours of ``theme``."""
    theme_color, background_color = colors_for(theme)
    return {
        "name": "CityU Mail Pilot",
        "short_name": "Mail Pilot",
        "start_url": "/app",
        # `scope` 显式写出来（按规范默认就是 start_url 的目录，但写出来更清楚）：
        # iOS 用 scope 判断「还在不在这个应用容器里」，掉出去就甩回浏览器（带地址栏）。
        # 与 `apple-mobile-web-app-capable` 是两条独立的路，**两条都要有**。
        "scope": "/",
        "display": "standalone",
        "background_color": background_color,
        "theme_color": theme_color,
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        ],
    }


def manifest_json(theme: str = DEFAULT_THEME) -> str:
    return json.dumps(manifest_document(theme), ensure_ascii=False, separators=(",", ":"))
