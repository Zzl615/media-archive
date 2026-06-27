# 算法：Hash 策略与时间识别

## Hash 策略

### 第一阶段：quick hash（扫描时计算）

```
file_size < 8MB  →  对整个文件计算 hash
                    quick_hash == content_hash，跳过第二阶段
file_size ≥ 8MB  →  对前 4MB + 后 4MB 计算 hash
                    quick_hash ≠ content_hash（内容未完全确认）
```

用途：快速筛选疑似重复候选，避免对几十 GB 视频全量哈希。

### 第二阶段：content hash（collision 时计算）

条件：`size 相同 AND quick_hash 相同` 的文件组

对组内所有文件计算完整 hash（读取整个文件），确认是否真正重复。

算法优先级：BLAKE3（速度快）→ SHA256（兜底，当 blake3 包不可用时）。

### 第三阶段（`mard meta` 时）：fullhash remaining

对有 quick_hash 但无 content_hash 的文件（即无 collision 的大文件）补算完整 hash。

这一步是归档的前提：content_hash 用于命名文件（短 hash 前 8 字符）。

### 第四阶段：感知 hash（待实现）

- 照片：pHash / dHash / aHash（via imagehash + Pillow）
- 视频：抽取关键帧 → 对帧计算 pHash（via ffmpeg-python）
- 结合 duration、resolution 判断近似重复

字段 `MediaAsset.perceptual_hash` 已预留。

---

## 时间识别规则

拍摄时间是归档路径的核心，多个来源按以下优先级取用：

| 优先级 | 来源 | 字段值 | 可靠性 |
|---|---|---|---|
| 1 | EXIF `DateTimeOriginal` | `exif` | 最高，相机直接写入 |
| 2 | QuickTime `CreateDate` / `TrackCreateDate` / `MediaCreateDate` | `quicktime` | 视频通用 |
| 3 | 文件名中的时间戳 | `filename` | 较高，如 `IMG20250510190339.jpg` |
| 4 | 文件修改时间（mtime）| `mtime` | 中等，`cp -p`/`rsync` 可保留 |
| 5 | 文件创建时间（ctime）| `ctime` | 较低，复制时会被更新 |
| 6 | 扫描时间 | `scan_time` | 最低，仅作兜底 |

结果存入 `MediaAsset.taken_at` + `MediaAsset.taken_at_source`，后续可筛选"哪些文件的时间不可信"。

### 格式支持说明

- **JPEG / RAW**：读取 EXIF `DateTimeOriginal`
- **MP4 / MOV**：读取 QuickTime `creation_time`（注意部分文件使用 UTC，ExifTool 会处理时区）
- **HEIC / HEIF**（iPhone 默认格式）：使用 EXIF，ExifTool 13+ 支持

### 文件名时间解析

支持常见手机/相机命名格式：

```
IMG20250510190339.jpg     → 2025-05-10 19:03:39
VID_20250801_091500.mp4   → 2025-08-01 09:15:00
20230615T143022.heic      → 2023-06-15 14:30:22
```

正则：`(\d{4})(\d{2})(\d{2})[_T-]?(\d{2})(\d{2})(\d{2})`
