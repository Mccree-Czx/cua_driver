"""多账号支持：新增 accounts 表，jobs/candidates 加 account_id（2026-10-14）

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-14

背景：系统原先单账号硬编码——登录态、每日配额、风控熔断都是全局单份，
数据模型里也没有账号维度。引入多账号后，多个猎聘招聘号各自独立。

关键改动是 candidates.liepin_user_id：由「全局唯一」改为「账号内唯一」。
不改这一条，同一个候选人在第二个号下就建不了档（撞唯一键），完整隔离
无从谈起。

存量数据全部归到 id=1 的「默认账号」。account_id 先带 DEFAULT 1 建列以
回填，随后 DROP DEFAULT——模型里该列没有 server_default，留着会造成
`alembic check` 漂移。

downgrade 的已知限制：若两个账号下确实建了相同的 liepin_user_id，恢复
全局唯一索引会失败。回滚前需先清理重复，或直接还原备份。
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. accounts 表
    op.execute(
        """
        CREATE TABLE accounts (
            id INT NOT NULL AUTO_INCREMENT,
            name VARCHAR(255) NOT NULL,
            is_active TINYINT(1) NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            KEY ix_accounts_is_active (is_active)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )

    # 2. 默认账号（存量数据全部归它），并置为当前生效
    op.execute("INSERT INTO accounts (id, name, is_active) VALUES (1, '默认账号', 1)")

    # 3. jobs.account_id（先带默认值回填，再撤掉默认值）
    op.execute("ALTER TABLE jobs ADD COLUMN account_id INT NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE jobs ALTER COLUMN account_id DROP DEFAULT")
    op.execute(
        "ALTER TABLE jobs ADD CONSTRAINT fk_jobs_account_id_accounts "
        "FOREIGN KEY (account_id) REFERENCES accounts (id)"
    )
    op.execute("ALTER TABLE jobs ADD KEY ix_jobs_account_id (account_id)")

    # 4. candidates.account_id（同上）
    op.execute("ALTER TABLE candidates ADD COLUMN account_id INT NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE candidates ALTER COLUMN account_id DROP DEFAULT")
    op.execute(
        "ALTER TABLE candidates ADD CONSTRAINT fk_candidates_account_id_accounts "
        "FOREIGN KEY (account_id) REFERENCES accounts (id)"
    )
    op.execute("ALTER TABLE candidates ADD KEY ix_candidates_account_id (account_id)")

    # 5. job_candidate.account_id（冗余列，见 models.py 里的说明）
    op.execute("ALTER TABLE job_candidate ADD COLUMN account_id INT NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE job_candidate ALTER COLUMN account_id DROP DEFAULT")
    op.execute(
        "ALTER TABLE job_candidate ADD CONSTRAINT fk_job_candidate_account_id_accounts "
        "FOREIGN KEY (account_id) REFERENCES accounts (id)"
    )
    op.execute("ALTER TABLE job_candidate ADD KEY ix_job_candidate_account_id (account_id)")

    # 6b. 把冗余列对齐到来源（当前全是账号 1，此处是空操作；写出来是为了让
    #     「job_candidate.account_id 恒等于其 job 的 account_id」这个不变量显式化）
    op.execute(
        "UPDATE job_candidate jc JOIN jobs j ON j.id = jc.job_id "
        "SET jc.account_id = j.account_id"
    )

    # 7. task_logs.account_id（可空）：存量日志全部归账号 1。
    #    /manual-queue 会带出候选人姓名，必须按它过滤，否则跨账号泄露。
    op.execute("ALTER TABLE task_logs ADD COLUMN account_id INT NULL DEFAULT 1")
    op.execute("ALTER TABLE task_logs ALTER COLUMN account_id DROP DEFAULT")
    op.execute(
        "ALTER TABLE task_logs ADD CONSTRAINT fk_task_logs_account_id_accounts "
        "FOREIGN KEY (account_id) REFERENCES accounts (id)"
    )
    op.execute("ALTER TABLE task_logs ADD KEY ix_task_logs_account_id (account_id)")

    # 8. 唯一约束：全局 → 账号内
    op.execute("ALTER TABLE candidates DROP INDEX liepin_user_id")
    op.execute(
        "ALTER TABLE candidates ADD CONSTRAINT uq_candidate_account_liepin "
        "UNIQUE (account_id, liepin_user_id)"
    )


def downgrade() -> None:
    # 恢复全局唯一（若跨账号已有重复 liepin_user_id，此处会失败——见模块 docstring）
    op.execute("ALTER TABLE candidates DROP INDEX uq_candidate_account_liepin")
    op.execute("ALTER TABLE candidates ADD UNIQUE KEY liepin_user_id (liepin_user_id)")

    op.execute("ALTER TABLE task_logs DROP FOREIGN KEY fk_task_logs_account_id_accounts")
    op.execute("ALTER TABLE task_logs DROP KEY ix_task_logs_account_id")
    op.execute("ALTER TABLE task_logs DROP COLUMN account_id")

    op.execute(
        "ALTER TABLE job_candidate DROP FOREIGN KEY fk_job_candidate_account_id_accounts"
    )
    op.execute("ALTER TABLE job_candidate DROP KEY ix_job_candidate_account_id")
    op.execute("ALTER TABLE job_candidate DROP COLUMN account_id")

    op.execute("ALTER TABLE candidates DROP FOREIGN KEY fk_candidates_account_id_accounts")
    op.execute("ALTER TABLE candidates DROP KEY ix_candidates_account_id")
    op.execute("ALTER TABLE candidates DROP COLUMN account_id")

    op.execute("ALTER TABLE jobs DROP FOREIGN KEY fk_jobs_account_id_accounts")
    op.execute("ALTER TABLE jobs DROP KEY ix_jobs_account_id")
    op.execute("ALTER TABLE jobs DROP COLUMN account_id")

    op.execute("DROP TABLE accounts")
