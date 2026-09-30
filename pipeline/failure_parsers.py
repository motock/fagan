"""Parsers for JVM (Maven/Gradle) test-failure output.

Extracts the failed test ids a Maven Surefire/Failsafe or Gradle run prints,
so pre-existing baseline failures can be exempted the same way pytest ids are.
"""
import re

# Maven summary lines: "[ERROR]   com.x.FooTest.bar:42 expected..." or
# "[ERROR]   FooTest.qux:42->helper:10 boom".  Two or more spaces after
# [ERROR] keeps the "[ERROR] Tests run:" / "[ERROR] Failures:" noise out.
_MAVEN_SUMMARY = re.compile(r"^\[ERROR\]\s{2,}([\w.$]+):", re.MULTILINE)

# Maven error-per-class lines:
# "[ERROR] com.x.FooTest.bar  Time elapsed: 0.01 s  <<< FAILURE!".
# [\w.$]+ (not \S+) so summary lines like
# "[ERROR] Tests run: 3, ... Time elapsed: 0.05 s <<< FAILURE!" do not match.
_MAVEN_PER_CLASS = re.compile(
    r"^\[ERROR\]\s+([\w.$]+)\s+Time elapsed:.*<<<\s*(?:FAILURE|ERROR)!",
    re.MULTILINE,
)

# Gradle: "FooTest > bar() FAILED" / "com.x.FooTest > baz(String) FAILED".
# The "(...)" is required so "> Task :test FAILED" does not match.
_GRADLE_FAILED = re.compile(
    r"^\s*([\w.$]+)\s+>\s+([\w$]+)\(.*?\)\s+FAILED\b", re.MULTILINE
)


def jvm_failed_test_ids(stdout: str) -> list[str]:
    """Return the sorted, unique failed test ids found in Maven/Gradle output.

    Maven Surefire/Failsafe summary lines and error-per-class lines yield
    ``Class.method`` ids; Gradle ``Class > method(...) FAILED`` lines are
    normalised to ``Class.method`` (parameters dropped).  Anything else
    (including pytest output) yields nothing.
    """
    if not stdout:
        return []
    ids: set[str] = set()
    for match in _MAVEN_SUMMARY.finditer(stdout):
        ids.add(match.group(1))
    for match in _MAVEN_PER_CLASS.finditer(stdout):
        ids.add(match.group(1))
    for match in _GRADLE_FAILED.finditer(stdout):
        ids.add(match.group(1) + "." + match.group(2))
    return sorted(ids)