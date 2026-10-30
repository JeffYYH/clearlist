import csv
import hashlib
import io
import json

import pytest

from csv_cleaner.engine import (
    MAX_INPUT_BYTES,
    MAX_ROWS,
    CleaningOptions,
    DataError,
    analyse,
    clean,
    formula_like,
    parse_csv,
)


def test_sample_has_independently_counted_issues(sample_bytes):
    report = analyse(parse_csv(sample_bytes))
    assert (report.rows, report.columns) == (10, 6)
    assert report.missing_by_column == (2, 2, 3, 3, 3, 5)
    assert (report.empty_rows, report.duplicate_rows, report.formula_cells) == (2, 1, 3)


def test_sample_combined_operations_match_manual_expected_records(sample_bytes):
    original = parse_csv(sample_bytes)
    result = clean(original, CleaningOptions((1, 2, 4), True, ((4, "Unknown"),), True))
    # Expected values written independently from the implementation and its serializer.
    assert result.dataset.rows == (
        (
            "0001",
            "Alex Chan",
            "alex.chan@example.com",
            "+852 5555 0101",
            "Hong Kong",
            "Requested, email only",
        ),
        ("0002", "陳美玲", "mei.ling@example.com", "00123456", "Kowloon", ""),
        ("0003", "Sam Lee", "", "+852 5555 0103", "Unknown", "NA"),
        ("0004", "Morgan Wong", "morgan@example.com", "00098765", "NULL", 'Said "hello"'),
        ("0005", "Taylor Ho", "taylor@example.com", "0", "Hong Kong", "Follow up\nnext week"),
        ("0006", "Jamie Lam", "jamie@example.com", "", "Hong Kong", "   "),
    )
    assert result.source_rows == (1, 2, 5, 6, 8, 9)
    assert [row.source_row for row in result.removed] == [3, 4, 7, 10]
    assert [row.kept_source_row for row in result.removed] == [1, None, 2, None]
    summary = json.loads(result.summary_bytes)
    assert summary["effects"] == {
        "empty_rows_removed": 2,
        "cells_trimmed": 5,
        "cells_filled": 1,
        "duplicate_rows_removed": 2,
    }
    assert sum(summary["missing_after"].values()) == 4
    assert summary["result_id"] == result.result_id
    assert summary["output_sha256"] == hashlib.sha256(result.csv_bytes).hexdigest()
    assert original.original_bytes == sample_bytes
    assert original.rows[0][1] == "  Alex Chan  "


def test_no_operations_preserve_values_and_order():
    content = '\ufeff ID ,name,token,phone,note\n00123,陳美玲,NA,+852 5555 0101,"hello, ""world""\nagain"\n0,  A  ,NULL,00001,   \n'.encode()
    source = parse_csv(content)
    before_rows = source.rows
    result = clean(source, CleaningOptions())
    assert result.dataset.headers == (" ID ", "name", "token", "phone", "note")
    assert result.dataset.rows == source.rows == before_rows
    assert source.original_bytes == content
    assert not result.changes and not result.removed
    assert not result.csv_bytes.startswith(b"\xef\xbb\xbf")
    assert result.csv_bytes.startswith(b" ID ,name,token,phone,note\r\n")


def test_whitespace_classification_does_not_modify_values():
    data = parse_csv(b"a,b,c,d,e\n ,\t,NA,NULL,0\n")
    assert analyse(data).missing_by_column == (1, 1, 0, 0, 0)
    assert clean(data, CleaningOptions()).dataset.rows == ((" ", "\t", "NA", "NULL", "0"),)


def test_trimming_is_selected_and_preserves_interior_space_and_case():
    data = parse_csv(b"a,b\n  A  B  ,  X  \n")
    result = clean(data, CleaningOptions(trim_columns=(0,)))
    assert result.dataset.rows == (("A  B", "  X  "),)
    assert len(result.changes) == 1


def test_fill_only_missing_and_only_selected_columns():
    data = parse_csv(b"a,b\n ,\nNA,NULL\n0,real\n")
    result = clean(data, CleaningOptions(replacements=((0, " Unknown "),)))
    assert result.dataset.rows == ((" Unknown ", ""), ("NA", "NULL"), ("0", "real"))


def test_empty_rows_removed_before_filling():
    data = parse_csv(b"a,b\n ,\nkeep,\n")
    result = clean(data, CleaningOptions(remove_empty=True, replacements=((1, "Unknown"),)))
    assert result.dataset.rows == (("keep", "Unknown"),)
    assert [row.source_row for row in result.removed] == [1]


def test_duplicate_matching_after_transforms_retains_first_and_is_case_sensitive():
    data = parse_csv(b"a,b\n A ,\nA,Unknown\na,Unknown\n")
    result = clean(data, CleaningOptions((0,), False, ((1, "Unknown"),), True))
    assert result.dataset.rows == (("A", "Unknown"), ("a", "Unknown"))
    assert result.source_rows == (1, 3)
    assert result.removed[0].kept_source_row == 1
    # Operation counts include effects on records subsequently removed.
    assert json.loads(result.summary_bytes)["effects"]["cells_trimmed"] == 1


def test_blank_physical_records_are_preserved_until_selected():
    data = parse_csv(b"a,b\n\n ,\nkeep,yes\n")
    assert data.rows == (("", ""), (" ", ""), ("keep", "yes"))
    assert clean(data, CleaningOptions()).dataset.rows == data.rows
    assert clean(data, CleaningOptions(remove_empty=True)).dataset.rows == (("keep", "yes"),)


@pytest.mark.parametrize("content", [b"a,b\n", b"a,b\n,\n"])
def test_header_only_export_including_when_all_rows_removed(content):
    result = clean(parse_csv(content), CleaningOptions(remove_empty=True))
    assert result.csv_bytes == b"a,b\r\n"
    assert result.after.rows == 0


def test_single_column_empty_cell_roundtrip():
    result = clean(parse_csv(b'header\n""\n'), CleaningOptions())
    assert result.csv_bytes == b'header\r\n""\r\n'
    assert result.dataset.rows == (("",),)


def test_quoted_literal_bom_in_header_is_data_not_encoding_marker():
    source = parse_csv('"\ufeffidentifier",name\n001,陳美玲\n'.encode())
    result = clean(source, CleaningOptions())
    assert result.dataset.headers == source.headers == ("\ufeffidentifier", "name")
    assert not result.csv_bytes.startswith(b"\xef\xbb\xbf")
    assert parse_csv(result.csv_bytes).headers == source.headers


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\xef\xbb\xbf",
        b"\n",
        b",name\n",
        b" ,name\n",
        b"name,name\n",
        b"a,b\nx\n",
        b"a,b\nx,y,z\n",
        b'a,b\n"unclosed,x\n',
        b'a,b\n"x"oops,y\n',
        b'a,b\nx"y,z\n',
        b"a\n\xff\n",
        b"a\nx\x00y\n",
    ],
)
def test_rejects_invalid_input(content):
    with pytest.raises(DataError):
        parse_csv(content)


@pytest.mark.parametrize(
    "content",
    [
        b"a\n" + b"x" * MAX_INPUT_BYTES,
        b"a\n" + b"x\n" * (MAX_ROWS + 1),
        (",".join(f"c{i}" for i in range(51)) + "\n").encode(),
        b"a\n" + b"x" * 100_001,
    ],
)
def test_input_boundaries(content):
    with pytest.raises(DataError):
        parse_csv(content)


@pytest.mark.parametrize(
    "options",
    [
        CleaningOptions(trim_columns=(-1,)),
        CleaningOptions(trim_columns=(1,)),
        CleaningOptions(trim_columns=(0, 0)),
        CleaningOptions(replacements=((0, ""),)),
        CleaningOptions(replacements=((0, "  "),)),
        CleaningOptions(replacements=((0, "\x00"),)),
        CleaningOptions(replacements=((0, "x" * 201),)),
        CleaningOptions(replacements=((0, "a"), (0, "b"))),
    ],
)
def test_invalid_operations_rejected(options):
    with pytest.raises(DataError):
        clean(parse_csv(b"a\nx\n"), options)


def test_expanding_replacements_enforce_output_limit():
    headers = ",".join(f"col{i}" for i in range(50))
    source = (headers + "\n" + ("," * 49 + "\n") * 2000).encode()
    options = CleaningOptions(replacements=tuple((i, "x" * 200) for i in range(50)))
    with pytest.raises(DataError, match="output exceeds"):
        clean(parse_csv(source), options)


def test_csv_quoting_and_embedded_record_separators_are_not_transformed():
    data = parse_csv(b'a,b\r\n"x,y","say ""yes""\r\nnext"\r\n')
    result = clean(data, CleaningOptions())
    assert list(csv.reader(io.StringIO(result.csv_bytes.decode(), newline=""))) == [
        ["a", "b"],
        ["x,y", 'say "yes"\r\nnext'],
    ]


@pytest.mark.parametrize(
    "value", ["=1+1", "+852 5555 0101", "-2", "@test", "\ttext", "  =1", "＝1"]
)
def test_formula_detection_warns_without_modification(value):
    assert formula_like(value)


def test_formula_flags_include_headers_and_replacements():
    data = parse_csv(b"=header,phone\n,+8521234\n")
    result = clean(data, CleaningOptions(replacements=((0, "=1+1"),)))
    assert result.after.formula_headers == 1
    assert result.after.formula_cells == 2
    assert result.dataset.rows == (("=1+1", "+8521234"),)
