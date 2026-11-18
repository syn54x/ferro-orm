//! Dependency ordering shared by every path that emits or reconciles tables.

use std::collections::HashSet;

/// Order `items` so every FK dependency of an item is emitted before it.
///
/// This is the single dependency-order primitive: both the CREATE TABLE
/// emitter ([`crate::order_models_for_create`]) and the runtime's migrate
/// reconcile loop funnel through it, so the semantics live in one place:
///
/// - A dependency on the item's **own** table (a self-referential FK) is not
///   an ordering constraint — the inline `REFERENCES` resolves against the
///   table its own `CREATE TABLE` statement is creating (#302).
/// - A dependency on a table **outside** `items` (already live in the DB)
///   does not constrain ordering.
/// - Tables that reference each other have no such order. When no remaining
///   item is ready, the first remaining one, in input order, is emitted and
///   the ordering goes on: an item that only references the cycle still
///   follows its target, and a cycle of three leaves one forward reference,
///   not two. The emitted item names a table created after it; SQLite
///   accepts that `REFERENCES`, and on Postgres the key is deferred past the
///   last `CREATE TABLE` (`deferred_foreign_keys`, #304).
///
/// Ordering is deterministic: input order is preserved except where a
/// dependency forces an item later.
pub fn order_by_dependencies<T>(
    items: Vec<T>,
    table_of: impl Fn(&T) -> String,
    deps_of: impl Fn(&T) -> Vec<String>,
) -> Vec<T> {
    let mut remaining = items;
    let mut ordered = Vec::with_capacity(remaining.len());
    let mut created: HashSet<String> = HashSet::new();

    while !remaining.is_empty() {
        let available: HashSet<String> = remaining.iter().map(&table_of).collect();
        let mut progress = false;
        let mut index = 0;
        while index < remaining.len() {
            let table = table_of(&remaining[index]);
            let satisfied = deps_of(&remaining[index])
                .iter()
                .all(|dep| dep == &table || created.contains(dep) || !available.contains(dep));
            if satisfied {
                let item = remaining.remove(index);
                created.insert(table);
                ordered.push(item);
                progress = true;
            } else {
                index += 1;
            }
        }
        if !progress {
            let item = remaining.remove(0);
            created.insert(table_of(&item));
            ordered.push(item);
        }
    }
    ordered
}
