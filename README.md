# starhist

GitHub star-history charts from the terminal, with **multi-repo comparison on one axis**.

```bash
starhist chart myorg/repo-a myorg/repo-b myorg/repo-c -o stars.svg
```

![Six repos on one axis](docs/example-multi.svg)

## Why this exists

On **2026-06-30 GitHub restricted the stargazers API** to repo admins and
collaborators ([changelog](https://github.blog/changelog/2026-06-30-upcoming-access-restrictions-to-public-api-endpoints-and-ui-views/)).
Hosted chart services can now only render repos *their own* server token
administers, so `star-history.com` and `starchart.cc` return errors or blank
images for essentially every repo, and thousands of README embeds broke at once.

Several good local tools appeared in response, but each renders **one repo per
chart**. If you maintain a family of repos, the question you actually want
answered is which of them is growing, and that needs them on a shared axis.
That is the gap `starhist` fills.

## Install

```bash
pipx install git+https://github.com/svemyh/star-history-cli
```

Or from a clone:

```bash
git clone https://github.com/svemyh/star-history-cli && cd star-history-cli
pipx install .
```

Requires Python 3.10+. **No runtime dependencies** — that is deliberate. This
tool handles a GitHub token, and an empty dependency tree is the whole security
argument. It is standard library only, roughly 900 lines you can read in a
sitting.

## Authenticate

If you already use the `gh` CLI, you are done; `starhist` borrows its token
automatically. Otherwise:

```bash
starhist auth login                  # prompts, input hidden
starhist auth login --token ghp_...  # or pass it directly
```

The token is verified against GitHub before it is stored, and stored in the
**macOS Keychain** where available, otherwise a `0600` file under
`~/.config/starhist/`. Resolution order:

```
--token  >  $STARHIST_TOKEN  >  $GITHUB_TOKEN  >  stored  >  gh CLI
```

Check what is in play, and whether it can actually see a given repo:

```bash
starhist auth status --repo myorg/myrepo
```

```
Token:  ghp_…mnop  (from macOS Keychain)
User:   svemyh
Access: can read myorg/myrepo stargazers (70 stars)
```

`starhist auth logout` removes the stored token. It never touches your env vars
or your `gh` login.

**Scope:** `public_repo` is enough for public repos. Tokens with no scopes at
all stopped working in 2026, so pick at least that one.

## Use

```bash
# One repo, star-history.com's xkcd look
starhist chart myorg/myrepo

# Several repos on one axis, which is the point
starhist chart myorg/a myorg/b myorg/c -o compare.svg

# Aligned from each repo's first star, so launches are comparable
starhist chart myorg/a myorg/b --type timeline

# Dark, for a README that switches themes
starhist chart myorg/myrepo --dark -o stars-dark.svg

# Crisp instead of hand-drawn
starhist chart myorg/myrepo --style clean

# Raw data out
starhist export myorg/a myorg/b -f csv -o stars.csv
```

| Flag | Default | Notes |
|---|---|---|
| `-o, --output` | `<repo>.svg` | |
| `--title` | `Star History` | |
| `--style` | `xkcd` | or `clean` |
| `--type` | `date` | or `timeline` (days since each repo's first star) |
| `--dark` | off | GitHub-dark palette |
| `--width` / `--height` | `800` / `533` | |
| `--color` | palette | repeatable, in repo order |
| `--no-cache` | off | curves are cached 6h under `~/.cache/starhist` |
| `--no-attribution` | off | |

### Embedding in a README

The output is a self-contained SVG with the font embedded, so it renders
anywhere with no network call. Commit it and point at it:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="stars-dark.svg">
  <img alt="Star history" src="stars.svg">
</picture>
```

**Rendering is deterministic.** The hand-drawn wobble comes from a generator
seeded on the data, not from randomness, so re-running on unchanged stars
produces a byte-identical file. Committing the chart on a schedule will not
produce a diff unless the stars actually moved.

## Limits, stated plainly

- **You can only chart repos you own or collaborate on.** That is GitHub's
  restriction, not a choice here. A repo you do not administer returns 404, and
  `starhist` says so in those words rather than claiming the repo is missing.
- **Above 40,000 stars the curve is interpolated.** GitHub paginates stargazers
  at 400 pages x 100, so beyond that the exact curve is unobtainable by anyone.
  `starhist` samples evenly-spaced pages, anchors the final point to the live
  count, and prints a note saying it did. Every tool in this category faces this;
  not all of them mention it.
- **Eight repos maximum per chart.** Past eight, categorical colours stop being
  reliably distinguishable, so it refuses rather than cycling the palette.
- **Cumulative curves only**, reconstructed from `starred_at`. Like every
  reconstruct-from-timestamps tool, this cannot show *unstars*: a removed star
  simply vanishes from history. For true up-and-down tracking you need to
  snapshot counts forward on a schedule.

## Colours

The categorical palette is validated for colour-vision deficiency in both light
and dark mode, and hues are assigned by fixed slot order so adding or removing a
repo never repaints the others. Series identity is also carried by the legend
text, never by colour alone.

## Development

```bash
python3 -m unittest discover -s tests -v
```

27 tests, no network and no Keychain access: the HTTP transport and the token
store are injected, so the suite is hermetic.

## Prior art

- [star-history.com](https://star-history.com) — the original, and the look this
  borrows. No CLI.
- [carsteneu/mystarhistory](https://github.com/carsteneu/mystarhistory) — single-repo
  SVG via `gh`; the closest thing to this and worth using if one repo is all you need.
- [dtolnay/star-history](https://github.com/dtolnay/star-history) — Rust, D3 in a
  browser, handles users and orgs.
- [ykdojo/gh-star-history](https://github.com/ykdojo/gh-star-history) — Plotly HTML,
  incremental cache, stargazer region breakdown.

## Licence

MIT. Bundles the [Handlee](https://fonts.google.com/specimen/Handlee) typeface
under the SIL Open Font License; see `starhist/assets/Handlee-OFL.txt`.
