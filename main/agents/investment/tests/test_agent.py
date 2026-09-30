"""100점 입력을 30/30/30/10으로 합산하고 보고서로 전달하는 규칙 검증."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from main.agents.investment import agent
from main.agents.investment.example import sample_state


def assessment(parts=(3, 2, 3), ref='team'):
    return {'team_rating': {'criteria': {
        key: {'score': score, 'reason': '가상 세부 평가', 'evidence_ids': [ref]}
        for key, score in zip(agent.TEAM_WEIGHTS, parts)},
        'reason': '가상 팀 평가', 'evidence_ids': [ref]},
        'judgment_notes': ['가상 검증 메모 [technology]'],
        'commitment_note': '지속 행동을 확인하고 인터뷰 필요 사항을 구분함 [team]',
        'missing_items': [], 'conflicts': []}


class FakeModel:
    def __init__(self, outputs):
        self.outputs = outputs if isinstance(outputs, list) else [outputs]
        self.calls = []

    def with_structured_output(self, schema):
        self.schema = schema
        return self

    def invoke(self, messages):
        self.calls.append(messages)
        return deepcopy(self.outputs[min(len(self.calls) - 1, len(self.outputs) - 1)])


def new_team_data():
    evidence = deepcopy(sample_state()['evidence_registry']['team'])
    evidence.update(id='team-new', document_id='new-document', chunk_id='new-chunk',
                    url='https://example.test/team-new')
    return {'team_info': {'founders': ['가상 창업자'], 'roles': '수집한 현장 운영 담당자',
                          'evidence_ids': ['team-new']}, 'evidence': [evidence],
            'missing_items': [], 'conflicts': []}


class JudgeTests(unittest.TestCase):
    def test_new_weights_and_exact_sum(self):
        result = agent.investment_judge_node(sample_state())
        self.assertEqual(agent.WEIGHTS, {'technology': 30, 'market': 30, 'competitive_advantage': 30, 'team': 10})
        self.assertEqual([x['score'] for x in result['scorecard'].values()], [27, 24, 24, 8])
        self.assertEqual([result['scorecard'][key]['source_score'] for key in agent.WEIGHTS if key != 'team'],
                         [90, 80, 80])
        self.assertEqual(result['total_score'], 83)
        self.assertEqual(result['upstream_score_90'], 75)
        self.assertEqual(result['judge_score_10'], 8)
        self.assertEqual(result['report_payload']['evaluation']['upstream_score_90'], 75)
        self.assertEqual(result['report_payload']['evaluation']['judge_score_10'], 8)
        self.assertEqual(result['decision'], 'invest')
        self.assertNotIn('risk_response', result['scorecard'])

    def test_inputs_and_upstream_scores_not_mutated(self):
        state = sample_state()
        before = deepcopy(state)
        result = agent.investment_judge_node(state)
        self.assertEqual(state, before)
        for field, key in agent.ANALYSIS_KEYS.items():
            self.assertEqual(result['scorecard'][key]['score'], state[field]['score'] * 0.3)
            self.assertEqual(result['scorecard'][key]['source_score'], state[field]['score'])

    def test_upstream_evidence_issue_is_reported_without_rewriting_score(self):
        state = sample_state()
        state['technology_analysis']['evidence'][0]['source_type'] = 'company'
        result = agent.investment_judge_node(state)
        self.assertEqual(result['scorecard']['technology']['score'], 27)
        self.assertEqual(result['total_score'], 83)
        self.assertEqual(result['decision'], 'invest')
        self.assertTrue(result['needs_research'])

    def test_missing_upstream_reference_retains_submitted_score(self):
        state = sample_state()
        state['market_analysis']['evidence_ids'] = ['unknown']
        result = agent.investment_judge_node(state)
        self.assertEqual(result['scorecard']['market']['score'], 24)
        self.assertEqual(result['decision'], 'invest')

    def test_none_does_not_redistribute(self):
        state = sample_state()
        state['market_analysis']['score'] = None
        result = agent.investment_judge_node(state)
        self.assertIsNone(result['total_score'])
        self.assertEqual(result['scorecard']['technology']['score'], 27)
        self.assertEqual(result['scorecard']['market']['max_score'], 30)

    def test_old_rating_contract_rejected(self):
        state = sample_state()
        state['technology_analysis'] = {'summary': '旧形式', 'ratings': {'technology': {'score': 4}}}
        with self.assertRaises(ValueError):
            agent.investment_judge_node(state)

    def test_scores_reject_bool_nonfinite_and_wrong_scale(self):
        for score in (-1, 101, True, float('nan'), float('inf'), '25'):
            state = sample_state()
            state['technology_analysis']['score'] = score
            with self.subTest(score=score), self.assertRaises(ValueError):
                agent.investment_judge_node(state)
        state = sample_state()
        state['market_analysis']['max_score'] = 30
        with self.assertRaises(ValueError):
            agent.investment_judge_node(state)

    def test_confirmed_zero_is_a_score_not_na(self):
        state = sample_state()
        for field in agent.ANALYSIS_KEYS:
            state[field]['score'] = 0
        state['team_rating']['score'] = 0
        result = agent.investment_judge_node(state)
        self.assertEqual(result['total_score'], 0)
        self.assertEqual(result['decision'], 'hold')
        self.assertFalse(result['needs_research'])

    def test_decimal_scores_preserved(self):
        state = sample_state()
        state['market_analysis']['score'] = 81.5
        result = agent.investment_judge_node(state)
        self.assertEqual(result['scorecard']['market']['score'], 24.45)
        self.assertEqual(result['total_score'], 83.45)

    def test_team_raw_ten_point_assessment(self):
        state = sample_state()
        state['team_rating']['score'] = 10
        result = agent.investment_judge_node(state)
        self.assertEqual(result['team_rating']['score'], 10)
        self.assertEqual(result['total_score'], 85)

    def test_team_criteria_four_two_four(self):
        state = sample_state()
        state.pop('team_rating')
        result = agent.judge_investment(state, team_judgment=assessment())
        self.assertEqual(result['team_rating']['score'], 8)
        self.assertEqual([x['max_score'] for x in result['team_rating']['criteria'].values()], [4, 2, 4])

    def test_missing_team_component_counts_as_zero(self):
        result = agent.judge_investment(sample_state(), team_judgment=assessment((3, None, 3)))
        self.assertEqual(result['team_rating']['score'], 6)
        self.assertEqual(result['total_score'], 81)

    def test_missing_team_requests_judges_own_research(self):
        state = sample_state()
        state.pop('team_rating')
        result = agent.investment_judge_node(state)
        self.assertEqual(result['total_score'], 75)
        self.assertEqual(result['team_rating']['score'], 0)
        self.assertEqual(result['research_requests'][0]['target_agent'], 'investment_judge')

    def test_team_company_only_cap(self):
        state = sample_state()
        state['evidence_registry']['team']['source_type'] = 'company'
        result = agent.investment_judge_node(state)
        self.assertEqual(result['team_rating']['score'], 4)
        self.assertEqual(result['team_rating']['uncapped_score'], 8)
        self.assertEqual(result['decision'], 'hold')

    def test_other_company_cannot_validate_team(self):
        state = sample_state()
        state['evidence_registry']['team']['company_id'] = 'other'
        result = agent.investment_judge_node(state)
        self.assertEqual(result['team_rating']['score'], 0)

    def test_risk_blocks_high_total_without_a_fifth_score(self):
        state = sample_state()
        state['competition_analysis']['critical_risks'] = [
            {'description': '가상 안전 위험', 'evidence_ids': ['competition'], 'resolved': False}]
        result = agent.investment_judge_node(state)
        self.assertEqual(result['total_score'], 83)
        self.assertEqual(result['decision'], 'hold')
        self.assertEqual(result['current_evaluation']['decision_category'], '중대한 위험 미해결')
        self.assertEqual(len(result['scorecard']), 4)

    def test_unregistered_risk_still_preserved_for_hold_report(self):
        state = sample_state()
        state['competition_analysis']['critical_risks'] = [
            {'description': '확인 필요 위험', 'evidence_ids': ['unknown'], 'resolved': False}]
        result = agent.investment_judge_node(state)
        self.assertEqual(result['decision'], 'hold')
        self.assertFalse(result['current_evaluation']['critical_risks'][0]['evidence_verified'])
        self.assertIn('확인 필요 위험', str(result['hold_payload']))

    def test_conflict_blocks_recommendation(self):
        state = sample_state()
        state['market_analysis']['conflicts'] = ['시장 규모의 기준연도 상충']
        result = agent.investment_judge_node(state)
        self.assertEqual(result['decision'], 'hold')

    def test_eligibility_unverified_or_exited(self):
        state = sample_state()
        state['current_candidate'].pop('eligibility')
        result = agent.investment_judge_node(state)
        self.assertEqual(result['decision'], 'invest')
        self.assertEqual(result['current_evaluation']['eligibility_status'], 'unverified')
        state = sample_state()
        state['current_candidate']['eligibility']['has_exited'] = True
        result = agent.investment_judge_node(state)
        self.assertEqual(result['current_evaluation']['decision_category'], '평가대상 외')
        self.assertFalse(result['needs_research'])

    def test_recommendation_and_hold_routes(self):
        result = agent.investment_judge_node(sample_state())
        self.assertEqual(agent.route_after_investment(result), 'report')
        self.assertEqual(result['next_action'], 'report')
        self.assertIsNotNone(result['report_payload'])
        state = sample_state()
        state['team_rating']['score'] = 4
        result = agent.investment_judge_node(state)
        self.assertEqual(agent.route_after_investment(result), 'hold')
        self.assertIsNone(result['report_payload'])
        self.assertIsNotNone(result['hold_payload'])

    def test_report_payload_preserves_business_funding_and_evidence(self):
        state = sample_state()
        state['current_candidate']['funding_info'] = {'stage': 'Seed', 'amount': '미확인'}
        result = agent.investment_judge_node(state)
        payload = result['report_payload']
        self.assertEqual(payload['company']['funding_info'], state['current_candidate']['funding_info'])
        self.assertEqual(payload['evaluation']['analysis']['market_analysis'], state['market_analysis'])
        self.assertIn('team', payload['evidence_registry'])
        self.assertEqual(payload['evaluation']['score'], 83)
        self.assertTrue(payload['evaluation']['is_mock'])

    def test_configurable_decision_policy(self):
        state = sample_state()
        state['decision_policy'] = {'recommend_min_score': 90}
        self.assertEqual(agent.investment_judge_node(state)['decision'], 'hold')
        state['decision_policy'] = {'recommend_min_score': 80, 'minimum_scores': {'team': 9}}
        self.assertEqual(agent.investment_judge_node(state)['decision'], 'hold')

    def test_market_definition_lock(self):
        state = sample_state()
        state['market_definition'] = '기존 시장'
        result = agent.investment_judge_node(state)
        self.assertEqual(result['market_definition'], '기존 시장')
        self.assertEqual(result['decision'], 'hold')

    def test_llm_only_assesses_team(self):
        state = sample_state()
        state.pop('team_rating')
        model = FakeModel(assessment())
        result = agent.make_investment_judge_node(model)(state)
        self.assertEqual(result['total_score'], 83)
        self.assertEqual(len(model.calls), 1)
        self.assertIn('너는 앞선 점수를 수정하거나', model.calls[0][0]['content'])

    def test_model_identifier_uses_team_judgment_model(self):
        model = FakeModel(assessment())
        state = sample_state()
        state.pop('team_rating')
        with patch('main.agents.investment.agent.resolve_chat_model', return_value=model) as resolve:
            result = agent.make_investment_judge_node('openai:gpt-4.1')(state)
        resolve.assert_called_once_with('openai:gpt-4.1')
        self.assertEqual(result['scorecard']['team']['score'], 8)
        self.assertEqual(result['total_score'], 83)
        self.assertIsNotNone(result['report_payload'])

    def test_llm_cannot_override_total_or_decision(self):
        invalid = assessment()
        invalid['decision'] = 'invest'
        with self.assertRaises(ValueError):
            agent.make_investment_judge_node(FakeModel(invalid))(sample_state())

    def test_llm_hallucinated_reference_is_na(self):
        result = agent.make_investment_judge_node(FakeModel(assessment(ref='imagined')))(sample_state())
        self.assertEqual(result['team_rating']['score'], 0)
        self.assertEqual(result['decision'], 'hold')

    def test_web_research_when_existing_team_data_missing(self):
        state = sample_state()
        state.pop('team_rating')
        state['current_candidate']['team_info'] = {}
        state['evidence_registry'].pop('team')
        calls = []
        def research(request):
            calls.append(request)
            return new_team_data()
        result = agent.make_investment_judge_node(FakeModel(assessment(ref='team-new')), team_researcher=research)(state)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['retry_count'], 1)
        self.assertEqual(result['decision'], 'invest')
        self.assertIn('team-new', result['report_payload']['evidence_registry'])
        self.assertEqual(result['updated_candidate']['team_info']['roles'], '수집한 현장 운영 담당자')
        self.assertEqual(result['current_candidate']['team_info']['roles'], '수집한 현장 운영 담당자')

    def test_existing_team_data_skips_web_research(self):
        def forbidden(request):
            self.fail('기존 팀 자료가 충분하면 수집하지 않아야 함')
        result = agent.make_investment_judge_node(FakeModel(assessment()), team_researcher=forbidden)(sample_state())
        self.assertEqual(result['retry_count'], 0)

    def test_llm_detected_gap_triggers_one_followup_search(self):
        calls = []
        model = FakeModel([assessment((3, None, 3)), assessment(ref='team-new')])
        def research(request):
            calls.append(request)
            return new_team_data()
        result = agent.make_investment_judge_node(model, team_researcher=research)(sample_state())
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result['decision'], 'invest')

    def test_two_search_cap_and_hold_when_still_missing(self):
        calls = []
        def research(request):
            calls.append(request)
            return {'team_info': {}, 'evidence': []}
        model = FakeModel(assessment((None, None, None)))
        result = agent.make_investment_judge_node(model, team_researcher=research)(sample_state())
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(result['retry_count'], 2)
        self.assertFalse(result['retry_available'])
        self.assertEqual(result['decision'], 'hold')

    def test_prior_search_count_consumes_budget(self):
        state = sample_state()
        state['retry_count'] = 1
        calls = []
        def research(request):
            calls.append(request)
            return {'evidence': []}
        result = agent.make_investment_judge_node(FakeModel(assessment((None, None, None))), team_researcher=research)(state)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['retry_count'], 2)

    def test_search_failure_recorded_as_hold(self):
        def failing(request):
            raise OSError('가상 검색 연결 실패')
        result = agent.make_investment_judge_node(FakeModel(assessment((None, None, None))), team_researcher=failing)(sample_state())
        self.assertEqual(result['decision'], 'hold')
        self.assertEqual(len(result['team_research_log']), 2)
        self.assertTrue(all(x['status'] == 'failed' for x in result['team_research_log']))

    def test_research_off_and_always_modes(self):
        calls = []
        def research(request):
            calls.append(request)
            return new_team_data()
        node = agent.make_investment_judge_node(FakeModel(assessment()), team_researcher=research, research_mode='off')
        self.assertEqual(node(sample_state())['retry_count'], 0)
        self.assertEqual(calls, [])
        node = agent.make_investment_judge_node(FakeModel(assessment()), team_researcher=research, research_mode='always')
        self.assertEqual(node(sample_state())['retry_count'], 1)
        self.assertEqual(len(calls), 1)

    def test_search_without_llm_does_not_invent_score(self):
        state = sample_state()
        state.pop('team_rating')
        state['current_candidate']['team_info'] = {}
        result = agent.make_investment_judge_node(team_researcher=lambda request: new_team_data())(state)
        self.assertEqual(result['team_rating']['score'], 0)
        self.assertEqual(result['decision'], 'hold')

    def test_ineligible_candidate_skips_model_and_search(self):
        state = sample_state()
        state['current_candidate']['eligibility']['has_exited'] = True
        model = FakeModel(assessment())
        def forbidden(request):
            self.fail('부적격 기업을 추가 검색하면 안 됨')
        result = agent.make_investment_judge_node(model, team_researcher=forbidden)(state)
        self.assertEqual(result['decision'], 'hold')
        self.assertEqual(model.calls, [])

    def test_graph_state_keeps_report_payload(self):
        try:
            import langgraph
        except ImportError:
            self.skipTest("langgraph is not installed")
        from langgraph.graph import END, START, StateGraph
        builder = StateGraph(agent.InvestmentJudgeState)
        builder.add_node('judge', agent.make_investment_judge_node(FakeModel(assessment())))
        routes = []
        def report(state):
            routes.append('report')
            self.assertEqual(state['report_payload']['evaluation']['score'], 83)
            return {}
        def hold(state):
            routes.append('hold')
            self.assertIsNotNone(state['hold_payload'])
            return {}
        builder.add_node('report', report)
        builder.add_node('hold', hold)
        builder.add_edge(START, 'judge')
        builder.add_conditional_edges('judge', agent.route_after_investment, {'report': 'report', 'hold': 'hold'})
        builder.add_edge('report', END)
        builder.add_edge('hold', END)
        graph = builder.compile()
        graph.invoke(sample_state())
        state = sample_state()
        state['technology_analysis']['score'] = 10
        graph.invoke(state)
        self.assertEqual(routes, ['report', 'hold'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
