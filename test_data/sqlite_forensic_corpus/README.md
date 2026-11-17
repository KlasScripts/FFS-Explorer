# SQLite Forensic Corpus v2.0

Used by `tests/test_sqlite_corpus.py` to score the deleted-record carver
(`app/sqlite_recovery.py`) against independent ground truth.

- Source: https://digitalcorpora.org/corpora/sql/ (file
  `corpora/sql/sqlite_forensic_corpus_v2.0.zip`, downloaded 2026-11-17)
- SHA-256: `8d71ff0ca595716b60ca7b6e9f0c7789e4612383f230e3742c9fc06ad39e95d8`
- Licence: CC0 1.0 (public domain), `license.txt` inside the zip.
- Citation: Sebastian Nemetz, Sven Schmitt, Felix Freiling, "A standardized
  corpus for SQLite database forensics", DFRWS EU 2018; Sven Schmitt,
  "Introducing Anti-Forensics to SQLite Corpora and Tool Testing", 2018.

77 databases: 50 with crafted peculiarities (names, encodings, page
structures, anti-forensic damage) and 27 with deleted or overwritten
records/tables (folders 0A-0E). Each database ships with the SQL that built
it; the test replays that SQL with triggers to know exactly which rows were
deleted, overwritten or dropped. Kept zipped; the test unpacks it to a
temporary folder.
