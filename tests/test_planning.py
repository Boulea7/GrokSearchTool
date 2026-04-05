import json

import pytest

from grok_search import planning
from grok_search import server


@pytest.fixture(autouse=True)
def reset_planning_state():
    planning.engine.reset()
    yield
    planning.engine.reset()


@pytest.mark.asyncio
async def test_legacy_plan_flow_level_1_still_completes_after_sub_query():
    intent = json.loads(
        await server.plan_intent(
            thought="Simple factual lookup.",
            core_question="What is OpenAI?",
            query_type="factual",
            time_sensitivity="irrelevant",
        )
    )
    session_id = intent["session_id"]

    complexity = json.loads(
        await server.plan_complexity(
            session_id=session_id,
            thought="Low complexity.",
            level=1,
            estimated_sub_queries=1,
            estimated_tool_calls=2,
            justification="One direct lookup is enough.",
        )
    )
    assert complexity["plan_complete"] is False

    decomposition = json.loads(
        await server.plan_sub_query(
            session_id=session_id,
            thought="One sub-query is enough.",
            id="sq1",
            goal="Identify OpenAI.",
            expected_output="One-sentence company definition.",
            boundary="Exclude product history and leadership details.",
            tool_hint="web_search",
        )
    )
    assert decomposition["plan_complete"] is True
    assert decomposition["complexity_level"] == 1


@pytest.mark.asyncio
async def test_missing_session_returns_structured_error():
    result = json.loads(
        await server.plan_complexity(
            session_id="",
            thought="Missing session.",
            level=1,
            estimated_sub_queries=1,
            estimated_tool_calls=2,
            justification="Should fail without a session.",
        )
    )

    assert result["error"] == "session_not_found"
    assert result["restart_from_intent_analysis"] is True
    assert "expected_phase_order" in result


@pytest.mark.asyncio
async def test_out_of_order_phase_returns_error():
    intent = json.loads(
        await server.plan_intent(
            thought="Start planning.",
            core_question="Resolve an ambiguous query.",
            query_type="exploratory",
            time_sensitivity="recent",
        )
    )

    wrong = json.loads(
        await server.plan_sub_query(
            session_id=intent["session_id"],
            thought="Skip complexity on purpose.",
            id="sq1",
            goal="Wrong order",
            expected_output="Should fail.",
            boundary="Testing invalid order.",
            tool_hint="web_search",
        )
    )

    assert "requires 'complexity_assessment'" in wrong["error"]
    assert wrong["expected_phase_order"][0] == "intent_analysis"


@pytest.mark.asyncio
async def test_revision_cannot_create_later_phase_out_of_order():
    intent = json.loads(
        await server.plan_intent(
            thought="Start planning.",
            core_question="Resolve an ambiguous query.",
            query_type="exploratory",
            time_sensitivity="recent",
        )
    )

    result = json.loads(
        await server.plan_execution(
            session_id=intent["session_id"],
            thought="Try to bypass ordering with a revision.",
            parallel_groups="sq1",
            sequential="sq1",
            estimated_rounds=1,
            is_revision=True,
        )
    )

    assert "requires 'tool_selection'" in result["error"]


@pytest.mark.asyncio
async def test_level_1_blocks_later_phases():
    intent = json.loads(
        await server.plan_intent(
            thought="Simple factual lookup.",
            core_question="What is OpenAI?",
            query_type="factual",
            time_sensitivity="irrelevant",
        )
    )
    session_id = intent["session_id"]

    await server.plan_complexity(
        session_id=session_id,
        thought="Low complexity.",
        level=1,
        estimated_sub_queries=1,
        estimated_tool_calls=2,
        justification="One direct lookup is enough.",
    )

    await server.plan_sub_query(
        session_id=session_id,
        thought="Complete the required level 1 decomposition first.",
        id="sq1",
        goal="Identify OpenAI.",
        expected_output="A short definition.",
        boundary="Exclude unrelated details.",
        tool_hint="web_search",
    )

    result = json.loads(
        await server.plan_search_term(
            session_id=session_id,
            thought="This should be blocked for level 1.",
            term="openai company",
            purpose="sq1",
            round=1,
            approach="targeted",
        )
    )

    assert result["error"] == "Level 1 planning completes after query_decomposition."


@pytest.mark.asyncio
async def test_first_search_term_requires_approach():
    intent = json.loads(
        await server.plan_intent(
            thought="Moderate lookup.",
            core_question="Compare Grok and Tavily.",
            query_type="comparative",
            time_sensitivity="recent",
        )
    )
    session_id = intent["session_id"]

    await server.plan_complexity(
        session_id=session_id,
        thought="Need search strategy.",
        level=2,
        estimated_sub_queries=2,
        estimated_tool_calls=4,
        justification="Needs decomposition and strategy.",
    )

    await server.plan_sub_query(
        session_id=session_id,
        thought="Search first axis.",
        id="sq1",
        goal="Compare feature coverage.",
        expected_output="A concise feature comparison.",
        boundary="Exclude performance discussion.",
        tool_hint="web_search",
    )

    result = json.loads(
        await server.plan_search_term(
            session_id=session_id,
            thought="Missing approach on first term.",
            term="grok tavily comparison",
            purpose="sq1",
            round=1,
        )
    )

    assert result["error"] == "first_search_term_requires_approach"


@pytest.mark.asyncio
async def test_plan_tool_mapping_rejects_invalid_tool():
    intent = json.loads(
        await server.plan_intent(
            thought="Moderate lookup.",
            core_question="Compare providers.",
            query_type="comparative",
            time_sensitivity="recent",
        )
    )
    session_id = intent["session_id"]

    await server.plan_complexity(
        session_id=session_id,
        thought="Need strategy and mapping.",
        level=2,
        estimated_sub_queries=1,
        estimated_tool_calls=3,
        justification="Requires explicit tool selection.",
    )

    await server.plan_sub_query(
        session_id=session_id,
        thought="Single sub-query.",
        id="sq1",
        goal="Compare providers.",
        expected_output="A single comparison paragraph.",
        boundary="Exclude implementation details.",
        tool_hint="web_search",
    )

    await server.plan_search_term(
        session_id=session_id,
        thought="Seed search strategy.",
        term="grok tavily provider",
        purpose="sq1",
        round=1,
        approach="targeted",
    )

    result = json.loads(
        await server.plan_tool_mapping(
            session_id=session_id,
            thought="Use an invalid tool.",
            sub_query_id="sq1",
            tool="web_scrape",
            reason="Should fail.",
        )
    )

    assert result["error"] == "invalid_tool"


@pytest.mark.asyncio
async def test_plan_search_term_rejects_more_than_eight_words():
    intent = json.loads(
        await server.plan_intent(
            thought="Moderate lookup.",
            core_question="Compare providers.",
            query_type="comparative",
            time_sensitivity="recent",
        )
    )
    session_id = intent["session_id"]

    await server.plan_complexity(
        session_id=session_id,
        thought="Need strategy.",
        level=2,
        estimated_sub_queries=1,
        estimated_tool_calls=3,
        justification="Requires search strategy.",
    )

    await server.plan_sub_query(
        session_id=session_id,
        thought="Single sub-query.",
        id="sq1",
        goal="Compare providers.",
        expected_output="A single comparison paragraph.",
        boundary="Exclude implementation details.",
        tool_hint="web_search",
    )

    result = json.loads(
        await server.plan_search_term(
            session_id=session_id,
            thought="Too many words.",
            term="one two three four five six seven eight nine",
            purpose="sq1",
            round=1,
            approach="targeted",
        )
    )

    assert result["error"] == "validation_error"


@pytest.mark.asyncio
async def test_plan_search_term_rejects_multiple_sub_query_purposes():
    intent = json.loads(
        await server.plan_intent(
            thought="Moderate lookup.",
            core_question="Compare providers.",
            query_type="comparative",
            time_sensitivity="recent",
        )
    )
    session_id = intent["session_id"]

    await server.plan_complexity(
        session_id=session_id,
        thought="Need strategy.",
        level=2,
        estimated_sub_queries=1,
        estimated_tool_calls=3,
        justification="Requires search strategy.",
    )

    await server.plan_sub_query(
        session_id=session_id,
        thought="Single sub-query.",
        id="sq1",
        goal="Compare providers.",
        expected_output="A single comparison paragraph.",
        boundary="Exclude implementation details.",
        tool_hint="web_search",
    )

    result = json.loads(
        await server.plan_search_term(
            session_id=session_id,
            thought="Multiple purposes are invalid.",
            term="grok provider comparison",
            purpose="sq1+sq2",
            round=1,
            approach="targeted",
        )
    )

    assert result["error"] == "validation_error"


@pytest.mark.asyncio
async def test_plan_tool_mapping_rejects_invalid_params_json():
    intent = json.loads(
        await server.plan_intent(
            thought="Moderate lookup.",
            core_question="Compare providers.",
            query_type="comparative",
            time_sensitivity="recent",
        )
    )
    session_id = intent["session_id"]

    await server.plan_complexity(
        session_id=session_id,
        thought="Need strategy and mapping.",
        level=2,
        estimated_sub_queries=1,
        estimated_tool_calls=3,
        justification="Requires explicit tool selection.",
    )

    await server.plan_sub_query(
        session_id=session_id,
        thought="Single sub-query.",
        id="sq1",
        goal="Compare providers.",
        expected_output="A single comparison paragraph.",
        boundary="Exclude implementation details.",
        tool_hint="web_search",
    )

    await server.plan_search_term(
        session_id=session_id,
        thought="Seed search strategy.",
        term="grok tavily provider",
        purpose="sq1",
        round=1,
        approach="targeted",
    )

    result = json.loads(
        await server.plan_tool_mapping(
            session_id=session_id,
            thought="Invalid params_json should fail.",
            sub_query_id="sq1",
            tool="web_search",
            reason="Need valid params.",
            params_json="{bad json}",
        )
    )

    assert result["error"] == "validation_error"


@pytest.mark.asyncio
async def test_plan_sub_query_rejects_duplicate_ids():
    intent = json.loads(
        await server.plan_intent(
            thought="Start planning.",
            core_question="Compare providers.",
            query_type="comparative",
            time_sensitivity="recent",
        )
    )
    session_id = intent["session_id"]

    await server.plan_complexity(
        session_id=session_id,
        thought="Need decomposition.",
        level=2,
        estimated_sub_queries=2,
        estimated_tool_calls=4,
        justification="Need multiple sub-queries.",
    )

    await server.plan_sub_query(
        session_id=session_id,
        thought="First sub-query.",
        id="sq1",
        goal="Compare pricing.",
        expected_output="A concise pricing comparison.",
        boundary="Exclude API compatibility discussion.",
        tool_hint="web_search",
    )

    result = json.loads(
        await server.plan_sub_query(
            session_id=session_id,
            thought="Duplicate sub-query id should fail.",
            id="sq1",
            goal="Compare API compatibility.",
            expected_output="A concise compatibility comparison.",
            boundary="Exclude pricing discussion.",
            tool_hint="web_search",
        )
    )

    assert result["error"] == "validation_error"
    assert "duplicate sub-query id" in result["message"].lower()


@pytest.mark.asyncio
async def test_plan_sub_query_rejects_unknown_dependency():
    intent = json.loads(
        await server.plan_intent(
            thought="Start planning.",
            core_question="Compare providers.",
            query_type="comparative",
            time_sensitivity="recent",
        )
    )
    session_id = intent["session_id"]

    await server.plan_complexity(
        session_id=session_id,
        thought="Need decomposition.",
        level=2,
        estimated_sub_queries=2,
        estimated_tool_calls=4,
        justification="Need multiple sub-queries.",
    )

    result = json.loads(
        await server.plan_sub_query(
            session_id=session_id,
            thought="Unknown dependency should fail.",
            id="sq2",
            goal="Compare API compatibility.",
            expected_output="A concise compatibility comparison.",
            boundary="Exclude pricing discussion.",
            depends_on="sq1",
            tool_hint="web_search",
        )
    )

    assert result["error"] == "validation_error"
    assert "unknown sub-query dependency" in result["message"].lower()


@pytest.mark.asyncio
async def test_plan_search_term_rejects_unknown_sub_query_reference():
    intent = json.loads(
        await server.plan_intent(
            thought="Start planning.",
            core_question="Compare providers.",
            query_type="comparative",
            time_sensitivity="recent",
        )
    )
    session_id = intent["session_id"]

    await server.plan_complexity(
        session_id=session_id,
        thought="Need decomposition.",
        level=2,
        estimated_sub_queries=1,
        estimated_tool_calls=3,
        justification="Need one sub-query and one search term.",
    )

    await server.plan_sub_query(
        session_id=session_id,
        thought="Only one valid sub-query.",
        id="sq1",
        goal="Compare providers.",
        expected_output="A concise comparison.",
        boundary="Exclude implementation details.",
        tool_hint="web_search",
    )

    result = json.loads(
        await server.plan_search_term(
            session_id=session_id,
            thought="Unknown purpose should fail.",
            term="provider comparison",
            purpose="sq2",
            round=1,
            approach="targeted",
        )
    )

    assert result["error"] == "validation_error"
    assert "unknown sub-query id" in result["message"].lower()


@pytest.mark.asyncio
async def test_plan_tool_mapping_rejects_unknown_sub_query_reference():
    intent = json.loads(
        await server.plan_intent(
            thought="Start planning.",
            core_question="Compare providers.",
            query_type="comparative",
            time_sensitivity="recent",
        )
    )
    session_id = intent["session_id"]

    await server.plan_complexity(
        session_id=session_id,
        thought="Need decomposition.",
        level=2,
        estimated_sub_queries=1,
        estimated_tool_calls=3,
        justification="Need one sub-query and one mapping.",
    )

    await server.plan_sub_query(
        session_id=session_id,
        thought="Only one valid sub-query.",
        id="sq1",
        goal="Compare providers.",
        expected_output="A concise comparison.",
        boundary="Exclude implementation details.",
        tool_hint="web_search",
    )

    await server.plan_search_term(
        session_id=session_id,
        thought="Valid search term first.",
        term="provider comparison",
        purpose="sq1",
        round=1,
        approach="targeted",
    )

    result = json.loads(
        await server.plan_tool_mapping(
            session_id=session_id,
            thought="Unknown mapping target should fail.",
            sub_query_id="sq2",
            tool="web_search",
            reason="Should be rejected.",
        )
    )

    assert result["error"] == "validation_error"
    assert "unknown sub-query id" in result["message"].lower()


@pytest.mark.asyncio
async def test_plan_execution_rejects_unknown_or_repeated_ids_and_dependency_order():
    intent = json.loads(
        await server.plan_intent(
            thought="Start planning.",
            core_question="Compare providers deeply.",
            query_type="comparative",
            time_sensitivity="recent",
        )
    )
    session_id = intent["session_id"]

    await server.plan_complexity(
        session_id=session_id,
        thought="Need full planning.",
        level=3,
        estimated_sub_queries=2,
        estimated_tool_calls=6,
        justification="Need dependency-aware execution order.",
    )

    await server.plan_sub_query(
        session_id=session_id,
        thought="First sub-query.",
        id="sq1",
        goal="Collect baseline facts.",
        expected_output="A baseline summary.",
        boundary="Exclude downstream comparison synthesis.",
        tool_hint="web_search",
    )

    await server.plan_sub_query(
        session_id=session_id,
        thought="Second sub-query depends on first.",
        id="sq2",
        goal="Compare findings against baseline.",
        expected_output="A comparison summary.",
        boundary="Exclude baseline collection.",
        depends_on="sq1",
        tool_hint="web_search",
    )

    await server.plan_search_term(
        session_id=session_id,
        thought="First search term.",
        term="provider baseline",
        purpose="sq1",
        round=1,
        approach="targeted",
    )

    await server.plan_search_term(
        session_id=session_id,
        thought="Second search term.",
        term="provider comparison",
        purpose="sq2",
        round=2,
    )

    await server.plan_tool_mapping(
        session_id=session_id,
        thought="Map sq1.",
        sub_query_id="sq1",
        tool="web_search",
        reason="Need baseline facts.",
    )

    await server.plan_tool_mapping(
        session_id=session_id,
        thought="Map sq2.",
        sub_query_id="sq2",
        tool="web_search",
        reason="Need comparison facts.",
    )

    unknown = json.loads(
        await server.plan_execution(
            session_id=session_id,
            thought="Unknown ID should fail.",
            parallel_groups="sq1,sq3",
            sequential="sq2",
            estimated_rounds=2,
        )
    )
    assert unknown["error"] == "validation_error"
    assert "unknown sub-query id" in unknown["message"].lower()

    repeated = json.loads(
        await server.plan_execution(
            session_id=session_id,
            thought="Repeated ID should fail.",
            parallel_groups="sq1;sq2",
            sequential="sq2",
            estimated_rounds=2,
        )
    )
    assert repeated["error"] == "validation_error"
    assert "duplicate execution id" in repeated["message"].lower()

    dependency = json.loads(
        await server.plan_execution(
            session_id=session_id,
            thought="Dependency order should fail.",
            parallel_groups="sq1,sq2",
            sequential="",
            estimated_rounds=1,
        )
    )
    assert dependency["error"] == "validation_error"
    assert "dependency order" in dependency["message"].lower()
