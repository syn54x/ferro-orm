"""The docs harness's shared pieces: the one fence reader and the one runner."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests._docs_harness import Fence, fenced_blocks, fenced_examples, run_python

PAGE = """\
# Page

```python {test=run prelude=reference}
print(1)
```

!!! note

    <!-- prints: predicates.py:first-query -->
    ```text
    Rent -1450
    ```

````markdown
```python
not a fence of its own
```
````

```sql
-- predicates.py:first-query (sqlite)
SELECT 1;
```
"""


def test_fenced_blocks_reads_every_fence_with_its_line_and_note() -> None:
    blocks = fenced_blocks(Path("page.md"), PAGE)
    assert [(block.language, block.line) for block in blocks] == [
        ("python", 3),
        ("text", 10),
        ("markdown", 14),
        ("sql", 20),
    ]
    python, text, markdown, sql = blocks
    assert python.info == "python {test=run prelude=reference}"
    assert python.body == "print(1)\n"
    assert text.body == "Rent -1450\n"
    assert text.indent == "    "
    assert text.note == ("prints", "predicates.py:first-query")
    assert python.note is None
    assert markdown.body == "```python\nnot a fence of its own\n```\n"
    assert sql.where == "page.md:20"


def test_fenced_blocks_filters_by_language() -> None:
    [sql] = fenced_blocks(Path("page.md"), PAGE, language="sql")
    assert sql == Fence(
        path=Path("page.md"),
        line=20,
        language="sql",
        info="sql",
        body="-- predicates.py:first-query (sqlite)\nSELECT 1;\n",
        indent="",
        note=None,
    )


def test_an_unclosed_fence_is_refused_naming_its_line() -> None:
    with pytest.raises(
        ValueError, match=r"page\.md:3: the ```python fence is never closed"
    ):
        fenced_blocks(Path("page.md"), "# Page\n\n```python\nprint(1)\n")


def test_fenced_examples_are_the_python_fences() -> None:
    [example] = fenced_examples(Path("page.md"), PAGE)
    assert (example.start_line, example.prefix, example.source) == (
        3,
        "python {test=run prelude=reference}",
        "print(1)\n",
    )


def test_run_python_returns_what_the_script_printed() -> None:
    result = run_python(
        ["-c", "import os; print(os.environ['DOCS_X'])"], env={"DOCS_X": "1"}
    )
    assert result.stdout == "1\n"


def test_run_python_raises_with_both_streams() -> None:
    with pytest.raises(AssertionError) as failure:
        run_python(["-c", "print('out'); import sys; sys.exit('err')"])
    message = str(failure.value)
    assert message.startswith(f"{sys.executable} -c ")
    assert "exited 1\nstdout:\nout\n\nstderr:\nerr\n" in message
