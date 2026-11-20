# 分阶段服务措施效果评估：资料说明

用于分析社区服务措施在不同网格分期实施后的变化，包含合成结果和同期因素。

每个 JSON 文件均为 UTF-8 编码。空字符串表示来源没有给出该事实，不等同于零、否或业务终态。标识仅在本项目资料内有意义。

## `grids.json`

字段：id, population, area_type。共 4 条合成记录。

## `rollouts.json`

字段：grid, start_date, intensity, paused_from, paused_to。共 4 条合成记录。
可选字段 `end_date`（退出日期）与 `intensity_segments`（[{from,to,intensity}]，
启动后强度变化）可缺省；缺省表示来源没有给出退出或强度变化。

## `outcomes.json`

字段：grid, month, received, timely, matured。共 6 条合成记录。

## `shocks.json`

字段：grid, month, type, intensity。共 3 条合成记录。

## 关系与口径提示

rollouts.grid、outcomes.grid 和 shocks.grid 对应 grids.id；网格启动月份后不能自动视作未实施对照，matured 小于 received 表示仍有结果未成熟。

- 空 `start_date` 表示尚未启动，此时 `intensity` 必须为 `none`；空字符串不是“否”“零”或终态。
- `paused_from`/`paused_to` 必须同时给出或同时为空，且不早于启动日期。
- `matured` 小于 `received` 时，差额是仍在办理、尚未成熟的事项，不计入分母，也不计为失败。
- `timely` 大于 `matured`（或 `matured` 大于 `received`）属于计数口径矛盾：该网格月份不进入估计，
  保留在覆盖表中并列入“资料核查”，不会被修正为其他数值。
- 缺失的网格月份表示没有观测，不补零。
- 每张表的字段名固定；出现未知字段会被拒绝，以免拼写错误被静默忽略。
