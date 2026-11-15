"""Single source of truth for the geolux-radar project version.

Bump VERSION for every release and tag the commit in git:

    git tag -a v<VERSION> -m "geolux-radar <VERSION>"
    git push origin v<VERSION>

Both programs print it: `--version`, the TUI header and every plot title.
"""

VERSION = "1.0.1"
