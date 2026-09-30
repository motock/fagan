"""Tests for pipeline.failure_parsers.jvm_failed_test_ids and its wiring into
pipeline.build_detect.failed_node_ids.

The fixtures below are real captured-shape Maven Surefire/Failsafe and Gradle
output, kept as string literals so the parser is graded on the shapes it will
actually see in a benchmark run.
"""
from pipeline import server as p


def jvm_failed_test_ids(stdout):
    """Lazy import: a missing implementation must fail these tests, not break
    collection for every other test module in the suite."""
    from pipeline.failure_parsers import jvm_failed_test_ids as _impl

    return _impl(stdout)


# A Maven BUILD FAILURE block: 2 failures + 1 error, reported twice (once as
# an error-per-class line, once in the short summary), plus the noise lines a
# real surefire run prints around them.
MAVEN_BUILD_FAILURE = """\
[INFO] Scanning for projects...
[INFO] -------------------------------------------------------
[INFO]  T E S T S
[INFO] -------------------------------------------------------
[INFO] Running com.x.FooTest
[ERROR] Tests run: 3, Failures: 2, Errors: 1, Skipped: 0, Time elapsed: 0.05 s <<< FAILURE! - in com.x.FooTest
[ERROR] com.x.FooTest.bar  Time elapsed: 0.01 s  <<< FAILURE!
java.lang.AssertionError: expected:<1> but was:<2>
\tat com.x.FooTest.bar(FooTest.java:42)
[ERROR] com.x.FooTest.baz  Time elapsed: 0.02 s  <<< ERROR!
java.lang.NullPointerException
\tat com.x.FooTest.baz(FooTest.java:50)
[ERROR] Failures:
[ERROR]   com.x.FooTest.bar:42 expected:<1> but was:<2>
[ERROR]   FooTest.qux:42->helper:10 boom
[ERROR] Errors:
[ERROR]   com.x.FooTest.baz:50 NullPointerException
[INFO]
[ERROR] Tests run: 3, Failures: 2, Errors: 1, Skipped: 0
[INFO] BUILD FAILURE
[ERROR] Failed to execute goal org.apache.maven.plugins:maven-surefire-plugin:3.2.5:test (default-test) on project demo: There are test failures.
"""

MAVEN_EXPECTED = ["FooTest.qux", "com.x.FooTest.bar", "com.x.FooTest.baz"]

GRADLE_FAILURE = """\
> Task :test FAILED

FooTest > bar() FAILED
    java.lang.AssertionError at FooTest.java:42

com.x.FooTest > baz(String) FAILED
    java.lang.NullPointerException at FooTest.java:50

2 tests completed, 2 failed

FAILURE: Build failed with an exception.
"""

PYTEST_OUTPUT = """\
=========================== short test summary info ============================
FAILED tests/unit/test_x.py::test_y - assert False
ERROR tests/unit/test_z.py::test_w - RuntimeError
========================= 1 failed, 1 error in 0.12s ==========================
"""


def test_maven_build_failure_block_yields_class_method_ids():
    assert jvm_failed_test_ids(MAVEN_BUILD_FAILURE) == MAVEN_EXPECTED


def test_maven_error_per_class_lines_yield_ids():
    block = (
        "[ERROR] com.x.FooTest.bar  Time elapsed: 0.01 s  <<< FAILURE!\n"
        "[ERROR] com.x.FooTest.baz  Time elapsed: 0.02 s  <<< ERROR!\n"
    )
    assert jvm_failed_test_ids(block) == ["com.x.FooTest.bar", "com.x.FooTest.baz"]


def test_maven_summary_arrow_form_drops_helper_suffix():
    block = "[ERROR]   FooTest.qux:42->helper:10 boom\n"
    assert jvm_failed_test_ids(block) == ["FooTest.qux"]


def test_gradle_failed_lines_normalise_to_class_method():
    assert jvm_failed_test_ids(GRADLE_FAILURE) == ["FooTest.bar", "com.x.FooTest.baz"]


def test_pytest_output_yields_no_jvm_ids():
    assert jvm_failed_test_ids(PYTEST_OUTPUT) == []


def test_maven_noise_lines_are_ignored():
    block = (
        "[ERROR] Tests run: 3, Failures: 2, Errors: 1, Skipped: 0\n"
        "[ERROR] Failures:\n"
        "[ERROR] Errors:\n"
        "[ERROR] BUILD FAILURE\n"
        "[ERROR] Failed to execute goal "
        "org.apache.maven.plugins:maven-surefire-plugin:3.2.5:test "
        "(default-test) on project demo: There are test failures.\n"
        "[ERROR]   no colon on this line\n"
    )
    assert jvm_failed_test_ids(block) == []


def test_duplicate_failure_lines_collapse_to_one_id():
    block = (
        "[ERROR]   com.x.FooTest.bar:42 boom\n"
        "[ERROR]   com.x.FooTest.bar:42 boom\n"
        "[ERROR] com.x.FooTest.bar  Time elapsed: 0.01 s  <<< FAILURE!\n"
    )
    assert jvm_failed_test_ids(block) == ["com.x.FooTest.bar"]


def test_empty_and_none_input_yield_empty_list():
    assert jvm_failed_test_ids("") == []
    assert jvm_failed_test_ids(None) == []


def test_failed_node_ids_includes_jvm_ids_from_maven_output():
    assert p.failed_node_ids(MAVEN_BUILD_FAILURE) == MAVEN_EXPECTED


def test_failed_node_ids_unions_pytest_and_jvm_ids():
    combined = PYTEST_OUTPUT + "\n" + GRADLE_FAILURE
    assert p.failed_node_ids(combined) == [
        "FooTest.bar",
        "com.x.FooTest.baz",
        "tests/unit/test_x.py::test_y",
        "tests/unit/test_z.py::test_w",
    ]


def test_failed_node_ids_docstring_names_maven_and_gradle():
    doc = (p.failed_node_ids.__doc__ or "").lower()
    assert "pytest" in doc
    assert "maven" in doc
    assert "gradle" in doc
