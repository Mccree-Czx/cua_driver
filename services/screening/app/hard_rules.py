"""硬规则过滤（spec v1.6 §3 步骤 3）：学历 / 年限 / 城市 / 排除词。

规则键集（controller 裁定）：
- min_education:   最低学历（按级别比较）
- min_years:       最低工作年限（整数；简历 years_of_experience 解析首个数字）
- cities:          允许城市列表（精确匹配）
- exclude_keywords: 排除词列表（命中任一 → 不通过；扫描简历全部文本字段）

缺键 / 值为 None / 空列表 = 无该约束。判定保守：学历无法识别、年限无法
解析均判不通过；规则值本身无法识别（如 min_education="本科及以上"）fail
closed 产出"规则配置非法"理由——学历门槛是合规闸门，不得静默放行
（评审 Important，宁可人工复核，不漏放）。
"""

import re

from hr_workbuddy import MinimalResume

# 学历级别（低 → 高）；不在表内的学历视为低于一切（保守判不通过）
EDUCATION_LEVELS = ("高中", "中专", "大专", "本科", "硕士", "博士")

_YEARS_RE = re.compile(r"(\d+)")

# 排除词扫描的文本字段（除 liepin_user_id 外的全部字符串字段）
_KEYWORD_FIELDS = ("name", "education", "city", "salary", "experience_summary")


def evaluate(rules: dict, resume: MinimalResume) -> tuple[bool, list[str]]:
    """返回 (是否通过, 不通过原因列表)；通过时 reasons 为空。"""
    reasons: list[str] = []

    min_education = rules.get("min_education")
    if min_education:
        rule_level = _education_level(min_education)
        if rule_level == -1:
            # fail closed（评审 Important）：规则值无法识别（如 "本科及以上"）时
            # 对称比较会静默放行全部简历——学历门槛是合规闸门，必须产出理由拒绝
            reasons.append(f"规则配置非法: 学历要求'{min_education}'无法识别")
        elif _education_level(resume.education) < rule_level:
            reasons.append(f"学历{resume.education}低于要求{min_education}")

    min_years = rules.get("min_years")
    if min_years is not None:
        try:
            min_years = int(min_years)
        except (TypeError, ValueError):
            min_years = None  # 规则配置非法：跳过该规则（LLM 阶段仍会把关）
        if min_years is not None:
            years = _parse_years(resume.years_of_experience)
            if years is None:
                reasons.append(f"工作年限'{resume.years_of_experience}'无法解析")
            elif years < min_years:
                reasons.append(
                    f"工作年限{resume.years_of_experience}低于要求{min_years}年"
                )

    cities = rules.get("cities")
    if cities:
        if resume.city not in cities:
            reasons.append(f"城市{resume.city}不在允许列表{'/'.join(map(str, cities))}")

    exclude_keywords = rules.get("exclude_keywords")
    if exclude_keywords:
        hits = [kw for kw in exclude_keywords if _contains_keyword(resume, kw)]
        if hits:
            reasons.append("排除词命中: " + "/".join(hits))

    return (not reasons, reasons)


def _education_level(education: str) -> int:
    """学历 → 级别索引；无法识别返回 -1（低于一切，保守判不通过）。

    -1 是对称的：evaluate 对简历侧与规则侧分别处理——简历侧 -1 判不通过
    （保守），规则侧 -1 必须 fail closed 产出"规则配置非法"理由，
    不得让两侧同为 -1 时静默通过。
    """
    try:
        return EDUCATION_LEVELS.index(education)
    except ValueError:
        return -1


def _parse_years(raw: str) -> int | None:
    """'5' / '5年' / '3-5年' → 首个整数；无法解析返回 None。"""
    match = _YEARS_RE.search(raw)
    return int(match.group(1)) if match else None


def _contains_keyword(resume: MinimalResume, keyword: str) -> bool:
    text = " ".join(str(getattr(resume, field)) for field in _KEYWORD_FIELDS)
    return keyword in text
