from pipeline import repo_health

# ---------------------------------------------------------------------------
# Module wiring
# ---------------------------------------------------------------------------


def test_all_exports_classify_repo_health_and_format_findings():
    assert "classify_repo_health" in repo_health.__all__
    assert "format_findings" in repo_health.__all__


def test_prior_probe_names_still_exported():
    # This new work must not regress the exports added by earlier stories.
    assert "lint_baseline_finding" in repo_health.__all__
    assert "suite_baseline_finding" in repo_health.__all__
    assert "oracle_finding" in repo_health.__all__
    assert "ci_finding" in repo_health.__all__


# ---------------------------------------------------------------------------
# classify_repo_health — helpers
# ---------------------------------------------------------------------------


def _stub_all_none(monkeypatch):
    monkeypatch.setattr(repo_health, "lint_baseline_finding", lambda checkout: None)
    monkeypatch.setattr(repo_health, "suite_baseline_finding", lambda checkout: None)
    monkeypatch.setattr(repo_health, "oracle_finding", lambda story, checkout: None)
    monkeypatch.setattr(repo_health, "ci_finding", lambda ci_status: None)


def _stub_all_findings(monkeypatch):
    monkeypatch.setattr(
        repo_health, "lint_baseline_finding", lambda checkout: {"kind": "lint_baseline_red", "detail": "lint broke"}
    )
    monkeypatch.setattr(
        repo_health,
        "suite_baseline_finding",
        lambda checkout: {"kind": "suite_baseline_red", "detail": "suite broke"},
    )
    monkeypatch.setattr(
        repo_health, "oracle_finding", lambda story, checkout: {"kind": "oracle_broken", "detail": "oracle broke"}
    )
    monkeypatch.setattr(repo_health, "ci_finding", lambda ci_status: {"kind": "ci_red", "detail": "ci broke"})


# ---------------------------------------------------------------------------
# classify_repo_health — behavior
# ---------------------------------------------------------------------------


def test_classify_repo_health_all_none_returns_empty_list(monkeypatch):
    _stub_all_none(monkeypatch)
    result = repo_health.classify_repo_health({"acceptance": []}, "/tmp/repo", ci_status=None)
    assert result == []


def test_classify_repo_health_all_findings_returns_four_in_documented_order(monkeypatch):
    _stub_all_findings(monkeypatch)
    result = repo_health.classify_repo_health({"acceptance": []}, "/tmp/repo", ci_status={"state": "fail"})
    assert len(result) == 4
    kinds = [f["kind"] for f in result]
    assert kinds == ["lint_baseline_red", "suite_baseline_red", "oracle_broken", "ci_red"]


def test_classify_repo_health_calls_probes_in_fixed_order(monkeypatch):
    call_order = []

    monkeypatch.setattr(
        repo_health,
        "lint_baseline_finding",
        lambda checkout: call_order.append("lint") or None,
    )
    monkeypatch.setattr(
        repo_health,
        "suite_baseline_finding",
        lambda checkout: call_order.append("suite") or None,
    )
    monkeypatch.setattr(
        repo_health,
        "oracle_finding",
        lambda story, checkout: call_order.append("oracle") or None,
    )
    monkeypatch.setattr(
        repo_health,
        "ci_finding",
        lambda ci_status: call_order.append("ci") or None,
    )

    repo_health.classify_repo_health({"acceptance": []}, "/tmp/repo", ci_status=None)
    assert call_order == ["lint", "suite", "oracle", "ci"]


def test_classify_repo_health_passes_checkout_to_lint_and_suite_probes(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        repo_health, "lint_baseline_finding", lambda checkout: captured.setdefault("lint_checkout", checkout) and None
    )
    monkeypatch.setattr(
        repo_health,
        "suite_baseline_finding",
        lambda checkout: captured.setdefault("suite_checkout", checkout) and None,
    )
    monkeypatch.setattr(repo_health, "oracle_finding", lambda story, checkout: None)
    monkeypatch.setattr(repo_health, "ci_finding", lambda ci_status: None)

    repo_health.classify_repo_health({"acceptance": []}, "/tmp/my-checkout", ci_status=None)
    assert captured["lint_checkout"] == "/tmp/my-checkout"
    assert captured["suite_checkout"] == "/tmp/my-checkout"


def test_classify_repo_health_passes_story_and_checkout_to_oracle_finding(monkeypatch):
    captured = {}
    story = {"acceptance": [{"path": "tests/acceptance_foo.py", "source": "# fixture"}]}

    monkeypatch.setattr(repo_health, "lint_baseline_finding", lambda checkout: None)
    monkeypatch.setattr(repo_health, "suite_baseline_finding", lambda checkout: None)

    def fake_oracle(passed_story, passed_checkout):
        captured["story"] = passed_story
        captured["checkout"] = passed_checkout

    monkeypatch.setattr(repo_health, "oracle_finding", fake_oracle)
    monkeypatch.setattr(repo_health, "ci_finding", lambda ci_status: None)

    repo_health.classify_repo_health(story, "/tmp/repo", ci_status=None)
    assert captured["story"] is story
    assert captured["checkout"] == "/tmp/repo"


def test_classify_repo_health_passes_ci_status_to_ci_finding(monkeypatch):
    captured = {}
    ci_status = {"state": "fail", "error": "boom"}

    monkeypatch.setattr(repo_health, "lint_baseline_finding", lambda checkout: None)
    monkeypatch.setattr(repo_health, "suite_baseline_finding", lambda checkout: None)
    monkeypatch.setattr(repo_health, "oracle_finding", lambda story, checkout: None)

    def fake_ci(passed_status):
        captured["ci_status"] = passed_status

    monkeypatch.setattr(repo_health, "ci_finding", fake_ci)

    repo_health.classify_repo_health({"acceptance": []}, "/tmp/repo", ci_status=ci_status)
    assert captured["ci_status"] is ci_status


def test_classify_repo_health_ci_status_defaults_to_none(monkeypatch):
    captured = {}

    monkeypatch.setattr(repo_health, "lint_baseline_finding", lambda checkout: None)
    monkeypatch.setattr(repo_health, "suite_baseline_finding", lambda checkout: None)
    monkeypatch.setattr(repo_health, "oracle_finding", lambda story, checkout: None)

    def fake_ci(passed_status):
        captured["ci_status"] = passed_status

    monkeypatch.setattr(repo_health, "ci_finding", fake_ci)

    # ci_status omitted entirely — must default to None, not raise TypeError.
    repo_health.classify_repo_health({"acceptance": []}, "/tmp/repo")
    assert captured["ci_status"] is None


def test_classify_repo_health_partial_findings_preserve_order_skipping_nones(monkeypatch):
    # Only suite and ci return findings; lint and oracle are None.
    monkeypatch.setattr(repo_health, "lint_baseline_finding", lambda checkout: None)
    monkeypatch.setattr(
        repo_health,
        "suite_baseline_finding",
        lambda checkout: {"kind": "suite_baseline_red", "detail": "suite broke"},
    )
    monkeypatch.setattr(repo_health, "oracle_finding", lambda story, checkout: None)
    monkeypatch.setattr(repo_health, "ci_finding", lambda ci_status: {"kind": "ci_red", "detail": "ci broke"})

    result = repo_health.classify_repo_health({"acceptance": []}, "/tmp/repo", ci_status={"state": "fail"})
    assert [f["kind"] for f in result] == ["suite_baseline_red", "ci_red"]


def test_classify_repo_health_lint_probe_raising_still_returns_other_three(monkeypatch):
    def raiser(checkout):
        raise RuntimeError("lint probe exploded unexpectedly")

    monkeypatch.setattr(repo_health, "lint_baseline_finding", raiser)
    monkeypatch.setattr(
        repo_health,
        "suite_baseline_finding",
        lambda checkout: {"kind": "suite_baseline_red", "detail": "suite broke"},
    )
    monkeypatch.setattr(
        repo_health, "oracle_finding", lambda story, checkout: {"kind": "oracle_broken", "detail": "oracle broke"}
    )
    monkeypatch.setattr(repo_health, "ci_finding", lambda ci_status: {"kind": "ci_red", "detail": "ci broke"})

    result = repo_health.classify_repo_health({"acceptance": []}, "/tmp/repo", ci_status={"state": "fail"})
    assert len(result) == 3
    assert [f["kind"] for f in result] == ["suite_baseline_red", "oracle_broken", "ci_red"]


def test_classify_repo_health_suite_probe_raising_does_not_propagate(monkeypatch):
    monkeypatch.setattr(
        repo_health, "lint_baseline_finding", lambda checkout: {"kind": "lint_baseline_red", "detail": "lint broke"}
    )

    def raiser(checkout):
        raise ValueError("suite probe exploded")

    monkeypatch.setattr(repo_health, "suite_baseline_finding", raiser)
    monkeypatch.setattr(repo_health, "oracle_finding", lambda story, checkout: None)
    monkeypatch.setattr(repo_health, "ci_finding", lambda ci_status: None)

    # Must not raise.
    result = repo_health.classify_repo_health({"acceptance": []}, "/tmp/repo", ci_status=None)
    assert [f["kind"] for f in result] == ["lint_baseline_red"]


def test_classify_repo_health_oracle_probe_raising_does_not_propagate(monkeypatch):
    monkeypatch.setattr(repo_health, "lint_baseline_finding", lambda checkout: None)
    monkeypatch.setattr(repo_health, "suite_baseline_finding", lambda checkout: None)

    def raiser(story, checkout):
        raise KeyError("oracle probe exploded")

    monkeypatch.setattr(repo_health, "oracle_finding", raiser)
    monkeypatch.setattr(repo_health, "ci_finding", lambda ci_status: {"kind": "ci_red", "detail": "ci broke"})

    result = repo_health.classify_repo_health({"acceptance": []}, "/tmp/repo", ci_status={"state": "fail"})
    assert [f["kind"] for f in result] == ["ci_red"]


def test_classify_repo_health_ci_probe_raising_does_not_propagate(monkeypatch):
    monkeypatch.setattr(
        repo_health, "lint_baseline_finding", lambda checkout: {"kind": "lint_baseline_red", "detail": "lint broke"}
    )
    monkeypatch.setattr(repo_health, "suite_baseline_finding", lambda checkout: None)
    monkeypatch.setattr(repo_health, "oracle_finding", lambda story, checkout: None)

    def raiser(ci_status):
        raise TypeError("ci probe exploded")

    monkeypatch.setattr(repo_health, "ci_finding", raiser)

    result = repo_health.classify_repo_health({"acceptance": []}, "/tmp/repo", ci_status={"state": "fail"})
    assert [f["kind"] for f in result] == ["lint_baseline_red"]


def test_classify_repo_health_all_four_probes_raising_returns_empty_list(monkeypatch):
    def raiser1(checkout):
        raise RuntimeError("lint boom")

    def raiser2(checkout):
        raise RuntimeError("suite boom")

    def raiser3(story, checkout):
        raise RuntimeError("oracle boom")

    def raiser4(ci_status):
        raise RuntimeError("ci boom")

    monkeypatch.setattr(repo_health, "lint_baseline_finding", raiser1)
    monkeypatch.setattr(repo_health, "suite_baseline_finding", raiser2)
    monkeypatch.setattr(repo_health, "oracle_finding", raiser3)
    monkeypatch.setattr(repo_health, "ci_finding", raiser4)

    result = repo_health.classify_repo_health({"acceptance": []}, "/tmp/repo", ci_status={"state": "fail"})
    assert result == []


# ---------------------------------------------------------------------------
# format_findings
# ---------------------------------------------------------------------------


def test_format_findings_empty_list_returns_empty_string():
    assert repo_health.format_findings([]) == ""


def test_format_findings_none_returns_empty_string():
    assert repo_health.format_findings(None) == ""


def test_format_findings_starts_with_header_and_contains_both_kinds():
    findings = [
        {"kind": "lint_baseline_red", "detail": "lint broke"},
        {"kind": "ci_red", "detail": "ci broke"},
    ]
    output = repo_health.format_findings(findings)
    assert output.startswith("REPO-HEALTH FINDINGS (measured, not inferred):")
    assert "lint_baseline_red" in output
    assert "ci_red" in output


def test_format_findings_one_line_per_finding_in_input_order():
    findings = [
        {"kind": "lint_baseline_red", "detail": "lint broke"},
        {"kind": "suite_baseline_red", "detail": "suite broke"},
        {"kind": "oracle_broken", "detail": "oracle broke"},
    ]
    output = repo_health.format_findings(findings)
    lines = output.splitlines()
    # First line is the header; one line per finding follows, in order.
    assert lines[0] == "REPO-HEALTH FINDINGS (measured, not inferred):"
    assert lines[1] == "- lint_baseline_red: lint broke"
    assert lines[2] == "- suite_baseline_red: suite broke"
    assert lines[3] == "- oracle_broken: oracle broke"


def test_format_findings_missing_kind_renders_as_unknown():
    output = repo_health.format_findings([{"detail": "no kind here"}])
    assert "- unknown: no kind here" in output


def test_format_findings_missing_detail_renders_empty_detail():
    output = repo_health.format_findings([{"kind": "lint_baseline_red"}])
    assert "- lint_baseline_red: " in output


def test_format_findings_empty_dict_does_not_raise_and_contains_unknown():
    output = repo_health.format_findings([{}])
    assert "unknown" in output


def test_format_findings_detail_truncated_to_400_chars():
    long_detail = "z" * 5000
    output = repo_health.format_findings([{"kind": "lint_baseline_red", "detail": long_detail}])
    lines = output.splitlines()
    detail_line = lines[1]
    prefix = "- lint_baseline_red: "
    assert detail_line.startswith(prefix)
    rendered_detail = detail_line[len(prefix):]
    assert len(rendered_detail) == 400
    assert rendered_detail == long_detail[:400]


def test_format_findings_whole_output_truncated_to_2000_chars():
    findings = [{"kind": f"finding_kind_{i}", "detail": "x" * 400} for i in range(20)]
    output = repo_health.format_findings(findings)
    assert len(output) <= 2000


def test_format_findings_malformed_finding_missing_both_keys_does_not_raise():
    findings = [{}, {"kind": "ci_red"}, {"detail": "only detail"}]
    output = repo_health.format_findings(findings)
    assert "unknown" in output
    assert "- ci_red: " in output
