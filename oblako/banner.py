"""The oblako banner: the name in block letters, in oblako blue.

Shown by a bare ``oblako`` and at the start of ``oblako up``, on a terminal only, so
CI logs and pipes stay plain. ``NO_COLOR`` (https://no-color.org) prints it without
color.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

from oblako import __version__

# figlet's "ANSI Shadow" font
ART = """\
 ██████╗ ██████╗ ██╗      █████╗ ██╗  ██╗ ██████╗
██╔═══██╗██╔══██╗██║     ██╔══██╗██║ ██╔╝██╔═══██╗
██║   ██║██████╔╝██║     ███████║█████╔╝ ██║   ██║
██║   ██║██╔══██╗██║     ██╔══██║██╔═██╗ ██║   ██║
╚██████╔╝██████╔╝███████╗██║  ██║██║  ██╗╚██████╔╝
 ╚═════╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝"""

TAGLINE = "real behavior, simulated topology"

BLUE = (0x49, 0xA0, 0xF8)  # oblako blue: the letters
SHADOW = (0x2B, 0x5E, 0x93)  # the same blue, darker: the box-drawing shadow
MUTED = (0x8A, 0x94, 0xA3)  # the tagline


def _rgb(color: tuple[int, int, int]) -> str:
    return "\033[38;2;{};{};{}m".format(*color)


RESET = "\033[0m"


def render(color: bool = True) -> str:
    """Return the banner, with 24-bit color codes unless ``color`` is false."""
    tagline = f"{TAGLINE}  ·  v{__version__}"
    if not color:
        return f"{ART}\n{tagline}\n"
    lines = []
    for line in ART.splitlines():
        out, current = [], None
        for ch in line:
            want = BLUE if ch == "█" else SHADOW if ch.strip() else None
            if want and want != current:
                out.append(_rgb(want))
                current = want
            out.append(ch)
        lines.append("".join(out) + RESET)
    return "\n".join(lines) + f"\n{_rgb(MUTED)}{tagline}{RESET}\n"


def show(stream: TextIO | None = None) -> None:
    """Print the banner if the stream is a terminal; in color unless NO_COLOR is set."""
    stream = stream or sys.stdout
    if not stream.isatty():
        return
    stream.write(render(color=not os.environ.get("NO_COLOR")))
    stream.write("\n")
    stream.flush()
