# SalaryGo

SalaryGo 是一个本地运行的个人投资决策助手。当前已实现 M1：本地用户档案、确定性校验、约束冲突识别、版本管理、备份与恢复。

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

运行 M1 自动化验收：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

完整字段与约束见 [docs/M1_PROFILE.md](docs/M1_PROFILE.md)。本工具不自动下单，收益目标也不是收益承诺。

