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
async def test_plan_intent_revision_rejects_existing_downstream_phases():
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
        thought="Need complexity before revision.",
        level=2,
        estimated_sub_queries=1,
        estimated_tool_calls=3,
        justification="Need downstream phases to exist.",
    )

    result = json.loads(
        await server.plan_intent(
            session_id=session_id,
            thought="Intent revision should fail once downstream exists.",
            core_question="Compare providers in detail.",
            query_type="comparative",
            time_sensitivity="recent",
            is_revision=True,
        )
    )

    assert result["error"] == "validation_error"
    assert "restart planning" in result["message"].lower()
    assert result["details"][0]["field"] == "is_revision"


@pytest.mark.asyncio
async def test_plan_intent_revision_requires_existing_session():
    result = json.loads(
        await server.plan_intent(
            session_id="missing-session",
            thought="Revision against a missing session should fail.",
            core_question="Compare providers.",
            query_type="comparative",
            time_sensitivity="recent",
            is_revision=True,
        )
    )

    assert result["error"] == "session_not_found"
    assert result["restart_from_intent_analysis"] is True


@pytest.mark.asyncio
async def test_plan_intent_revision_rejects_empty_session_id():
    result = json.loads(
        await server.plan_intent(
            session_id="",
            thought="Empty revision session should fail.",
            core_question="Compare providers.",
            query_type="comparative",
            time_sensitivity="recent",
            is_revision=True,
        )
    )

    assert result["error"] == "session_not_found"
    assert result["restart_from_intent_analysis"] is True


@pytest.mark.asyncio
async def test_plan_complexity_revision_rejects_existing_downstream_phases():
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
        thought="Initial complexity.",
        level=2,
        estimated_sub_queries=1,
        estimated_tool_calls=3,
        justification="Need search strategy.",
    )

    await server.plan_sub_query(
        session_id=session_id,
        thought="Create downstream phase first.",
        id="sq1",
        goal="Compare providers.",
        expected_output="A concise comparison.",
        boundary="Exclude implementation details.",
        tool_hint="web_search",
    )

    result = json.loads(
        await server.plan_complexity(
            session_id=session_id,
            thought="Complexity revision should fail once downstream exists.",
            level=1,
            estimated_sub_queries=1,
            estimated_tool_calls=2,
            justification="Should force a restart instead of mutating in place.",
            is_revision=True,
        )
    )

    assert result["error"] == "validation_error"
    assert "restart planning" in result["message"].lower()
    assert result["details"][0]["field"] == "is_revision"


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
async def test_level_2_plan_success_returns_complete_executable_plan():
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
        thought="Need search strategy and tool mapping.",
        level=2,
        estimated_sub_queries=2,
        estimated_tool_calls=5,
        justification="Need a complete level 2 plan.",
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
    await server.plan_sub_query(
        session_id=session_id,
        thought="Second sub-query.",
        id="sq2",
        goal="Compare API compatibility.",
        expected_output="A concise compatibility comparison.",
        boundary="Exclude pricing discussion.",
        tool_hint="web_search",
    )

    await server.plan_search_term(
        session_id=session_id,
        thought="Seed strategy.",
        term="provider pricing",
        purpose="sq1",
        round=1,
        approach="targeted",
    )
    await server.plan_search_term(
        session_id=session_id,
        thought="Append another search term.",
        term="provider api compatibility",
        purpose="sq2",
        round=2,
    )

    await server.plan_tool_mapping(
        session_id=session_id,
        thought="Map pricing.",
        sub_query_id="sq1",
        tool="web_search",
        reason="Need pricing facts.",
    )
    result = json.loads(
        await server.plan_tool_mapping(
            session_id=session_id,
            thought="Map compatibility.",
            sub_query_id="sq2",
            tool="web_search",
            reason="Need compatibility facts.",
        )
    )

    assert result["plan_complete"] is True
    assert "phases_remaining" not in result
    assert result["complexity_level"] == 2
    assert result["completed_phases"] == [
        "intent_analysis",
        "complexity_assessment",
        "query_decomposition",
        "search_strategy",
        "tool_selection",
    ]
    assert result["executable_plan"]["search_strategy"]["approach"] == "targeted"
    assert [item["purpose"] for item in result["executable_plan"]["search_strategy"]["search_terms"]] == ["sq1", "sq2"]
    assert [item["id"] for item in result["executable_plan"]["query_decomposition"]] == ["sq1", "sq2"]
    assert [item["sub_query_id"] for item in result["executable_plan"]["tool_selection"]] == ["sq1", "sq2"]


@pytest.mark.asyncio
async def test_level_2_blocks_execution_phase_after_tool_selection():
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
        thought="Need a complete level 2 plan.",
        level=2,
        estimated_sub_queries=1,
        estimated_tool_calls=3,
        justification="Level 2 should stop at tool selection.",
    )
    await server.plan_sub_query(
        session_id=session_id,
        thought="Single sub-query.",
        id="sq1",
        goal="Compare providers.",
        expected_output="A concise comparison.",
        boundary="Exclude implementation details.",
        tool_hint="web_search",
    )
    await server.plan_search_term(
        session_id=session_id,
        thought="Seed strategy.",
        term="provider comparison",
        purpose="sq1",
        round=1,
        approach="targeted",
    )
    await server.plan_tool_mapping(
        session_id=session_id,
        thought="Map the only sub-query.",
        sub_query_id="sq1",
        tool="web_search",
        reason="Need direct comparison facts.",
    )

    result = json.loads(
        await server.plan_execution(
            session_id=session_id,
            thought="Execution should be blocked for level 2.",
            parallel_groups="sq1",
            sequential="",
            estimated_rounds=1,
        )
    )

    assert result["error"] == "Level 2 planning completes after tool_selection."


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


@pytest.mark.asyncio
async def test_plan_sub_query_rejects_invalid_tool_hint():
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
        justification="Need a valid sub-query tool hint.",
    )

    result = json.loads(
        await server.plan_sub_query(
            session_id=session_id,
            thought="Invalid tool hint should fail.",
            id="sq1",
            goal="Compare providers.",
            expected_output="A concise comparison.",
            boundary="Exclude implementation details.",
            tool_hint="web_scrape",
        )
    )

    assert result["error"] == "validation_error"


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


@pytest.mark.asyncio
async def test_plan_sub_query_revision_rejects_dangling_downstream_references():
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
        thought="Need full planning.",
        level=2,
        estimated_sub_queries=1,
        estimated_tool_calls=3,
        justification="Need downstream phases before revision.",
    )

    await server.plan_sub_query(
        session_id=session_id,
        thought="Original sub-query.",
        id="sq1",
        goal="Compare providers.",
        expected_output="A concise comparison.",
        boundary="Exclude implementation details.",
        tool_hint="web_search",
    )

    await server.plan_search_term(
        session_id=session_id,
        thought="Valid search term.",
        term="provider comparison",
        purpose="sq1",
        round=1,
        approach="targeted",
    )

    result = json.loads(
        await server.plan_sub_query(
            session_id=session_id,
            thought="Revision should fail after downstream phases exist.",
            id="sq2",
            goal="Replace decomposition.",
            expected_output="A replacement sub-query.",
            boundary="Exclude the old decomposition.",
            tool_hint="web_search",
            is_revision=True,
        )
    )

    assert result["error"] == "validation_error"
    assert "restart planning" in result["message"].lower()


@pytest.mark.asyncio
async def test_plan_execution_requires_all_sub_queries_to_be_scheduled():
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
        justification="Need execution coverage validation.",
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

    result = json.loads(
        await server.plan_execution(
            session_id=session_id,
            thought="Missing sq1 should fail.",
            parallel_groups="",
            sequential="sq2",
            estimated_rounds=1,
        )
    )

    assert result["error"] == "validation_error"
    assert "missing sub-query ids" in result["message"].lower()


@pytest.mark.asyncio
async def test_level_3_plan_flow_completes_with_execution_order_in_executable_plan():
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
        justification="Need complete decomposition and execution order.",
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

    result = json.loads(
        await server.plan_execution(
            session_id=session_id,
            thought="Schedule baseline first, comparison second.",
            parallel_groups="sq1",
            sequential="sq2",
            estimated_rounds=2,
        )
    )

    assert result["plan_complete"] is True
    assert result["complexity_level"] == 3
    assert result["executable_plan"]["execution_order"] == {
        "parallel": [["sq1"]],
        "sequential": ["sq2"],
        "estimated_rounds": 2,
    }


@pytest.mark.asyncio
async def test_level_2_plan_does_not_complete_until_all_sub_queries_are_mapped():
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
        thought="Need full level 2 planning.",
        level=2,
        estimated_sub_queries=2,
        estimated_tool_calls=5,
        justification="Need two mapped sub-queries.",
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
        thought="Second sub-query.",
        id="sq2",
        goal="Compare findings against baseline.",
        expected_output="A comparison summary.",
        boundary="Exclude baseline collection.",
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

    result = json.loads(
        await server.plan_tool_mapping(
            session_id=session_id,
            thought="Only map one sub-query first.",
            sub_query_id="sq1",
            tool="web_search",
            reason="Need baseline facts.",
        )
    )

    assert result["plan_complete"] is False
    assert "tool_selection" in result["completed_phases"]


@pytest.mark.asyncio
async def test_plan_execution_rejects_incomplete_tool_mapping_coverage():
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
        justification="Need execution coverage validation.",
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
        thought="Second sub-query.",
        id="sq2",
        goal="Compare findings against baseline.",
        expected_output="A comparison summary.",
        boundary="Exclude baseline collection.",
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
        thought="Only map one sub-query first.",
        sub_query_id="sq1",
        tool="web_search",
        reason="Need baseline facts.",
    )

    result = json.loads(
        await server.plan_execution(
            session_id=session_id,
            thought="Execution should fail before all sub-queries are mapped.",
            parallel_groups="sq1",
            sequential="sq2",
            estimated_rounds=2,
        )
    )

    assert result["error"] == "validation_error"
    assert "missing tool mapping" in result["message"].lower()


@pytest.mark.asyncio
async def test_plan_sub_query_rejects_self_dependency():
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
        level=1,
        estimated_sub_queries=1,
        estimated_tool_calls=2,
        justification="Need dependency validation.",
    )

    result = json.loads(
        await server.plan_sub_query(
            session_id=session_id,
            thought="Self dependency should fail.",
            id="sq1",
            goal="Compare providers.",
            expected_output="A concise comparison.",
            boundary="Exclude implementation details.",
            depends_on="sq1",
            tool_hint="web_search",
        )
    )

    assert result["error"] == "validation_error"
    assert "cannot depend on itself" in result["message"].lower()


@pytest.mark.asyncio
async def test_plan_sub_query_rejects_duplicate_dependencies():
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
        level=1,
        estimated_sub_queries=2,
        estimated_tool_calls=2,
        justification="Need dependency validation.",
    )

    await server.plan_sub_query(
        session_id=session_id,
        thought="Baseline sub-query.",
        id="sq1",
        goal="Collect baseline facts.",
        expected_output="A baseline summary.",
        boundary="Exclude comparison synthesis.",
        tool_hint="web_search",
    )

    result = json.loads(
        await server.plan_sub_query(
            session_id=session_id,
            thought="Duplicate dependency should fail.",
            id="sq2",
            goal="Compare findings against baseline.",
            expected_output="A comparison summary.",
            boundary="Exclude baseline collection.",
            depends_on="sq1,sq1",
            tool_hint="web_search",
        )
    )

    assert result["error"] == "validation_error"
    assert "duplicate sub-query dependency" in result["message"].lower()


@pytest.mark.asyncio
async def test_plan_tool_mapping_invalid_params_json_uses_standard_details_shape():
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
        thought="Need tool mapping.",
        level=2,
        estimated_sub_queries=1,
        estimated_tool_calls=3,
        justification="Need mapping validation.",
    )

    await server.plan_sub_query(
        session_id=session_id,
        thought="Only one sub-query.",
        id="sq1",
        goal="Compare providers.",
        expected_output="A concise comparison.",
        boundary="Exclude implementation details.",
        tool_hint="web_search",
    )

    await server.plan_search_term(
        session_id=session_id,
        thought="Valid search term.",
        term="provider comparison",
        purpose="sq1",
        round=1,
        approach="targeted",
    )

    result = json.loads(
        await server.plan_tool_mapping(
            session_id=session_id,
            thought="Invalid params JSON should fail.",
            sub_query_id="sq1",
            tool="web_search",
            reason="Need baseline facts.",
            params_json="{bad-json",
        )
    )

    assert result["error"] == "validation_error"
    assert result["details"][0]["field"] == "params_json"
    assert result["details"][0]["type"] == "json_invalid"


@pytest.mark.asyncio
async def test_plan_tool_mapping_rejects_duplicate_mapping_for_same_sub_query():
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
        thought="Need tool mapping.",
        level=2,
        estimated_sub_queries=1,
        estimated_tool_calls=3,
        justification="Need mapping validation.",
    )

    await server.plan_sub_query(
        session_id=session_id,
        thought="Only one sub-query.",
        id="sq1",
        goal="Compare providers.",
        expected_output="A concise comparison.",
        boundary="Exclude implementation details.",
        tool_hint="web_search",
    )

    await server.plan_search_term(
        session_id=session_id,
        thought="Valid search term.",
        term="provider comparison",
        purpose="sq1",
        round=1,
        approach="targeted",
    )

    await server.plan_tool_mapping(
        session_id=session_id,
        thought="First mapping.",
        sub_query_id="sq1",
        tool="web_search",
        reason="Need baseline facts.",
    )

    result = json.loads(
        await server.plan_tool_mapping(
            session_id=session_id,
            thought="Second mapping for same sub-query should fail.",
            sub_query_id="sq1",
            tool="web_fetch",
            reason="Should be rejected.",
        )
    )

    assert result["error"] == "validation_error"
    assert "duplicate tool mapping" in result["message"].lower()


@pytest.mark.asyncio
async def test_plan_sub_query_revision_rejects_dependencies_on_removed_ids():
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
        thought="Need decomposition only.",
        level=1,
        estimated_sub_queries=2,
        estimated_tool_calls=2,
        justification="Need revision coverage.",
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

    result = json.loads(
        await server.plan_sub_query(
            session_id=session_id,
            thought="Revision should not keep dependencies on removed ids.",
            id="sq3",
            goal="Replacement sub-query.",
            expected_output="A replacement summary.",
            boundary="Exclude the old decomposition.",
            depends_on="sq1",
            tool_hint="web_search",
            is_revision=True,
        )
    )

    assert result["error"] == "validation_error"
    assert "unknown sub-query dependency" in result["message"].lower()


@pytest.mark.asyncio
async def test_plan_tool_mapping_revision_rejects_existing_execution_order():
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
        estimated_sub_queries=1,
        estimated_tool_calls=4,
        justification="Need execution before revision.",
    )

    await server.plan_sub_query(
        session_id=session_id,
        thought="Single sub-query.",
        id="sq1",
        goal="Compare providers.",
        expected_output="A concise comparison.",
        boundary="Exclude implementation details.",
        tool_hint="web_search",
    )

    await server.plan_search_term(
        session_id=session_id,
        thought="Valid search term.",
        term="provider comparison",
        purpose="sq1",
        round=1,
        approach="targeted",
    )

    await server.plan_tool_mapping(
        session_id=session_id,
        thought="Initial mapping.",
        sub_query_id="sq1",
        tool="web_search",
        reason="Need provider facts.",
    )

    await server.plan_execution(
        session_id=session_id,
        thought="Initial execution order.",
        parallel_groups="sq1",
        sequential="",
        estimated_rounds=1,
    )

    result = json.loads(
        await server.plan_tool_mapping(
            session_id=session_id,
            thought="Revision should fail after execution order exists.",
            sub_query_id="sq1",
            tool="web_fetch",
            reason="Try to rewrite mapping after execution.",
            is_revision=True,
        )
    )

    assert result["error"] == "validation_error"
    assert "restart planning" in result["message"].lower()
