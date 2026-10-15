# The change summary crosses FFI as data, with one plain rendering

`ferro migrate new initial` on a 45-model project prints every new model and enum type as one comma-joined line, and `check` reports the same string as its "ungenerated" message. The generator built that string in Rust, so the CLI could only print it. Readable output (counts, wrapped lists, a colour per verb) needs the names, not the sentence.

The generator now returns the summary as data (verb, kind, names), and one plain rendering turns it into today's wording for `check`, `new` and the library's error text. The CLI builds its layout from the data and never parses the sentence. This follows ADR-0052: a report that more than one reader needs is a typed value whose text is derived, not a string each reader takes apart.

Considered: splitting the string in Python. Rejected because it makes a second decision table that must agree with the Rust one by hand (AGENTS.md I-1, I-6).
