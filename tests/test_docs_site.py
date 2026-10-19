"""The built documentation site renders what the configuration promises.

The site is built once per session with ``zensical build --strict`` into a
temporary directory, so a warning anywhere fails the session, and every test
below reads the HTML a reader would get:

- a code block embedded from ``docs/examples`` starts at column 0, so a
  pasted block runs (``pymdownx.snippets.dedent_subsections``);
- Assignment/Annotated tabs carry word anchors and switch together site-wide;
- acronyms such as DDL carry a hover definition from the glossary;
- ``#NNN`` references link to GitHub;
- the old migrations URL and its anchors redirect to the sections that
  replaced them;
- the API reference hides source blocks, dunder and ``__ferro_*__`` members,
  links the types in its signatures, renders ``Field`` once and gives
  ``Relation.all()`` / ``.first()`` their inherited docstrings;
- the docs build in CI and in the publish workflow is strict.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urljoin

import pytest

pytest.importorskip("zensical", reason="the docs dependency group is not installed")

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

COPIED = ("zensical.toml", "docs/pages", "docs/includes")
"""Paths copied into the scratch project: Zensical refuses a ``docs_dir``
that resolves outside the project root, so the pages are copied."""

LINKED = ("docs/examples", "src", "README.md", "CHANGELOG.md", "CONTRIBUTING.md")
"""Paths the pages embed or document, linked into the scratch project."""

_CODE_BLOCK = re.compile(
    r'<div class="language-[\w-]+ highlight">(?:<span class="filename">.*?</span>)?'
    r"<pre><span></span><code>(.*?)</code></pre>",
    re.DOTALL,
)
_TAG = re.compile(r"<[^>]+>")


@pytest.fixture(scope="session")
def built_site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the docs site with ``zensical build --strict``; return its root."""
    project = tmp_path_factory.mktemp("docs-site")
    for name in COPIED:
        source, target = REPO_ROOT / name, project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    for name in LINKED:
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(REPO_ROOT / name)
    result = subprocess.run(
        [sys.executable, "-m", "zensical", "build", "--clean", "--strict"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, (
        f"zensical build --strict failed\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return project / "site"


def rendered_pages(built_site: Path) -> Iterator[tuple[str, str]]:
    """Yield ``(url_path, html)`` for every HTML page of the built site."""
    for page in sorted(built_site.rglob("*.html")):
        relative = page.relative_to(built_site).as_posix()
        url_path = "/" + relative.removesuffix("index.html")
        yield url_path, page.read_text(encoding="utf-8")


def _page(built_site: Path, url_path: str) -> str:
    page = built_site / url_path.strip("/") / "index.html"
    assert page.is_file(), f"{url_path} was not built"
    return page.read_text(encoding="utf-8")


def _code_lines(block: str) -> list[str]:
    text = html.unescape(_TAG.sub("", block))
    return [line for line in text.splitlines() if line.strip()]


def _indented_blocks(page_html: str) -> list[str]:
    """Return the code blocks whose every line shares leading whitespace."""
    indented = []
    for match in _CODE_BLOCK.finditer(page_html):
        lines = _code_lines(match.group(1))
        if lines and all(line[0] in " \t" for line in lines):
            indented.append("\n".join(lines))
    return indented


def test_embedded_code_starts_at_column_zero(built_site: Path) -> None:
    offenders = {
        url: blocks[0]
        for url, page_html in rendered_pages(built_site)
        if (blocks := _indented_blocks(page_html))
    }
    assert not offenders, "code blocks render indented:\n" + "\n\n".join(
        f"{url}:\n{block}" for url, block in offenders.items()
    )


def test_quickstart_has_code_blocks_to_check(built_site: Path) -> None:
    assert len(_CODE_BLOCK.findall(_page(built_site, "getting-started/quickstart"))) > 3


def test_declaration_tabs_have_word_anchors_and_switch_together(
    built_site: Path,
) -> None:
    page = _page(built_site, "guide/models-and-fields")
    assert page.count('class="tabbed-set') >= 2
    labels = re.findall(r'<label for="([^"]+)">(Assignment|Annotated)<', page)
    assert labels, "no Assignment/Annotated tabs on the models page"
    for anchor, label in labels:
        assert not anchor.startswith("__tabbed_"), anchor
        assert anchor.endswith(f"-{label.lower()}"), anchor
    assert "content.tabs.link" in _theme_features(page)


def _theme_features(page_html: str) -> list[str]:
    match = re.search(
        r'<script id="__config" type="application/json">(.*?)</script>',
        page_html,
        re.DOTALL,
    )
    assert match, "page carries no theme config"
    return json.loads(match.group(1))["features"]


def test_acronyms_carry_their_glossary_definition(built_site: Path) -> None:
    page = _page(built_site, "concepts/architecture")
    match = re.search(r'<abbr title="([^"]+)">DDL</abbr>', page)
    assert match, "DDL has no <abbr> on the architecture page"
    assert "CREATE" in match.group(1)


def test_issue_references_link_to_github(built_site: Path) -> None:
    page = _page(built_site, "changelog")
    assert re.search(
        r'<a [^>]*href="https://github\.com/syn54x/ferro-orm/issues/\d+"', page
    )


OLD_MIGRATIONS_ANCHORS = {
    "": "guide/schema/overview/",
    "#three-ways-to-manage-schema": "guide/schema/overview/#the-ladder",
    "#auto-migration": "guide/schema/auto-migrate/",
    "#creating-tables-with-auto_migratetrue": "guide/schema/auto-migrate/#creating-tables-with-auto_migratetrue",
    "#applying-column-changes-with-migrate_updates": "guide/schema/auto-migrate/#applying-column-changes-with-migrate_updates",
    "#evolving-enums-label-addition": "guide/schema/auto-migrate/#evolving-enums-label-addition",
    "#destructive-drops-with-migrate_destructive": "guide/schema/auto-migrate/#destructive-drops-with-migrate_destructive",
    "#on-demand-migrate": "guide/schema/auto-migrate/#on-demand-migrate",
    "#safety-guidance": "guide/schema/auto-migrate/#safety-guidance",
    "#alembic-for-production": "guide/schema/alembic/",
    "#install": "guide/schema/alembic/#install",
    "#initialize": "guide/schema/alembic/#initialize",
    "#configure-envpy": "guide/schema/alembic/#configure-envpy",
    "#autogenerate": "guide/schema/alembic/#one-decider",
    "#review-apply": "guide/schema/alembic/#one-decider",
    "#choosing-a-workflow": "guide/schema/overview/#choosing",
}
"""Every anchor of the old ``guide/migrations/`` page and the section that
replaced it, as the deleted hand-written stub mapped them."""


def test_old_migrations_page_is_gone_from_the_source() -> None:
    assert not (REPO_ROOT / "docs/pages/guide/migrations.md").exists()


def test_old_migrations_url_redirects_each_anchor(built_site: Path) -> None:
    old_url = "https://ferro-orm.x54.sh/guide/migrations/"
    targets = _redirect_targets(_page(built_site, "guide/migrations"))
    for anchor, section in OLD_MIGRATIONS_ANCHORS.items():
        assert anchor in targets, f"old anchor {anchor or '(none)'} is not redirected"
        resolved = urljoin(old_url, targets[anchor])
        assert resolved == urljoin(old_url, "/" + section), (
            f"{anchor or '(none)'} redirects to {resolved}, expected {section}"
        )
        path, _, fragment = section.partition("#")
        target_page = _page(built_site, path)
        if fragment:
            assert f'id="{fragment}"' in target_page, f"{section} has no such anchor"


def _redirect_targets(page_html: str) -> dict[str, str]:
    """Read the anchor map of a generated redirect page."""
    targets: dict[str, str] = {}
    refresh = re.search(r'http-equiv="refresh" content="0; url=([^"]+)"', page_html)
    assert refresh, "the old migrations URL is not a redirect page"
    targets[""] = html.unescape(refresh.group(1))
    for script in re.findall(r"<script[^>]*>(.*?)</script>", page_html, re.DOTALL):
        for anchor, target in re.findall(r'"(#[\w-]+)"\s*:\s*"([^"]+)"', script):
            targets[anchor] = target
    return targets


def test_old_migrations_url_is_not_in_the_sitemap(built_site: Path) -> None:
    sitemap = (built_site / "sitemap.xml").read_text(encoding="utf-8")
    assert "guide/migrations/" not in sitemap
    assert "guide/schema/overview/" in sitemap


def test_model_api_page_shows_the_public_surface_only(built_site: Path) -> None:
    page = _page(built_site, "api/model")
    assert "Source code in" not in page
    ids = re.findall(r'id="(ferro\.[\w.]+)"', page)
    assert ids, "the Model API page documents nothing"
    dunders = [anchor for anchor in ids if re.search(r"\.__\w+__$", anchor)]
    assert not dunders, f"dunder members rendered: {dunders}"
    signatures = re.findall(
        r'<div class="language-python doc-signature highlight">.*?</code>',
        page,
        re.DOTALL,
    )
    assert signatures, "no separate signatures on the Model API page"
    assert any('<a class="autorefs' in signature for signature in signatures), (
        "no linked type annotation in any Model signature"
    )


def test_field_renders_once_without_its_overloads(built_site: Path) -> None:
    page = _page(built_site, "api/fields")
    assert page.count('id="ferro.Field"') == 1
    assert "doc-overloads" not in page
    assert 'id="ferro.Field(index)"' in page


def test_relation_query_methods_inherit_their_docstrings(built_site: Path) -> None:
    page = _page(built_site, "api/relationships")
    for method in ("all", "first"):
        section = re.search(
            rf'id="ferro\.Relation\.{method}" class="doc doc-heading">(.*?)(?:class="doc doc-heading"|</article>)',
            page,
            re.DOTALL,
        )
        assert section, f"Relation.{method} is not documented"
        text = html.unescape(_TAG.sub("", section.group(1)))
        assert "Returns:" in text, f"Relation.{method} renders without a docstring"
        assert "Examples:" in text, f"Relation.{method} renders without a docstring"


@pytest.mark.parametrize("workflow", ["publish-docs.yml", "ci.yml"])
def test_docs_build_in_workflows_is_strict(workflow: str) -> None:
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    builds = re.findall(r"zensical build[^\n]*", text)
    assert builds, f"{workflow} does not build the docs"
    for build in builds:
        assert "--strict" in build, f"{workflow}: {build}"
