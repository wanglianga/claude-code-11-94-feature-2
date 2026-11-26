class BusinessRuleError(Exception):
    """业务规则违反（409）"""

    def __init__(self, message: str, code: str = "business_rule_violation"):
        self.message = message
        self.code = code
        super().__init__(message)


class NotFoundError(Exception):
    def __init__(self, message: str = "资源不存在"):
        self.message = message
        super().__init__(message)


class AuthError(Exception):
    def __init__(self, message: str = "鉴权失败"):
        self.message = message
        super().__init__(message)
