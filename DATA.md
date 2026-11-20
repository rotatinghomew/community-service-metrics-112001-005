# 分阶段服务措施效果评估：资料说明

用于分析社区服务措施在不同网格分期实施后的变化，包含合成结果和同期因素。

每个 JSON 文件均为 UTF-8 编码。空字符串表示来源没有给出该事实，不等同于零、否或业务终态。标识仅在本项目资料内有意义。

## `grids.json`

字段：id, population, area_type。共 4 条合成记录。

## `rollouts.json`

字段：grid, start_date, intensity, paused_from, paused_to。共 4 条合成记录。

## `outcomes.json`

字段：grid, month, received, timely, matured。共 6 条合成记录。

## `shocks.json`

字段：grid, month, type, intensity。共 3 条合成记录。

## 关系与口径提示

rollouts.grid、outcomes.grid 和 shocks.grid 对应 grids.id；网格启动月份后不能自动视作未实施对照，matured 小于 received 表示仍有结果未成熟。
