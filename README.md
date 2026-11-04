# iris-hf-stream

Load a [Hugging Face](https://huggingface.co/datasets) dataset into an InterSystems IRIS table with **one line**, from SQL or from ObjectScript. It streams the dataset, so it only downloads the rows you ask for, and it inserts them in batches.

```sql
CALL HF.LoadDataset('stanfordnlp/imdb', 'train', 'Demo.Imdb', 1000)
```

```objectscript
write ##class(HF.Loader).Load("stanfordnlp/imdb", "train", "Demo.Imdb", 1000)
```

This project implements the community idea **DPI-I-543 – "Load datasets from Hugging Face into IRIS"**.

## Why another loader

| | Typical approach | iris-hf-stream |
| --- | --- | --- |
| Download | Whole dataset first, `limit` applied afterwards | Streaming: `load_dataset(..., streaming=True)` + `take(limit)` |
| Insert | One row at a time | Batches of ~1000 rows, one transaction per batch |
| Text columns | Fixed `VARCHAR(4000)` | `VARCHAR(n)` sized from a sample, `LONGVARCHAR` for long text |
| Interface | ObjectScript class | ObjectScript class **and** SQL stored procedure (usable from the Management Portal, JDBC, ODBC) |
| Dependencies | Unpinned | Pinned in `requirements.txt` |

## Quick start

Requirements: Docker. The image is built on `intersystemsdc/iris-community:latest`.

```bash
git clone https://github.com/muriloprzy-code/iris-hf-stream.git
cd iris-hf-stream
docker compose up -d --build
```

Open an IRIS terminal in the `USER` namespace:

```bash
docker compose exec iris iris session IRIS -U USER
```

```objectscript
USER> write ##class(HF.Loader).Load("stanfordnlp/imdb", "train", "Demo.Imdb", 1000), !
{"status":"ok","table":"Demo.Imdb","rows_loaded":1000,"seconds":...,"columns":[...]}
```

Or use SQL, for example in the Management Portal (<http://localhost:52773/csp/sys/UtilHome.csp>, user `SuperUser`, password `SYS`; **System Explorer → SQL**, namespace `USER`):

```sql
CALL HF.LoadDataset('stanfordnlp/imdb', 'train', 'Demo.Imdb', 1000)
SELECT TOP 5 * FROM Demo.Imdb
```

### Install with ZPM / IPM

```objectscript
zpm "install iris-hf-stream"
```

The Python dependencies are not installed by the module; install them once with IRIS's own Python:

```bash
irispython -m pip install --target <install-dir>/mgr/python -r requirements.txt
```

## Demo walkthrough

This section walks through a complete session, from an empty IRIS instance to SQL queries over real Hugging Face data. Every command was run on IRIS Community 2026.1 with `datasets` 5.1.0. The outputs shown are the real ones; only the `seconds` values change from run to run.

### 1. Start IRIS

```bash
git clone https://github.com/muriloprzy-code/iris-hf-stream.git
cd iris-hf-stream
docker compose up -d --build
docker compose exec iris iris session IRIS -U USER
```

The build installs the Python dependencies (`datasets`, `pyarrow`, ...) into IRIS's embedded Python directory and compiles the `HF` package. Nothing else needs to be installed on the host.

> The compose file starts IRIS with `/iris-main` instead of the image's default entrypoint, because the default entrypoint's post-start hook fails on the 2026.1 image and shuts IRIS down. The `USER` namespace that hook would create already exists.

### 2. Preview a dataset before loading it

You can check what table a dataset would produce without writing anything. `Info` streams the first 1000 rows, infers the column types and reports them:

```objectscript
USER> write ##class(HF.Loader).Info("stanfordnlp/imdb", "train"), !
{"status": "ok", "dataset": "stanfordnlp/imdb", "split": "train", "sampled_rows": 1000,
 "columns": [{"name": "text", "source": "text", "type": "LONGVARCHAR"},
             {"name": "label", "source": "label", "type": "BIGINT"}]}
```

The movie reviews are long (up to about 6000 characters), so `text` becomes `LONGVARCHAR` instead of a fixed `VARCHAR`. The integer `label` becomes `BIGINT`.

### 3. Load 1000 rows with one line of ObjectScript

```objectscript
USER> write ##class(HF.Loader).Load("stanfordnlp/imdb", "train", "Demo.Imdb", 1000), !
{"status": "ok", "table": "Demo.Imdb", "rows_loaded": 1000, "seconds": 2.942,
 "columns": [{"name": "text", "type": "LONGVARCHAR"}, {"name": "label", "type": "BIGINT"}]}
```

That one call did the following:
1. opened the dataset in streaming mode and downloaded only enough data for 1000 rows (not the full 25,000-row split);
2. created `Demo.Imdb` with the inferred columns;
3. inserted the rows with a prepared statement, one transaction per 1000-row batch.

The table is a normal IRIS SQL table:

```sql
SELECT AVG(CHARACTER_LENGTH(text)) AS avg_chars, MAX(CHARACTER_LENGTH(text)) AS max_chars FROM Demo.Imdb
-- avg_chars  max_chars
-- 1311.289   6103

SELECT TOP 3 label, SUBSTRING(text, 1, 70) AS preview FROM Demo.Imdb
-- 0  I rented I AM CURIOUS-YELLOW from my video store because of all the co
-- 0  "I Am Curious: Yellow" is a risible and pretentious steaming pile. It
-- 0  If only to avoid making this type of film in the future. This film is
```

`LONGVARCHAR` columns are stored as character streams. Use `SUBSTRING(column, 1, n)` when you want the text itself in a result set, for example from Embedded Python, where a plain `SELECT` returns the stream reference.

> **`limit` takes the first rows in file order.** The IMDB `train` split is sorted by label, so the first 1000 rows are all negative reviews (`label = 0`). Use `limit = 0` to load the whole split, or pick a split or dataset that is already shuffled.

### 4. Load a whole split from SQL

The same loader is exposed as a SQL stored procedure. That means it can be called from the Management Portal (**System Explorer → SQL**, namespace `USER`, user `SuperUser` / password `SYS`), or from any JDBC or ODBC client connected to port 1972:

```sql
CALL HF.LoadDataset('stanfordnlp/imdb', 'train', 'Demo.ImdbFull', 0)

SELECT label, COUNT(*) AS reviews FROM Demo.ImdbFull GROUP BY label
-- label  reviews
-- 0      12500
-- 1      12500
```

With `limit = 0`, all 25,000 reviews were loaded in about 8 seconds, download included (same test machine as the [Benchmark](#benchmark)). To keep a default from SQL, pass `''` (for example `CALL HF.LoadDataset('stanfordnlp/imdb', '', 'Demo.T', 100)` uses the `train` split).

### 5. Different kinds of data

**Booleans become `BIT`.** In BoolQ, each question has a yes/no answer:

```objectscript
USER> write ##class(HF.Loader).Load("google/boolq", "train", "Demo.BoolQ", 500), !
{"status": "ok", "table": "Demo.BoolQ", "rows_loaded": 500, "seconds": 2.278,
 "columns": [{"name": "question", "type": "VARCHAR(512)"}, {"name": "answer", "type": "BIT"},
             {"name": "passage", "type": "LONGVARCHAR"}]}
```

```sql
SELECT answer, COUNT(*) AS n FROM Demo.BoolQ GROUP BY answer
-- answer  n
-- 0       184
-- 1       316
```

Short text columns get a `VARCHAR(n)` sized from the sample (here `question` → `VARCHAR(512)`). Long ones get `LONGVARCHAR` (`passage`).

**Nested values become JSON.** In SQuAD, the `answers` column is a dictionary of lists. It is stored as JSON text, which IRIS SQL can unpack with `JSON_TABLE`:

```objectscript
USER> write ##class(HF.Loader).Load("rajpurkar/squad", "train", "Demo.Squad", 500), !
{"status": "ok", "table": "Demo.Squad", "rows_loaded": 500, "seconds": 3.105,
 "columns": [{"name": "id", "type": "VARCHAR(128)"}, {"name": "title", "type": "VARCHAR(128)"},
             {"name": "context", "type": "LONGVARCHAR"}, {"name": "question", "type": "VARCHAR(1024)"},
             {"name": "answers", "type": "LONGVARCHAR"}]}
```

```sql
SELECT TOP 2 question, SUBSTRING(answers, 1, 80) AS answers FROM Demo.Squad
-- To whom did the Virgin Mary allegedly appear in 1858 in Lourdes France?  {"text": ["Saint Bernadette Soubirous"], "answer_start": [515]}
-- What is in front of the Notre Dame Main Building?                        {"text": ["a copper statue of Christ"], "answer_start": [188]}

SELECT TOP 3 s.question, j.answer
FROM Demo.Squad s,
     JSON_TABLE(SUBSTRING(s.answers, 1, 4000), '$.text[*]' COLUMNS (answer VARCHAR(200) PATH '$')) j
-- To whom did the Virgin Mary allegedly appear in 1858 in Lourdes France?       Saint Bernadette Soubirous
-- What is in front of the Notre Dame Main Building?                             a copper statue of Christ
-- The Basilica of the Sacred heart at Notre Dame is beside to which structure?  the Main Building
```

**Column names are made SQL-safe.** `lhoestq/demo1` has a column called `date`, which is a reserved word in IRIS SQL. The loader renames it to `date_`, so you can query it without quoting. Spaces and other symbols become `_`, and duplicate names get a numeric suffix.

```objectscript
USER> write ##class(HF.Loader).Load("lhoestq/demo1", "train", "Demo.Reviews", 0), !
{"status": "ok", "table": "Demo.Reviews", "rows_loaded": 5, "seconds": 1.643,
 "columns": [{"name": "id", "type": "VARCHAR(256)"}, {"name": "package_name", "type": "VARCHAR(128)"},
             {"name": "review", "type": "LONGVARCHAR"}, {"name": "date_", "type": "VARCHAR(64)"},
             {"name": "star", "type": "BIGINT"}, {"name": "version_id", "type": "BIGINT"}]}
```

```sql
SELECT TOP 3 star, date_, package_name FROM Demo.Reviews
-- 4  October 12 2016  com.mantz_it.rfanalyzer
-- 4  August 23 2016   com.mantz_it.rfanalyzer
-- 5  August 04 2016   com.mantz_it.rfanalyzer
```

**Datasets with several configurations.** Pass the configuration name as the fifth argument. GLUE, for example, has one configuration per task:

```objectscript
USER> write ##class(HF.Loader).Load("nyu-mll/glue", "validation", "Demo.Sst2", 200, "sst2"), !
{"status": "ok", "table": "Demo.Sst2", "rows_loaded": 200, "seconds": 6.14,
 "columns": [{"name": "sentence", "type": "VARCHAR(1024)"}, {"name": "label", "type": "BIGINT"},
             {"name": "idx", "type": "BIGINT"}]}
```

Missing values (`None` in Python) are stored as SQL `NULL` in every column type, and empty strings stay empty strings.

### 6. Errors come back as JSON, never as exceptions

`Load` never throws, so it is safe to call from SQL, from a production or from a scheduled task. Every problem is reported with a `code` and a readable message:

```objectscript
USER> write ##class(HF.Loader).Load("stanfordnlp/imdb", "train", "Demo.Imdb", 10), !
{"status": "error", "code": "VALIDATION", "message": "table Demo.Imdb already exists; drop it first (DROP TABLE Demo.Imdb)"}

USER> write ##class(HF.Loader).Load("no-such-user/no-such-dataset", "train", "Demo.Nope", 10), !
{"status": "error", "code": "DOWNLOAD", "message": "DatasetNotFoundError: Dataset 'no-such-user/no-such-dataset' doesn't exist on the Hub or cannot be accessed."}

USER> write ##class(HF.Loader).Load("stanfordnlp/imdb", "no_such_split", "Demo.Nope", 10), !
{"status": "error", "code": "DOWNLOAD", "message": "ValueError: Bad split: no_such_split. Available splits: ['train', 'test', 'unsupervised']"}

USER> write ##class(HF.Loader).Load("stanfordnlp/imdb", "train", "NoSchema", 10), !
{"status": "error", "code": "VALIDATION", "message": "invalid target 'NoSchema'; expected schema.table with letters, digits and underscores, starting with a letter"}
```

Existing tables are never overwritten. If a load fails halfway (for example a later row that does not fit the inferred type), the current batch is rolled back and the table created by that call is dropped, so nothing half-filled is left behind.

### 7. Measure batching

```objectscript
USER> do ##class(HF.Benchmark).Run("stanfordnlp/imdb", "train", 5000, 1000)
Warm-up (not timed)...
Bench.RowByRow  batchSize=1  status=ok  elapsed=3.91s
Bench.Batched  batchSize=1000  status=ok  elapsed=2.99s
```

See [Benchmark](#benchmark) for the numbers across 57 runs and how to read them.

### 8. Clean up

```sql
DROP TABLE Demo.Imdb
DROP TABLE Demo.ImdbFull
DROP TABLE Demo.BoolQ
DROP TABLE Demo.Squad
DROP TABLE Demo.Reviews
DROP TABLE Demo.Sst2
```

To start over completely, run `docker compose down`. The container keeps no volumes, so everything is removed.

## Usage

`HF.Loader.Load(dataset, split, target, limit, config, batchSize)` and the SQL procedure `HF.LoadDataset(...)` take the same arguments:

| Parameter | Meaning |
| --- | --- |
| `dataset` | Dataset id on Hugging Face, e.g. `stanfordnlp/imdb` (required) |
| `split` | Split name; `train` when empty |
| `target` | Destination table as `schema.table`. It must **not** exist yet |
| `limit` | Maximum number of rows to load; `0` loads the whole split |
| `config` | *(optional)* Dataset configuration name, for datasets that have several (e.g. `sst2`) |
| `batchSize` | *(optional)* Rows per transaction, default `1000` |

The call never throws. It returns a JSON string:

```json
{"status": "ok", "table": "Demo.Imdb", "rows_loaded": 1000, "seconds": 2.41,
 "columns": [{"name": "text", "type": "LONGVARCHAR"}, {"name": "label", "type": "BIGINT"}]}
```

Validation, download and database problems come back as JSON too:

```json
{"status": "error", "code": "VALIDATION", "message": "table Demo.Imdb already exists; drop it first (DROP TABLE Demo.Imdb)"}
```

| `code` | Meaning |
| --- | --- |
| `VALIDATION` | Bad dataset id, split, target or numeric argument; target table already exists |
| `DOWNLOAD` | Dataset or split not found, network failure, authentication failure |
| `EMPTY` | The split returned no rows |
| `DATABASE` | IRIS rejected a statement |
| `SCHEMA` / `VALUE_TOO_LONG` | A row after the sampled ones does not fit the inferred column type |
| `INTERNAL` | Anything unexpected |

### Preview the table first

`HF.Loader.Info` (SQL: `HF.DatasetInfo`) samples the first rows and reports the columns and types that `Load` would create. Nothing is written.

```objectscript
write ##class(HF.Loader).Info("stanfordnlp/imdb", "train"), !
```

### Private or gated datasets

Set the `HF_TOKEN` environment variable for the IRIS process. With Docker Compose: `HF_TOKEN=hf_xxx docker compose up -d`.

## How it works

1. **Stream.** The dataset is opened with `streaming=True` and `take(limit)`, so only the shards needed for those rows are read.
2. **Infer types.** The first 1000 rows are sampled. Integers become `BIGINT`, floats `DOUBLE`, booleans `BIT`, dates `DATE`, timestamps `TIMESTAMP`. For strings, the longest sampled value (with 4× headroom) picks `VARCHAR(n)`, or `LONGVARCHAR` when the text is long. Lists and dictionaries are stored as JSON text. Column names are made SQL-safe (spaces, reserved words, duplicates).
3. **Insert in batches.** Rows are inserted with a prepared statement; each batch of `batchSize` rows runs in its own transaction.
4. **Clean failure.** If a batch fails, it is rolled back and the table created by this call is dropped, so a failed load leaves nothing behind.

| Value from the dataset | IRIS column |
| --- | --- |
| `int` | `BIGINT` |
| `float` | `DOUBLE` (NaN and ±Infinity are stored as `NULL`) |
| `bool` | `BIT` |
| `datetime` | `TIMESTAMP` (timezone-aware values are converted to UTC) |
| `date` | `DATE` |
| `str` | `VARCHAR(32…1024)` or `LONGVARCHAR` |
| `list`, `dict` | `LONGVARCHAR` containing JSON |
| mixed types, all-`NULL` columns | `LONGVARCHAR` |

## Benchmark

`HF.Benchmark` compares one commit per row (`batchSize = 1`) with batched commits. It runs a warm-up load first so the download does not skew the numbers.

```objectscript
do ##class(HF.Benchmark).Run("stanfordnlp/imdb", "train", 5000, 1000)
```

| batchSize | rows | seconds (median) | min – max |
| --- | --- | --- | --- |
| 1 (row by row) | 5000 | 3.91 | 3.75 – 5.25 |
| 1000 | 5000 | 2.99 | 2.85 – 5.28 |

Measured on 2026-11-04: `stanfordnlp/imdb`, `train` split, 57 runs, IRIS Community 2026.1 in Docker Desktop (Windows 11, 8 CPUs, 8 GB for Docker), `datasets` 5.1.0. Batched commits were about 1.3× faster.

Both timings include streaming the rows from the Hugging Face Hub. Streaming does not cache data, so every load downloads its rows again, and the network accounts for most of the elapsed time. The gap between the two rows (about 0.9 s) is roughly the cost of 5000 separate commits.

## Limitations

- The target table must not exist. Drop it first to reload.
- Column types come from the first 1000 rows. If a later row does not fit (a string longer than the chosen `VARCHAR(n)`, a text value in an integer column), the load stops with a `VALUE_TOO_LONG` or `SCHEMA` error and the table is dropped.
- One split per call.
- Binary columns (images, audio) are not supported.
- Datasets that depend on custom loading scripts are not supported by recent versions of the `datasets` library.
- No `VECTOR` column for embeddings yet.

## Development

The Python logic is tested without IRIS, using SQLite in place of the IRIS backend:

```bash
pip install pytest
pytest
```

If the `datasets` package is installed, `tests/test_streaming_local.py` also checks streaming and `take()` against a local JSONL file (no network needed).

```
iris-hf-stream/
├── src/HF/Loader.cls        # public API: Load, Info (+ SQL procedures)
├── src/HF/Benchmark.cls     # row-by-row vs batched comparison
├── python/hfstream/         # streaming, type inference, batched inserts
├── tests/
├── Dockerfile, docker-compose.yml, iris.script, module.xml
└── requirements.txt         # pinned
```

## License

MIT
