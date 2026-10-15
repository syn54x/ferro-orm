# The change summary crosses FFI as data, with one plain rendering

`ferro migrate new initial` on a 45-model project prints every new model and enum type as one comma-joined line, and `check` reports the same string as its "ungenerated" message. The generator built that string in Rust, so the CLI could only print it. Readable output (counts, wrapped lists, a colour per verb) needs the names, not the sentence.

The generator now returns the summary as data (verb, kind, names), and one plain rendering, in Rust, turns it into today's wording: it serves `check`'s `ungenerated` message, which is also the library's error text (`CheckReport.raise_for_problems`). `new`, in both forms, and `check` on a terminal lay out the data: `new` prints a count heading per change and the names beneath it (one per line when piped), and `check` on a terminal prints the problem's sentence without the summary (its `headline`, carried beside the message as data) with the same layout beneath. Piped, `check` prints the message as it is. The CLI never parses the sentence. This follows ADR-0052: a report that more than one reader needs is a typed value whose text is derived, not a string each reader takes apart.

Considered: splitting the string in Python. Rejected because it makes a second decision table that must agree with the Rust one by hand (AGENTS.md I-1, I-6).
