"""Regression tests for _detect_memory_type's issue/todo auto-classification.

Background: the detector used a plain substring test over a flat keyword list. That
mislabelled 130 knowledge memories as issues in a real store, from two independent
causes:

  substring bleed  "fault" matched inside DEFAULT, "patch" inside DISPATCH,
                   "bug" inside DEBUG, "fix" inside FIXTURES/PREFIX,
                   "issue" inside ISSUED
  double counting  "resolve" and "resolved" were separate list entries, so ONE
                   occurrence of "resolved" scored 2 and cleared the >=2 threshold
                   by itself

The bleed cases below are the exact words that caused it. They are ordinary
vocabulary in this codebase's notes, so a regression here is not hypothetical.
"""
import pytest

from memora.storage import _detect_memory_type


def _detected(content, metadata=None, tags=None):
    """Return the detected type ('issue' / 'todo') or None."""
    result = _detect_memory_type(content, metadata, tags)
    return result["_detected_type"] if result else None


# --- substring bleed: these words must NOT count as issue keywords ---------

@pytest.mark.parametrize(
    "first, second",
    [
        # Each pair contains TWO different bleed words, so it scores 2 under the old
        # substring test and gets classified as an issue. A single bleed word only
        # scores 1 and would pass under both algorithms — i.e. it could not fail, and
        # would prove nothing about the fix.
        ("default", "dispatch"),   # fault, patch
        ("debug", "prefix"),       # bug, fix
        ("issued", "default"),     # issue, fault
        ("fixtures", "dispatched"),  # fix, patch
        ("debugging", "issued"),   # bug, issue
    ],
)
def test_issue_keywords_do_not_match_inside_longer_words(first, second):
    content = (
        f"The {first} value is read at startup, then the {second} path runs "
        f"and the {first} handler returns."
    )
    for token in content.replace(",", " ").split():
        assert token.strip(".") not in {
            "fault", "patch", "bug", "fix", "issue", "crash",
        }, "test setup: no bare issue keyword may appear"
    assert _detected(content) is None


# --- inflections of one concept must count once, not once per list entry ---

def test_single_resolved_does_not_count_twice():
    # "resolve" AND "resolved" were both entries, so this alone used to score 2.
    content = "The symlink is resolved by the deploy step before hashing."
    assert _detected(content) is None


def test_repeating_one_concept_is_still_one_signal():
    content = "We fixed it, then fixed it again, and the fix was fixed once more."
    assert _detected(content) is None


# --- real knowledge notes that were misclassified in production ------------

@pytest.mark.parametrize(
    "content",
    [
        # #911 — matched via "fixtures" and "issued"
        "Reviewers and implementers must verify against --local D1, SQL fixtures, or "
        "the fake-D1 harness instead. This was issued in registry msg 2459 after the "
        "codex worker requested a remote D1 command.",
        # #907 — matched via "default" ("fault")
        "A test that CANNOT fail is the default state, not an edge case. The shipped "
        "code was correct every single time.",
        # #903 — matched via "resolved" counting twice
        "wrangler pages deploy follows the symlink and hashes the RESOLVED content, "
        "not the 29-byte link text.",
    ],
)
def test_knowledge_notes_are_not_classified_as_issues(content):
    assert _detected(content) is None


# --- true positives must survive the tightening ---------------------------

def test_genuine_bug_report_is_still_detected():
    content = (
        "**memora web UI: favorite button has overlapping-request race**\n"
        "Bug: toggleFavorite optimistically flips the UI state before issuing the "
        "PATCH, and reverts only on error. Two requests can complete out of order, "
        "leaving the DB at the older request's state. Fix ideas: debounce the toggle."
    )
    assert _detected(content) == "issue"


def test_explicit_type_still_wins():
    assert _detected("anything at all", metadata={"type": "todo"}) is None


def test_existing_issue_tag_short_circuits():
    content = "A bug caused a crash and the error was a regression."
    assert _detected(content, tags=["memora/issues"]) is None
