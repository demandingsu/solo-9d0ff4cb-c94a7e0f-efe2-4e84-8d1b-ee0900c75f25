"""领域异常。"""
from __future__ import annotations


class DomainError(Exception):
    """业务校验失败（缺字、文件格式错、资源不存在等），HTTP 400/404。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class MissingGlyphError(DomainError):
    """存在没有任何字库收录的字，无法交印。"""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(f"缺字（无任何字库收录）: {''.join(missing)}", 422)


class SaveError(DomainError):
    """保存失败：事务回滚，各册仍沿用原清单。"""

    def __init__(self, message: str = "保存失败，已回滚，仍沿用原清单"):
        super().__init__(message, 500)


class ClearedIdempotencyKeyError(DomainError):
    """该幂等键对应的交印单已被清空，不能再次出单或改动共需清单。"""

    def __init__(self, idem_key: str):
        super().__init__(
            f"该笔交印已清空（幂等键 {idem_key}），不能重复出单，共需清单未改动",
            409,
        )


class OrderNotFoundError(DomainError):
    """单号不存在，或该单所属数据已被清空。"""

    def __init__(self, order_no: str):
        super().__init__(f"单据不存在：{order_no}（可能已被清空）", 404)


class ClearFailureError(SaveError):
    """清空失败：整笔回滚，原单仍可查询和复用。"""

    def __init__(self, message: str = "清空失败，已回滚，原单仍可查询和复用"):
        super().__init__(message)
