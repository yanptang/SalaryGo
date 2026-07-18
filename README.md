# SalaryGo

SalaryGo 是一个本地运行的个人投资决策助手。M0–M10 的 MVP 模块均已实现：用户档案、持仓账本、市场数据刷新、候选产品筛选、指数与个股评分、资金分配、Agent 工作流、本地页面和独立回测。

## 启动本地页面

```bash
python3 run_salarygo.py
```

然后访问 `http://127.0.0.1:8765`。服务只监听本机地址，不开放局域网或公网。

## M1 快速开始

项目只依赖 Python 3.11+ 标准库。以下命令不会写入真实数据目录：

```bash
PYTHONPATH=src python3 -m salarygo validate examples/profile.example.json
```

保存为本机真实档案：

```bash
PYTHONPATH=src python3 -m salarygo save examples/profile.example.json
PYTHONPATH=src python3 -m salarygo show
```

真实数据默认写入 `data/private/`，备份写入 `data/backups/`，两者均被 Git 忽略。可以用环境变量把数据放到其他目录：

```bash
SALARYGO_DATA_DIR=/path/to/private-data PYTHONPATH=src python3 -m salarygo show
```

备份与恢复：

```bash
PYTHONPATH=src python3 -m salarygo backup
PYTHONPATH=src python3 -m salarygo restore data/backups/profile-r1-<hash>.json --replace
```

运行 M1–M5 自动化验收：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

模块设计见 `docs/`，逐模块验收记录见 `docs/acceptance/`。当前自动刷新按钮使用醒目标记的离线验收数据；真实数据可以通过手动录入或实现统一 provider 接口接入，未配置正式数据源时系统不得把测试数据当成真实建议依据。本工具不自动下单，收益目标也不是收益承诺。

Codex 可以先验证本地工具：

```bash
PYTHONPATH=src python3 -m salarygo health
```

