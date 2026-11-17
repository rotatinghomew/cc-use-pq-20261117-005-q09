"""本地盲审用的审阅者身份目录。

这里刻意区分两类东西：

* 稳定审阅者身份（``reviewer_id``）：只在本地数据库中保存，投票、序号覆盖和
  一致性汇总都以它为准。它一经分配就不再改变。
* 展示代号（``reviewer_code``）：日常队列与报告中出现的合成代号。代号可以显式
  轮换；轮换只影响之后的显示，不会改写既有投票的归属。

本地接入口令（``alias``）也只保存在本机。它可能是一位审阅者在课堂上使用的
临时标签，也可能是某个历史代号。重复提交或代号轮换时，别名目录负责把口令
解析回同一个 ``reviewer_id``。

本工具没有用户认证：代号只是本地记录标签，不是凭据。任何能读取数据库的人
都能看到别名目录，因此这里不宣称匿名性来自访问控制。
"""

from __future__ import annotations

import secrets

from story_patterns.errors import NotFoundError, StoryPatternError, ValidationError

POLICY_ID = "stable-local-reviewer-codes/v1"
CODE_PREFIX = "R-"
CODE_HEX_DIGITS = 8
CODE_FORMAT = f"{CODE_PREFIX}{'0' * CODE_HEX_DIGITS}（大写十六进制）"
REVIEWER_ID_PREFIX = "reviewer-"
_MAX_ALLOCATION_ATTEMPTS = 50


def policy_metadata(codes_in_report: list[str] | set[str] | None = None) -> dict:
    """返回写进报告、随内容指纹一起冻结的匿名化口径说明。"""
    return {
        "policy": POLICY_ID,
        "method": "每位审阅者一个本地随机合成代号，不是由姓名推导",
        "code_format": CODE_FORMAT,
        "identity_basis": "稳定内部 reviewer_id；投票不按展示代号重新分组",
        "display_scope": "日常审阅队列，以及保存或导出的报告",
        "code_rotation": "可显式轮换；轮换只影响后续显示，既有决定的 reviewer_id 不变",
        "historical_reports": "已保存报告和已导出文件保持原样，不因之后的代号轮换而改写",
        "names_or_local_aliases_exported": False,
        "decision_identifiers": "报告为复核列出 decision_id；提交者应使用不含姓名的不透明标识",
        "authentication": "不提供用户认证；代号是本地标签，不是凭据",
        "codes_in_report": sorted(set(codes_in_report or [])),
    }


def decision_reader_key(vote: dict) -> str:
    """汇总时使用的稳定键。兼容只带 annotator 的直接分析调用。"""
    return vote.get("reviewer_id") or vote["annotator"]


def decision_reader_code(vote: dict) -> str:
    """展示用代号。生产路径由存储层联表填充 reviewer_code。"""
    return vote.get("reviewer_code") or vote.get("reviewer_id") or vote["annotator"]


def _new_reviewer_id() -> str:
    return REVIEWER_ID_PREFIX + secrets.token_hex(6)


def _new_code() -> str:
    return CODE_PREFIX + secrets.token_hex(CODE_HEX_DIGITS // 2).upper()


def _code_or_alias_exists(connection, code: str) -> bool:
    return bool(
        connection.execute("SELECT 1 FROM reviewer_codes WHERE code=?", (code,)).fetchone()
        or connection.execute("SELECT 1 FROM reviewer_aliases WHERE alias=?", (code,)).fetchone()
    )


def _allocate_code(connection) -> str:
    for _ in range(_MAX_ALLOCATION_ATTEMPTS):
        candidate = _new_code()
        if not _code_or_alias_exists(connection, candidate):
            return candidate
    raise StoryPatternError("could not allocate a unique reviewer code")


def _allocate_reviewer_id(connection) -> str:
    for _ in range(_MAX_ALLOCATION_ATTEMPTS):
        candidate = _new_reviewer_id()
        if not connection.execute(
            "SELECT 1 FROM reviewers WHERE reviewer_id=?", (candidate,)
        ).fetchone():
            return candidate
    raise StoryPatternError("could not allocate a unique reviewer identity")


def resolve_reviewer(connection, alias: str) -> str:
    """把本地口令或（历史）代号解析为稳定身份；首次出现时登记新审阅者。"""
    if not isinstance(alias, str) or not alias.strip():
        raise ValidationError("annotator must be a non-empty string")
    alias = alias.strip()
    existing = connection.execute(
        "SELECT reviewer_id FROM reviewer_aliases WHERE alias=?", (alias,)
    ).fetchone()
    if existing:
        return existing["reviewer_id"]
    reviewer_id = _allocate_reviewer_id(connection)
    code = _allocate_code(connection)
    connection.execute("INSERT INTO reviewers(reviewer_id) VALUES(?)", (reviewer_id,))
    connection.execute(
        "INSERT INTO reviewer_codes(code, reviewer_id, active) VALUES(?,?,1)",
        (code, reviewer_id),
    )
    connection.executemany(
        "INSERT INTO reviewer_aliases(alias, reviewer_id) VALUES(?,?)",
        [(alias, reviewer_id), (code, reviewer_id)],
    )
    return reviewer_id


def active_code(connection, reviewer_id: str) -> str | None:
    row = connection.execute(
        "SELECT code FROM reviewer_codes WHERE reviewer_id=? AND active=1", (reviewer_id,)
    ).fetchone()
    return row["code"] if row else None


def rotate_code(connection, reference: str) -> dict:
    """为一位审阅者发放新代号。

    ``reference`` 可以是当前代号、历史代号或本地口令；它们都通过别名目录解析。
    旧代号保留在代号历史中，因此历史报告和重复提交仍能解释其归属。
    """
    if not isinstance(reference, str) or not reference.strip():
        raise ValidationError("reviewer code must be a non-empty string")
    reference = reference.strip()
    row = connection.execute(
        "SELECT reviewer_id FROM reviewer_aliases WHERE alias=?", (reference,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"reviewer code {reference!r} is not known in this database")
    reviewer_id = row["reviewer_id"]
    previous = active_code(connection, reviewer_id)
    new_code = _allocate_code(connection)
    connection.execute(
        "UPDATE reviewer_codes SET active=0 WHERE reviewer_id=? AND active=1", (reviewer_id,)
    )
    connection.execute(
        "INSERT INTO reviewer_codes(code, reviewer_id, active) VALUES(?,?,1)",
        (new_code, reviewer_id),
    )
    connection.execute(
        "INSERT INTO reviewer_aliases(alias, reviewer_id) VALUES(?,?)", (new_code, reviewer_id)
    )
    return {"previous_code": previous, "reviewer_code": new_code}
