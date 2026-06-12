import importlib.util
import json
from pathlib import Path

import clawbytes_threads as ct

_spec = importlib.util.spec_from_file_location(
    "claw_leaderboard_monitor",
    Path(__file__).resolve().parent.parent / "scripts" / "claw-leaderboard-monitor.py",
)
lb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lb)


AIDER_YML = """- dirname: 2026-05-01-x
  model: GPT-6 (high)
  pass_rate_2: 88.4
  other: 1
- dirname: 2026-05-02-y
  model: "Claude 4.5 Opus"
  pass_rate_2: 91.2
- dirname: 2026-05-03-z
  model: Gemini 3 Pro
  pass_rate_2: 84.0
"""

SWEB_JSON = json.dumps({
    "leaderboards": [
        {"name": "Verified", "results": [
            {"name": "AgentA + ModelX", "resolved": 79.2},
            {"name": "AgentB", "resolved": 78.8},
            {"name": "AgentC", "resolved": "77.5"},
            {"name": "", "resolved": 99.0},
        ]},
        {"name": "Lite", "results": [{"name": "Other", "resolved": 50.0}]},
    ]
})


def test_parse_aider_sorts_by_pass_rate():
    tops = lb.parse_aider_polyglot(AIDER_YML)
    assert tops[0] == ("Claude 4.5 Opus", 91.2)
    assert [name for name, _ in tops] == ["Claude 4.5 Opus", "GPT-6 (high)", "Gemini 3 Pro"]


def test_parse_swebench_picks_named_board_and_sorts():
    tops = lb.parse_swebench(SWEB_JSON, "Verified")
    assert tops[0] == ("AgentA + ModelX", 79.2)
    assert len(tops) == 3  # nameless entry dropped


def test_diff_tops_baseline_and_movement():
    new = [("A", 80.0), ("B", 79.0), ("C", 78.0)]
    assert lb.diff_tops([], new) is None  # baseline, not news
    assert lb.diff_tops(new, new) is None
    assert lb.diff_tops([("B", 79.0), ("A", 78.0), ("C", 77.0)], new) == "new_leader"
    assert lb.diff_tops([("A", 80.0), ("C", 78.0), ("B", 79.0)], new) == "top3_change"


def test_classify_leaderboard_routes_to_ship():
    item = lb.build_item(lb.BOARDS[0], "new_leader",
                         [("AgentA + ModelX", 79.2), ("AgentB", 78.8)],
                         "2026-06-10T16:00:00+00:00")
    candidate = ct.classify_leaderboard(item)
    assert candidate is not None
    assert candidate["primaryCategory"] == "ship"
    assert "read" in candidate["categories"]
    assert "takes #1 on SWE-bench Verified" in candidate["title"]
    # URL must be unique per movement or postedUrls dedup eats future changes
    assert "#swebench-verified-20260610" in candidate["url"]


LIVEBENCH_CSV = """model,coding,reasoning,math
claude-fable-5,90.0,88.0,86.0
gpt-6,89.0,,91.0
broken-row,,,
"""


def test_parse_livebench_means_numeric_columns():
    tops = lb.parse_livebench(LIVEBENCH_CSV)
    assert tops[0] == ("gpt-6", 90.0)      # mean of 89, 91 (blank skipped)
    assert tops[1] == ("claude-fable-5", 88.0)
    assert len(tops) == 2                   # all-blank row dropped


def test_pick_latest_table_is_lexical():
    names = ["table_2025_06_01.csv", "table_2026_01_08.csv", "categories_2026_01_08.json", "table_2026_01_08.csv.bak"]
    assert lb.pick_latest_table(names, "table_", ".csv") == "table_2026_01_08.csv"
