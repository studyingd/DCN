"""密码策略钉子（越权审计批次 3）。

审计报告称"仅校验长度，12345678 被接受"——与当前实现不符（大概率测的
旧版本）。这里把真实口径钉死：≥8 位 + 3 类字符 + 弱口令黑名单。
防止将来被无意放宽成"只查长度"（那正是审计报告描述的行为）。
"""

import pytest

from app.validators import PASSWORD_MIN_LENGTH, validate_password_strength


def test_pure_numeric_password_is_rejected():
    """纯数字只有 1 类字符，必须拒绝（审计报告声称被接受的那条）。"""
    with pytest.raises(ValueError, match="3种"):
        validate_password_strength("12345678")


def test_short_password_is_rejected():
    with pytest.raises(ValueError, match="长度"):
        validate_password_strength("Ab1!" * (PASSWORD_MIN_LENGTH // 4 - 1))


def test_two_category_password_is_rejected():
    with pytest.raises(ValueError, match="3种"):
        validate_password_strength("abcdefgh1")


def test_compliant_password_is_accepted():
    assert validate_password_strength("Xk9$mVq2pLr7") == "Xk9$mVq2pLr7"


@pytest.mark.parametrize(
    "weak",
    [
        "Pass-1234",  # 越权审计测试账号的口令：3 类字符但烂大街
        "Admin@123",
        "Qwer1234",
        "P@ssw0rd1",
        "Root@123",
        "pass-1234",  # 大小写不敏感
        "ADMIN@123",
    ],
)
def test_blacklisted_passwords_are_rejected(weak):
    """过了复杂度校验的常见弱口令也必须拒绝。"""
    assert weak.lower() in {w.lower() for w in {weak}}  # 形态自检
    with pytest.raises(ValueError, match="过于常见"):
        validate_password_strength(weak)


def test_blacklist_does_not_block_similar_but_strong_password():
    """黑名单是精确匹配，不是前缀/包含匹配：普通强口令不能误伤。"""
    assert validate_password_strength("Pass-1234xQm!") == "Pass-1234xQm!"
