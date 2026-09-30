"""Tests for JVM (Maven/Gradle) acceptance scoping in
pipeline.build_detect._scope_test_cmd_to_acceptance.

A story's acceptance fixtures must be the ONLY thing graded, so a Maven or
Gradle test command is narrowed to the fixture classes; anything that cannot
be mapped falls back to the full suite (None).
"""
from pipeline import server as p

MAVEN_TAIL = [
    "-Dsurefire.failIfNoSpecifiedTests=false",
    "-Dfailsafe.failIfNoSpecifiedTests=false",
]


def _single_flag_value(cmd, prefix):
    """The value of the one arg starting with `prefix` (fails if not exactly one)."""
    values = [a[len(prefix):] for a in cmd if a.startswith(prefix)]
    assert len(values) == 1, f"expected exactly one {prefix} arg, got {values}"
    return values[0]


def test_scope_mvn_maps_java_fixture_to_fqcn(tmp_path):
    scoped = p._scope_test_cmd_to_acceptance(
        ["mvn", "test"], ["src/test/java/com/x/FooTest.java"], tmp_path)
    assert scoped == [
        "mvn", "test",
        "-Dtest=com.x.FooTest",
        "-Dit.test=com.x.FooTest",
        *MAVEN_TAIL,
    ]


def test_scope_mvnw_wrapper_is_scoped(tmp_path):
    scoped = p._scope_test_cmd_to_acceptance(
        ["./mvnw", "test"], ["src/test/java/com/x/FooTest.java"], tmp_path)
    assert scoped is not None
    assert scoped[:2] == ["./mvnw", "test"]
    assert "-Dtest=com.x.FooTest" in scoped
    assert "-Dit.test=com.x.FooTest" in scoped
    assert scoped[-2:] == MAVEN_TAIL


def test_scope_gradle_maps_java_fixture_to_fqcn(tmp_path):
    scoped = p._scope_test_cmd_to_acceptance(
        ["gradle", "test"], ["src/test/java/com/x/FooTest.java"], tmp_path)
    assert scoped == ["gradle", "test", "--tests", "com.x.FooTest"]


def test_scope_gradlew_wrapper_is_scoped(tmp_path):
    scoped = p._scope_test_cmd_to_acceptance(
        ["./gradlew", "test"], ["src/test/java/com/x/FooTest.java"], tmp_path)
    assert scoped == ["./gradlew", "test", "--tests", "com.x.FooTest"]


def test_scope_mvn_joins_two_fixtures(tmp_path):
    scoped = p._scope_test_cmd_to_acceptance(
        ["mvn", "test"],
        ["src/test/java/com/x/FooTest.java", "src/test/java/com/x/BarTest.java"],
        tmp_path)
    assert scoped is not None
    assert len(scoped) == 6
    assert scoped[:2] == ["mvn", "test"]
    assert set(_single_flag_value(scoped, "-Dtest=").split(",")) == {
        "com.x.FooTest", "com.x.BarTest"}
    assert set(_single_flag_value(scoped, "-Dit.test=").split(",")) == {
        "com.x.FooTest", "com.x.BarTest"}
    assert scoped[-2:] == MAVEN_TAIL


def test_scope_gradle_repeats_tests_flag_per_fixture(tmp_path):
    scoped = p._scope_test_cmd_to_acceptance(
        ["gradle", "test"],
        ["src/test/java/com/x/FooTest.java", "src/test/java/com/x/BarTest.java"],
        tmp_path)
    assert scoped is not None
    assert len(scoped) == 6
    assert scoped[:2] == ["gradle", "test"]
    assert scoped[2::2] == ["--tests", "--tests"]
    assert set(scoped[3::2]) == {"com.x.FooTest", "com.x.BarTest"}


def test_scope_mvn_maps_kotlin_fixture(tmp_path):
    scoped = p._scope_test_cmd_to_acceptance(
        ["mvn", "test"], ["src/test/kotlin/com/x/FooTest.kt"], tmp_path)
    assert scoped is not None
    assert "-Dtest=com.x.FooTest" in scoped


def test_scope_mvn_maps_fixture_in_submodule(tmp_path):
    # src/test/java can appear anywhere in the path (multi-module builds).
    scoped = p._scope_test_cmd_to_acceptance(
        ["mvn", "test"], ["module-a/src/test/java/com/x/FooTest.java"], tmp_path)
    assert scoped is not None
    assert "-Dtest=com.x.FooTest" in scoped


def test_scope_mvn_maps_absolute_fixture_path(tmp_path):
    path = str(tmp_path / "src" / "test" / "java" / "com" / "x" / "FooTest.java")
    scoped = p._scope_test_cmd_to_acceptance(["mvn", "test"], [path], tmp_path)
    assert scoped is not None
    assert "-Dtest=com.x.FooTest" in scoped


def test_scope_jvm_returns_none_for_path_outside_src_test(tmp_path):
    assert p._scope_test_cmd_to_acceptance(
        ["mvn", "test"], ["src/main/java/com/x/FooTest.java"], tmp_path) is None
    assert p._scope_test_cmd_to_acceptance(
        ["gradle", "test"], ["tests/unit/test_x.py"], tmp_path) is None


def test_scope_jvm_returns_none_when_any_fixture_unmappable(tmp_path):
    # Never silently drop a fixture: one unmappable path -> full suite.
    assert p._scope_test_cmd_to_acceptance(
        ["mvn", "test"],
        ["src/test/java/com/x/FooTest.java", "acceptance.py"],
        tmp_path) is None
    assert p._scope_test_cmd_to_acceptance(
        ["gradle", "test"],
        ["src/test/java/com/x/FooTest.java", "acceptance.py"],
        tmp_path) is None


def test_scope_jvm_returns_none_for_empty_acceptance_list(tmp_path):
    assert p._scope_test_cmd_to_acceptance(["mvn", "test"], [], tmp_path) is None
    assert p._scope_test_cmd_to_acceptance(["gradle", "test"], [], tmp_path) is None


def test_scope_non_jvm_runner_with_java_fixture_returns_none(tmp_path):
    assert p._scope_test_cmd_to_acceptance(
        ["make", "test"], ["src/test/java/com/x/FooTest.java"], tmp_path) is None


def test_scope_pytest_unaffected(tmp_path):
    assert p._scope_test_cmd_to_acceptance(
        ["pytest"], ["tests/unit/test_x.py"], tmp_path) == [
            "pytest", "tests/unit/test_x.py"]


def test_scope_cargo_unaffected(tmp_path):
    assert p._scope_test_cmd_to_acceptance(
        ["cargo", "test"], ["tests/test_acceptance.rs"], tmp_path) == [
            "cargo", "test", "--test", "test_acceptance"]


def test_scope_docstring_names_maven_and_gradle():
    doc = (p._scope_test_cmd_to_acceptance.__doc__ or "").lower()
    assert "maven" in doc
    assert "gradle" in doc
