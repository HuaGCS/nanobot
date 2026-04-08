from nanobot.agent.memory_metadata import (
    format_memory_metadata_summary,
    parse_memory_fact_metadata,
    summarize_memory_metadata,
)


def test_parse_memory_fact_metadata_reads_structured_comment() -> None:
    metadata = parse_memory_fact_metadata(
        "- Prefers concise code review. "
        "<!-- nanobot-meta: confidence=high last_verified=2026-04-08 -->"
    )

    assert metadata is not None
    assert metadata.confidence == "high"
    assert metadata.last_verified == "2026-04-08"
    assert metadata.legacy_verify is False


def test_parse_memory_fact_metadata_accepts_last_verified_alias_and_legacy_verify() -> None:
    metadata = parse_memory_fact_metadata(
        "- Prefer short loops (verify) "
        "<!-- nanobot-meta: confidence=low last-verified=2026-04-01 -->"
    )

    assert metadata is not None
    assert metadata.confidence == "low"
    assert metadata.last_verified == "2026-04-01"
    assert metadata.legacy_verify is True


def test_summarize_memory_metadata_reports_structured_and_legacy_counts() -> None:
    markdown = """# Insights

- One <!-- nanobot-meta: confidence=high last_verified=2026-04-08 -->
- Two <!-- nanobot-meta: confidence=medium -->
- Three (verify)
"""

    summary = summarize_memory_metadata(markdown)

    assert summary.tagged_bullets == 2
    assert summary.high_confidence == 1
    assert summary.medium_confidence == 1
    assert summary.low_confidence == 0
    assert summary.with_last_verified == 1
    assert summary.legacy_verify_markers == 1
    assert "Confidence counts: high=1, medium=1, low=0" in format_memory_metadata_summary(markdown)
