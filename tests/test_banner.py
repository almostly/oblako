"""Unit tests: the CLI banner shows on a terminal only, in color unless NO_COLOR."""

import io

from oblako import banner


class _Terminal(io.StringIO):
    def isatty(self):
        return True


def test_plain_render_is_the_art_and_tagline():
    text = banner.render(color=False)
    assert banner.ART in text
    assert "real behavior, simulated topology" in text
    assert "\033[" not in text


def test_color_render_paints_letters_blue_and_the_shadow_darker():
    text = banner.render(color=True)
    assert "\033[38;2;73;160;248m█" in text  # oblako blue on the blocks
    assert "\033[38;2;43;94;147m╗" in text  # the darker shadow
    assert text.count("\033[0m") >= len(banner.ART.splitlines())


def test_nothing_is_printed_off_a_terminal():
    out = io.StringIO()  # a pipe or a CI log
    banner.show(out)
    assert out.getvalue() == ""


def test_no_color_prints_the_plain_banner(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    out = _Terminal()
    banner.show(out)
    assert banner.ART in out.getvalue() and "\033[" not in out.getvalue()
