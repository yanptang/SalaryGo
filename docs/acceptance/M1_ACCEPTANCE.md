# M1 验收记录

> 验收日期：2026-07-18  
> 运行环境：Apple Silicon Mac，Python 3.13.2  
> 结论：通过

## 范围

本次只验收 `MVP_REQUIREMENTS.md` 中 M1“本地数据与用户档案”，未将持仓、行情、候选池或策略逻辑提前纳入。

## 需求验收

| 正式验收项 | 实现 | 验证结果 |
| --- | --- | --- |
| 能保存和重新加载完整档案 | `ProfileRepository.save/load`；原子 JSON 写入 | 通过 |
| 必填字段缺失时明确报错 | 全量校验返回 `path/code/message/severity` | 通过 |
| 冲突约束能够被识别 | 收益/回撤、流动性、集中度、账户币种等跨字段规则 | 通过 |
| 测试数据和真实数据隔离 | 临时测试目录、`SALARYGO_DATA_DIR`、Git 忽略私有目录 | 通过 |

## 附加安全验收

- 首次保存生成修订号，后续更新递增修订号；旧版本写入会被拒绝。
- 校验失败不会覆盖当前有效档案。
- 保存采用临时文件、刷盘和原子替换，文件权限为 `0600`。
- 备份包含 SHA-256 manifest，恢复时检查摘要。
- 恢复默认拒绝覆盖；即使显式覆盖，也拒绝不同 `profile_id`。
- 未支持的结构版本会明确拒绝，不会猜测字段含义。
- 示例档案不含真实个人信息，真实档案及备份均不会进入 Git。

## 自动化结果

执行命令：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src tests
PYTHONPATH=src python3 -m salarygo validate examples/profile.example.json
git diff --check
git check-ignore -v data/private/profile.json data/backups/example.json
```

结果：23 项测试全部通过；源码可编译；示例档案返回 `valid: true`；Git 忽略规则生效；无空白错误。

## 未进入 M1 的事项

- 当前没有录入用户的真实档案；初始化多轮对话属于后续 Agent 工作流。
- 当前没有图形页面；页面读写档案将在本地页面模块接入。
- 当前没有持仓、行情、评分、资金分配或投资建议。
- M0 的“一条命令启动本地服务”和页面技术验证尚未实施。

