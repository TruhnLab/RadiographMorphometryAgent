# Example guideline corpus

A small demo corpus for the `local` guideline backend, so the pipeline can be tried
without a retrieval server:

```
GUIDELINES_DIR=guidelines/example_corpus
GUIDELINES_LANGUAGE=de
```

**Not for clinical use.** The values are commonly cited textbook ranges, summarised for
demonstration. They are not clinically validated and they are incomplete. Replace this
folder (or point `GUIDELINES_DIR` elsewhere) with a source you have checked.

## Format

Any `.md` or `.txt` file in the folder, searched recursively. Files are split into
paragraphs at blank lines. A Markdown heading names the paragraphs below it, and the
first `# ` heading names the document. Each paragraph is one search result, so keep a
measurement's definition and its reference range in the same paragraph. Files whose name
starts with `README` are not indexed.
