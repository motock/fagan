import json
import pipeline.server as p
from pipeline import persistence as ppers


def test_probe(tmp_path, monkeypatch):
    d = tmp_path / "plans"
    d.mkdir()
    monkeypatch.setattr(p, "PLAN_DIR", d)
    monkeypatch.setattr(ppers, "PLAN_DIR", d)
    p._store.records().append_decision("default", "probe", {"question": "failure triage", "story_key": "s1", "decided_at": "D"})
    path = ppers._decisions_path("probe")
    print("EXISTS", path.exists(), path)
    if path.exists():
        print(path.read_text())
    print("READ_DECISIONS", p and __import__("pipeline.decision_log", fromlist=["x"]).read_decisions("probe"))
