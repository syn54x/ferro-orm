"""The model-change casebook (cases A–F) as before/after modelsets.

Each case is the pair of ``models.py`` bodies a generator test
(``tests/test_generate_*.py``, tickets #524–#536) migrates between, built
from those tests' own module-level helpers so the parity pins in
``tests/test_cross_emitter_parity.py`` exercise exactly the changes the
generator tests round-trip — never a second copy of a model.
"""

from __future__ import annotations

from dataclasses import dataclass

from tests import test_generate_backfill as backfill
from tests import test_generate_columns as columns
from tests import test_generate_enum_removal as enum_removal
from tests import test_generate_enums as enums
from tests import test_generate_indexes as indexes
from tests import test_generate_postgres_staging as staging
from tests import test_generate_renames as renames
from tests import test_generate_row_security as row_security
from tests import test_generate_sqlite_rebuild as sqlite_rebuild
from tests.test_migrate_new import AUTHOR, LIBRARY


@dataclass(frozen=True)
class Case:
    """One casebook change: ``before`` is migration ``0001``'s models,
    ``after`` the edit ``0002`` generates."""

    id: str
    before: str
    after: str


_SHIPMENT = """

class EnmShipKind(StrEnum):
    POST = "post"
    COURIER = "courier"


class EnmShipment(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    kind: EnmShipKind
"""

_POST_DROPPED = """
class Status(StrEnum):
    DRAFT = "draft"
    LIVE = "live"


class Author(Model):
    id: Annotated[int | None, FerroField(primary_key=True)] = None
    name: Annotated[str, FerroField(unique=True)]
    status: Status = Status.DRAFT
"""

_CHECKED_MOOD = (
    columns.MOOD
    + AUTHOR
    + '    mood: Annotated[Mood | None, FerroField(db_type="text", db_check=True)]'
    + " = None\n"
)

_TEAM_FK = (
    columns.TEAM
    + '    members: Relation[list["Author"]] = BackRef()\n'
    + AUTHOR
    + '    team: Annotated[Team | None, ForeignKey(related_name="members", '
    'on_delete="SET NULL")] = None\n'
)

_STAGING_HEAD = staging.HEAD


CASES: tuple[Case, ...] = (
    # -- A: one column ------------------------------------------------------------
    Case("A1-optional-column", AUTHOR, columns.BIO),
    Case(
        "A1-optional-enum-column",
        AUTHOR,
        columns.MOOD + AUTHOR + "    mood: Mood | None = None\n",
    ),
    Case("A1-optional-checked-column", AUTHOR, _CHECKED_MOOD),
    Case(
        "A2-required-column-with-a-literal-default",
        AUTHOR,
        AUTHOR + '    tier: str = "free"\n',
    ),
    Case(
        "A2b-required-column-with-a-factory",
        backfill.HEAD,
        backfill.HEAD + "    token: UUID = Field(default_factory=uuid.uuid4)\n",
    ),
    Case(
        "A3-required-column-without-a-default",
        backfill.HEAD,
        backfill.HEAD + backfill.SLUG + backfill.NONEMPTY,
    ),
    Case("A4-drop-an-optional-column", columns.BIO, AUTHOR),
    Case("A4-drop-a-required-column", columns.NICKNAME, AUTHOR),
    Case("A4-drop-a-checked-column", _CHECKED_MOOD, AUTHOR),
    Case("A5-rename-a-column", renames.A5_BEFORE, renames.A5_AFTER),
    Case(
        "A5-rename-an-indexed-column",
        renames.INDEXED_BEFORE,
        renames.INDEXED_AFTER,
    ),
    Case(
        "A6-change-a-type",
        sqlite_rebuild.AGE_INT,
        sqlite_rebuild.AGE_TEXT,
    ),
    Case(
        "A7a-make-a-column-required",
        backfill.HEAD + backfill.OPTIONAL_SLUG,
        backfill.HEAD + backfill.REQUIRED_SLUG,
    ),
    Case(
        "A7b-make-a-column-optional",
        columns.NICKNAME,
        sqlite_rebuild.OPTIONAL_NICKNAME,
    ),
    Case(
        "A9-add-a-unique",
        _STAGING_HEAD + staging.EMAIL,
        _STAGING_HEAD + staging.UNIQUE_EMAIL,
    ),
    Case(
        "A9-drop-a-unique",
        _STAGING_HEAD + staging.UNIQUE_EMAIL,
        _STAGING_HEAD + staging.EMAIL,
    ),
    Case(
        "A10-add-a-table-check",
        _STAGING_HEAD + staging.EMAIL,
        _STAGING_HEAD + staging.EMAIL + staging.NONEMPTY,
    ),
    Case(
        "A10-change-a-table-check",
        sqlite_rebuild.checked("author.age > 0"),
        sqlite_rebuild.checked("author.age > 1"),
    ),
    Case(
        "A10-drop-a-table-check",
        sqlite_rebuild.checked("author.age > 0"),
        sqlite_rebuild.CHECKS + "    age: int | None = None\n",
    ),
    Case(
        "A11-redefine-an-index-under-a-cut-name",
        indexes.NARROW,
        indexes.WIDE,
    ),
    Case(
        "A11-redefine-an-index-under-a-joined-name",
        indexes.JOINED,
        indexes.REJOINED,
    ),
    # -- B: whole models ----------------------------------------------------------
    Case(
        "B1-new-model-with-a-new-type",
        enums.models(refund=False),
        enums.models(refund=False) + _SHIPMENT,
    ),
    Case(
        "B1-new-model-reusing-a-type",
        enums.models(refund=False),
        enums.models(),
    ),
    Case("B1-new-model-with-a-unique", AUTHOR, AUTHOR + columns.TAG),
    Case("B2-drop-a-model", LIBRARY, _POST_DROPPED),
    Case(
        "B3-rename-a-model",
        renames.b3("writer", "postgres"),
        renames.b3("author", "postgres"),
    ),
    # -- C: foreign keys ----------------------------------------------------------
    Case("C1-add-an-optional-foreign-key", columns.TEAM + AUTHOR, _TEAM_FK),
    Case(
        "C1-add-a-required-foreign-key",
        backfill.TEAM + backfill.HEAD,
        backfill.TEAM + backfill.MEMBERS + backfill.HEAD + backfill.REQUIRED_TEAM,
    ),
    Case(
        "C2-drop-a-foreign-key-column",
        sqlite_rebuild.members("Team"),
        sqlite_rebuild.TEAM + sqlite_rebuild.CLUB + AUTHOR,
    ),
    Case(
        "C3-retarget-a-foreign-key",
        sqlite_rebuild.members("Team"),
        sqlite_rebuild.members("Club"),
    ),
    Case(
        "C4-drop-a-foreign-key-keep-its-column",
        indexes.LINKED,
        indexes.UNLINKED,
    ),
    # -- D: enums -----------------------------------------------------------------
    Case(
        "D1-add-a-label",
        enums.models(),
        enums.models(("paid", "canceled", "refunded")),
    ),
    Case(
        "D1-add-a-label-and-a-column-of-its-type",
        enums.models(),
        enums.models(
            ("paid", "canceled", "refunded"),
            extra="    previous: EnmOrderStatus | None = None\n",
        ),
    ),
    Case(
        "D2-remove-a-label",
        enum_removal.models(enum_removal.ALL),
        enum_removal.models(enum_removal.KEPT),
    ),
    Case("D3-rename-a-label", enums.models(), enums.RENAMED),
    Case("D4-rename-an-enum-class", enums.models(), enums.models(cls="EnmOrderState")),
    # -- E: row security ----------------------------------------------------------
    Case(
        "E1-add-row-security",
        row_security.models(declared=False),
        row_security.models(),
    ),
    Case(
        "E2-change-a-row-policy",
        row_security.models(),
        row_security.models(setting="app.tenant_id"),
    ),
    Case(
        "E3-remove-row-security",
        row_security.models(),
        row_security.models(declared=False),
    ),
    Case(
        "E-add-a-second-row-policy",
        row_security.models(),
        row_security.models(
            row_security.TENANT.format(setting="app.tenant"), row_security.OWNER
        ),
    ),
    # -- F: several changes at once -----------------------------------------------
    Case(
        "F1-several-models-at-once",
        LIBRARY,
        LIBRARY.replace(
            "    status: Status = Status.DRAFT\n",
            "    status: Status = Status.DRAFT\n    bio: str | None = None\n",
        )
        + "    subtitle: str | None = None\n",
    ),
    Case(
        "F2-a-rename-and-a-type-change",
        renames.A5_BEFORE,
        renames.A5_AFTER.replace(
            'full_name: str = Field(index=True, renamed_from="name")',
            'full_name: str = Field(index=True, renamed_from="name", '
            'db_type="varchar(80)")',
        ),
    ),
    Case(
        "F3-a-type-change-and-a-new-index",
        sqlite_rebuild.AGE_INT,
        AUTHOR + "    age: Annotated[str | None, FerroField(index=True)] = None\n",
    ),
)
