#!/usr/bin/env python3
"""Draws every image into memory and checks each one, without touching assets/.

    python scripts/check.py

Run it after changing profile.toml or build.py. It stops with a message when a
text no longer fits on its card, when a picture isn't valid SVG, or when one
grows past 200 KB (GitHub shows big images slowly).
"""
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402


def main() -> None:
    cfg = tomllib.loads(build.CONFIG.read_text(encoding="utf-8"))
    images = {}
    for theme in build.THEMES:
        images[f"header-{theme}"] = build.header(cfg, theme)
        images[f"journey-{theme}"] = build.journey(cfg, theme)
        for link in cfg["links"]:
            images[f"link-{link['file']}-{theme}"] = build.link_button(link, theme)
        for project in cfg["projects"]:
            images[f"card-{project['file']}-{theme}"] = build.card(project, theme)
        images[f"activity-{theme}"] = build.activity_card(build.SAMPLE, theme, cfg["activity"].get("also_used", []))

    problems = []
    for name, svg in images.items():
        try:
            root = ET.fromstring(svg)
        except ET.ParseError as error:
            problems.append(f"{name}: not valid SVG ({error})")
            continue
        if not root.get("viewBox"):
            problems.append(f"{name}: has no viewBox")
        size = len(svg.encode()) / 1024
        if size > 200:
            problems.append(f"{name}: {size:.0f} KB, more than 200 KB")

    for problem in problems:
        print(problem)
    print(f"{len(images)} images drawn, {len(problems)} problems")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
