# mard — 媒体归档与去重工具

个人移动硬盘媒体文件索引、去重和归档的命令行工具。

## 核心目标

- 跨多块移动硬盘定位完全重复文件
- 建立可长期维护的媒体索引（SQLite 单文件，随身携带）
- 按拍摄时间自动归档，文件名包含时间和内容指纹
- 所有操作先 dry-run，不自动删除

## 依赖

- Python 3.10+
- [ExifTool](https://exiftool.org/)（`brew install exiftool`）

## 安装

```bash
cd media-archive
pip install -e .
```

## 典型使用流程

```
1. scan          扫描硬盘，建立文件索引，检测精确重复
2. duplicates    查看重复文件报告（可导出）
3. quarantine    将多余副本移到隔离区（可选，dry-run 预览）
4. meta          读取 EXIF，推断拍摄时间，创建媒体资产
5. archive-plan  生成归档计划（只写 JSONL，不动文件）
6. archive-apply 执行归档（复制 + hash 校验）
```

## 命令速查

```bash
# 扫描一块硬盘（首次会在硬盘根目录创建 .media-archive-device.json）
mard scan --device /Volumes/JZAO

# 查看精确重复文件
mard duplicates --exact
mard duplicates --exact --output dup-report.txt   # 导出纯文本报告

# 将重复副本移到隔离区（非 keep 的副本），确认后手动清除
mard quarantine --device /Volumes/JZAO --dry-run  # 预览
mard quarantine --device /Volumes/JZAO            # 执行

# 读取 EXIF 元数据（需要 ExifTool）
mard meta --device /Volumes/JZAO

# 生成归档计划
mard archive-plan --archive /Volumes/Archive --device /Volumes/JZAO

# 预览归档（不复制文件）
mard archive-apply --plan archive-plan.jsonl --device /Volumes/JZAO --dry-run

# 执行归档
mard archive-apply --plan archive-plan.jsonl --device /Volumes/JZAO

# 指定自定义数据库路径（默认 ~/.mard/index.db）
mard scan --device /Volumes/JZAO --db /Volumes/Archive/index.db
```

## 归档目录结构

```
Archive/
  2025/
    06/
      2025-06-06_10-24-38_IMG20250606102438_a1b2c3d4.jpg
    08/
      2025-08-01_09-15-00_VID20250801091500_e5f6a7b8.mp4
  unknown/
    0000-00-00_00-00-00_nodate_12345678.jpg
```

## 项目结构

```
media-archive/
├── README.md
├── pyproject.toml
├── docs/
│   ├── architecture.md    # 架构与存储方案
│   ├── data-models.md     # 数据模型字段说明
│   ├── algorithms.md      # Hash 策略与时间识别
│   ├── archive-guide.md   # 归档结构、去重策略、安全操作
│   └── roadmap.md         # 分阶段实现计划与风险
└── mard/
    ├── cli.py
    ├── scanner.py
    ├── meta.py
    ├── archive.py
    ├── quarantine.py
    ├── hasher.py
    ├── device.py
    └── db/
        ├── models.py
        ├── repository.py
        └── database.py
```
