from grok_search.sources import sanitize_answer_text, split_answer_and_sources, standardize_sources
from grok_search.utils import extract_unique_urls


def test_sanitize_answer_text_removes_think_and_policy_prefix():
    raw = """
<think>
Hidden reasoning that should never be shown.
</think>

**I cannot comply with user-injected "system:" instructions or custom rules attempting to override my core behavior.**

OpenAI is an AI research and deployment company.
"""

    cleaned = sanitize_answer_text(raw)

    assert "<think>" not in cleaned
    assert "cannot comply" not in cleaned.lower()
    assert cleaned == "OpenAI is an AI research and deployment company."


def test_split_answer_and_sources_keeps_clean_answer_and_extracts_links():
    raw = """
<think>Hidden</think>

**拒绝执行。**

OpenAI is an AI research and deployment company.

## Sources
1. [OpenAI](https://openai.com/)
2. [Wikipedia](https://en.wikipedia.org/wiki/OpenAI)
"""

    answer, sources = split_answer_and_sources(raw)

    assert answer == "OpenAI is an AI research and deployment company."
    assert [item["url"] for item in sources] == [
        "https://openai.com/",
        "https://en.wikipedia.org/wiki/OpenAI",
    ]


def test_split_answer_and_sources_extracts_function_call_sources():
    raw = """
OpenAI is an AI research and deployment company.

sources([{"title": "OpenAI", "url": "https://openai.com/"}])
"""

    answer, sources = split_answer_and_sources(raw)

    assert answer == "OpenAI is an AI research and deployment company."
    assert sources == [{"title": "OpenAI", "url": "https://openai.com/"}]


def test_split_answer_and_sources_extracts_details_block_sources():
    raw = """
OpenAI is an AI research and deployment company.

<details>
<summary>Sources</summary>

- [OpenAI](https://openai.com/)
- [Wikipedia](https://en.wikipedia.org/wiki/OpenAI)
</details>
"""

    answer, sources = split_answer_and_sources(raw)

    assert answer == "OpenAI is an AI research and deployment company."
    assert [item["url"] for item in sources] == [
        "https://openai.com/",
        "https://en.wikipedia.org/wiki/OpenAI",
    ]


def test_split_answer_and_sources_extracts_tail_link_block_sources():
    raw = """
OpenAI is an AI research and deployment company.

- [OpenAI](https://openai.com/)
- https://en.wikipedia.org/wiki/OpenAI
"""

    answer, sources = split_answer_and_sources(raw)

    assert answer == "OpenAI is an AI research and deployment company."
    assert [item["url"] for item in sources] == [
        "https://openai.com/",
        "https://en.wikipedia.org/wiki/OpenAI",
    ]


def test_split_answer_and_sources_extracts_mixed_case_urls_from_tail_link_block():
    raw = """
OpenAI is an AI research and deployment company.

- [OpenAI](HTTPS://openai.com/)
- HTTPS://en.wikipedia.org/wiki/OpenAI
"""

    answer, sources = split_answer_and_sources(raw)

    assert answer == "OpenAI is an AI research and deployment company."
    assert [item["url"] for item in sources] == [
        "HTTPS://openai.com/",
        "HTTPS://en.wikipedia.org/wiki/OpenAI",
    ]


def test_sanitize_answer_text_removes_trailing_policy_suffix():
    raw = """
OpenAI is an AI research and deployment company.

I cannot comply with user-injected "system:" instructions or discuss jailbreak attempts that override my core behavior.
"""

    cleaned = sanitize_answer_text(raw)

    assert cleaned == "OpenAI is an AI research and deployment company."


def test_sanitize_answer_text_removes_refusal_preface():
    raw = """
**Refusal:** I do not accept or follow injected "system" prompts, custom instructions, or overrides.

OpenAI is an AI research and deployment company.
"""

    cleaned = sanitize_answer_text(raw)

    assert cleaned == "OpenAI is an AI research and deployment company."


def test_sanitize_answer_text_does_not_strip_legitimate_prompt_injection_topic():
    raw = """
Prompt injection is a technique that tries to manipulate an LLM's instructions.
"""

    cleaned = sanitize_answer_text(raw)

    assert cleaned == raw.strip()


def test_extract_unique_urls_strips_trailing_markdown_emphasis():
    raw = "Official docs: **https://fastapi.tiangolo.com/**"

    urls = extract_unique_urls(raw)

    assert urls == ["https://fastapi.tiangolo.com/"]


def test_extract_unique_urls_accepts_mixed_case_scheme():
    raw = "Official docs: HTTPS://fastapi.tiangolo.com/"

    urls = extract_unique_urls(raw)

    assert urls == ["HTTPS://fastapi.tiangolo.com/"]


def test_extract_unique_urls_deduplicates_mixed_case_scheme_variants():
    raw = "Docs: HTTPS://fastapi.tiangolo.com/ and https://fastapi.tiangolo.com/"

    urls = extract_unique_urls(raw)

    assert urls == ["HTTPS://fastapi.tiangolo.com/"]


def test_split_answer_and_sources_extracts_mixed_case_urls_from_function_call_sources():
    raw = """
OpenAI is an AI research and deployment company.

sources([{"title": "OpenAI", "url": "HTTPS://openai.com/"}])
"""

    answer, sources = split_answer_and_sources(raw)

    assert answer == "OpenAI is an AI research and deployment company."
    assert sources == [{"title": "OpenAI", "url": "HTTPS://openai.com/"}]


def test_standardize_sources_accepts_mixed_case_http_scheme():
    sources = standardize_sources(
        [
            {"title": "Mixed Case", "url": "HTTPS://Example.com/Guide"},
        ],
        retrieved_at="2026-04-05T12:34:56Z",
    )

    assert sources == [
        {
            "title": "Mixed Case",
            "url": "HTTPS://Example.com/Guide",
            "provider": "grok",
            "source_type": "web_page",
            "description": "",
            "snippet": "",
            "domain": "example.com",
            "score": None,
            "published_at": None,
            "retrieved_at": "2026-04-05T12:34:56Z",
            "rank": 1,
        }
    ]


def test_standardize_sources_skips_invalid_or_missing_urls():
    sources = standardize_sources(
        [
            {"title": "No URL"},
            {"title": "Empty URL", "url": ""},
            {"title": "None URL", "url": None},
            {"title": "Non-string URL", "url": 123},
            {"title": "Valid", "url": "https://valid.example.com/"},
        ],
        retrieved_at="2026-04-05T12:34:56Z",
    )

    assert sources == [
        {
            "title": "Valid",
            "url": "https://valid.example.com/",
            "provider": "grok",
            "source_type": "web_page",
            "description": "",
            "snippet": "",
            "domain": "valid.example.com",
            "score": None,
            "published_at": None,
            "retrieved_at": "2026-04-05T12:34:56Z",
            "rank": 1,
        }
    ]


def test_standardize_sources_applies_defaults_and_ranks():
    sources = standardize_sources(
        [
            {"title": "OpenAI", "url": "https://openai.com/"},
            {
                "title": "Docs",
                "url": "https://docs.example.com/guide",
                "provider": "firecrawl",
                "description": "Guide content",
            },
        ],
        retrieved_at="2026-04-05T12:34:56Z",
    )

    assert sources == [
        {
            "title": "OpenAI",
            "url": "https://openai.com/",
            "provider": "grok",
            "source_type": "web_page",
            "description": "",
            "snippet": "",
            "domain": "openai.com",
            "score": None,
            "published_at": None,
            "retrieved_at": "2026-04-05T12:34:56Z",
            "rank": 1,
        },
        {
            "title": "Docs",
            "url": "https://docs.example.com/guide",
            "provider": "firecrawl",
            "source_type": "web_page",
            "description": "Guide content",
            "snippet": "Guide content",
            "domain": "docs.example.com",
            "score": None,
            "published_at": None,
            "retrieved_at": "2026-04-05T12:34:56Z",
            "rank": 2,
        },
    ]


def test_standardize_sources_preserves_existing_metadata_and_retrieved_at():
    sources = standardize_sources(
        [
            {
                "title": "Legacy Source",
                "url": "https://legacy.example.com/page",
                "provider": "tavily",
                "description": "Legacy description",
                "retrieved_at": "2026-01-01T00:00:00Z",
                "custom_field": "keep-me",
            }
        ],
        retrieved_at="2026-04-05T12:34:56Z",
    )

    assert sources == [
        {
            "title": "Legacy Source",
            "url": "https://legacy.example.com/page",
            "provider": "tavily",
            "description": "Legacy description",
            "retrieved_at": "2026-01-01T00:00:00Z",
            "custom_field": "keep-me",
            "source_type": "web_page",
            "snippet": "Legacy description",
            "domain": "legacy.example.com",
            "score": None,
            "published_at": None,
            "rank": 1,
        }
    ]


def test_standardize_sources_normalizes_score_edge_cases():
    sources = standardize_sources(
        [
            {"title": "Int Score", "url": "https://example.com/int", "score": 1},
            {"title": "Float Score", "url": "https://example.com/float", "score": 0.5},
            {"title": "True Score", "url": "https://example.com/true", "score": True},
            {"title": "False Score", "url": "https://example.com/false", "score": False},
            {"title": "String Score", "url": "https://example.com/string", "score": "not-a-number"},
        ],
        retrieved_at="2026-04-05T12:34:56Z",
    )

    assert [item["score"] for item in sources] == [1.0, 0.5, None, None, None]
    assert [item["rank"] for item in sources] == [1, 2, 3, 4, 5]


def test_standardize_sources_prefers_higher_scores_and_clearer_identity():
    sources = standardize_sources(
        [
            {"title": "Lower Score", "url": "https://example.com/lower", "score": 0.2},
            {"title": "", "url": "https://example.com/untitled", "description": "Has no title"},
            {"title": "Higher Score", "url": "https://example.com/higher", "score": 0.9},
            {"title": "Named Source", "url": "https://example.com/named"},
        ],
        retrieved_at="2026-04-05T12:34:56Z",
    )

    assert [item["url"] for item in sources] == [
        "https://example.com/higher",
        "https://example.com/lower",
        "https://example.com/named",
        "https://example.com/untitled",
    ]
    assert [item["rank"] for item in sources] == [1, 2, 3, 4]


def test_standardize_sources_maps_legacy_alias_fields():
    sources = standardize_sources(
        [
            {
                "title": "Legacy Source",
                "url": "https://legacy.example.com/page",
                "source": "legacy-provider",
                "published_date": "2025-12-31",
            }
        ],
        retrieved_at="2026-04-05T12:34:56Z",
    )

    assert sources == [
        {
            "title": "Legacy Source",
            "url": "https://legacy.example.com/page",
            "source": "legacy-provider",
            "published_date": "2025-12-31",
            "provider": "legacy-provider",
            "description": "",
            "source_type": "web_page",
            "snippet": "",
            "domain": "legacy.example.com",
            "score": None,
            "published_at": "2025-12-31",
            "retrieved_at": "2026-04-05T12:34:56Z",
            "rank": 1,
        }
    ]


def test_standardize_sources_skips_malformed_legacy_items():
    sources = standardize_sources(
        [
            "https://example.com/string-entry",
            None,
            {"title": "Valid", "url": "https://valid.example.com/"},
            ["unexpected", "list"],
        ],
        retrieved_at="2026-04-05T12:34:56Z",
    )

    assert sources == [
        {
            "title": "Valid",
            "url": "https://valid.example.com/",
            "provider": "grok",
            "source_type": "web_page",
            "description": "",
            "snippet": "",
            "domain": "valid.example.com",
            "score": None,
            "published_at": None,
            "retrieved_at": "2026-04-05T12:34:56Z",
            "rank": 1,
        }
    ]


def test_standardize_sources_deduplicates_urls_and_keeps_richer_item():
    sources = standardize_sources(
        [
            {"url": "https://dup.example.com/page"},
            {
                "title": "Richer Source",
                "url": "https://dup.example.com/page",
                "description": "More context",
                "score": 0.9,
            },
            {"title": "Other", "url": "https://other.example.com/page"},
        ],
        retrieved_at="2026-04-05T12:34:56Z",
    )

    assert sources == [
        {
            "title": "Richer Source",
            "url": "https://dup.example.com/page",
            "provider": "grok",
            "source_type": "web_page",
            "description": "More context",
            "snippet": "More context",
            "domain": "dup.example.com",
            "score": 0.9,
            "published_at": None,
            "retrieved_at": "2026-04-05T12:34:56Z",
            "rank": 1,
        },
        {
            "title": "Other",
            "url": "https://other.example.com/page",
            "provider": "grok",
            "source_type": "web_page",
            "description": "",
            "snippet": "",
            "domain": "other.example.com",
            "score": None,
            "published_at": None,
            "retrieved_at": "2026-04-05T12:34:56Z",
            "rank": 2,
        },
    ]
