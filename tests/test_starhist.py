"""Tests. No network, no Keychain, no home directory: every collaborator is
injected, which is the point of keeping fetch/render/store separate."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starhist import auth, render
from starhist.cache import Cache
from starhist.github import (MAX_EXACT, AccessRestricted, Client, GitHubError,
                             Series, _even_pages)


def fake_transport(meta=None, stargazers=None, meta_status=200, stargazers_status=200):
    """Serve the two endpoints the client actually calls, and nothing else.

    Matching on endpoint shape rather than substring, so `/repos/o/r` cannot
    accidentally satisfy a request for `/repos/o/r/stargazers`.
    """
    def transport(url: str, headers: dict[str, str]):
        path = url.split("api.github.com", 1)[-1].split("?", 1)[0]
        if path.endswith("/stargazers"):
            if stargazers is None:
                return 404, b'{"message":"Not Found"}'
            return stargazers_status, json.dumps(stargazers).encode()
        if path.startswith("/repos/"):
            if meta is None:
                return 404, b'{"message":"Not Found"}'
            return meta_status, json.dumps(meta).encode()
        return 404, b'{"message":"Not Found"}'
    return transport


def stars(n: int, start: datetime | None = None) -> list[dict]:
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [{"starred_at": (start + timedelta(days=i)).isoformat().replace("+00:00", "Z")}
            for i in range(n)]


class TestAuthPrecedence(unittest.TestCase):
    def test_explicit_token_wins(self):
        store = auth.NullStore()
        token, source = auth.resolve("explicit", store, env={"GITHUB_TOKEN": "env"})
        self.assertEqual((token, source), ("explicit", "--token"))

    def test_starhist_env_beats_github_env(self):
        token, source = auth.resolve(
            None, auth.NullStore(), env={"STARHIST_TOKEN": "a", "GITHUB_TOKEN": "b"}
        )
        self.assertEqual((token, source), ("a", "$STARHIST_TOKEN"))

    def test_store_used_when_no_explicit_or_env(self):
        store = auth.FileStore(path=Path(self.tmp.name) / "token")
        store.set("stored-token")
        token, source = auth.resolve(None, store, env={})
        self.assertEqual(token, "stored-token")
        self.assertEqual(source, "config file")

    def test_file_store_is_owner_only(self):
        path = Path(self.tmp.name) / "token"
        auth.FileStore(path=path).set("secret")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_clear_removes_token(self):
        store = auth.FileStore(path=Path(self.tmp.name) / "token")
        store.set("secret")
        store.clear()
        self.assertIsNone(store.get())

    def test_redact_never_reveals_middle(self):
        self.assertEqual(auth.redact("ghp_abcdefghijklmnop"), "ghp_…mnop")
        self.assertNotIn("efghijkl", auth.redact("ghp_abcdefghijklmnop"))

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)


class TestFetching(unittest.TestCase):
    def test_builds_cumulative_curve(self):
        client = Client("t", fake_transport(
            meta={"stargazers_count": 3}, stargazers=stars(3)))
        series = client.stargazers("o/r")
        self.assertEqual(series.total, 3)
        self.assertEqual([c for _, c in series.points], [1, 2, 3])

    def test_404_on_stargazers_is_reported_as_a_permissions_problem(self):
        # Repo metadata is readable, but the stargazers listing 404s: exactly
        # the shape of the 2026 restriction for a repo you do not administer.
        client = Client("t", fake_transport(
            meta={"stargazers_count": 5}, stargazers=None))
        with self.assertRaises(AccessRestricted) as ctx:
            client.stargazers("o/r")
        message = str(ctx.exception)
        self.assertIn("admins and collaborators", message)
        self.assertIn("not that the repo is missing", message)

    def test_401_is_distinguished_from_404(self):
        client = Client("t", fake_transport(meta={}, meta_status=401))
        with self.assertRaises(GitHubError) as ctx:
            client.star_count("o/r")
        self.assertIn("401", str(ctx.exception))

    def test_zero_stars_yields_empty_series(self):
        client = Client("t", fake_transport(meta={"stargazers_count": 0}))
        self.assertEqual(client.stargazers("o/r").points, [])

    def test_even_pages_always_spans_first_to_last(self):
        pages = _even_pages(400, count=60)
        self.assertEqual(pages[0], 1)
        self.assertEqual(pages[-1], 400)
        self.assertLessEqual(len(pages), 60)

    def test_above_the_wall_the_curve_is_flagged_and_anchored(self):
        total = MAX_EXACT + 25_000
        client = Client("t", fake_transport(
            meta={"stargazers_count": total}, stargazers=stars(100)))
        series = client.stargazers("o/big")
        self.assertTrue(series.sampled)
        # However it is interpolated, it must end on the number GitHub reports.
        self.assertEqual(series.points[-1][1], total)


class TestRendering(unittest.TestCase):
    def series(self, repo: str, n: int, offset_days: int = 0) -> Series:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=offset_days)
        pts = [(start + timedelta(days=i), i + 1) for i in range(n)]
        return Series(repo=repo, points=pts, total=n)

    def test_single_repo_renders_valid_svg(self):
        svg = render.render([self.series("o/r", 10)], render.Options())
        self.assertTrue(svg.startswith("<svg"))
        self.assertTrue(svg.rstrip().endswith("</svg>"))
        self.assertIn("o/r", svg)

    def test_multi_repo_draws_one_line_per_repo(self):
        many = [self.series(f"o/r{i}", 10 + i) for i in range(4)]
        svg = render.render(many, render.Options())
        for s in many:
            self.assertIn(s.repo, svg)
        # One stroked series path plus one end marker per repo.
        self.assertEqual(svg.count("stroke-width=\"2.6\""), 4)
        self.assertEqual(svg.count("<circle"), 4)

    def test_multi_repo_uses_distinct_colours(self):
        many = [self.series(f"o/r{i}", 10) for i in range(5)]
        svg = render.render(many, render.Options())
        used = {c for c in render.LIGHT_SERIES if c in svg}
        self.assertEqual(len(used), 5)

    def test_refuses_more_repos_than_the_palette_holds(self):
        many = [self.series(f"o/r{i}", 5) for i in range(render.MAX_SERIES + 1)]
        with self.assertRaises(ValueError):
            render.render(many, render.Options())

    def test_output_is_deterministic(self):
        one = [self.series("o/r", 30)]
        self.assertEqual(render.render(one, render.Options()),
                         render.render(one, render.Options()))

    def test_timeline_mode_aligns_repos_that_started_apart(self):
        apart = [self.series("o/early", 10), self.series("o/late", 10, offset_days=200)]
        timeline = render.render(apart, render.Options(timeline=True))
        self.assertIn("Days since first star", timeline)
        self.assertIn("0d", timeline)

    def test_clean_style_omits_the_embedded_font(self):
        one = [self.series("o/r", 10)]
        self.assertNotIn("Handlee", render.render(one, render.Options(xkcd=False)))
        self.assertIn("Handlee", render.render(one, render.Options(xkcd=True)))

    def test_dark_theme_changes_the_surface(self):
        one = [self.series("o/r", 10)]
        self.assertIn("#0d1117", render.render(one, render.Options(dark=True)))

    def test_repo_names_are_escaped(self):
        evil = Series(repo='o/<script>&"', total=1,
                      points=[(datetime(2026, 1, 1, tzinfo=timezone.utc), 1)])
        svg = render.render([evil], render.Options())
        self.assertNotIn("<script>", svg)
        self.assertIn("&lt;script&gt;", svg)

    def test_empty_input_is_refused(self):
        with self.assertRaises(ValueError):
            render.render([], render.Options())


class TestCache(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Cache(root=Path(self.tmp.name))

    def test_roundtrip_preserves_the_curve(self):
        original = Series(
            repo="o/r", total=2,
            points=[(datetime(2026, 1, 1, tzinfo=timezone.utc), 1),
                    (datetime(2026, 1, 2, tzinfo=timezone.utc), 2)],
        )
        self.cache.put(original)
        restored = self.cache.get("o/r")
        self.assertEqual(restored.points, original.points)
        self.assertEqual(restored.total, 2)

    def test_expired_entries_are_ignored(self):
        stale = Cache(root=Path(self.tmp.name), ttl=timedelta(seconds=-1))
        stale.put(Series(repo="o/r", total=1,
                         points=[(datetime(2026, 1, 1, tzinfo=timezone.utc), 1)]))
        self.assertIsNone(stale.get("o/r"))

    def test_miss_returns_none(self):
        self.assertIsNone(self.cache.get("never/fetched"))


class TestRepoParsing(unittest.TestCase):
    def test_accepts_urls_and_bare_names(self):
        from starhist.cli import _normalise
        for given in ("o/r", "https://github.com/o/r", "github.com/o/r", "o/r/"):
            self.assertEqual(_normalise(given), "o/r")

    def test_rejects_malformed(self):
        from starhist.cli import _normalise
        for bad in ("justname", "a/b/c"):
            with self.assertRaises(ValueError):
                _normalise(bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
