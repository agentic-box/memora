#!/usr/bin/env python3
"""Measure absorb relationship classification against labeled local fixtures."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memora import storage  # noqa: E402
from memora.backends import LocalSQLiteBackend  # noqa: E402


CLASSES = ("duplicate", "update", "contradiction", "related", "new")
RELATIONSHIPS = {
    "duplicate": "DUPLICATE",
    "update": "UPDATE",
    "contradiction": "CONTRADICT",
    "related": "RELATED",
    "new": "UNRELATED",
}
ACTION_CLASSES = {
    "skipped": "duplicate",
    "supersede": "update",
    "contradict": "contradiction",
    "create_and_link": "related",
    "create": "new",
}
DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "absorb_classifier_pairs.json"


def load_cases(path: Path) -> List[Dict[str, Any]]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("fixture file must contain a non-empty JSON list")
    for case in cases:
        missing = {"id", "expected", "memory", "fact"} - set(case)
        if missing:
            raise ValueError(f"fixture {case.get('id', '<unknown>')} missing {sorted(missing)}")
        if case["expected"] not in CLASSES:
            raise ValueError(f"fixture {case['id']} has invalid class {case['expected']!r}")
    return cases


def _database_snapshot(conn) -> str:
    return "\n".join(conn.iterdump())


def _predict_case(case: Dict[str, Any], mode: str, db_path: Path) -> Dict[str, Any]:
    original_backend = storage.STORAGE_BACKEND
    original_model = storage.EMBEDDING_MODEL
    original_compute = storage._compute_embedding
    original_search = storage._search_by_vector
    original_classifier = storage._classify_fact_against_matches
    storage.STORAGE_BACKEND = LocalSQLiteBackend(db_path)
    storage.EMBEDDING_MODEL = "tfidf"
    storage._compute_embedding = lambda *args, **kwargs: {"fixture": 1.0}

    try:
        with storage.connect() as conn:
            existing = storage.add_memory(conn, content=case["memory"])
            storage._search_by_vector = lambda *args, **kwargs: [{
                "score": 0.5,
                "memory": existing,
            }]

            if mode == "stub":
                predicted = case.get("stub_prediction", case["expected"])
                if predicted not in CLASSES:
                    raise ValueError(f"fixture {case['id']} has invalid stub_prediction")

                def stub_classifier(fact, matches):
                    return ([{
                        "memory_id": existing["id"],
                        "relationship": RELATIONSHIPS[predicted],
                        "reason": "measurement stub",
                    }], [])

                storage._classify_fact_against_matches = stub_classifier
            else:
                live_classifier = original_classifier

                def checked_live_classifier(fact, matches):
                    classifications, suggested_tags = live_classifier(fact, matches)
                    if not classifications:
                        raise RuntimeError(
                            f"live classifier returned no result for fixture {case['id']}"
                        )
                    return classifications, suggested_tags

                storage._classify_fact_against_matches = checked_live_classifier

            before = _database_snapshot(conn)
            result = storage.absorb_memory(conn, [case["fact"]], dry_run=True)
            after = _database_snapshot(conn)
            if before != after:
                raise AssertionError(f"dry-run mutated local database for fixture {case['id']}")

            decisions = result["decisions"]
            if len(decisions) != 1:
                raise AssertionError(
                    f"fixture {case['id']} produced {len(decisions)} decisions, expected 1"
                )
            action = decisions[0]["action"]
            if action not in ACTION_CLASSES:
                raise AssertionError(f"fixture {case['id']} produced unsupported action {action!r}")
            return {
                "id": case["id"],
                "expected": case["expected"],
                "predicted": ACTION_CLASSES[action],
                "database_unchanged": True,
            }
    finally:
        storage.STORAGE_BACKEND = original_backend
        storage.EMBEDDING_MODEL = original_model
        storage._compute_embedding = original_compute
        storage._search_by_vector = original_search
        storage._classify_fact_against_matches = original_classifier


def evaluate(
    cases: Iterable[Dict[str, Any]],
    *,
    mode: str,
    work_dir: Path | None = None,
) -> Dict[str, Any]:
    if mode not in {"stub", "live"}:
        raise ValueError("mode must be 'stub' or 'live'")
    if mode == "live" and storage._get_llm_client() is None:
        raise RuntimeError("live mode requires LLM credentials (OPENAI_API_KEY)")

    case_list = list(cases)
    if work_dir is None:
        with tempfile.TemporaryDirectory(prefix="memora-classifier-") as temp:
            return evaluate(case_list, mode=mode, work_dir=Path(temp))

    work_dir.mkdir(parents=True, exist_ok=True)
    outcomes = [
        _predict_case(case, mode, work_dir / f"{index:03d}.db")
        for index, case in enumerate(case_list)
    ]
    confusion = {
        expected: {
            predicted: sum(
                row["expected"] == expected and row["predicted"] == predicted
                for row in outcomes
            )
            for predicted in CLASSES
        }
        for expected in CLASSES
    }
    metrics = {}
    for label in CLASSES:
        true_positive = confusion[label][label]
        actual = sum(confusion[label].values())
        predicted = sum(confusion[expected][label] for expected in CLASSES)
        precision = true_positive / predicted if predicted else 0.0
        recall = true_positive / actual if actual else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        metrics[label] = {"precision": precision, "recall": recall, "f1": f1, "support": actual}

    return {
        "mode": mode,
        "outcomes": outcomes,
        "metrics": metrics,
        "confusion": confusion,
        "macro_f1": sum(metrics[label]["f1"] for label in CLASSES) / len(CLASSES),
    }


def meets_threshold(report: Dict[str, Any], minimum_macro_f1: float) -> bool:
    return report["macro_f1"] >= minimum_macro_f1


def print_report(report: Dict[str, Any]) -> None:
    print(f"mode: {report['mode']}")
    print("class          precision  recall  f1      support")
    for label in CLASSES:
        metric = report["metrics"][label]
        print(
            f"{label:<14} {metric['precision']:<10.3f} {metric['recall']:<7.3f} "
            f"{metric['f1']:<7.3f} {metric['support']}"
        )
    print(f"macro_f1: {report['macro_f1']:.3f}")
    print("\nconfusion (rows=expected, columns=predicted)")
    print("expected\\pred " + " ".join(f"{label[:5]:>6}" for label in CLASSES))
    for expected in CLASSES:
        counts = report["confusion"][expected]
        print(f"{expected:<13} " + " ".join(f"{counts[label]:>6}" for label in CLASSES))

    errors = Counter(
        (row["expected"], row["predicted"])
        for row in report["outcomes"]
        if row["expected"] != row["predicted"]
    )
    if errors:
        print("\nmisclassifications")
        for (expected, predicted), count in sorted(errors.items()):
            print(f"{expected} -> {predicted}: {count}")


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--mode", choices=("stub", "live"), default="live")
    parser.add_argument("--min-macro-f1", type=float, default=0.0)
    args = parser.parse_args(argv)
    try:
        report = evaluate(load_cases(args.fixtures), mode=args.mode)
    except (AssertionError, RuntimeError, ValueError) as exc:
        print(f"measurement failed: {exc}", file=sys.stderr)
        return 2
    print_report(report)
    return 0 if meets_threshold(report, args.min_macro_f1) else 1


if __name__ == "__main__":
    raise SystemExit(main())
