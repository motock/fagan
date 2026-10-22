from pipeline.pin_guard import Violation, scan_source

HEX64 = "a" * 64


def _hits(source):
    return [(v.line, v.rule) for v in scan_source(source)]


def test_should_flag_sha256_literal():
    assert _hits(f'DIGEST = "{HEX64}"\n') == [(1, "sha256_literal")]


def test_should_flag_exact_enumeration_against_list():
    assert _hits("assert __all__ == ['a', 'b']\n") == [(1, "exact_enumeration")]


def test_should_flag_exact_enumeration_with_suffix_name_and_not_eq():
    assert _hits("assert MY_TOOLS != {'a'}\n") == [(1, "exact_enumeration")]


def test_should_flag_exact_count():
    assert _hits("assert len(TOOLS) == 3\n") == [(1, "exact_count")]


def test_should_not_flag_63_hex_literal():
    assert _hits(f'X = "{"a" * 63}"\n') == []


def test_should_not_flag_65_hex_literal():
    assert _hits(f'X = "{"a" * 65}"\n') == []


def test_should_not_flag_uppercase_hex_literal():
    assert _hits(f'X = "{"A" * 64}"\n') == []


def test_should_not_flag_non_cumulative_name_enumeration():
    assert _hits("assert result == [1, 2]\n") == []


def test_should_not_flag_enumeration_compared_to_non_literal():
    assert _hits("assert __all__ == other\n") == []


def test_should_not_flag_len_of_ordinary_name():
    assert _hits("assert len(items) == 3\n") == []


def test_should_not_flag_normal_string_comparison():
    assert _hits("assert name == 'abc'\n") == []


def test_should_not_report_sha256_docstring_by_design():
    source = f'def f():\n    """{HEX64}"""\n'
    assert _hits(source) == []


def test_should_not_report_sha256_module_docstring_by_design():
    assert _hits(f'"""{HEX64}"""\n') == []


def test_should_not_report_sha256_in_comment():
    assert _hits(f"# {HEX64}\n") == []


def test_should_return_empty_for_malformed_source():
    assert scan_source("def broken(:\n") == []


def test_should_return_empty_for_empty_source():
    assert scan_source("") == []


def test_should_return_violations_in_ascending_line_order():
    source = f'assert len(TOOLS) == 1\nX = "{HEX64}"\nassert __all__ == []\n'
    assert _hits(source) == [
        (1, "exact_count"),
        (2, "sha256_literal"),
        (3, "exact_enumeration"),
    ]


def test_violation_equality_is_value_based():
    assert Violation(1, "r", "d") == Violation(1, "r", "d")
    assert Violation(1, "r", "d") != Violation(2, "r", "d")
