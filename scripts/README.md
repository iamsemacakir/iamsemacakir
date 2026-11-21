# Profile images

The pictures in the profile README are drawn by `build.py` from the words in
`profile.toml`. You don't need to run anything yourself: edit `profile.toml` on
GitHub, commit, and the **Profile images** workflow redraws `assets/*.svg`
about a minute later.

## What to edit

| To change | Edit in `profile.toml` |
| --- | --- |
| The greetings, name, tagline, sticky note and terminal line | `[person]` |
| The buttons under the header | `[[links]]` |
| The timeline under "My journey" | `[[journey]]`, oldest first |
| The project cards | `[[projects]]`; the build stops and tells you if an `about` text is too long |
| Languages left out of the chart | `hide_languages` under `[activity]` |
| Languages written in by hand, for ones not in public repos | `also_used` under `[activity]` |

The text around the pictures ("A bit about me", the live demo links, the
headings) lives in `README.md` itself.

## What runs on its own

- **Profile images** (`.github/workflows/images.yml`) redraws the header,
  buttons, journey and project cards whenever `profile.toml` or `build.py`
  changes, and commits them to `assets/`.
- **Profile activity** (`.github/workflows/activity.yml`) runs every night. It
  reads the activity and languages from the GitHub API, draws the activity card
  and the snake, and publishes them to the `output` branch.

GitHub keeps images for about five minutes, so a change can take that long to
show on the profile.

## Running it on your computer

```sh
pip install -r scripts/requirements.txt
python scripts/build.py assets                        # header, buttons, journey, cards
python scripts/build.py activity --out dist --sample  # activity card with sample numbers
```

The fonts (Caveat, Inter and Courier Prime, under the SIL Open Font License)
are downloaded from github.com/google/fonts on the first run and kept in
`.cache/`.

## Adding a project card

Copy one `[[projects]]` block in `profile.toml` and change it:

```toml
[[projects]]
file = "my-project"          # names the images: assets/card-my-project-*.svg
title = "My Project"
about = "One or two sentences about what it does."
tags = ["C#", "React"]       # three or four fit on one line
picture = "schedule"         # schedule, service-map, flashcard or route
```

Then add the card to `README.md` next to the others, pointing at
`assets/card-my-project-dark.svg` and `assets/card-my-project-light.svg`.

## Checking a change

`python scripts/check.py` draws every image in memory and tells you if a text
doesn't fit or a picture is broken, without changing anything in `assets/`.
Pull requests run the same check on their own.
