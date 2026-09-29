"""JVM test-command detection: grade the full verification lifecycle.

A Maven repo must be graded with ``verify`` (Surefire *and* Failsafe ``*IT``
tests), a Gradle repo with ``check``, and both must go through the repo's own
wrapper (``./mvnw`` / ``./gradlew``) when one is present.
"""

from pathlib import Path

from pipeline.ecosystem_detect import detect_test_command


def _detect(repo: Path) -> list[str]:
    cwd, cmd = detect_test_command(repo)
    assert cwd == repo
    return cmd


def test_pom_xml_with_mvnw_uses_wrapper_verify(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "mvnw").write_text("#!/bin/sh\n")
    assert _detect(tmp_path) == ["./mvnw", "-B", "verify"]


def test_pom_xml_without_mvnw_uses_plain_mvn_verify(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    assert _detect(tmp_path) == ["mvn", "-B", "verify"]


def test_build_gradle_with_gradlew_uses_wrapper_check(tmp_path):
    (tmp_path / "build.gradle").write_text("")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    assert _detect(tmp_path) == ["./gradlew", "check"]


def test_build_gradle_without_gradlew_uses_plain_gradle_check(tmp_path):
    (tmp_path / "build.gradle").write_text("")
    assert _detect(tmp_path) == ["gradle", "check"]


def test_build_gradle_kts_without_gradlew_uses_plain_gradle_check(tmp_path):
    (tmp_path / "build.gradle.kts").write_text("")
    assert _detect(tmp_path) == ["gradle", "check"]


def test_build_gradle_kts_with_gradlew_uses_wrapper_check(tmp_path):
    (tmp_path / "build.gradle.kts").write_text("")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    assert _detect(tmp_path) == ["./gradlew", "check"]


def test_maven_wrapper_alone_is_not_a_maven_project(tmp_path):
    # A stray ./mvnw with no pom.xml must not be read as Maven.
    (tmp_path / "mvnw").write_text("#!/bin/sh\n")
    (tmp_path / "package.json").write_text("{}")
    assert _detect(tmp_path) == ["npm", "test"]


def test_gradle_wrapper_alone_is_not_a_gradle_project(tmp_path):
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    (tmp_path / "package.json").write_text("{}")
    assert _detect(tmp_path) == ["npm", "test"]


def test_mvnw_does_not_leak_into_gradle_detection(tmp_path):
    # pom.xml absent: a Maven wrapper must not turn a Gradle repo into Maven.
    (tmp_path / "build.gradle").write_text("")
    (tmp_path / "mvnw").write_text("#!/bin/sh\n")
    assert _detect(tmp_path) == ["gradle", "check"]


def test_pom_xml_still_wins_over_gradle_and_package_json(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "build.gradle").write_text("")
    (tmp_path / "package.json").write_text("{}")
    assert _detect(tmp_path) == ["mvn", "-B", "verify"]


def test_gradle_still_wins_over_package_json(tmp_path):
    (tmp_path / "build.gradle").write_text("")
    (tmp_path / "package.json").write_text("{}")
    assert _detect(tmp_path) == ["gradle", "check"]


def _reference_intro() -> str:
    """The intro paragraph of REFERENCE.md's test/lint/build section (the
    marker list), excluding its subsections."""
    text = (Path(__file__).resolve().parents[2] / "REFERENCE.md").read_text()
    rest = text[text.index("## Test, lint and build commands"):]
    stops = [i for i in (rest.find("\n### "), rest.find("\n## ")) if i != -1]
    return rest[:min(stops)] if stops else rest


def test_reference_documents_jvm_verification_commands():
    section = _reference_intro()
    assert "verify" in section
    assert "mvnw" in section
    assert "check" in section
    assert "gradlew" in section
    # one sentence on why verify/check: they run the integration tests too
    assert "integration" in section.lower()
