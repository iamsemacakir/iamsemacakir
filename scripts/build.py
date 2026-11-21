#!/usr/bin/env python3
"""Draws the animated SVG images used by the profile README.

    python scripts/build.py assets                        # header, buttons, project cards
    python scripts/build.py activity --out dist           # activity card, needs GITHUB_TOKEN
    python scripts/build.py activity --out dist --sample  # activity card with sample numbers

The words come from scripts/profile.toml. The fonts (SIL Open Font License)
are downloaded from github.com/google/fonts, cut down to the letters each
image uses and embedded, so every image looks the same wherever GitHub shows it.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import itertools
import json
import math
import os
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

import uharfbuzz as hb
from fontTools import subset as ftsubset
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "scripts" / "profile.toml"
CACHE = ROOT / ".cache" / "fonts"
GOOGLE_FONTS = "https://raw.githubusercontent.com/google/fonts/06bfe0d62a98375cb09c942fa18e68bd32cff057/"

FONTS = {  # key: (file in google/fonts, variable font axes, CSS family)
    "hand": ("ofl/caveat/Caveat[wght].ttf", {"wght": 700}, "sema-hand"),
    "sans": ("ofl/inter/Inter[opsz,wght].ttf", {"opsz": 14, "wght": 400}, "sema-sans"),
    "sans-bold": ("ofl/inter/Inter[opsz,wght].ttf", {"opsz": 14, "wght": 600}, "sema-sans-bold"),
    "code": ("ofl/courierprime/CourierPrime-Regular.ttf", None, "sema-code"),
    "code-bold": ("ofl/courierprime/CourierPrime-Bold.ttf", None, "sema-code-bold"),
}

# A notebook page in daylight, and the same page under a desk lamp.
THEMES = {
    "light": {
        "page": "#FDFDFB", "edge": "#DDE3EA", "grid": "#E4EBF5", "margin": "#F2B3B3",
        "rule": "#D5E2F3", "red_rule": "#EFA3A3",
        "ink": "#1E40AF", "accent": "#2563EB", "text": "#1F2328", "muted": "#59636E", "faint": "#8C959F",
        "sticky": "#FDE68A", "sticky_shade": "#F4D35E", "sticky_text": "#433405",
        "red": "#DC2626", "green": "#16A34A", "amber": "#D97706", "pink": "#DB2777",
        "chip": "#EDF2FA", "shadow": 0.08, "note_shadow": 0.20, "ink_soft": "#6179C6", "pencil_line": "#1F2328",
    },
    "dark": {
        "page": "#151C28", "edge": "#2B3647", "grid": "#1F2938", "margin": "#5C2D37",
        "rule": "#25324A", "red_rule": "#62313D",
        "ink": "#9DC1FF", "accent": "#60A5FA", "text": "#E6EDF3", "muted": "#A7B1C0", "faint": "#7D889B",
        "sticky": "#F5D776", "sticky_shade": "#E0BC4E", "sticky_text": "#3A2D05",
        "red": "#F87171", "green": "#4ADE80", "amber": "#FBBF24", "pink": "#F472B6",
        "chip": "#1E2839", "shadow": 0.0, "note_shadow": 0.55, "ink_soft": "#7894C4", "pencil_line": "#0B0F17",
    },
}
HIGHLIGHTERS = ["#93C5FD", "#FCD34D", "#F9A8D4", "#86EFAC", "#C4B5FD", "#FDBA74", "#5EEAD4", "#FDA4AF",
                "#BEF264", "#A5B4FC", "#7DD3FC", "#F0ABFC", "#FDE68A", "#99F6E4"]

REDUCED_MOTION = "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"
SHARED_CSS = (
    "@keyframes draw{from{stroke-dashoffset:1}}"
    "@keyframes ink{from{fill-opacity:0}}"
    "@keyframes pop{from{opacity:0}}"
    "@keyframes fade{from{opacity:0;transform:translateY(6px)}}"
    "@keyframes grow{from{transform:scaleX(0)}}"
    ".grow{transform-box:fill-box;transform-origin:0 50%;animation:grow .6s cubic-bezier(.3,.7,.2,1) both}"
    ".edge{stroke-dasharray:1 2;animation:draw .7s ease-in-out both}"
)


def num(value: float) -> str:
    text = f"{value:.1f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def secs(value: float) -> str:
    return f"{value:.2f}s"


# Fonts ---------------------------------------------------------------------

def font_file(key: str) -> Path:
    source, axes, _ = FONTS[key]
    CACHE.mkdir(parents=True, exist_ok=True)
    raw = CACHE / Path(source).name
    if not raw.exists():
        url = GOOGLE_FONTS + urllib.parse.quote(source, safe="/,")
        with urllib.request.urlopen(url, timeout=120) as response:
            raw.write_bytes(response.read())
    if not axes:
        return raw
    static = CACHE / f"{key}.ttf"
    if not static.exists():
        instancer.instantiateVariableFont(TTFont(raw), axes).save(static)
    return static


class Font:
    _loaded: dict[str, "Font"] = {}

    def __init__(self, key: str):
        self.family = FONTS[key][2]
        self.path = font_file(key)
        face = hb.Face(hb.Blob.from_file_path(str(self.path)))
        self.hb_font = hb.Font(face)
        self.upem = face.upem
        tt = TTFont(self.path)
        self.glyphs = tt.getGlyphSet()
        self.order = tt.getGlyphOrder()

    @classmethod
    def get(cls, key: str) -> "Font":
        if key not in cls._loaded:
            cls._loaded[key] = cls(key)
        return cls._loaded[key]

    def shape(self, text: str):
        buffer = hb.Buffer()
        buffer.add_str(text)
        buffer.guess_segment_properties()
        hb.shape(self.hb_font, buffer)
        return list(zip(buffer.glyph_infos, buffer.glyph_positions))

    def width(self, text: str, size: float) -> float:
        return sum(pos.x_advance for _, pos in self.shape(text)) * size / self.upem

    def outlines(self, text: str, size: float, x: float, y: float) -> list[str]:
        """Path data for each visible glyph, the baseline starting at (x, y)."""
        scale = size / self.upem
        advance = 0
        paths = []
        for info, pos in self.shape(text):
            pen = SVGPathPen(self.glyphs, ntos=num)
            origin_x = x + (advance + pos.x_offset) * scale
            origin_y = y - pos.y_offset * scale
            glyph = self.glyphs[self.order[info.codepoint]]
            glyph.draw(TransformPen(pen, (scale, 0, 0, -scale, origin_x, origin_y)))
            if commands := pen.getCommands():
                paths.append(commands)
            advance += pos.x_advance
        return paths


def subset_woff2(path: Path, text: str) -> bytes:
    options = ftsubset.Options()
    options.flavor = "woff2"
    options.layout_features = ["*"]
    options.hinting = False
    options.name_IDs = [0, 1, 2, 3, 4, 5, 6, 13, 14]
    font = ftsubset.load_font(str(path), options)
    subsetter = ftsubset.Subsetter(options)
    subsetter.populate(text=text)
    subsetter.subset(font)
    buffer = io.BytesIO()
    ftsubset.save_font(font, buffer, options)
    return buffer.getvalue()


def wrap(font: Font, text: str, size: float, max_width: float) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if line and font.width(candidate, size) > max_width:
            lines.append(line)
            line = word
        else:
            line = candidate
    return lines + [line] if line else lines


def balanced(font: Font, text: str, size: float, max_width: float) -> list[str]:
    """As few lines as wrap() needs, with the words shared out so no line is left nearly empty."""
    words, count = text.split(), len(wrap(font, text, size, max_width))
    best = None
    for breaks in itertools.combinations(range(1, len(words)), count - 1):
        lines = [" ".join(words[a:b]) for a, b in zip((0,) + breaks, breaks + (len(words),))]
        widest = max(font.width(line, size) for line in lines)
        if widest <= max_width and (best is None or widest < best[0]):
            best = (widest, lines)
    return best[1] if best else wrap(font, text, size, max_width)


# Canvas --------------------------------------------------------------------

class Canvas:
    def __init__(self, width: float, height: float, title: str):
        self.width, self.height, self.title = width, height, title
        self.defs: list[str] = []
        self.body: list[str] = []
        self.css: list[str] = [SHARED_CSS]
        self.chars: dict[str, set[str]] = {}

    def add(self, *parts: str) -> None:
        self.body.extend(parts)

    def text(self, key: str, value: str, x: float, y: float, size: float, fill: str,
             anchor: str = "start", cls: str = "", style: str = "") -> str:
        self.chars.setdefault(key, set()).update(value)
        extra = f' text-anchor="{anchor}"' if anchor != "start" else ""
        extra += f' class="{cls}"' if cls else ""
        extra += f' style="{style}"' if style else ""
        return (f'<text x="{num(x)}" y="{num(y)}" font-family="{Font.get(key).family}" '
                f'font-size="{num(size)}" fill="{fill}"{extra}>{escape(value)}</text>')

    def render(self) -> str:
        faces = "".join(
            "@font-face{font-family:%s;src:url(data:font/woff2;base64,%s) format('woff2')}"
            % (Font.get(key).family,
               base64.b64encode(subset_woff2(Font.get(key).path, "".join(sorted(chars)))).decode())
            for key, chars in sorted(self.chars.items()))
        label = escape(self.title, {'"': "&quot;"})
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{num(self.width)}" height="{num(self.height)}" '
                f'viewBox="0 0 {num(self.width)} {num(self.height)}" role="img" aria-label="{label}">'
                f'<title>{escape(self.title)}</title><style>{faces}{"".join(self.css)}{REDUCED_MOTION}</style>'
                f'<defs>{"".join(self.defs)}</defs>{"".join(self.body)}</svg>\n')


def page(c: Canvas, t: dict, x: float, y: float, w: float, h: float, grid: bool = True, step: int = 24) -> None:
    """A sheet of paper: squared like a maths notebook, or plain for index cards."""
    if t["shadow"]:
        c.defs.append(f'<filter id="lift" x="-5%" y="-5%" width="110%" height="115%">'
                      f'<feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#0F172A" '
                      f'flood-opacity="{t["shadow"]}"/></filter>')
    lift = ' filter="url(#lift)"' if t["shadow"] else ""
    c.add(f'<rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" rx="10" fill="{t["page"]}"{lift}/>')
    if grid:
        c.defs.append(f'<pattern id="squares" x="{num(x)}" y="{num(y)}" width="{step}" height="{step}" '
                      f'patternUnits="userSpaceOnUse"><path d="M{step} 0H0V{step}" fill="none" '
                      f'stroke="{t["grid"]}" stroke-width="1"/></pattern>')
        c.add(f'<rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" rx="10" fill="url(#squares)"/>')
    c.add(f'<rect x="{num(x + .5)}" y="{num(y + .5)}" width="{num(w - 1)}" height="{num(h - 1)}" rx="9.5" '
          f'fill="none" stroke="{t["edge"]}"/>')


# Header --------------------------------------------------------------------

def header(cfg: dict, theme: str) -> str:
    t = THEMES[theme]
    person = cfg["person"]
    c = Canvas(1000, 304, f'{person["greetings"][0]} {person["name"]}. {person["tagline"]}')
    page(c, t, 8, 8, 984, 288)
    c.add(f'<path d="M104 9V295" stroke="{t["margin"]}" stroke-width="1.5"/>')
    c.css.append(
        f".w{{fill:{t['ink']};stroke:{t['ink']};stroke-width:1.1;stroke-linejoin:round;stroke-dasharray:1 2;"
        "animation:draw .75s cubic-bezier(.5,.1,.3,1) both,ink .5s ease-out both}"
        ".soft{opacity:.7}"
        ".k{animation:pop 1ms both}"
        ".fade{animation:fade .7s ease-out both}"
        "@keyframes blink{50%{opacity:0}}"
        ".cursor{animation:pop 1ms both,blink 1.1s steps(1) infinite}"
        "@keyframes drop{from{opacity:0;transform:translateY(-26px) rotate(-6deg) scale(1.05)}60%{opacity:1}}"
        "@keyframes float{50%{transform:translateY(-5px) rotate(-.7deg)}}"
        ".drop{transform-box:fill-box;transform-origin:50% 0;animation:drop .8s cubic-bezier(.2,.9,.3,1.15) both}"
        ".float{transform-box:fill-box;transform-origin:50% 0;animation:float 7s ease-in-out infinite}"
        ".heart{stroke-dasharray:1 2;animation:draw .9s ease-in-out both}"
    )

    hand = Font.get("hand")
    left = 134
    clock = 0.2
    writing = []
    # The greeting and the name write themselves, letter by letter.
    # "Hi, I'm" first, then the same greeting in Bulgarian, Turkish and German, over and over.
    greetings, window = person["greetings"], 5.0
    cycle = window * len(greetings)

    def at(seconds: float) -> str:
        return f"{100 * seconds / cycle:.2f}%"

    c.css.append(
        f"@keyframes greet{{0%{{stroke-dashoffset:1;fill-opacity:0;opacity:1}}{at(.45)}{{fill-opacity:0}}"
        f"{at(.9)}{{stroke-dashoffset:0}}{at(1.15)}{{fill-opacity:1}}{at(4.3)}{{opacity:1}}"
        f"{at(4.75)},100%{{opacity:0;stroke-dashoffset:0;fill-opacity:1}}}}"
        f".gr{{fill:{t['ink_soft']};stroke:{t['ink_soft']};stroke-width:1;stroke-linejoin:round;"
        f"stroke-dasharray:1 2;opacity:0;animation:greet {num(cycle)}s linear infinite both}}.gr.first{{opacity:1}}")
    for k, greeting in enumerate(greetings):
        for i, d in enumerate(hand.outlines(greeting, 40, left + 4, 62)):
            writing.append(f'<path class="gr{" first" if k == 0 else ""}" pathLength="1" d="{d}" '
                           f'style="animation-delay:{secs(clock + window * k + .06 * i)}"/>')
    clock += .06 * len(hand.outlines(greetings[0], 40, 0, 0)) + .25
    for d in hand.outlines(person["name"], 104, left - 4, 140):
        writing.append(f'<path class="w" pathLength="1" d="{d}" '
                       f'style="animation-delay:{secs(clock)},{secs(clock + .45)}"/>')
        clock += .13
    c.add(*writing)

    clock += .35
    sans = Font.get("sans")
    lines = wrap(sans, person["tagline"], 20, 500)
    for i, line in enumerate(lines[:2]):
        c.add(c.text("sans", line, left, 186 + 27 * i, 20, t["text"], cls="fade",
                     style=f"animation-delay:{secs(clock + .12 * i)}"))

    # A terminal line types the current focus.
    clock += .8
    code_size = 17
    step = Font.get("code").width("0", code_size)
    first, second = 252, 276
    c.add(c.text("code-bold", "~ $", left, first, code_size, t["accent"], cls="k",
                 style=f"animation-delay:{secs(clock)}"))
    clock += .35
    for i, ch in enumerate(person["now_command"]):
        if ch != " ":
            c.add(c.text("code", ch, left + step * (4 + i), first, code_size, t["muted"], cls="k",
                         style=f"animation-delay:{secs(clock)}"))
        clock += .065
    clock += .4
    for i, ch in enumerate(person["now"]):
        if ch != " ":
            c.add(c.text("code", ch, left + step * i, second, code_size, t["text"], cls="k",
                         style=f"animation-delay:{secs(clock)}"))
        clock += .022
    c.add(f'<rect class="cursor" x="{num(left + step * len(person["now"]) + 3)}" y="{second - 13}" '
          f'width="{num(step * .85)}" height="16" fill="{t["accent"]}" '
          f'style="animation-delay:{secs(clock)},{secs(clock)}"/>')

    # The sticky note lands on the page while the name is being written.
    c.defs.append(
        f'<filter id="note-shadow" x="-20%" y="-20%" width="140%" height="150%">'
        f'<feDropShadow dx="0" dy="7" stdDeviation="7" flood-color="#000" flood-opacity="{t["note_shadow"]}"/></filter>'
        f'<linearGradient id="note-paper" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{t["sticky_shade"]}"/><stop offset=".16" stop-color="{t["sticky"]}"/>'
        f'<stop offset="1" stop-color="{t["sticky"]}"/></linearGradient>')
    nw, nh = 240, 178
    note_lines = "".join(
        c.text("hand", line, 0, -22 + 40 * i, 34, t["sticky_text"], anchor="middle")
        for i, line in enumerate(person["note"]))
    heart = ("M0 4.2C-1.5-2.8 7-4.3 8.2 2.1C9.7-4.4 18.2-2.6 16.4 4.3C15.2 9.4 10.5 12.5 8 16"
             "C5.6 12.4 0.9 9.6 0 4.2Z")
    c.add(
        f'<g transform="translate(822 150) rotate(3.2)">'
        f'<g class="drop" style="animation-delay:.9s"><g class="float" style="animation-delay:1.8s">'
        f'<rect x="{-nw / 2}" y="{-nh / 2}" width="{nw}" height="{nh}" rx="2" fill="url(#note-paper)" '
        f'filter="url(#note-shadow)"/>'
        f'<path d="M{nw / 2} {nh / 2 - 22}L{nw / 2 - 22} {nh / 2}H{nw / 2}Z" fill="#000" fill-opacity=".06"/>'
        f'{note_lines}'
        f'<path class="heart" pathLength="1" d="{heart}" transform="translate(78 52) rotate(-8) scale(1.25)" '
        f'fill="none" stroke="{t["pink"]}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
        f'style="animation-delay:{secs(clock - .4)}"/>'
        f'</g></g></g>')
    return c.render()


# Link buttons --------------------------------------------------------------

ICONS = {  # 24 x 24, drawn with a 1.9 px pen
    "globe": '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/>'
             '<path d="M3 12h18M4.7 7.5h14.6M4.7 16.5h14.6"/>',
    "briefcase": '<rect x="3" y="7.5" width="18" height="12.5" rx="2.5"/>'
                 '<path d="M8.5 7.5V5.9A1.9 1.9 0 0 1 10.4 4h3.2a1.9 1.9 0 0 1 1.9 1.9v1.6M3 13h18"/>',
    "envelope": '<rect x="3" y="5.5" width="18" height="13" rx="2.5"/><path d="m3.9 7.2 8.1 5.9 8.1-5.9"/>',
    "camera": '<path d="M4.3 8.2h2.9l1.6-2.7h6.4l1.6 2.7h2.9a1.3 1.3 0 0 1 1.3 1.3v8.7a1.3 1.3 0 0 1-1.3 1.3H4.3'
              'A1.3 1.3 0 0 1 3 18.2V9.5a1.3 1.3 0 0 1 1.3-1.3z"/><circle cx="12" cy="13.4" r="3.5"/>',
}


def link_button(link: dict, theme: str) -> str:
    t = THEMES[theme]
    size, height = 15.5, 42
    width = round(17 + 20 + 9 + Font.get("sans-bold").width(link["label"], size) + 19)
    c = Canvas(width, height, link["label"])
    c.add(f'<rect x=".75" y=".75" width="{width - 1.5}" height="{height - 1.5}" rx="{num((height - 1.5) / 2)}" '
          f'fill="{t["page"]}" stroke="{t["ink"]}" stroke-width="1.5"/>',
          f'<g transform="translate(17 {num((height - 20) / 2)}) scale(.8333)" fill="none" stroke="{t["ink"]}" '
          f'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">{ICONS[link["icon"]]}</g>',
          c.text("sans-bold", link["label"], 46, height / 2 + size * .36, size, t["ink"]))
    return c.render()


# Project cards ---------------------------------------------------------------

def card(project: dict, theme: str) -> str:
    t = THEMES[theme]
    c = Canvas(500, 252, f'{project["title"]}: {project["about"]}')
    page(c, t, 6, 6, 488, 240, grid=False)
    rules = "".join(f"M7 {y}H493" for y in range(100, 231, 26))
    c.add(f'<path d="M7 74H493" stroke="{t["red_rule"]}" stroke-width="1.4"/>',
          f'<path d="{rules}" stroke="{t["rule"]}" stroke-width="1"/>',
          c.text("hand", project["title"], 26, 57, 44, t["ink"]))
    lines = wrap(Font.get("sans"), project["about"], 16.5, 296)
    if len(lines) > 4:
        raise SystemExit(f'{project["title"]}: the "about" text needs {len(lines)} lines, 4 fit. Shorten it.')
    for i, line in enumerate(lines):
        c.add(c.text("sans", line, 26, 94 + 26 * i, 16.5, t["text"]))
    x = 26
    for tag in project["tags"]:
        w = Font.get("code").width(tag, 13.5) + 14
        c.add(f'<rect x="{num(x)}" y="206" width="{num(w)}" height="22" rx="5" fill="{t["chip"]}"/>',
              c.text("code", tag, x + 7, 221.5, 13.5, t["muted"]))
        x += w + 8
    PICTURES[project["picture"]](c, t, project, 338, 88, 138, 112)
    return c.render()


def schedule(c: Canvas, t: dict, project: dict, bx: float, by: float, bw: float, bh: float) -> None:
    """Bays on a 15-minute grid; the bookings fill in and a red 'now' line walks the day."""
    hours = "".join(f"M{num(bx + bw * f)} {by}V{by + bh}" for f in (0, .25, .5, .75, 1))
    c.add(f'<path d="{hours}" stroke="{t["ink"]}" stroke-opacity=".28" stroke-dasharray="2 4"/>')
    bookings = [(0, 0, .36, 0), (0, .45, .82, 1), (1, .1, .55, 2), (1, .63, .97, 3),
                (2, 0, .27, 1), (2, .35, .72, 4), (3, .2, .5, 0), (3, .58, .88, 5)]
    for i, (row, start, end, colour) in enumerate(bookings):
        y = by + 4 + row * 27
        c.add(f'<rect class="grow" x="{num(bx + bw * start)}" y="{num(y)}" width="{num(bw * (end - start))}" '
              f'height="17" rx="4" fill="{HIGHLIGHTERS[colour]}" stroke="{t["ink"]}" stroke-width="1.2" '
              f'style="animation-delay:{secs(.35 + .09 * i)}"/>')
    c.css.append(f"@keyframes now{{from{{transform:translateX(0)}}to{{transform:translateX({num(bw)}px)}}}}"
                 f".now{{transform:translateX({num(bw * .42)}px);animation:now 16s linear 1.2s infinite both}}")
    c.add(f'<g class="now"><path d="M{num(bx)} {by - 3}V{by + bh}" stroke="{t["red"]}" stroke-width="1.6"/>'
          f'<circle cx="{num(bx)}" cy="{by - 3}" r="3.2" fill="{t["red"]}"/></g>')


def service_map(c: Canvas, t: dict, project: dict, bx: float, by: float, bw: float, bh: float) -> None:
    """Services and what they depend on; now and then one turns amber and recovers."""
    nodes = {"gateway": (.1, .46), "api": (.47, .46), "auth": (.87, .1), "db": (.87, .82), "jobs": (.47, .96)}
    at = {k: (bx + bw * fx, by + bh * fy) for k, (fx, fy) in nodes.items()}
    for i, (a, b) in enumerate([("gateway", "api"), ("api", "auth"), ("api", "db"), ("api", "jobs"),
                                ("jobs", "db")]):
        (x1, y1), (x2, y2) = at[a], at[b]
        c.add(f'<path class="edge" pathLength="1" d="M{num(x1)} {num(y1)}L{num(x2)} {num(y2)}" '
              f'stroke="{t["ink"]}" stroke-width="1.3" style="animation-delay:{secs(.3 + .12 * i)}"/>')
    c.css.append(
        "@keyframes node{from{opacity:0;transform:scale(.6)}}"
        ".node{transform-box:fill-box;transform-origin:50% 50%;animation:node .45s cubic-bezier(.3,.7,.3,1.3) both}"
        f"@keyframes warn{{0%,58%,92%,100%{{fill:{t['green']}}}64%,86%{{fill:{t['amber']}}}}}"
        ".warn{animation:warn 8s 2s infinite}"
        "@keyframes ring{0%,62%{opacity:0;transform:scale(1)}66%{opacity:.9}86%,100%{opacity:0;transform:scale(3.2)}}"
        ".ring{transform-box:fill-box;transform-origin:50% 50%;opacity:0;animation:ring 8s 2s infinite}")
    for i, (name, (x, y)) in enumerate(at.items()):
        dot = "warn" if name == "db" else ""
        ring = (f'<circle class="ring" cx="{num(x + 16)}" cy="{num(y - 10)}" r="3.6" fill="none" '
                f'stroke="{t["amber"]}" stroke-width="1.4"/>') if dot else ""
        c.add(f'<g class="node" style="animation-delay:{secs(.15 + .1 * i)}">'
              f'<rect x="{num(x - 16)}" y="{num(y - 10)}" width="32" height="20" rx="6" fill="{t["page"]}" '
              f'stroke="{t["ink"]}" stroke-width="1.3"/>'
              f'<path d="M{num(x - 9)} {num(y - 2.5)}h14M{num(x - 9)} {num(y + 3.5)}h9" stroke="{t["faint"]}" '
              f'stroke-width="1.6" stroke-linecap="round"/>'
              f'{ring}<circle class="{dot}" cx="{num(x + 16)}" cy="{num(y - 10)}" r="3.6" fill="{t["green"]}" '
              f'stroke="{t["page"]}" stroke-width="1.5"/></g>')


def flashcard(c: Canvas, t: dict, project: dict, bx: float, by: float, bw: float, bh: float) -> None:
    """A flashcard that flips between two languages, and Sevi typing an answer."""
    front, back = project.get("flashcard", ["Здравей!", "Hello!"])
    cx, cy, w, h = bx + bw * .44, by + bh * .38, 116, 68
    c.css.append(
        "@keyframes front{0%,42%,100%{transform:scaleX(1)}46%,96%{transform:scaleX(0)}}"
        "@keyframes back{0%,46%,96%,100%{transform:scaleX(0)}50%,92%{transform:scaleX(1)}}"
        ".side{transform-box:fill-box;transform-origin:50% 50%}"
        ".front{animation:front 9s 2s infinite both}"
        ".back{transform:scaleX(0);animation:back 9s 2s infinite both}"
        "@keyframes dot{0%,60%,100%{opacity:.25}30%{opacity:1}}"
        ".dot{animation:dot 1.4s infinite}")

    def side(word: str, fill: str, cls: str) -> str:
        return (f'<g class="side {cls}"><rect x="{num(cx - w / 2)}" y="{num(cy - h / 2)}" width="{w}" height="{h}" '
                f'rx="7" fill="{fill}" stroke="{t["ink"]}" stroke-width="1.3"/>'
                + c.text("hand", word, cx, cy + 10, 30, t["ink"], anchor="middle") + "</g>")

    c.add(f'<g transform="rotate(-5 {num(cx)} {num(cy)})">{side(front, t["page"], "front")}'
          f'{side(back, HIGHLIGHTERS[1] if theme_is_light(t) else "#3A3420", "back")}</g>')
    x, y = bx + bw - 54, by + bh - 28
    dots = "".join(f'<circle class="dot" cx="{num(x + 15 + 10 * i)}" cy="{num(y + 13)}" r="2.6" '
                   f'fill="{t["ink"]}" style="animation-delay:{secs(.2 * i)}"/>' for i in range(3))
    c.add(f'<path d="M{num(x + 12)} {num(y)}h28a12 12 0 0 1 0 26h-28a12 12 0 0 1-9-4l-5 4 1-7a12 12 0 0 1 13-19z" '
          f'fill="{t["chip"]}" stroke="{t["ink"]}" stroke-width="1.2"/>{dots}')


def theme_is_light(t: dict) -> bool:
    return t is THEMES["light"]


def route(c: Canvas, t: dict, project: dict, bx: float, by: float, bw: float, bh: float) -> None:
    """A dotted route between two pins with a paper plane flying it."""
    (x0, y0), (x1, y1), (x2, y2) = (bx + 12, by + bh - 6), (bx + bw * .12, by + 4), (bx + bw - 14, by + 24)

    def point(s: float) -> tuple[float, float, float]:
        x = (1 - s) ** 2 * x0 + 2 * (1 - s) * s * x1 + s * s * x2
        y = (1 - s) ** 2 * y0 + 2 * (1 - s) * s * y1 + s * s * y2
        dx = 2 * (1 - s) * (x1 - x0) + 2 * s * (x2 - x1)
        dy = 2 * (1 - s) * (y1 - y0) + 2 * s * (y2 - y1)
        return x, y, math.degrees(math.atan2(dy, dx))

    c.add(f'<path d="M{num(x0)} {num(y0)}Q{num(x1)} {num(y1)} {num(x2)} {num(y2)}" fill="none" '
          f'stroke="{t["ink"]}" stroke-width="1.4" stroke-dasharray="1 6" stroke-linecap="round"/>')
    pin = "M0 0C-6-8-8.5-11.5-8.5-15.5a8.5 8.5 0 0 1 17 0C8.5-11.5 6-8 0 0Z"
    for (x, y), fill in (((x0, y0), HIGHLIGHTERS[0]), ((x2, y2), HIGHLIGHTERS[2])):
        c.add(f'<g transform="translate({num(x)} {num(y + 3)})"><path d="{pin}" fill="{fill}" '
              f'stroke="{t["ink"]}" stroke-width="1.3"/><circle cy="-15.5" r="3" fill="{t["page"]}" '
              f'stroke="{t["ink"]}" stroke-width="1.2"/></g>')
    frames = []
    for i in range(21):
        s = .06 + .86 * i / 20
        x, y, angle = point(s)
        frames.append(f"{num(4 + 66 * i / 20)}%{{transform:translate({num(x)}px,{num(y - 10)}px) rotate({num(angle)}deg)}}")
    x, y, angle = point(.06)
    rest_x, rest_y, rest_angle = point(.55)
    c.css.append(
        "@keyframes fly{0%%{opacity:0;transform:translate(%spx,%spx) rotate(%sdeg)}4%%{opacity:1}%s"
        "76%%{opacity:0}100%%{opacity:0}}" % (num(x), num(y - 10), num(angle), "".join(frames))
        + ".plane{transform:translate(%spx,%spx) rotate(%sdeg);animation:fly 7s ease-in-out .6s infinite both}"
        % (num(rest_x), num(rest_y - 10), num(rest_angle)))
    c.add(f'<g class="plane"><path d="M-9-6.5 11 0-9 6.5-4.5 0Z" fill="{t["page"]}" stroke="{t["ink"]}" '
          f'stroke-width="1.3" stroke-linejoin="round"/><path d="M-4.5 0H11" stroke="{t["ink"]}" '
          f'stroke-width="1"/></g>')


PICTURES = {"schedule": schedule, "service-map": service_map, "flashcard": flashcard, "route": route}


# Journey ---------------------------------------------------------------------

def smooth_path(points: list[tuple[float, float]]):
    """A Catmull-Rom curve through the points, as SVG path data and cubic segments."""
    segments = []
    for i in range(len(points) - 1):
        p0, p1, p2 = points[max(i - 1, 0)], points[i], points[i + 1]
        p3 = points[min(i + 2, len(points) - 1)]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        segments.append((p1, c1, c2, p2))
    d = f"M{num(points[0][0])} {num(points[0][1])}" + "".join(
        f"C{num(a[0])} {num(a[1])} {num(b[0])} {num(b[1])} {num(e[0])} {num(e[1])}" for _, a, b, e in segments)
    return d, segments


def walk(segments, per_segment: int = 48):
    """Points along the curve, their running length, and the length at the end of each segment."""
    points, lengths, ends = [segments[0][0]], [0.0], []
    for (x0, y0), (x1, y1), (x2, y2), (x3, y3) in segments:
        for j in range(1, per_segment + 1):
            s = j / per_segment
            u = 1 - s
            p = (u ** 3 * x0 + 3 * u * u * s * x1 + 3 * u * s * s * x2 + s ** 3 * x3,
                 u ** 3 * y0 + 3 * u * u * s * y1 + 3 * u * s * s * y2 + s ** 3 * y3)
            lengths.append(lengths[-1] + math.dist(points[-1], p))
            points.append(p)
        ends.append(lengths[-1])
    return points, lengths, ends


def point_at(points, lengths, target: float) -> tuple[float, float]:
    for i in range(1, len(lengths)):
        if lengths[i] >= target:
            f = (target - lengths[i - 1]) / ((lengths[i] - lengths[i - 1]) or 1)
            return (points[i - 1][0] + f * (points[i][0] - points[i - 1][0]),
                    points[i - 1][1] + f * (points[i][1] - points[i - 1][1]))
    return points[-1]


def pencil(t: dict) -> str:
    """A yellow pencil with its point at (0, 0), held at an angle."""
    line = f'stroke="{t["pencil_line"]}" stroke-width="1.2" stroke-linejoin="round"'
    return (f'<g transform="rotate(-38)">'
            f'<path d="M0 0 13-5V5Z" fill="#F3D2AC" {line}/>'
            f'<path d="M0 0 4.6-1.8V1.8Z" fill="{t["pencil_line"]}"/>'
            f'<rect x="13" y="-5" width="38" height="10" fill="#FCD34D" {line}/>'
            f'<path d="M13-1.7H51M13 1.7H51" stroke="#D9A514" stroke-width=".8"/>'
            f'<rect x="51" y="-5.4" width="6" height="10.8" fill="#CBD5E1" {line}/>'
            f'<path d="M57-5h4.6a3 3 0 0 1 3 3v4a3 3 0 0 1-3 3H57Z" fill="#F9A8D4" {line}/></g>')


def journey(cfg: dict, theme: str) -> str:
    """A pencil draws the timeline and each milestone pops up as the line reaches it.
    The drawing starts a moment after the picture appears and repeats every 16 seconds,
    so whoever scrolls down to it sees the pencil at work."""
    t = THEMES[theme]
    items = cfg["journey"]
    c = Canvas(1000, 250, "My journey. " + " ".join(f'{i["when"]}: {i["what"]}.' for i in items))
    page(c, t, 8, 8, 984, 234)
    xs = [118 + 764 * k / (len(items) - 1) for k in range(len(items))]
    ys = [126 + (-11 if k % 2 == 0 else 11) for k in range(len(items))]
    d, segments = smooth_path([(52, 134)] + list(zip(xs, ys)) + [(912, 118)])
    points, lengths, ends = walk(segments)
    start, duration, cycle = 1.0, 4.6, 16.0
    fade_from, fade_to = cycle - 1.4, cycle - .7

    def at(seconds: float) -> str:
        return f"{100 * seconds / cycle:.2f}%"

    def spot(fraction: float) -> str:
        x, y = point_at(points, lengths, lengths[-1] * fraction)
        return f"translate({num(x)}px,{num(y)}px)"

    first = spot(0)
    resting = f"translate({num(points[-1][0] + 2)}px,{num(points[-1][1] - 7)}px)"
    pencil_frames = (
        f"0%{{transform:{first};opacity:1}}{at(start)}{{transform:{first}}}"
        + "".join(f"{at(start + duration * i / 40)}{{transform:{spot(i / 40)}}}" for i in range(1, 41))
        + f"{at(start + duration + .4)}{{transform:{resting}}}{at(fade_from)}{{transform:{resting};opacity:1}}"
        f"{at(fade_to)}{{transform:{resting};opacity:0}}{at(cycle - .3)}{{transform:{first};opacity:0}}"
        f"100%{{transform:{first};opacity:1}}")
    c.css.append(
        f"@keyframes pencil{{{pencil_frames}}}"
        f".pencil{{transform:{resting};animation:pencil {num(cycle)}s linear infinite}}"
        f"@keyframes trail{{0%,{at(start)}{{stroke-dashoffset:1;opacity:1}}{at(start + duration)}{{stroke-dashoffset:0}}"
        f"{at(fade_from)}{{opacity:1}}{at(fade_to)},100%{{stroke-dashoffset:0;opacity:0}}}}"
        f".trail{{stroke-dasharray:1 2;animation:trail {num(cycle)}s linear infinite}}"
        ".dot{transform-box:fill-box;transform-origin:50% 50%}"
        ".rise{animation:fade .6s ease-out both}"
        "@keyframes pulse{0%{opacity:.8;transform:scale(1)}70%,100%{opacity:0;transform:scale(2.6)}}"
        ".pulse{transform-box:fill-box;transform-origin:50% 50%;opacity:0;animation:pulse 2.4s ease-out infinite}")
    c.add(f'<path class="trail" pathLength="1" d="{d}" fill="none" stroke="{t["ink"]}" stroke-width="2.4" '
          f'stroke-linecap="round"/>')
    sans = Font.get("sans")
    for k, item in enumerate(items):
        x, y = xs[k], ys[k]
        reached = start + duration * ends[k] / lengths[-1]
        c.css.append(
            f"@keyframes dot{k}{{0%,{at(reached)}{{opacity:0;transform:scale(.3)}}"
            f"{at(reached + .3)}{{opacity:1;transform:scale(1.15)}}{at(reached + .45)}{{transform:scale(1)}}"
            f"{at(fade_from)}{{opacity:1;transform:scale(1)}}{at(fade_to)},100%{{opacity:0;transform:scale(1)}}}}"
            f".dot{k}{{animation:dot{k} {num(cycle)}s linear infinite}}")
        if k == len(items) - 1:
            c.add(f'<circle class="pulse" cx="{num(x)}" cy="{num(y)}" r="9" fill="none" stroke="{t["accent"]}" '
                  f'stroke-width="2" style="animation-delay:{secs(reached + .5)}"/>')
        c.add(f'<circle class="dot dot{k}" cx="{num(x)}" cy="{num(y)}" r="9" '
              f'fill="{HIGHLIGHTERS[k % len(HIGHLIGHTERS)]}" stroke="{t["ink"]}" stroke-width="2"/>',
              c.text("hand", item["when"], x, 86, 34, t["ink"], anchor="middle", cls="rise",
                     style=f"animation-delay:{secs(reached)}"))
        for j, line in enumerate(balanced(sans, item["what"], 13.5, 172)[:3]):
            c.add(c.text("sans", line, x, 176 + 18 * j, 13.5, t["text"], anchor="middle", cls="rise",
                         style=f"animation-delay:{secs(reached + .1)}"))
    c.add(f'<g class="pencil">{pencil(t)}</g>')
    return c.render()


# Year card -------------------------------------------------------------------

def year_card(days: list, theme: str) -> str:
    """The contribution calendar coloured in by pen on squared paper; a red pen circles the longest streak."""
    t = THEMES[theme]
    days = sorted(days)
    sunday = dt.date.fromisoformat(days[0][0])
    sunday -= dt.timedelta(days=(sunday.weekday() + 1) % 7)
    place = {}
    for date_text, count in days:
        date = dt.date.fromisoformat(date_text)
        place[date_text] = ((date - sunday).days // 7, (date.weekday() + 1) % 7, count, date)
    columns = max(col for col, *_ in place.values()) + 1
    counts = sorted(count for _, _, count, _ in place.values() if count)
    steps = [counts[int(len(counts) * f)] for f in (.25, .5, .75)] if counts else [0, 0, 0]
    total = sum(count for _, count in days)
    c = Canvas(1000, 240, f"My contribution calendar for the last 12 months, {total} contributions, coloured in by pen.")
    page(c, t, 8, 8, 984, 224, step=16)
    x0, y0, cycle = 88, 56, 18.0

    def at(seconds: float) -> str:
        return f"{100 * seconds / cycle:.2f}%"

    hold, gone = at(12.6), at(13.2)
    c.css.append(
        f"@keyframes colour{{0%{{stroke-dashoffset:1;opacity:1}}{at(.6)}{{stroke-dashoffset:0}}{hold}{{opacity:1}}"
        f"{gone},100%{{opacity:0;stroke-dashoffset:0}}}}"
        f".scribble{{stroke-dasharray:1 2;animation:colour {num(cycle)}s linear infinite both}}"
        f"@keyframes wash{{0%{{opacity:0}}{at(.6)}{{opacity:1}}{hold}{{opacity:1}}{gone},100%{{opacity:0}}}}"
        f".wash{{animation:wash {num(cycle)}s linear infinite both}}"
)

    labelled = -9
    for _, (col, row, count, date) in sorted(place.items()):
        if date.day == 1 and col - labelled >= 3:
            c.add(c.text("sans", f"{date:%b}", x0 + col * 16 + 2, y0 - 9, 11.5, t["faint"]))
            labelled = col
    for row, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        c.add(c.text("sans", name, x0 - 8, y0 + row * 16 + 12, 11, t["faint"], anchor="end"))

    for _, (col, row, count, date) in sorted(place.items()):
        x, y = x0 + col * 16 + 2, y0 + row * 16 + 2
        c.add(f'<rect x="{x}" y="{y}" width="12" height="12" rx="2" fill="{t["chip"]}"/>')
        if not count:
            continue
        level = 1 + sum(count > step for step in steps)
        delay = secs(.3 + 4.2 * col / columns)
        if level >= 3:
            c.add(f'<rect class="wash" x="{x}" y="{y}" width="12" height="12" rx="2" fill="{t["ink"]}" '
                  f'fill-opacity="{(.2, .36)[level - 3]}" style="animation-delay:{delay}"/>')
        strokes = 8
        zigzag = []
        for i in range(strokes):  # back-and-forth diagonal strokes, like colouring in a square
            reach = 4 + 14 * i / (strokes - 1)
            ends = [(x + max(2, reach - 8), y + min(10, reach - 2)), (x + min(10, reach - 2), y + max(2, reach - 8))]
            zigzag += ends if i % 2 == 0 else ends[::-1]
        path = "M" + "L".join(f"{num(px)} {num(py)}" for px, py in zigzag)
        c.add(f'<path class="scribble" pathLength="1" d="{path}" fill="none" stroke="{t["ink"]}" '
              f'stroke-width="1.2" stroke-opacity="{(.45, .62, .8, 1)[level - 1]}" stroke-linecap="round" stroke-linejoin="round" '
              f'style="animation-delay:{delay}"/>')

    return c.render()


# Activity card ---------------------------------------------------------------

QUERY = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      totalCommitContributions
      totalPullRequestContributions
      contributionCalendar { totalContributions weeks { contributionDays { date contributionCount } } }
    }
    repositories(ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC, first: 100) {
      nodes { name languages(first: 30, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name } } } }
    }
  }
}
"""

SAMPLE = {
    "contributions": 136, "commits": 102, "pull_requests": 5, "longest_streak": 4,
    "languages": [["C#", 38.4], ["Java", 24.9], ["TypeScript", 15.2], ["JavaScript", 9.8], ["HTML", 6.9],
                  ["Other", 4.8]],
    "updated": "2026-11-21",
    "days": [[(dt.date(2026, 11, 21) - dt.timedelta(days=370 - i)).isoformat(),
              (i * 7 % 11) if i % 9 in (0, 1, 4) or 337 <= i <= 347 else 0] for i in range(371)],
}


def fetch_activity(settings: dict, token: str) -> dict:
    body = json.dumps({"query": QUERY, "variables": {"login": settings["user"]}}).encode()
    request = urllib.request.Request("https://api.github.com/graphql", data=body, headers={
        "Authorization": f"bearer {token}", "Content-Type": "application/json", "User-Agent": "profile-images"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    if payload.get("errors"):
        raise SystemExit("GitHub API: " + "; ".join(e["message"] for e in payload["errors"]))
    user = payload["data"]["user"]
    calendar = user["contributionsCollection"]["contributionCalendar"]
    longest = run = 0
    previous = None
    for day in sorted((d for w in calendar["weeks"] for d in w["contributionDays"]), key=lambda d: d["date"]):
        date = dt.date.fromisoformat(day["date"])
        follows = previous is not None and (date - previous).days == 1
        run = (run + 1 if follows else 1) if day["contributionCount"] else 0
        longest = max(longest, run)
        previous = date
    hidden = {name.lower() for name in settings.get("hide_languages", [])}
    sizes: dict[str, int] = {}
    for repo in user["repositories"]["nodes"]:
        if repo["name"].lower() == settings["user"].lower():
            continue  # this profile repo only holds the script that draws these pictures
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            if name.lower() not in hidden:
                sizes[name] = sizes.get(name, 0) + edge["size"]
    total = sum(sizes.values()) or 1
    languages = [[name, 100 * size / total] for name, size in sorted(sizes.items(), key=lambda item: -item[1])]
    return {
        "contributions": calendar["totalContributions"],
        "commits": user["contributionsCollection"]["totalCommitContributions"],
        "pull_requests": user["contributionsCollection"]["totalPullRequestContributions"],
        "longest_streak": longest,
        "languages": languages,
        "days": [[d["date"], d["contributionCount"]] for w in calendar["weeks"] for d in w["contributionDays"]],
        "updated": dt.date.today().isoformat(),
    }


LANGUAGE_NAMES = {"PLSQL": "PL/SQL"}


def activity_card(data: dict, theme: str, also: list | tuple = ()) -> str:
    """Four numbers from the last year, then every language from my public repos as one
    hand-drawn bar with a legend, plus languages I've used elsewhere written in by hand."""
    t = THEMES[theme]
    stats = [(data["contributions"], "contributions in the last year"), (data["commits"], "commits"),
             (data["pull_requests"], "pull requests"), (data["longest_streak"], "days in my longest streak")]
    languages = [(LANGUAGE_NAMES.get(name, name), pct) for name, pct in data["languages"]]
    bold, code, hand = Font.get("sans-bold"), Font.get("code"), Font.get("hand")

    def share(pct: float) -> str:
        return "<0.1%" if pct < .05 else f"{pct:.1f}%"

    legend, x, row = [], 48, 0
    for i, (name, pct) in enumerate(languages):
        width = 18 + bold.width(name, 13.5) + 6 + code.width(share(pct), 12.5)
        if x + width > 952 and x > 48:
            x, row = 48, row + 1
        legend.append((x, row, i, name, pct))
        x += width + 26
    written = []
    for name in also:
        width = hand.width(f"+ {name}", 25)
        if x + width > 952 and x > 48:
            x, row = 48, row + 1
        written.append((x, row, f"+ {name}"))
        x += width + 26
    legend_top = 226
    height = legend_top + 28 * row + 62

    c = Canvas(1000, height, "My year on GitHub: " + ", ".join(f"{n} {label}" for n, label in stats)
               + ". Languages in my public repos: " + ", ".join(f"{name} {share(pct)}" for name, pct in languages)
               + "." + (" Also: " + ", ".join(also) + "." if also else ""))
    page(c, t, 8, 8, 984, height - 16)
    c.css.append(".fade{animation:fade .7s ease-out both}"
                 ".seg{transform-box:fill-box;transform-origin:0 50%;animation:grow linear both}")
    for i, (value, label) in enumerate(stats):
        x = 56 + 232 * i
        c.add(c.text("hand", f"{value:,}", x, 86, 58, t["ink"], cls="fade", style=f"animation-delay:{secs(.15 + .12 * i)}"),
              c.text("sans", label, x, 111, 14, t["muted"], cls="fade", style=f"animation-delay:{secs(.25 + .12 * i)}"))
    c.add(f'<path d="M48 146H952" stroke="{t["ink"]}" stroke-opacity=".3" stroke-dasharray="2 5" stroke-linecap="round"/>')

    total = sum(pct for _, pct in languages) or 1
    c.defs.append('<clipPath id="bar"><rect x="48" y="166" width="904" height="18" rx="9"/></clipPath>')
    segments, done = [], 0.0
    for i, (name, pct) in enumerate(languages):
        segments.append(
            f'<rect class="seg" x="{num(48 + 904 * done / total)}" y="166" width="{num(904 * pct / total + .6)}" '
            f'height="18" fill="{HIGHLIGHTERS[i % len(HIGHLIGHTERS)]}" stroke="{t["ink"]}" stroke-width="1" '
            f'style="animation-duration:{secs(max(.08, 1.2 * pct / total))};animation-delay:{secs(.4 + 1.2 * done / total)}"/>')
        done += pct
    c.add(f'<g clip-path="url(#bar)">{"".join(segments)}</g>',
          f'<rect x="48" y="166" width="904" height="18" rx="9" fill="none" stroke="{t["ink"]}" stroke-width="1.4"/>')

    for x, row, i, name, pct in legend:
        y = legend_top + 28 * row
        delay = f"animation-delay:{secs(.5 + .06 * i)}"
        c.add(f'<circle class="fade" cx="{num(x + 6)}" cy="{num(y - 4.5)}" r="6" fill="{HIGHLIGHTERS[i % len(HIGHLIGHTERS)]}" '
              f'stroke="{t["ink"]}" stroke-width="1.1" style="{delay}"/>',
              c.text("sans-bold", name, x + 18, y, 13.5, t["text"], cls="fade", style=delay),
              c.text("code", share(pct), x + 18 + bold.width(name, 13.5) + 6, y, 12.5, t["muted"], cls="fade", style=delay))
    for x, row, label in written:
        c.add(c.text("hand", label, x, legend_top + 28 * row + 1, 25, t["ink"], cls="fade",
                     style=f"animation-delay:{secs(1.4)}"))
    updated = dt.date.fromisoformat(data["updated"])
    c.add(c.text("code", f"Activity from the last 12 months, languages from my public repos. "
                         f"Updated {updated.day} {updated:%b %Y}", 952, height - 24, 11.5, t["faint"], anchor="end"))
    return c.render()


# Main ----------------------------------------------------------------------

def write(path: Path, svg: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")
    print(f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}  {len(svg.encode()) / 1024:.0f} KB")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("assets", help="header, buttons and project cards into assets/")
    activity = commands.add_parser("activity", help="activity card from the GitHub API")
    activity.add_argument("--out", default="dist", help="folder for activity-light.svg and activity-dark.svg")
    activity.add_argument("--sample", action="store_true", help="use sample numbers instead of the API")
    args = parser.parse_args()
    cfg = tomllib.loads(CONFIG.read_text(encoding="utf-8"))

    if args.command == "assets":
        out = ROOT / "assets"
        for theme in THEMES:
            write(out / f"header-{theme}.svg", header(cfg, theme))
            write(out / f"journey-{theme}.svg", journey(cfg, theme))
            for link in cfg["links"]:
                write(out / f"link-{link['file']}-{theme}.svg", link_button(link, theme))
            for project in cfg["projects"]:
                write(out / f"card-{project['file']}-{theme}.svg", card(project, theme))
    else:
        if args.sample:
            data = SAMPLE
        else:
            token = os.environ.get("GITHUB_TOKEN") or raise_missing_token()
            data = fetch_activity(cfg["activity"], token)
        for theme in THEMES:
            write(Path(args.out).resolve() / f"activity-{theme}.svg",
                  activity_card(data, theme, cfg["activity"].get("also_used", [])))
            write(Path(args.out).resolve() / f"year-{theme}.svg", year_card(data["days"], theme))


def raise_missing_token() -> str:
    raise SystemExit("Set GITHUB_TOKEN to read the activity from the GitHub API, or pass --sample.")


if __name__ == "__main__":
    main()
