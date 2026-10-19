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
- every exported name has one API entry under its public path, every public
  object and member has a docstring, no entry renders empty, each API page is
  titled after its objects, and every link from an API page lands on an
  anchor that exists;
- the docs build in CI and in the publish workflow is strict;
- the nav has six tabs holding every page once, each label equal to its
  page's title; every page has a one-sentence description and new pages are
  marked new; every moved page, and every anchor that moved, redirects to the
  section that replaced it;
- no page renders a literal Sphinx role, an unresolved cross-reference, a
  list merged into a paragraph, a docstring section as prose or a section
  table row that stops mid-sentence, and griffe reads every docstring without
  a warning; a second build, of a page and a module seeded with each defect,
  shows every check failing on it.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urljoin

import pytest

pytest.importorskip("zensical", reason="the docs dependency group is not installed")
yaml = pytest.importorskip("yaml", reason="the docs dependency group is not installed")

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


BUILD_LOG = "build.log"
"""The build's output, written next to ``site/`` in the scratch project."""


def _scratch_project(project: Path) -> Path:
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
    return project


def _build(project: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "zensical", "build", "--clean", *flags],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=600,
    )
    (project / BUILD_LOG).write_text(result.stdout + result.stderr, encoding="utf-8")
    return result


@pytest.fixture(scope="session")
def built_site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the docs site with ``zensical build --strict``; return its root."""
    project = _scratch_project(tmp_path_factory.mktemp("docs-site"))
    result = _build(project, "--strict")
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


RETURNED_TYPES = (
    "ferro.models.ModelConnection",
    "ferro.session.EngineManager",
    "ferro.session.EngineManager.session",
    "ferro.migrations.runner.RunReport",
    "ferro.migrations.report.StatusReport",
    "ferro.migrations.generate.CheckReport",
    "ferro.pass_report.Subject",
)
"""Objects no ``__all__`` exports that a public call returns or is called on:
``Model.using()``, ``engines.session()``, ``up()``, ``status()``,
``check()`` and ``Report.subject``. They are documented under the module that
defines them."""

_DOC_HEADING_ID = re.compile(r'id="([\w.]+)" class="doc doc-heading"')


def _public_paths() -> set[str]:
    """Return the public path of every exported name, and the returned types."""
    import ferro
    import ferro.migrations
    import ferro.migrations.testing
    import ferro.query

    paths = {f"ferro.{name}" for name in ferro.__all__}
    paths |= {
        f"ferro.query.{name}"
        for name in ferro.query.__all__
        if name not in ferro.__all__
    }
    paths |= {f"ferro.migrations.{name}" for name in ferro.migrations.__all__}
    paths |= {
        f"ferro.migrations.testing.{name}" for name in ferro.migrations.testing.__all__
    }
    return paths | set(RETURNED_TYPES)


def _api_entries(built_site: Path) -> dict[str, list[str]]:
    """Map each object the API pages document to the pages that render it."""
    entries: dict[str, list[str]] = {}
    for url, page_html in rendered_pages(built_site):
        if url.startswith("/api/"):
            for anchor in _DOC_HEADING_ID.findall(page_html):
                entries.setdefault(anchor, []).append(url)
    return entries


def test_every_export_has_an_api_entry(built_site: Path) -> None:
    entries = _api_entries(built_site)
    public = _public_paths()
    missing = sorted(path for path in public if path not in entries)
    assert not missing, f"exported names with no API entry: {missing}"
    repeated = {path: entries[path] for path in public if len(entries[path]) > 1}
    assert not repeated, f"exported names with more than one API entry: {repeated}"


def test_api_entries_use_public_paths(built_site: Path) -> None:
    public = _public_paths()
    private = sorted(
        path
        for path in _api_entries(built_site)
        if path not in public and not any(path.startswith(f"{p}.") for p in public)
    )
    assert not private, f"API entries under a non-public path: {private}"


def test_api_pages_are_titled_after_their_objects(built_site: Path) -> None:
    entries = _api_entries(built_site)
    for url, page_html in rendered_pages(built_site):
        if not url.startswith("/api/"):
            continue
        title = re.search(r"<h1[^>]*>(.*?)</h1>", page_html, re.DOTALL)
        assert title, f"{url} has no title"
        words = re.findall(r"[\w.]+", html.unescape(_TAG.sub("", title.group(1))))
        named = set()
        for path, pages in entries.items():
            if url in pages:
                parts = path.split(".")
                named |= {".".join(parts[:i]) for i in range(2, len(parts) + 1)}
                named.add(parts[-1])
        assert named & set(words), (
            f"{url} is titled {words}, naming none of its objects"
        )


def test_no_api_entry_renders_empty(built_site: Path) -> None:
    config = (REPO_ROOT / "zensical.toml").read_text(encoding="utf-8")
    assert re.search(r"^show_if_no_docstring = false$", config, re.MULTILINE)
    empty = []
    for url, page_html in rendered_pages(built_site):
        if not url.startswith("/api/"):
            continue
        for section in re.split(
            r'(?=<h\d id="[\w.]+" class="doc doc-heading")', page_html
        )[1:]:
            anchor = _DOC_HEADING_ID.search(section)
            contents = section.split('class="doc doc-contents', 1)[-1]
            contents = re.sub(
                r'<div class="[^"]*doc-signature.*?</div>',
                "",
                contents,
                flags=re.DOTALL,
            )
            if not re.search(r"<p>", contents.split("doc doc-heading", 1)[0]):
                empty.append(f"{url} {anchor.group(1) if anchor else '?'}")
    assert not empty, "API entries with no text:\n" + "\n".join(empty)


def test_every_public_object_and_member_has_a_docstring() -> None:
    """``show_if_no_docstring`` is off, so an undocumented member would vanish."""
    import griffe

    package = griffe.load(
        "ferro",
        search_paths=[str(REPO_ROOT / "src")],
        extensions=griffe.load_extensions("griffe_inherited_docstrings"),
        resolve_aliases=True,
        resolve_external=False,
    )
    undocumented = []
    for path in sorted(_public_paths()):
        obj = package[path.removeprefix("ferro.")]
        if not obj.docstring:
            undocumented.append(path)
        if obj.is_class:
            undocumented += [
                f"{path}.{name}"
                for name, member in obj.members.items()
                if not name.startswith("_")
                and name != "model_config"
                and not member.docstring
            ]
    assert not undocumented, f"public objects with no docstring: {undocumented}"


_HREF = re.compile(r'<a\b[^>]*\bhref="([^"]+)"')


def test_links_from_api_pages_land_on_existing_anchors(built_site: Path) -> None:
    dead = []
    for url, page_html in rendered_pages(built_site):
        if not url.startswith("/api/"):
            continue
        for href in sorted({html.unescape(href) for href in _HREF.findall(page_html)}):
            if re.match(r"[a-z]+:", href):
                continue
            path, _, fragment = urljoin(url, href).partition("#")
            if not fragment:
                continue
            target = built_site / path.strip("/") / "index.html"
            if not target.is_file() or f'id="{fragment}"' not in target.read_text(
                encoding="utf-8"
            ):
                dead.append(f"{url} -> {href}")
    assert not dead, "links to anchors that do not exist:\n" + "\n".join(dead)


# --- Navigation, front matter and moved pages (#723) ---

DOCS_PAGES = REPO_ROOT / "docs" / "pages"

NAV_TABS = ["Get started", "Guide", "How-to", "Concepts", "Reference", "Project"]

NEW_PAGES = {
    "concepts/row-level-security.md",
    "guide/schema/renames-and-retirements.md",
    "guide/schema/squash.md",
    "reference/migrations.md",
    "reference/row-level-security.md",
}
"""Pages created by the docs rewrite (#698); they carry ``status: new``."""

MOVED_PAGES = {
    "getting-started/next-steps.md": "getting-started/quickstart.md#next-steps",
    "howto/adopting-migrations.md": "guide/schema/adopting-migrations.md",
    "howto/upgrade-guide.md": "upgrading.md",
}
"""Every page moved or deleted by the nav rework, and where its URL now goes."""

_NEXT_STEPS = "getting-started/quickstart.md#next-steps"

MOVED_ANCHORS = {
    # The migrations guide split (#714): sections moved to their own pages,
    # or renamed on the page they stayed on.
    "guide/schema/migrations.md#renames": "guide/schema/renames-and-retirements.md#rename-hints",
    "guide/schema/migrations.md#retiring-a-column": "guide/schema/renames-and-retirements.md#retiring-a-column",
    "guide/schema/migrations.md#retiring-an-enum-label": "guide/schema/renames-and-retirements.md#retiring-an-enum-label",
    "guide/schema/migrations.md#headers": "reference/migrations.md#step-file-headers",
    "guide/schema/migrations.md#changes-existing-rows-need-a-value-for": "guide/schema/migrations.md#backfills",
    "guide/schema/migrations.md#checking": "guide/schema/migrations.md#check-status-and-drift",
    "guide/schema/migrations.md#going-back": "guide/schema/migrations.md#reverting-with-down",
    "guide/schema/migrations.md#editing-a-migration": "guide/schema/migrations.md#edited-migrations",
    "guide/schema/migrations.md#two-branches-one-number": "guide/schema/migrations.md#duplicate-migration-numbers-and-replace",
    "guide/schema/migrations.md#squashing-an-epics-migrations": "guide/schema/squash.md",
    "guide/schema/migrations.md#a-rename-inside-the-range": "guide/schema/squash.md#renames-inside-the-range",
    "guide/schema/migrations.md#every-database-that-applied-the-originals-inherits-the-squash": "guide/schema/squash.md#inheritance-on-databases-that-ran-the-originals",
    "guide/schema/migrations.md#a-squash-is-pre-release": "guide/schema/squash.md#squash-timing",
    "guide/schema/migrations.md#squash-or---replace": "guide/schema/squash.md#squash-vs-replace",
    # The row-level security split (#715): each section of the old guide and
    # the page that holds it now.
    "guide/row-level-security.md#the-pooler-constraint": "concepts/row-level-security.md#the-pooler-constraint",
    "guide/row-level-security.md#deploy-ordering": "guide/row-level-security.md#deploy-order",
    "guide/row-level-security.md#declaring-row-security": "guide/row-level-security.md#the-tenant-policy",
    "guide/row-level-security.md#the-shorthand": "reference/row-level-security.md#the-shorthand",
    "guide/row-level-security.md#multiple-policies-composing-permissive-and-restrictive": "reference/row-level-security.md#composing-policies",
    "guide/row-level-security.md#the-raw-escape-hatch": "reference/row-level-security.md#the-sql-form",
    "guide/row-level-security.md#session-settings": "concepts/row-level-security.md#session-settings",
    "guide/row-level-security.md#opening-a-session-with-settings": "guide/row-level-security.md#tenant-scoped-sessions",
    "guide/row-level-security.md#deferred-resolution-current_sessionset_config": "guide/row-level-security.md#a-tenant-known-after-the-session-opens",
    "guide/row-level-security.md#nesting-and-inheritance": "concepts/row-level-security.md#nested-sessions",
    "guide/row-level-security.md#scope-stability-mid-operation": "concepts/row-level-security.md#visibility-of-a-new-value",
    "guide/row-level-security.md#the-nullif-contract": "concepts/row-level-security.md#policy-expressions",
    "guide/row-level-security.md#bypassrls": "concepts/row-level-security.md#force-and-bypassrls",
    "guide/row-level-security.md#the-json-claims-recipe": "guide/row-level-security.md#json-claims",
    "guide/row-level-security.md#framework-integration": "guide/row-level-security.md#web-framework-middleware",
    "guide/row-level-security.md#outside-the-request-context": "guide/row-level-security.md#background-jobs",
    "guide/row-level-security.md#connection-delivery-mode": "concepts/row-level-security.md#connection-delivery",
    "guide/row-level-security.md#raw-policies-and-drift": "concepts/row-level-security.md#drift-and-ownership",
    "guide/row-level-security.md#migrations-and-alembic": "guide/row-level-security.md#deploy-order",
    # The deleted Next steps page: every section lands on the quickstart's.
    "getting-started/next-steps.md#learn-by-use-case": _NEXT_STEPS,
    "getting-started/next-steps.md#building-an-api": _NEXT_STEPS,
    "getting-started/next-steps.md#data-processing": _NEXT_STEPS,
    "getting-started/next-steps.md#production-deployment": _NEXT_STEPS,
    "getting-started/next-steps.md#common-patterns": _NEXT_STEPS,
    "getting-started/next-steps.md#reference-material": _NEXT_STEPS,
    "getting-started/next-steps.md#get-help": _NEXT_STEPS,
}
"""Every anchor that moved to another page, or was renamed on its own page,
during the docs rewrite, and the section that replaced it."""


def _site_config() -> dict:
    with (REPO_ROOT / "zensical.toml").open("rb") as config:
        return tomllib.load(config)["project"]


def _nav_pages(
    entries: list, trail: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], str, str]]:
    """Yield ``(sections, label, page)`` for every page in a nav subtree."""
    for entry in entries:
        ((label, target),) = entry.items()
        if isinstance(target, list):
            yield from _nav_pages(target, (*trail, label))
        else:
            yield trail, label, target


def _front_matter(page: Path) -> dict:
    text = page.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    return (yaml.safe_load(match.group(1)) or {}) if match else {}


def _url_path(page: str) -> str:
    """The URL path of a page, without slashes: ``guide/queries.md`` → ``guide/queries``."""
    return "" if page == "index.md" else page.removesuffix(".md")


def _source_pages() -> list[str]:
    return sorted(
        page.relative_to(DOCS_PAGES).as_posix() for page in DOCS_PAGES.rglob("*.md")
    )


def test_nav_has_the_six_tabs() -> None:
    tabs = [next(iter(entry)) for entry in _site_config()["nav"]]
    assert tabs == NAV_TABS


def test_every_page_is_in_the_nav_once() -> None:
    in_nav = [page for _, _, page in _nav_pages(_site_config()["nav"])]
    duplicated = sorted({page for page in in_nav if in_nav.count(page) > 1})
    assert not duplicated, f"pages listed twice in the nav: {duplicated}"
    missing = sorted(set(_source_pages()) - set(in_nav))
    assert not missing, f"pages missing from the nav: {missing}"


def test_nav_labels_are_unique() -> None:
    labels = [label for _, label, _ in _nav_pages(_site_config()["nav"])]
    assert sorted({label for label in labels if labels.count(label) > 1}) == []


def _h1(page_html: str) -> str:
    match = re.search(r"<h1\b[^>]*>(.*?)</h1>", page_html, re.DOTALL)
    assert match, "page has no <h1>"
    heading = re.sub(r'<a class="headerlink".*?</a>', "", match.group(1))
    return html.unescape(_TAG.sub("", heading)).strip()


def test_every_nav_label_equals_its_page_h1(built_site: Path) -> None:
    mismatched = [
        f"{page}: nav {label!r}, page {heading!r}"
        for _, label, page in _nav_pages(_site_config()["nav"])
        if (heading := _h1(_page(built_site, _url_path(page)))) != label
    ]
    assert not mismatched, "nav labels differ from page titles:\n" + "\n".join(
        mismatched
    )


def test_every_page_has_a_one_sentence_description() -> None:
    lacking = []
    for page in _source_pages():
        description = _front_matter(DOCS_PAGES / page).get("description")
        if (
            not isinstance(description, str)
            or not description.endswith(".")
            or ". " in description
        ):
            lacking.append(page)
    assert not lacking, f"pages without a one-sentence description: {lacking}"


def test_page_descriptions_reach_the_built_pages(built_site: Path) -> None:
    for page in _source_pages():
        description = _front_matter(DOCS_PAGES / page)["description"]
        meta = re.search(
            r'<meta name="description" content="([^"]*)"',
            _page(built_site, _url_path(page)),
        )
        assert meta, f"{page} has no description meta tag"
        assert html.unescape(meta.group(1)) == description, page


def test_pages_new_in_the_rewrite_are_marked_new() -> None:
    marked = {
        page
        for page in _source_pages()
        if _front_matter(DOCS_PAGES / page).get("status") == "new"
    }
    assert marked == NEW_PAGES
    assert "new" in _site_config()["extra"]["status"]


@pytest.mark.parametrize("old", sorted(MOVED_PAGES))
def test_moved_page_is_gone_and_its_url_redirects(built_site: Path, old: str) -> None:
    assert not (DOCS_PAGES / old).exists(), f"{old} is still in the source"
    old_url = f"https://ferro-orm.x54.sh/{_url_path(old)}/"
    targets = _redirect_targets(_page(built_site, _url_path(old)))
    path, _, fragment = MOVED_PAGES[old].partition("#")
    expected = f"/{_url_path(path)}/" + (f"#{fragment}" if fragment else "")
    assert urljoin(old_url, targets[""]) == urljoin(old_url, expected)
    sitemap = (built_site / "sitemap.xml").read_text(encoding="utf-8")
    assert f"/{_url_path(old)}/<" not in sitemap


def test_moved_anchors_redirect_to_existing_sections(built_site: Path) -> None:
    redirects = json.loads((built_site / "redirect.json").read_text(encoding="utf-8"))
    for old, new in MOVED_ANCHORS.items():
        old_page, _, old_fragment = old.partition("#")
        new_page, _, new_fragment = new.partition("#")
        key = f"{_url_path(old_page)}/#{old_fragment}"
        expected = f"{_url_path(new_page)}/" + (
            f"#{new_fragment}" if new_fragment else ""
        )
        assert redirects.get(key) == expected, (
            f"{old} redirects to {redirects.get(key)}"
        )
        if new_fragment:
            target = _page(built_site, _url_path(new_page))
            assert f'id="{new_fragment}"' in target, f"{new} has no such anchor"
        if (DOCS_PAGES / old_page).exists():
            assert f'id="{old_fragment}"' not in _page(
                built_site, _url_path(old_page)
            ), f"{old} is a live section; redirecting it would hide it"


# --- Markup that renders wrong ---

_PRE = re.compile(r"<pre\b.*?</pre>", re.DOTALL)
_INLINE_CODE = re.compile(r"<code\b[^>]*>.*?</code>", re.DOTALL)
_PARAGRAPH = re.compile(r"<p>(.*?)</p>", re.DOTALL)
_SPHINX_ROLE = re.compile(r":(?:py:)?(?:class|meth|func|attr|data|mod|exc|obj):(?=\S)")
_LEFTOVER_REFERENCE = re.compile(r"\[[^\[\]]*\]\[[\w.:-]*\]")
_LIST_ITEM_LINE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s")
_SECTION_HEADER_LINE = re.compile(
    r"^\s*(?:Args|Arguments|Parameters|Attributes|Returns|Raises|Yields|Examples):\s*$"
)
_SECTION_TABLE = re.compile(
    r'<span class="doc-section-title">(\w+):</span></p>\s*<table>(.*?)</table>',
    re.DOTALL,
)
_SECTION_ROW = re.compile(r'<tr class="doc-section-item">(.*?)</tr>', re.DOTALL)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
_SENTENCE_END = re.compile(r"[.?!]\)?$")
_GRIFFE_WARNING = re.compile(r"^\s*griffe: .*$", re.MULTILINE)


def _text(fragment: str) -> str:
    return html.unescape(_TAG.sub("", fragment))


def markup_defects(page_html: str) -> list[str]:
    """Name each place a page renders Markdown or a docstring wrongly.

    Code blocks are skipped and inline code is blanked, so an example that
    shows ``x[0][1]`` or a Sphinx role on purpose is not a defect. Found:

    - a literal Sphinx role (``:class:`Model```), which MkDocs never renders;
    - a ``[text][target]`` left as text: a cross-reference nothing resolved;
    - a paragraph holding a line that starts ``- `` or ``1. ``: a list with
      no blank line before it, merged into the paragraph;
    - a paragraph holding an ``Args:`` or ``Attributes:`` line: a docstring
      section the parser did not recognise, rendered as prose;
    - a ``Returns:``/``Attributes:``/... table row whose description stops
      mid-sentence: a continuation line not indented, split into its own row.
    """
    defects = []
    prose = _INLINE_CODE.sub("<code>C</code>", _PRE.sub("", page_html))
    text = _text(prose)
    defects += [f"Sphinx role {m.group()}" for m in _SPHINX_ROLE.finditer(text)]
    defects += [
        f"unresolved cross-reference {m.group()}"
        for m in _LEFTOVER_REFERENCE.finditer(text)
    ]
    for paragraph in _PARAGRAPH.findall(prose):
        if paragraph.startswith('<span class="doc-section-title">'):
            continue
        lines = _text(paragraph).splitlines()
        if any(_LIST_ITEM_LINE.match(line) for line in lines[1:]):
            defects.append(f"list merged into a paragraph: {lines[0]!r}")
        defects += [
            f"docstring section rendered as prose: {line.strip()!r}"
            for line in lines
            if _SECTION_HEADER_LINE.match(line)
        ]
    for title, table in _SECTION_TABLE.findall(page_html):
        headers = re.findall(r"<th>(.*?)</th>", table)
        if "Description" not in headers:
            continue
        column = headers.index("Description")
        for row in _SECTION_ROW.findall(table):
            description = " ".join(_text(_CELL.findall(row)[column]).split())
            if not _SENTENCE_END.search(description):
                defects.append(f"{title} row is a sentence fragment: {description!r}")
    return defects


def docstring_warnings(build_log: str) -> list[str]:
    """Return griffe's warnings from a build's output.

    A docstring item griffe cannot read (an ``Args:`` entry with no
    ``name: description`` pair) is dropped from the page with only a
    warning, and ``--strict`` does not fail on it.
    """
    return [match.group().strip() for match in _GRIFFE_WARNING.finditer(build_log)]


def test_no_page_renders_markup_defects(built_site: Path) -> None:
    defects = [
        f"{url}: {defect}"
        for url, page_html in rendered_pages(built_site)
        for defect in markup_defects(page_html)
    ]
    assert not defects, "pages render markup wrongly:\n" + "\n".join(defects)


def test_every_docstring_parses_without_a_warning(built_site: Path) -> None:
    log = (built_site.parent / BUILD_LOG).read_text(encoding="utf-8")
    assert docstring_warnings(log) == []


SEEDED_MODULE = '''\
"""A module seeded with one docstring defect per check."""


def sphinx_role(value):
    """Return a :class:`ferro.Model` for ``value``."""


def unparsed_args(value):
    """Return ``value``.

    Args:
    value the value, with no colon and no indent
    """


def unreadable_args(value):
    """Return ``value``.

    Args:
        value the value, with no colon
    """


def split_returns():
    """Return a value.

    Returns:
        A value whose description continues
        on a second line that is not indented.
    """


class SplitAttributes:
    """A class whose attribute description splits into two rows.

    Attributes:
        name: The name, whose description runs
        on: to a second row.
    """


class DroppedAttributes:
    """A class whose attributes section is not indented.

    Attributes:
    name the name
    """


def unresolved():
    """Point at [`Nope`][ferro.Nope]."""
'''

SEEDED_PAGE = """\
---
description: "A page seeded with one Markdown defect per check."
---

# Seeded

A dash list with no blank line before it:
- one
- two

A numbered list with no blank line before it:
1. first
2. second

::: seeded
"""


@pytest.fixture(scope="session")
def seeded_site(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    """Build the site with a seeded page and module; return its HTML and log.

    The build is not strict: a strict build stops at the unresolved
    reference before the other defects reach a page.
    """
    project = _scratch_project(tmp_path_factory.mktemp("seeded-site"))
    (project / "seeded_src").mkdir()
    (project / "seeded_src" / "seeded.py").write_text(SEEDED_MODULE, encoding="utf-8")
    (project / "docs" / "pages" / "seeded.md").write_text(
        SEEDED_PAGE, encoding="utf-8"
    )
    config = project / "zensical.toml"
    text = config.read_text(encoding="utf-8")
    assert '\npaths = ["src"]\n' in text
    config.write_text(
        text.replace('\npaths = ["src"]\n', '\npaths = ["src", "seeded_src"]\n'),
        encoding="utf-8",
    )
    result = _build(project)
    assert result.returncode == 0, result.stdout + result.stderr
    page = _page(project / "site", "seeded")
    return page, (project / BUILD_LOG).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "expected",
    [
        "Sphinx role :class:",
        "unresolved cross-reference [C][ferro.Nope]",
        "list merged into a paragraph: 'A dash list with no blank line before it:'",
        "list merged into a paragraph: "
        "'A numbered list with no blank line before it:'",
        "docstring section rendered as prose: 'Args:'",
        "docstring section rendered as prose: 'Attributes:'",
        "Returns row is a sentence fragment: 'A value whose description continues'",
        "Attributes row is a sentence fragment: 'The name, whose description runs'",
    ],
)
def test_seeded_markup_defect_is_caught(
    seeded_site: tuple[str, str], expected: str
) -> None:
    page, _ = seeded_site
    assert expected in markup_defects(page)


def test_seeded_unreadable_docstring_item_is_caught(
    seeded_site: tuple[str, str],
) -> None:
    _, log = seeded_site
    assert any("seeded.py:" in warning for warning in docstring_warnings(log)), log
