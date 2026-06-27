# 架构与存储方案

## 三层架构

```
┌─────────────────────────────────────────┐
│  扫描索引层                              │
│  识别设备 → 遍历媒体文件 → 记录路径/时间/大小 │
└─────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────┐
│  指纹识别层                              │
│  quick hash → full hash → 感知 hash（第四阶段）│
└─────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────┐
│  归档决策层                              │
│  EXIF 时间 → 目标路径 → 重复分组 → 干跑计划  │
└─────────────────────────────────────────┘
```

## 存储方案

使用 **SQLite**，通过 SQLAlchemy ORM 访问，业务代码经由 Repository 层隔离，不直接调用 ORM API。

优点：
- 单文件数据库，可随归档盘一起备份
- 标准 SQL，复杂去重查询（`GROUP BY content_hash HAVING COUNT > 1`）自然表达
- 几十万行 + 索引性能足够
- 可用 DB Browser for SQLite 直接查看调试

WAL 模式已开启，支持扫描中读取而不阻塞。

## 必要索引

| 表 | 索引字段 | 用途 |
|---|---|---|
| FileInstance | `device_id` | 按设备筛选文件 |
| FileInstance | `quick_hash` | 快速筛选重复候选 |
| FileInstance | `content_hash` | 精确去重查询 |
| FileInstance | `last_scan_session_id` | 增量扫描进度追踪 |
| FileInstance | `exists` | 过滤已消失文件 |
| MediaAsset | `content_hash` | 唯一资产查找 |
| MediaAsset | `taken_at` | 按时间归档查询 |
| MediaAsset | `archive_status` | 过滤待归档资产 |

## 设备识别

不依赖挂载路径或盘符（两者都可能变化）。首次扫描时在硬盘根目录写入标记文件：

```
.media-archive-device.json
```

```json
{
  "device_id": "uuid",
  "name": "JZAO",
  "created_at": "2025-06-06T00:00:00Z"
}
```

后续扫描读取此文件获取稳定的 `device_marker_id`，与数据库中的 `StorageDevice` 对应。
