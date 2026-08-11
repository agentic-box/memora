"""Mechanical verification for the on-demand absorb classifier measurement."""

from copy import deepcopy
from pathlib import Path

import pytest

from memora import storage
from scripts.measure_absorb_classifier import (
    CLASSES,
    evaluate,
    load_cases,
    meets_threshold,
)


FIXTURES = Path(__file__).parent / "fixtures" / "absorb_classifier_pairs.json"


def test_stubbed_classifier_measurement_covers_classes_without_mutation(tmp_path):
    cases = load_cases(FIXTURES)
    assert {case["expected"] for case in cases} == set(CLASSES)
    assert {case["difficulty"] for case in cases} >= {"hard", "adversarial"}

    report = evaluate(cases, mode="stub", work_dir=tmp_path)

    assert len(report["outcomes"]) == len(cases)
    assert all(row["database_unchanged"] for row in report["outcomes"])
    assert report["macro_f1"] == 1.0
    assert all(
        report["confusion"][label][label] > 0
        for label in CLASSES
    )


def test_classifier_measurement_fails_threshold_when_fixture_label_is_mutated(tmp_path):
    cases = deepcopy(load_cases(FIXTURES))
    cases[0]["expected"] = "update"

    report = evaluate(cases, mode="stub", work_dir=tmp_path)

    assert report["confusion"]["update"]["duplicate"] == 1
    assert not meets_threshold(report, 1.0)


def test_live_measurement_rejects_empty_classifier_response(tmp_path, monkeypatch):
    cases = load_cases(FIXTURES)[:1]
    monkeypatch.setattr(storage, "_get_llm_client", lambda: object())
    monkeypatch.setattr(
        storage,
        "_classify_fact_against_matches",
        lambda fact, matches: ([], []),
    )

    with pytest.raises(RuntimeError, match="live classifier returned no result"):
        evaluate(cases, mode="live", work_dir=tmp_path)
