//! Canonicalizing row-policy expressions so Postgres's re-spelling is not drift.
//!
//! A policy body makes a round trip that changes its text but not its meaning.
//! Ferro renders
//!
//! ```sql
//! "ledger_id" = NULLIF(current_setting('pinch.ledger_id', true), '')::uuid
//! ```
//!
//! and `pg_get_expr(polqual, polrelid)` hands back
//!
//! ```sql
//! (ledger_id = (NULLIF(current_setting('pinch.ledger_id'::text, true), ''::text))::uuid)
//! ```
//!
//! Compared as text those differ on every connect, which would make an
//! unchanged declaration rebuild its policy forever. This module reduces both
//! strings to the same canonical token stream so the comparison answers the
//! only question that matters: *is this the same predicate?*
//!
//! What the server does, and what this module undoes:
//!
//! | Postgres writes | canonical form |
//! |---|---|
//! | `(expr)` wrapping the whole body | unwrapped |
//! | `(a IS NOT NULL) AND (b > 0)` | `a is not null and b > 0` — parens that cannot change grouping are dropped, by operator precedence |
//! | `'x'::text`, `(title)::text` | `'x'`, `title` — the implicit text painting is dropped |
//! | `"Ledger_id"` vs `ledger_id` | unquoted when the identifier needs no quotes |
//! | `SELECT probe_uid() AS probe_uid` | `select probe_uid()` — the deparser names every select-list output |
//! | `SELECT membership.doc_id … WHERE membership.member = …` | `select doc_id … where member = …` — the deparser qualifies column references inside sub-selects |
//! | newlines and indentation | single spaces |
//!
//! The last two rules trade a little precision for convergence, and the trade
//! is stated rather than hidden (see [`super::normalize_row_policy_expr`]).

/// One lexical unit of a policy expression.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Token {
    /// An unquoted word — identifier or keyword — folded to lower case, the
    /// way Postgres folds unquoted identifiers.
    Word(String),
    /// A quoted identifier that still needs its quotes (mixed case, reserved
    /// word, punctuation), rendered with them.
    Quoted(String),
    /// A string literal, rendered with its quotes and doubled inner quotes.
    Literal(String),
    /// An operator run (`=`, `<=`, `::`, `->>`, …) or a bare `.` / `,`.
    Op(String),
    Open,
    Close,
}

/// Operator precedence, loosest first. Used only to decide whether a
/// parenthesis can be dropped without changing grouping.
fn precedence(token: &Token) -> Option<u8> {
    let text = match token {
        Token::Op(op) => op.as_str(),
        Token::Word(word) => word.as_str(),
        _ => return None,
    };
    Some(match text {
        // A comma is not an operator, but treating it as the loosest one keeps
        // `(1, 2)` from ever losing the parentheses that make it a list.
        "," => 0,
        "or" => 1,
        "and" => 2,
        "not" => 3,
        "=" | "<>" | "!=" | "<" | ">" | "<=" | ">=" | "is" | "in" | "like" | "ilike"
        | "between" | "similar" | "~" | "~*" | "!~" | "!~*" | "@>" | "<@" | "&&" | "?"
        | "?|" | "?&" => 4,
        "||" | "->" | "->>" | "#>" | "#>>" => 5,
        "+" | "-" => 6,
        "*" | "/" | "%" => 7,
        "^" => 8,
        "::" => 9,
        _ => return None,
    })
}

/// Words that introduce a clause rather than call a function, so a `(` right
/// after one is a grouping paren and not an argument list.
const CLAUSE_WORDS: [&str; 24] = [
    "and", "or", "not", "in", "where", "having", "on", "select", "from", "values", "by",
    "when", "then", "else", "case", "exists", "all", "any", "using", "check", "returning",
    "distinct", "as", "is",
];

fn is_ident_start(ch: char) -> bool {
    ch.is_ascii_alphabetic() || ch == '_' || !ch.is_ascii()
}

fn is_ident_continue(ch: char) -> bool {
    is_ident_start(ch) || ch.is_ascii_digit() || ch == '$'
}

/// True when `word` can be written without double quotes and mean the same
/// thing — lower-case, starting with a letter or underscore.
fn is_bare_ident(word: &str) -> bool {
    let mut chars = word.chars();
    match chars.next() {
        Some(ch) if is_ident_start(ch) && !ch.is_ascii_uppercase() => {}
        _ => return false,
    }
    chars.all(|ch| is_ident_continue(ch) && !ch.is_ascii_uppercase())
}

const OPERATOR_CHARS: [char; 20] = [
    '=', '<', '>', '!', '~', '@', '#', '&', '|', '?', '+', '-', '*', '/', '%', '^', ':',
    '.', ',', ';',
];

/// Split a policy expression into [`Token`]s. Unrecognized bytes become
/// single-character `Op`s rather than being dropped: the comparison must never
/// silently equate two expressions by losing part of one.
pub(crate) fn tokenize(input: &str) -> Vec<Token> {
    let chars: Vec<char> = input.chars().collect();
    let mut tokens = Vec::new();
    let mut i = 0usize;
    while i < chars.len() {
        let ch = chars[i];
        if ch.is_whitespace() {
            i += 1;
            continue;
        }
        match ch {
            '(' => {
                tokens.push(Token::Open);
                i += 1;
            }
            ')' => {
                tokens.push(Token::Close);
                i += 1;
            }
            '\'' => {
                let (value, next) = take_delimited(&chars, i, '\'');
                tokens.push(Token::Literal(value));
                i = next;
            }
            '"' => {
                let (value, next) = take_delimited(&chars, i, '"');
                if is_bare_ident(&value) {
                    tokens.push(Token::Word(value));
                } else {
                    tokens.push(Token::Quoted(value));
                }
                i = next;
            }
            _ if ch.is_ascii_digit() => {
                let start = i;
                while i < chars.len() && (chars[i].is_ascii_digit() || chars[i] == '.') {
                    i += 1;
                }
                tokens.push(Token::Word(chars[start..i].iter().collect()));
            }
            _ if is_ident_start(ch) => {
                let start = i;
                while i < chars.len() && is_ident_continue(chars[i]) {
                    i += 1;
                }
                let word: String = chars[start..i].iter().collect();
                tokens.push(Token::Word(word.to_lowercase()));
            }
            _ if OPERATOR_CHARS.contains(&ch) => {
                let start = i;
                while i < chars.len() && OPERATOR_CHARS.contains(&chars[i]) {
                    i += 1;
                }
                let run: String = chars[start..i].iter().collect();
                // `.` and `,` are separators, never part of a longer operator.
                push_operator_run(&run, &mut tokens);
            }
            other => {
                tokens.push(Token::Op(other.to_string()));
                i += 1;
            }
        }
    }
    tokens
}

/// Split a run of operator characters so separators stand alone
/// (`membership.doc_id` must not read as one `.doc_id` operator).
fn push_operator_run(run: &str, tokens: &mut Vec<Token>) {
    let mut current = String::new();
    for ch in run.chars() {
        if ch == '.' || ch == ',' || ch == ';' {
            if !current.is_empty() {
                tokens.push(Token::Op(std::mem::take(&mut current)));
            }
            tokens.push(Token::Op(ch.to_string()));
        } else {
            current.push(ch);
        }
    }
    if !current.is_empty() {
        tokens.push(Token::Op(current));
    }
}

/// Read a `'`- or `"`-delimited run, honoring the doubled-delimiter escape.
/// Returns the *inner* text and the index just past the closing delimiter.
fn take_delimited(chars: &[char], start: usize, delim: char) -> (String, usize) {
    let mut value = String::new();
    let mut i = start + 1;
    while i < chars.len() {
        if chars[i] == delim {
            if chars.get(i + 1) == Some(&delim) {
                value.push(delim);
                i += 2;
                continue;
            }
            return (value, i + 1);
        }
        value.push(chars[i]);
        i += 1;
    }
    (value, i)
}

/// A parenthesis-aware view of the token stream.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Node {
    Leaf(Token),
    /// A parenthesized group. `call` marks an argument list (`nullif(…)`),
    /// which is never a grouping paren and never dropped.
    Group { call: bool, children: Vec<Node> },
}

/// Build the paren tree. Unbalanced input (never produced by the catalog or by
/// ferro, but possible from hand-written raw SQL) closes implicitly.
pub(crate) fn parse(tokens: &[Token]) -> Vec<Node> {
    let mut index = 0usize;
    parse_group(tokens, &mut index)
}

fn parse_group(tokens: &[Token], index: &mut usize) -> Vec<Node> {
    let mut nodes: Vec<Node> = Vec::new();
    while *index < tokens.len() {
        match &tokens[*index] {
            Token::Open => {
                let call = matches!(
                    nodes.last(),
                    Some(Node::Leaf(Token::Word(word)))
                        if !CLAUSE_WORDS.contains(&word.as_str())
                );
                *index += 1;
                let children = parse_group(tokens, index);
                nodes.push(Node::Group { call, children });
            }
            Token::Close => {
                *index += 1;
                return nodes;
            }
            token => {
                nodes.push(Node::Leaf(token.clone()));
                *index += 1;
            }
        }
    }
    nodes
}

/// Drop `::text` casts — Postgres paints them onto string literals and onto
/// `varchar` columns compared with text, and ferro never renders one itself.
fn strip_text_casts(nodes: Vec<Node>) -> Vec<Node> {
    let mut out: Vec<Node> = Vec::new();
    let mut i = 0usize;
    while i < nodes.len() {
        if let Node::Leaf(Token::Op(op)) = &nodes[i]
            && op == "::"
            && matches!(nodes.get(i + 1), Some(Node::Leaf(Token::Word(word))) if word == "text")
        {
            i += 2;
            continue;
        }
        let node = match nodes[i].clone() {
            Node::Group { call, children } => Node::Group {
                call,
                children: strip_text_casts(children),
            },
            leaf => leaf,
        };
        out.push(node);
        i += 1;
    }
    out
}

/// Drop the relation qualifier from a column reference (`membership.doc_id` →
/// `doc_id`). A qualified *function* name (`auth.uid()`) keeps its schema.
fn strip_column_qualifiers(nodes: Vec<Node>) -> Vec<Node> {
    let mut out: Vec<Node> = Vec::new();
    let mut i = 0usize;
    while i < nodes.len() {
        let is_qualifier = matches!(nodes.get(i), Some(Node::Leaf(Token::Word(_))) | Some(Node::Leaf(Token::Quoted(_))))
            && matches!(nodes.get(i + 1), Some(Node::Leaf(Token::Op(op))) if op == ".")
            && matches!(
                nodes.get(i + 2),
                Some(Node::Leaf(Token::Word(_))) | Some(Node::Leaf(Token::Quoted(_)))
            )
            && !matches!(nodes.get(i + 3), Some(Node::Group { call: true, .. }));
        if is_qualifier {
            i += 2;
            continue;
        }
        let node = match nodes[i].clone() {
            Node::Group { call, children } => Node::Group {
                call,
                children: strip_column_qualifiers(children),
            },
            leaf => leaf,
        };
        out.push(node);
        i += 1;
    }
    out
}

/// Drop select-list output aliases. `pg_get_expr` names every select-list item
/// (`SELECT probe_uid() AS probe_uid`); an alias inside a policy predicate can
/// never change which rows match.
fn strip_select_aliases(nodes: Vec<Node>) -> Vec<Node> {
    let mut out: Vec<Node> = Vec::new();
    let mut in_select_list = false;
    let mut i = 0usize;
    while i < nodes.len() {
        if let Node::Leaf(Token::Word(word)) = &nodes[i] {
            match word.as_str() {
                "select" => in_select_list = true,
                "from" => in_select_list = false,
                "as" if in_select_list => {
                    let alias_is_last = matches!(
                        nodes.get(i + 1),
                        Some(Node::Leaf(Token::Word(_))) | Some(Node::Leaf(Token::Quoted(_)))
                    ) && match nodes.get(i + 2) {
                        None => true,
                        Some(Node::Leaf(Token::Op(op))) => op == ",",
                        Some(Node::Leaf(Token::Word(next))) => next == "from",
                        _ => false,
                    };
                    if alias_is_last {
                        i += 2;
                        continue;
                    }
                }
                _ => {}
            }
        }
        // A bare `AS` outside a select list is noise the deparser omits for
        // table aliases (`FROM (VALUES (1)) AS t` → `FROM (VALUES (1)) t`).
        if matches!(&nodes[i], Node::Leaf(Token::Word(word)) if word == "as") && !in_select_list {
            i += 1;
            continue;
        }
        let node = match nodes[i].clone() {
            Node::Group { call, children } => Node::Group {
                call,
                children: strip_select_aliases(children),
            },
            leaf => leaf,
        };
        out.push(node);
        i += 1;
    }
    out
}

/// The loosest-binding operator at this level, or `None` when the group holds
/// no operator at all (a bare column, a literal, a function call).
fn loosest_precedence(nodes: &[Node]) -> Option<u8> {
    nodes
        .iter()
        .filter_map(|node| match node {
            Node::Leaf(token) => precedence(token),
            Node::Group { .. } => None,
        })
        .min()
}

/// Whether a group's content forces it to keep its parentheses regardless of
/// precedence: a sub-select or a `VALUES` list is only legal parenthesized.
fn is_subquery(children: &[Node]) -> bool {
    matches!(
        children.first(),
        Some(Node::Leaf(Token::Word(word))) if word == "select" || word == "values" || word == "with"
    )
}

/// Drop every parenthesis that cannot change how the expression groups.
///
/// A group goes when it is an argument list's sibling — that is, not a call
/// group, not a sub-select — and its own loosest operator binds *tighter* than
/// whichever operator sits beside it. `(a IS NOT NULL) AND (b > 0)` loses both
/// pairs (comparison binds tighter than `AND`); `(a OR b) AND c` keeps its
/// pair (`OR` binds looser than `AND`), because dropping it would change the
/// predicate.
fn drop_redundant_parens(nodes: Vec<Node>) -> Vec<Node> {
    let lowered: Vec<Node> = nodes
        .into_iter()
        .map(|node| match node {
            Node::Group { call, children } => Node::Group {
                call,
                children: drop_redundant_parens(children),
            },
            leaf => leaf,
        })
        .collect();

    let mut out: Vec<Node> = Vec::new();
    for (position, node) in lowered.iter().enumerate() {
        let Node::Group {
            call: false,
            children,
        } = node
        else {
            out.push(node.clone());
            continue;
        };
        let holds_a_list = children
            .iter()
            .any(|child| matches!(child, Node::Leaf(Token::Op(op)) if op == ","));
        if is_subquery(children) || holds_a_list {
            out.push(node.clone());
            continue;
        }
        let neighbor_precedence = [
            position.checked_sub(1).and_then(|prev| lowered.get(prev)),
            lowered.get(position + 1),
        ]
        .into_iter()
        .flatten()
        .filter_map(|neighbor| match neighbor {
            Node::Leaf(token) => precedence(token),
            Node::Group { .. } => None,
        })
        .max();
        let inner = loosest_precedence(children);
        let droppable = match (inner, neighbor_precedence) {
            // Nothing beside it: the parens can only be decoration.
            (_, None) => true,
            // No operator inside: a call, a column, a literal — never grouped.
            (None, Some(_)) => true,
            (Some(inner), Some(outer)) => inner > outer,
        };
        if droppable {
            out.extend(children.iter().cloned());
        } else {
            out.push(node.clone());
        }
    }
    out
}

/// Render a canonical string. Spacing is uniform rather than pretty — both
/// sides of the comparison run through this same renderer, so the only
/// requirement is that it be deterministic.
fn render(nodes: &[Node], out: &mut String) {
    for node in nodes {
        match node {
            Node::Leaf(token) => {
                let text = match token {
                    Token::Word(word) => word.clone(),
                    Token::Quoted(word) => format!("\"{}\"", word.replace('"', "\"\"")),
                    Token::Literal(value) => format!("'{}'", value.replace('\'', "''")),
                    Token::Op(op) => op.clone(),
                    Token::Open => "(".to_string(),
                    Token::Close => ")".to_string(),
                };
                push_spaced(out, &text);
            }
            Node::Group { children, .. } => {
                push_spaced(out, "(");
                render(children, out);
                out.push(')');
            }
        }
    }
}

fn push_spaced(out: &mut String, text: &str) {
    let joinable = matches!(text, ")" | "," | "." | "::" | ";");
    let last_joinable = out.ends_with('(') || out.ends_with('.') || out.ends_with("::");
    if !out.is_empty() && !joinable && !last_joinable {
        out.push(' ');
    }
    out.push_str(text);
}

/// Spell the operators Postgres normalizes one way: `!=` is stored as `<>`.
fn canonicalize_operators(nodes: Vec<Node>) -> Vec<Node> {
    nodes
        .into_iter()
        .map(|node| match node {
            Node::Leaf(Token::Op(op)) if op == "!=" => Node::Leaf(Token::Op("<>".to_string())),
            Node::Group { call, children } => Node::Group {
                call,
                children: canonicalize_operators(children),
            },
            leaf => leaf,
        })
        .collect()
}

/// Rewrite `CAST(x AS t)` to `x::t`, the form the catalog stores.
fn rewrite_casts(nodes: Vec<Node>) -> Vec<Node> {
    let mut out: Vec<Node> = Vec::new();
    let mut i = 0usize;
    while i < nodes.len() {
        if matches!(&nodes[i], Node::Leaf(Token::Word(word)) if word == "cast")
            && let Some(Node::Group {
                call: true,
                children,
            }) = nodes.get(i + 1)
        {
            let children = rewrite_casts(children.clone());
            if let Some(split) = children
                .iter()
                .position(|node| matches!(node, Node::Leaf(Token::Word(word)) if word == "as"))
            {
                let (value, target) = children.split_at(split);
                out.push(Node::Group {
                    call: false,
                    children: value.to_vec(),
                });
                out.push(Node::Leaf(Token::Op("::".to_string())));
                out.extend(target[1..].iter().cloned());
                i += 2;
                continue;
            }
        }
        let node = match nodes[i].clone() {
            Node::Group { call, children } => Node::Group {
                call,
                children: rewrite_casts(children),
            },
            leaf => leaf,
        };
        out.push(node);
        i += 1;
    }
    out
}

/// Rewrite the array forms Postgres stores an `IN` list as:
/// `= ANY (ARRAY[…])` back to `IN (…)`, `<> ALL (ARRAY[…])` to `NOT IN (…)`.
fn rewrite_any_all(nodes: Vec<Node>) -> Vec<Node> {
    let mut out: Vec<Node> = Vec::new();
    let mut i = 0usize;
    while i < nodes.len() {
        let quantifier = match (&nodes.get(i), &nodes.get(i + 1)) {
            (Some(Node::Leaf(Token::Op(op))), Some(Node::Leaf(Token::Word(word))))
                if op == "=" && word == "any" =>
            {
                Some(false)
            }
            (Some(Node::Leaf(Token::Op(op))), Some(Node::Leaf(Token::Word(word))))
                if op == "<>" && word == "all" =>
            {
                Some(true)
            }
            _ => None,
        };
        if let Some(negated) = quantifier
            && let Some(Node::Group { children, .. }) = nodes.get(i + 2)
            && let Some(items) = array_literal_items(children)
        {
            if negated {
                out.push(Node::Leaf(Token::Word("not".to_string())));
            }
            out.push(Node::Leaf(Token::Word("in".to_string())));
            out.push(Node::Group {
                call: false,
                children: rewrite_any_all(items),
            });
            i += 3;
            continue;
        }
        let node = match nodes[i].clone() {
            Node::Group { call, children } => Node::Group {
                call,
                children: rewrite_any_all(children),
            },
            leaf => leaf,
        };
        out.push(node);
        i += 1;
    }
    out
}

/// The elements of an `ARRAY[…]` node list, or `None` when this is not one.
fn array_literal_items(children: &[Node]) -> Option<Vec<Node>> {
    let mut iter = children.iter();
    match iter.next() {
        Some(Node::Leaf(Token::Word(word))) if word == "array" => {}
        _ => return None,
    }
    match iter.next() {
        Some(Node::Leaf(Token::Op(op))) if op == "[" => {}
        _ => return None,
    }
    let rest: Vec<Node> = iter.cloned().collect();
    match rest.last() {
        Some(Node::Leaf(Token::Op(op))) if op == "]" => {
            Some(rest[..rest.len() - 1].to_vec())
        }
        _ => None,
    }
}

/// Reduce one policy expression to its canonical form.
pub(crate) fn canonicalize(expr: &str) -> String {
    let nodes = parse(&tokenize(expr));
    let nodes = canonicalize_operators(nodes);
    let nodes = rewrite_casts(nodes);
    let nodes = rewrite_any_all(nodes);
    let nodes = strip_text_casts(nodes);
    let nodes = strip_column_qualifiers(nodes);
    let nodes = strip_select_aliases(nodes);
    let nodes = drop_redundant_parens(nodes);
    let mut out = String::new();
    render(&nodes, &mut out);
    out
}
