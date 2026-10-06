"""hard_rules.evaluate 两种判定 + 理由非空（T5 RED-first）。

fixtures 对齐 brief：min_education=本科 / min_years=3 / cities 列表 /
exclude_keywords 列表；每条规则单独触发不通过并断言理由非空，
通过时 reasons 为空。
"""

import pytest

from hr_workbuddy import MinimalResume

from app.hard_rules import evaluate


@pytest.fixture
def rules() -> dict:
    return {
        "min_education": "本科",
        "min_years": 3,
        "cities": ["深圳", "广州"],
        "exclude_keywords": ["外包", "劳务派遣"],
    }


@pytest.fixture
def make_resume():
    def _make(**overrides) -> MinimalResume:
        kwargs = {
            "name": "张伟",
            "liepin_user_id": "LP001",
            "education": "本科",
            "years_of_experience": "5",
            "city": "深圳",
            "salary": "20-30K",
            "experience_summary": "5 年产品经理经验，主导过企业级项目",
        }
        kwargs.update(overrides)
        return MinimalResume(**kwargs)

    return _make


class TestPass:
    def test_all_rules_satisfied(self, rules, make_resume):
        passed, reasons = evaluate(rules, make_resume())
        assert passed is True
        assert reasons == []

    def test_boundary_values_pass(self, rules, make_resume):
        # 学历恰好本科、年限恰好 3、城市在列表内、无排除词
        passed, reasons = evaluate(
            rules, make_resume(years_of_experience="3", city="广州")
        )
        assert passed is True
        assert reasons == []


class TestFail:
    def test_education_below_minimum(self, rules, make_resume):
        passed, reasons = evaluate(rules, make_resume(education="大专"))
        assert passed is False
        assert reasons and all(r for r in reasons)
        assert any("学历" in r for r in reasons)

    def test_years_below_minimum(self, rules, make_resume):
        passed, reasons = evaluate(rules, make_resume(years_of_experience="2"))
        assert passed is False
        assert reasons and all(r for r in reasons)
        assert any("年限" in r for r in reasons)

    def test_city_not_allowed(self, rules, make_resume):
        passed, reasons = evaluate(rules, make_resume(city="北京"))
        assert passed is False
        assert reasons and all(r for r in reasons)
        assert any("城市" in r for r in reasons)

    def test_exclude_keyword_hit(self, rules, make_resume):
        passed, reasons = evaluate(
            rules, make_resume(experience_summary="曾在某外包公司任职")
        )
        assert passed is False
        assert reasons and all(r for r in reasons)
        assert any("排除词" in r for r in reasons)

    def test_multiple_violations_collect_all_reasons(self, rules, make_resume):
        passed, reasons = evaluate(
            rules, make_resume(education="大专", years_of_experience="1", city="北京")
        )
        assert passed is False
        assert len(reasons) == 3


class TestRuleMisconfig:
    """评审 Important：规则值无法识别时 fail closed，不得静默放行。"""

    def test_unknown_min_education_rule_fails_closed(self, make_resume):
        # "本科及以上" 不在级别表 → 若对称比较会静默通过；必须产出理由拒绝
        passed, reasons = evaluate(
            {"min_education": "本科及以上"}, make_resume(education="博士")
        )
        assert passed is False
        assert reasons and all(r for r in reasons)
        assert any("无法识别" in r for r in reasons)

    def test_unknown_min_education_rule_rejects_even_high_education(self, make_resume):
        # 门槛被静默禁用的典型场景：再高的学历也必须被拦住
        passed, reasons = evaluate(
            {"min_education": "Bachelor"}, make_resume(education="博士")
        )
        assert passed is False
        assert any("无法识别" in r for r in reasons)

    def test_unknown_resume_education_still_fails_conservatively(
        self, make_resume
    ):
        # 规则值合法、简历学历无法识别：维持保守判不通过（既有行为，锁定）
        passed, reasons = evaluate(
            {"min_education": "本科"}, make_resume(education="MBA")
        )
        assert passed is False
        assert any("学历" in r for r in reasons)


class TestRuleKeys:
    def test_missing_keys_mean_no_constraint(self, make_resume):
        passed, reasons = evaluate({}, make_resume(education="高中", city="北京"))
        assert passed is True
        assert reasons == []

    def test_empty_values_mean_no_constraint(self, make_resume):
        passed, reasons = evaluate(
            {"min_education": None, "cities": [], "exclude_keywords": []},
            make_resume(education="大专", city="北京"),
        )
        assert passed is True
        assert reasons == []
