# Schema Migrations has moved

This page became the **Schema Management** group. Start at the [overview](schema/overview.md); each old section now lives here:

| Old section | Now |
| :--- | :--- |
| Three Ways to Manage Schema | [Overview › The ladder](schema/overview.md#the-ladder) |
| Auto-Migration | [Auto-migrate](schema/auto-migrate.md) |
| Applying column changes with `migrate_updates` | [Auto-migrate › Applying column changes](schema/auto-migrate.md#applying-column-changes-with-migrate_updates) |
| Evolving enums: label addition | [Auto-migrate › Evolving enums](schema/auto-migrate.md#evolving-enums-label-addition) |
| Destructive drops with `migrate_destructive` | [Auto-migrate › Destructive drops](schema/auto-migrate.md#destructive-drops-with-migrate_destructive) |
| On-demand `migrate()` | [Auto-migrate › On-demand `migrate()`](schema/auto-migrate.md#on-demand-migrate) |
| Safety guidance | [Auto-migrate › Safety guidance](schema/auto-migrate.md#safety-guidance) |
| Alembic for Production | [Alembic](schema/alembic.md) |
| Choosing a Workflow | [Overview › Choosing](schema/overview.md#choosing) |

<script>
  (function () {
    var moved = {
      "": "schema/overview/",
      "#three-ways-to-manage-schema": "schema/overview/#the-ladder",
      "#auto-migration": "schema/auto-migrate/",
      "#creating-tables-with-auto_migratetrue": "schema/auto-migrate/#creating-tables-with-auto_migratetrue",
      "#applying-column-changes-with-migrate_updates": "schema/auto-migrate/#applying-column-changes-with-migrate_updates",
      "#evolving-enums-label-addition": "schema/auto-migrate/#evolving-enums-label-addition",
      "#destructive-drops-with-migrate_destructive": "schema/auto-migrate/#destructive-drops-with-migrate_destructive",
      "#on-demand-migrate": "schema/auto-migrate/#on-demand-migrate",
      "#safety-guidance": "schema/auto-migrate/#safety-guidance",
      "#alembic-for-production": "schema/alembic/",
      "#install": "schema/alembic/#install",
      "#initialize": "schema/alembic/#initialize",
      "#configure-envpy": "schema/alembic/#configure-envpy",
      "#autogenerate": "schema/alembic/#one-decider",
      "#review-apply": "schema/alembic/#one-decider",
      "#choosing-a-workflow": "schema/overview/#choosing"
    };
    var target = moved[window.location.hash] || moved[""];
    window.location.replace(new URL("../" + target, window.location.href).href);
  })();
</script>
