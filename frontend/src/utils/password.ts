/**
 * 密码强度校验——与后端 `app/validators.py::validate_password_strength` 同口径。
 *
 * 为什么前端也要有一份：后端对不合规密码返回 422，而用户表单此前只做「非空」
 * 校验，弱密码一路提交到服务端才被拒，界面上只弹一句笼统的「创建用户失败」，
 * 用户完全不知道该改什么。把规则前移，错误直接标在密码输入框下面。
 *
 * 两份实现必须保持一致，改一边就要改另一边（后端是最终裁判）。
 */

export const PASSWORD_MIN_LENGTH = 8

const CATEGORY_PATTERNS: RegExp[] = [/[a-z]/, /[A-Z]/, /\d/, /[!@#$%^&*()\-_=+[\]{}|;:,.<>?]/]

/** 返回不合规原因；合规时返回空字符串。 */
export function passwordStrengthError(password: string): string {
  if (!password || password.length < PASSWORD_MIN_LENGTH) {
    return `密码长度不能少于${PASSWORD_MIN_LENGTH}个字符`
  }
  const matched = CATEGORY_PATTERNS.filter((pattern) => pattern.test(password)).length
  if (matched < 3) {
    return '密码必须包含大写字母、小写字母、数字、特殊字符中的至少3种'
  }
  return ''
}

export function isStrongPassword(password: string): boolean {
  return passwordStrengthError(password) === ''
}
