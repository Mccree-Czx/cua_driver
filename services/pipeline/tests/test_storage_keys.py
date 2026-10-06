"""§4 MinIO 对象键规范逐字测试（纯函数，不触 MinIO/MySQL）。

§4 binding：
- PDF 简历：resumes/{job_id}/{liepin_user_id}/{姓名}_{岗位}_{YYYYMMDD}[_{HHMMSS}].pdf
  重名（冲突）才加时间戳——ts=None 时不含 _HHMMSS。
- 在线简历截图：snapshots/{liepin_user_id}/{YYYYMMDD}_{HHMMSS}.png
  同一人重复截图保留历史，恒带时间戳。

conftest 的迁移 fixture 照常跑一次（session 级），本文件不打 MySQL。
"""

from datetime import date

from app.storage import resume_object_key, snapshot_object_key


def test_resume_key_canonical_without_ts():
    assert (
        resume_object_key(1, "LP001", "张伟", "产品经理", date(2026, 10, 5))
        == "resumes/1/LP001/张伟_产品经理_20261005.pdf"
    )


def test_resume_key_conflict_variant_with_ts():
    assert (
        resume_object_key(1, "LP001", "张伟", "产品经理", date(2026, 10, 5), "143025")
        == "resumes/1/LP001/张伟_产品经理_20261005_143025.pdf"
    )


def test_snapshot_key_always_ts():
    assert (
        snapshot_object_key("LP001", date(2026, 10, 5), "143025")
        == "snapshots/LP001/20261005_143025.png"
    )


def test_date_zero_padded_single_digit():
    """单数月份/日期补零（YYYYMMDD 逐字），ts 原样拼入。"""
    assert (
        resume_object_key(7, "LP002", "李雷", "后端工程师", date(2026, 1, 2), "010203")
        == "resumes/7/LP002/李雷_后端工程师_20260102_010203.pdf"
    )
