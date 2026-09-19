"""API 请求模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TextIn(BaseModel):
    text: str = Field(default="", description="批量录入文本")


class ConfigIn(BaseModel):
    transfer_minutes: int = Field(gt=0, description="换乘有效间隔（分钟）")
    transfer_discount: float = Field(ge=0, description="换乘抵扣额（元）")
    daily_cap: float = Field(ge=0, description="每卡每运营日封顶额（元）")
    day_split_hour: int = Field(ge=0, le=23, description="运营日分界时刻")


class TrialConfigIn(BaseModel):
    """规则试算对比的草案参数。

    不在此处做数值约束：非法草案应走 core.validate_config，
    返回与正式核验一致的 CONFIG_BAD 定位错误（HTTP 422）。
    """
    transfer_minutes: float = Field(description="草案：换乘有效间隔（分钟）")
    transfer_discount: float = Field(description="草案：换乘抵扣额（元）")
    daily_cap: float = Field(description="草案：每卡每运营日封顶额（元）")
    day_split_hour: float = Field(description="草案：运营日分界时刻（0-23）")


class PeakAuditIn(BaseModel):
    """站点闸机峰值审计参数。

    不在此处做数值约束：非法参数应走核心层校验，
    返回与其他接口一致的 CONFIG_BAD 定位错误（HTTP 422）。
    """
    window_minutes: float = Field(
        description="峰值统计窗口长度（分钟），取 1～120 的整数")
    min_alert: float = Field(
        default=1,
        description="最低告警数；峰值刷卡数达到该值的组才标记告警")


class MergePreviewIn(BaseModel):
    """外部刷卡批次合并预览参数。

    待导入记录文本与保存记录共用 parse_records_text 解析（解析错误按
    原有定位错误返回）；容差不在此处约束，非法值走核心层校验，
    返回与其他接口一致的 CONFIG_BAD 定位错误（HTTP 422）。
    """
    text: str = Field(default="", description="待导入刷卡记录文本")
    tolerance_seconds: float = Field(
        description="重复判定时间容差（秒），取 0～120 的整数")
