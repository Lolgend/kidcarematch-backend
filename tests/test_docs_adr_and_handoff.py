"""Verification tests for ADR documentation and frontend handoff specification."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_adr_0007_reflects_stage1_cap_top6_and_ranking_rationale() -> None:
    """ADR-0007 documents the 15 candidate cap, top-6 default, and ranking rationale."""
    adr7_path = REPO_ROOT / "docs" / "adr" / "0007-multi-criteria-matching-and-ai-explanation.md"
    assert adr7_path.is_file(), f"Expected {adr7_path} to exist"

    content = adr7_path.read_text(encoding="utf-8")
    assert "STAGE1_MAX_CANDIDATES = 15" in content or "15" in content
    assert "max(15, top_n)" in content
    assert "default 6" in content or "top_n = 6" in content
    assert "ranking_rationale" in content
    assert "FitExplanationResponse" in content


def test_adr_0011_records_decisions_and_rationale() -> None:
    """ADR-0011 records route caching, coalescing, top-6 shortlist, and ranking rationale."""
    adr11_path = (
        REPO_ROOT
        / "docs"
        / "adr"
        / "0011-onemap-route-caching-top6-shortlist-and-ranking-rationale.md"
    )
    assert adr11_path.is_file(), f"Expected {adr11_path} to exist"

    content = adr11_path.read_text(encoding="utf-8")
    # Decisions & Rationale assertions
    assert "STAGE1_MAX_CANDIDATES = 15" in content or "STAGE1_MAX_CANDIDATES" in content
    assert "max(15, top_n)" in content
    assert "top_n = 6" in content or "top_n" in content
    assert "1024" in content  # maxsize
    assert "3600" in content  # ttl
    assert "in-flight" in content.lower()
    assert "coalesc" in content.lower()
    assert "haversine" in content.lower()
    assert "ranking_rationale" in content
    assert "FitExplanationResponse" in content
    assert "_COMMUTE_POSTAL_CACHE" in content or "verified postal" in content.lower()


def test_glossary_aligned_with_top6_and_ranking_rationale() -> None:
    """GLOSSARY.md defines Fit Explanation with ranking rationale and shortlist context."""
    glossary_path = REPO_ROOT / "GLOSSARY.md"
    assert glossary_path.is_file()

    content = glossary_path.read_text(encoding="utf-8")
    assert "**Fit Explanation**:" in content
    assert "ranking rationale" in content.lower()
    assert "6" in content or "shortlist" in content.lower()


def test_frontend_handoff_document_complete() -> None:
    """Frontend handoff document contains complete guidance for frontend agent."""
    handoff_path = (
        REPO_ROOT / "docs" / "FRONTEND_HANDOFF_TOP6_AND_AI_EXPLANATION.md"
    )
    assert handoff_path.is_file(), f"Expected {handoff_path} to exist"

    content = handoff_path.read_text(encoding="utf-8")
    # 1. Payload and slicing
    assert "top_n: 6" in content
    assert "slice(0, 6)" in content or "slice" in content
    assert "Top 6" in content
    assert "/commute" in content

    # 2. AI Fit Explanation banner heading
    assert "AI FIT EXPLANATION" in content or "AI explanation" in content
    assert "heading" in content.lower() or "h3" in content.lower()

    # 3. ranking_rationale rendering and TypeScript interface
    assert "ranking_rationale" in content
    assert "FitExplanationResponse" in content
    assert "summary_narrative" in content
    assert "fallback_used" in content
