"""HTTP 响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class LoadComputeRequest(BaseModel):
    """负荷核算/重算请求：按发布单读取两份已发布原件。"""

    station_id: str = Field(..., description="站号（两份原件须同站同日）")
    sample_date: str = Field(..., description="采样日期 YYYY-MM-DD（UTC 日历日）")
    flow_order_no: str = Field(..., description="流量 CSV 的发布单号")
    conc_order_no: str = Field(..., description="浓度 CSV 的发布单号")
    expected_id: int | None = Field(
        default=None,
        description="要替换的结果编号；首次提交留空，重算须携带现行编号",
    )

    @field_validator("expected_id")
    @classmethod
    def _expected_id_positive(cls, value):
        if value is not None and value <= 0:
            raise ValueError("expected_id 须为正整数或留空")
        return value


class ReleaseOut(BaseModel):
    id: int = Field(..., description="叶子下标 = id - 1")
    station_id: str
    sample_date: str
    order_no: str
    blob_sha256: str
    size: int
    created_at: str


class PublishResult(BaseModel):
    reused: bool = Field(..., description="True 表示同单同输入重试，复用既有记录")
    release: ReleaseOut
    checkpoint: "CheckpointOut"


class CheckpointOut(BaseModel):
    seq: int = Field(..., description="截至该检查点的发布条数")
    root_hex: str
    created_at: str


class LeafRecordOut(BaseModel):
    v: int
    station_id: str
    sample_date: str
    order_no: str
    blob_sha256: str
    size: int


class InclusionProofOut(BaseModel):
    index: int
    checkpoint_seq: int
    checkpoint_root: str
    tree_depth: int
    leaf_record: LeafRecordOut
    leaf_hash: str
    path: list[dict]


class AppendProofOut(BaseModel):
    old_seq: int
    new_seq: int
    old_root: str
    new_root: str
    tree_depth: int
    entries: list[dict] = Field(
        ..., description="完美子树条目 (start, height, hash)，数量 O(log n)"
    )


class HealthOut(BaseModel):
    status: str
    release_count: int
    problems: list[str]


class HourlyLoadOut(BaseModel):
    hour: int = Field(..., description="整点小时序号 0..23（UTC）")
    window_start: str = Field(..., description="左闭右开窗口起点 HH:MM:SS")
    window_end: str = Field(..., description="左闭右开窗口终点 HH:MM:SS")
    load_low_g: float = Field(..., description="该小时可达到的负荷下界（克）")
    load_high_g: float = Field(..., description="该小时可达到的负荷上界（克）")


class DailyLoadOut(BaseModel):
    load_low_g: float = Field(..., description="全日可达到的负荷下界（克）")
    load_high_g: float = Field(..., description="全日可达到的负荷上界（克）")


class LoadResultOut(BaseModel):
    id: int = Field(..., description="现行结果编号（全表单调递增）")
    station_id: str
    sample_date: str
    flow_order_no: str = Field(..., description="流量原件发布单号")
    conc_order_no: str = Field(..., description="浓度原件发布单号")
    flow_blob_sha256: str = Field(..., description="流量原件字节摘要")
    conc_blob_sha256: str = Field(..., description="浓度原件字节摘要")
    hours: list[HourlyLoadOut] = Field(..., description="24 个整点小时区间")
    day: DailyLoadOut = Field(..., description="全日区间")
    created_at: str


class LoadResultRevisionOut(LoadResultOut):
    is_current: bool = Field(..., description="是否为该站日现行版本")


PublishResult.model_rebuild()
