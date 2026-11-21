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
        "chip": "#EDF2FA", "shadow": 0.08, "note_shadow": 0.20,
    },
    "dark": {
        "page": "#151C28", "edge": "#2B3647", "grid": "#1F2938", "margin": "#5C2D37",
        "rule": "#25324A", "red_rule": "#62313D",
        "ink": "#9DC1FF", "accent": "#60A5FA", "text": "#E6EDF3", "muted": "#A7B1C0", "faint": "#7D889B",
        "sticky": "#F5D776", "sticky_shade": "#E0BC4E", "sticky_text": "#3A2D05",
        "red": "#F87171", "green": "#4ADE80", "amber": "#FBBF24", "pink": "#F472B6",
        "chip": "#1E2839", "shadow": 0.0, "note_shadow": 0.55,
    },
}
HIGHLIGHTERS = ["#93C5FD", "#FCD34D", "#F9A8D4", "#86EFAC", "#C4B5FD", "#FDBA74"]

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


def page(c: Canvas, t: dict, x: float, y: float, w: float, h: float, grid: bool = True) -> None:
    """A sheet of paper: squared like a maths notebook, or plain for index cards."""
    if t["shadow"]:
        c.defs.append(f'<filter id="lift" x="-5%" y="-5%" width="110%" height="115%">'
                      f'<feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#0F172A" '
                      f'flood-opacity="{t["shadow"]}"/></filter>')
    lift = ' filter="url(#lift)"' if t["shadow"] else ""
    c.add(f'<rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" rx="10" fill="{t["page"]}"{lift}/>')
    if grid:
        c.defs.append(f'<pattern id="squares" x="{num(x)}" y="{num(y)}" width="24" height="24" '
                      f'patternUnits="userSpaceOnUse"><path d="M24 0H0V24" fill="none" '
                      f'stroke="{t["grid"]}" stroke-width="1"/></pattern>')
        c.add(f'<rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" rx="10" fill="url(#squares)"/>')
    c.add(f'<rect x="{num(x + .5)}" y="{num(y + .5)}" width="{num(w - 1)}" height="{num(h - 1)}" rx="9.5" '
          f'fill="none" stroke="{t["edge"]}"/>')


# Header --------------------------------------------------------------------

def header(cfg: dict, theme: str) -> str:
    t = THEMES[theme]
    person = cfg["person"]
    c = Canvas(1000, 304, f'{person["greeting"]} {person["name"]}. {person["tagline"]}')
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
    for d in hand.outlines(person["greeting"], 40, left + 4, 62):
        writing.append(f'<path class="w soft" pathLength="1" d="{d}" '
                       f'style="animation-delay:{secs(clock)},{secs(clock + .4)}"/>')
        clock += .07
    clock += .25
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
      nodes { languages(first: 10, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name } } } }
    }
  }
}
"""

SAMPLE = {
    "contributions": 136, "commits": 102, "pull_requests": 5, "longest_streak": 4,
    "languages": [["C#", 38.4], ["Java", 24.9], ["TypeScript", 15.2], ["JavaScript", 9.8], ["HTML", 6.9],
                  ["Other", 4.8]],
    "updated": "2026-11-21",
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
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            if name.lower() not in hidden:
                sizes[name] = sizes.get(name, 0) + edge["size"]
    total = sum(sizes.values()) or 1
    ranked = sorted(sizes.items(), key=lambda item: -item[1])
    shown = ranked if len(ranked) <= 6 else ranked[:5]
    languages = [[name, 100 * size / total] for name, size in shown]
    if len(ranked) > 6:
        languages.append(["Other", 100 * sum(size for _, size in ranked[5:]) / total])
    return {
        "contributions": calendar["totalContributions"],
        "commits": user["contributionsCollection"]["totalCommitContributions"],
        "pull_requests": user["contributionsCollection"]["totalPullRequestContributions"],
        "longest_streak": longest,
        "languages": languages,
        "updated": dt.date.today().isoformat(),
    }


def activity_card(data: dict, theme: str) -> str:
    t = THEMES[theme]
    stats = [(data["contributions"], "contributions in the last year"), (data["commits"], "commits"),
             (data["pull_requests"], "pull requests"), (data["longest_streak"], "days in my longest streak")]
    c = Canvas(1000, 264, "My year on GitHub: " + ", ".join(f"{n} {label}" for n, label in stats)
               + ". Languages: " + ", ".join(f"{name} {pct:.1f}%" for name, pct in data["languages"]) + ".")
    page(c, t, 8, 8, 984, 248)
    c.css.append(".fade{animation:fade .7s ease-out both}")
    for i, (value, label) in enumerate(stats):
        x, y = 56 + 230 * (i % 2), 90 + 100 * (i // 2)
        c.add(c.text("hand", f"{value:,}", x, y, 62, t["ink"], cls="fade", style=f"animation-delay:{secs(.15 + .12 * i)}"))
        for j, line in enumerate(wrap(Font.get("sans"), label, 14.5, 215)[:2]):
            c.add(c.text("sans", line, x, y + 25 + 18 * j, 14.5, t["muted"], cls="fade",
                         style=f"animation-delay:{secs(.25 + .12 * i)}"))
    c.add(f'<path d="M500 40V224" stroke="{t["ink"]}" stroke-opacity=".3" stroke-dasharray="2 5" stroke-linecap="round"/>')
    top = max((pct for _, pct in data["languages"]), default=1) or 1
    for i, (name, pct) in enumerate(data["languages"][:6]):
        y = 50 + 31 * i
        width = max(4, 236 * pct / top)
        c.add(c.text("sans-bold", name, 544, y + 5, 14.5, t["text"]),
              f'<rect class="grow" x="660" y="{y - 8}" width="{num(width)}" height="16" rx="4" '
              f'fill="{HIGHLIGHTERS[i % len(HIGHLIGHTERS)]}" stroke="{t["ink"]}" stroke-width="1.2" '
              f'style="animation-delay:{secs(.35 + .1 * i)}"/>',
              c.text("code", f"{pct:.1f}%", 660 + width + 10, y + 5, 13, t["muted"], cls="fade",
                     style=f"animation-delay:{secs(.8 + .1 * i)}"))
    updated = dt.date.fromisoformat(data["updated"])
    c.add(c.text("code", f"Last 12 months, updated {updated.day} {updated:%b %Y}", 970, 240, 12, t["faint"],
                 anchor="end"))
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
            write(Path(args.out).resolve() / f"activity-{theme}.svg", activity_card(data, theme))


def raise_missing_token() -> str:
    raise SystemExit("Set GITHUB_TOKEN to read the activity from the GitHub API, or pass --sample.")


if __name__ == "__main__":
    main()
