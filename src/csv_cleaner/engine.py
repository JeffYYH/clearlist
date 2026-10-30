"""Deterministic CSV operations; independent of Flask and storage."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from dataclasses import dataclass

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 10 * 1024 * 1024
MAX_ROWS = 10_000
MAX_COLUMNS = 50
MAX_CELL_CHARS = 100_000
MAX_REPLACEMENT_CHARS = 200


class DataError(ValueError):
    """An input or selection that the user can correct."""


def missing(value: str) -> bool:
    return not value.strip()


def formula_like(value: str) -> bool:
    # Conservative flag only: '+' may be an ordinary international phone number.
    return bool(value) and (
        value.startswith(("\t", "\r", "\n"))
        or value.lstrip().startswith(("=", "+", "-", "@", "＝", "＋", "－", "＠"))
    )


@dataclass(frozen=True)
class Dataset:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    original_bytes: bytes


@dataclass(frozen=True)
class QualityReport:
    rows: int
    columns: int
    missing_by_column: tuple[int, ...]
    empty_rows: int
    duplicate_rows: int
    formula_cells: int
    formula_headers: int


@dataclass(frozen=True)
class CleaningOptions:
    trim_columns: tuple[int, ...] = ()
    remove_empty: bool = False
    replacements: tuple[tuple[int, str], ...] = ()
    remove_duplicates: bool = False


@dataclass(frozen=True)
class CellChange:
    source_row: int
    column: int
    before: str
    after: str
    operation: str


@dataclass(frozen=True)
class RemovedRow:
    source_row: int
    reason: str
    kept_source_row: int | None = None


@dataclass(frozen=True)
class PreviewResult:
    result_id: str
    dataset: Dataset
    source_rows: tuple[int, ...]
    changes: tuple[CellChange, ...]
    removed: tuple[RemovedRow, ...]
    before: QualityReport
    after: QualityReport
    options: CleaningOptions
    csv_bytes: bytes
    summary_bytes: bytes


def _check_quotes(text: str) -> None:
    """Reject bare quotes too; csv.reader(strict=True) permits them unquoted."""
    state = "start"
    for char in text:
        if state == "quoted":
            if char == '"':
                state = "closed"
        elif state == "closed":
            if char == '"':
                state = "quoted"
            elif char in ",\r\n":
                state = "start"
            else:
                raise DataError(
                    "Malformed quoting: a closing quote must precede a comma or newline."
                )
        elif state == "start":
            if char == '"':
                state = "quoted"
            elif char not in ",\r\n":
                state = "plain"
        elif char == '"':
            raise DataError("Malformed quoting: quotes inside a field must be doubled and quoted.")
        elif char in ",\r\n":
            state = "start"
    if state == "quoted":
        raise DataError("Malformed quoting: a quoted field was not closed.")


def parse_csv(content: bytes, *, generated: bool = False) -> Dataset:
    limit = MAX_OUTPUT_BYTES if generated else MAX_INPUT_BYTES
    if len(content) > limit:
        raise DataError(
            f"The {'output' if generated else 'file'} exceeds {limit // 1024 // 1024} MiB."
        )
    try:
        text = content.decode("utf-8" if generated else "utf-8-sig")
    except UnicodeDecodeError as error:
        raise DataError("Use a UTF-8 CSV. Export the file as CSV UTF-8 and try again.") from error
    if "\x00" in text:
        raise DataError("NUL characters are not supported. Upload a text CSV file.")
    if not text:
        raise DataError("The file is empty. Include a header row.")
    _check_quotes(text)
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        headers = tuple(next(reader))
        if not headers or any(missing(header) for header in headers):
            raise DataError("Every column needs a nonblank header in the first record.")
        if len(headers) > MAX_COLUMNS:
            raise DataError(f"Use at most {MAX_COLUMNS} columns.")
        if len(set(headers)) != len(headers):
            raise DataError("Headers must be unique. Rename duplicate headers and try again.")
        if any(len(header) > MAX_CELL_CHARS for header in headers):
            raise DataError("A header is too long (maximum 100,000 characters).")
        rows = []
        for number, row in enumerate(reader, start=1):
            if number > MAX_ROWS:
                raise DataError(f"Use at most {MAX_ROWS:,} data records.")
            if not row:
                row = [""] * len(headers)
            if len(row) != len(headers):
                raise DataError(
                    f"Data record {number} has {len(row)} cells; expected {len(headers)}. "
                    "Check the comma delimiter and quote fields containing commas."
                )
            if any(len(value) > MAX_CELL_CHARS for value in row):
                raise DataError(
                    f"Data record {number} contains a field longer than 100,000 characters."
                )
            rows.append(tuple(row))
    except StopIteration as error:
        raise DataError("The file is empty. Include a header row.") from error
    except csv.Error as error:
        raise DataError(
            "Invalid CSV. Check quoting and field sizes (maximum 100,000 characters)."
        ) from error
    return Dataset(headers, tuple(rows), content)


def analyse(dataset: Dataset) -> QualityReport:
    return QualityReport(
        rows=len(dataset.rows),
        columns=len(dataset.headers),
        missing_by_column=tuple(
            sum(missing(row[index]) for row in dataset.rows)
            for index in range(len(dataset.headers))
        ),
        empty_rows=sum(all(missing(value) for value in row) for row in dataset.rows),
        duplicate_rows=len(dataset.rows) - len(set(dataset.rows)),
        formula_cells=sum(formula_like(value) for row in dataset.rows for value in row),
        formula_headers=sum(formula_like(value) for value in dataset.headers),
    )


def _validate_options(dataset: Dataset, options: CleaningOptions) -> None:
    indices = (*options.trim_columns, *(index for index, _ in options.replacements))
    if any(type(index) is not int or not 0 <= index < len(dataset.headers) for index in indices):
        raise DataError("A selected column is invalid. Reload the configuration page.")
    if len(set(options.trim_columns)) != len(options.trim_columns):
        raise DataError("Select each trim column only once.")
    if len({index for index, _ in options.replacements}) != len(options.replacements):
        raise DataError("Provide only one replacement per column.")
    for _, replacement in options.replacements:
        if not isinstance(replacement, str) or missing(replacement):
            raise DataError("Each enabled replacement needs nonblank text.")
        if "\x00" in replacement or len(replacement) > MAX_REPLACEMENT_CHARS:
            raise DataError("Replacement text must have no NUL and at most 200 characters.")


def clean(dataset: Dataset, options: CleaningOptions) -> PreviewResult:
    _validate_options(dataset, options)
    before = analyse(dataset)
    sources = []
    changes = []
    removed = []
    seen = {}
    trimmed = filled = removed_empty = removed_duplicates = 0
    # Keep serialization bounded while constructing it, including expanding replacements.
    buffer = io.StringIO(newline="")
    # A literal BOM character inside the first header is data, not an encoding marker.
    header_quoting = csv.QUOTE_ALL if dataset.headers[0].startswith("\ufeff") else csv.QUOTE_MINIMAL
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=header_quoting)
    writer.writerow(dataset.headers)
    output_size = len(buffer.getvalue().encode("utf-8"))
    for source, original in enumerate(dataset.rows, start=1):
        if options.remove_empty and all(missing(value) for value in original):
            removed.append(RemovedRow(source, "Fully empty row"))
            removed_empty += 1
            continue
        row = list(original)
        for index in options.trim_columns:
            value = row[index].strip()
            if value != row[index]:
                changes.append(CellChange(source, index, row[index], value, "Trim"))
                row[index] = value
                trimmed += 1
        for index, replacement in options.replacements:
            if missing(row[index]):
                changes.append(CellChange(source, index, row[index], replacement, "Fill missing"))
                row[index] = replacement
                filled += 1
        final = tuple(row)
        if options.remove_duplicates and final in seen:
            removed.append(
                RemovedRow(source, "Exact duplicate after selected operations", seen[final])
            )
            removed_duplicates += 1
            continue
        seen[final] = source
        sources.append(source)
        # A reusable per-record buffer avoids repeatedly copying the whole output.
        record_buffer = io.StringIO(newline="")
        csv.writer(record_buffer, lineterminator="\r\n").writerow(final)
        record = record_buffer.getvalue()
        output_size += len(record.encode("utf-8"))
        if output_size > MAX_OUTPUT_BYTES:
            raise DataError(
                "The proposed output exceeds 10 MiB. Use shorter replacements or a smaller file."
            )
        buffer.write(record)
    csv_bytes = buffer.getvalue().encode("utf-8")
    # The displayed dataset is read from exactly the bytes served by the download route.
    exported = parse_csv(csv_bytes, generated=True)
    after = analyse(exported)
    result_id = uuid.uuid4().hex
    summary = {
        "schema_version": 1,
        "result_id": result_id,
        "input_sha256": hashlib.sha256(dataset.original_bytes).hexdigest(),
        "output_sha256": hashlib.sha256(csv_bytes).hexdigest(),
        "input_rows": before.rows,
        "output_rows": after.rows,
        "columns": list(dataset.headers),
        "options": {
            "remove_empty_rows": options.remove_empty,
            "trim_columns": [dataset.headers[index] for index in options.trim_columns],
            "fill_missing": {
                dataset.headers[index]: value for index, value in options.replacements
            },
            "remove_exact_duplicates": options.remove_duplicates,
        },
        "operation_order": ["remove_empty_rows", "trim", "fill_missing", "remove_exact_duplicates"],
        "effects": {
            "empty_rows_removed": removed_empty,
            "cells_trimmed": trimmed,
            "cells_filled": filled,
            "duplicate_rows_removed": removed_duplicates,
        },
        "effect_count_note": "Sequential effects; changed cells may belong to rows removed later.",
        "missing_before": dict(zip(dataset.headers, before.missing_by_column, strict=True)),
        "missing_after": dict(zip(dataset.headers, after.missing_by_column, strict=True)),
        "exact_duplicates_before": before.duplicate_rows,
        "exact_duplicates_after": after.duplicate_rows,
        "formula_like_cells_before": before.formula_cells,
        "formula_like_cells_after": after.formula_cells,
        "formula_like_headers": after.formula_headers,
        "warning": "Values are preserved for import. CSV quoting does not neutralize spreadsheet formulas.",
        "record_number_note": "Source numbers count data records from 1, not physical file lines.",
    }
    return PreviewResult(
        result_id,
        exported,
        tuple(sources),
        tuple(changes),
        tuple(removed),
        before,
        after,
        options,
        csv_bytes,
        (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
