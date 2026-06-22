# 多移动硬盘媒体归档与去重设计文档

## 目标

设计一个本地媒体文件索引、归档和去重系统，用于管理多个移动硬盘中的照片、视频和其他媒体文件。

核心目标：

- 跨多个移动硬盘定位重复文件。
- 建立可长期维护的媒体文件索引。
- 支持按时间、来源、设备、路径查询文件。
- 支持安全的归档计划生成。
- 避免误删，所有删除或移动操作都应先经过 dry-run 或隔离区。

非目标：

- 第一版不做自动删除。
- 第一版不做复杂的人脸、场景、语义识别。
- 第一版不依赖云服务。

## 总体思路

系统分为三层：

1. 扫描索引层
   - 识别移动硬盘。
   - 遍历媒体文件。
   - 记录文件路径、大小、时间、扩展名和设备信息。

2. 指纹识别层
   - 计算快速 hash。
   - 对疑似重复文件计算完整 hash。
   - 后续可扩展照片和视频的感知 hash。

3. 归档决策层
   - 根据元数据生成归档路径。
   - 识别重复文件组。
   - 推荐保留文件。
   - 输出 dry-run 归档计划。

## 存储方案

可以使用 LiteDB 作为第一版数据库。

LiteDB 适合：

- 单机个人工具。
- 数据库文件易于复制和备份。
- 与 C#/.NET 项目集成简单。
- 文件规模在几十万到几百万以内。

为了避免未来被 LiteDB 绑定，建议业务代码不要直接散落调用 LiteDB API，而是封装 Repository 层。后续如果需要迁移到 SQLite，只替换存储实现。

### 必要索引

几十万文件规模下，以下字段必须建立索引，否则查询将退化为全表扫描：

| 集合 | 索引字段 | 原因 |
|---|---|---|
| FileInstance | `device_id` | 按设备筛选文件 |
| FileInstance | `quick_hash` | 快速筛选重复候选 |
| FileInstance | `content_hash` | 精确去重查询 |
| FileInstance | `last_scan_session_id` | 增量扫描进度追踪 |
| FileInstance | `exists` | 过滤已消失文件 |
| MediaAsset | `content_hash` | 唯一资产查找 |
| MediaAsset | `taken_at` | 按时间归档查询 |
| MediaAsset | `archive_status` | 过滤待归档资产 |

LiteDB 中使用 `EnsureIndex` 或在实体上标注 `[BsonIndex]` 实现。

## 数据模型

### StorageDevice

表示一块移动硬盘或存储设备。

字段建议：

```text
id
volume_label
filesystem_uuid
device_marker_id
mount_hint
first_seen_at
last_seen_at
notes
```

说明：

- 不要依赖盘符或挂载路径识别设备。
- macOS、Windows、Linux 上挂载路径可能变化。
- 建议在每块硬盘根目录创建一个隐藏标记文件。

示例：

```json
{
  "device_id": "uuid",
  "name": "JZAO",
  "created_at": "2026-06-16T00:00:00Z"
}
```

标记文件名建议：

```text
.media-archive-device.json
```

### FileInstance

表示某个文件在某块设备上的一次物理存在。

字段建议：

```text
id
device_id
path
file_name
extension
size
mtime
ctime
inode_or_file_id
quick_hash
content_hash
media_asset_id
scan_at
last_scan_session_id
exists
```

说明：

- 一个真实媒体文件可能在多个设备、多个路径下存在。
- `FileInstance` 记录的是位置，不代表逻辑媒体资产。
- `exists` 用于记录历史扫描中存在、后续扫描中已消失的文件。
- `inode_or_file_id`：FAT32 / exFAT 格式的移动硬盘没有 inode，此字段在这类设备上为空，不可作为唯一性依据，仅作辅助参考。
- `last_scan_session_id` 关联 `ScanSession`，用于判断文件是否在本次扫描中被访问到，支持增量扫描和中断恢复。

### ScanSession

表示一次扫描任务，用于支持中断恢复和增量扫描。

字段建议：

```text
id
device_id
started_at
finished_at
status
last_scanned_path
total_files
processed_files
```

`status` 可选：

```text
running
completed
interrupted
```

说明：

- 每次执行 `scan` 命令时新建一个 `ScanSession`，记录本次扫描的设备和进度。
- `FileInstance.scan_at` 记录的是该实例最近一次被扫描到的时间；结合 `ScanSession.id`（可在 FileInstance 增加 `last_scan_session_id` 字段），可以区分"上次扫描过"和"本次扫描到"。
- 中断恢复时，读取上次 `interrupted` 的 session，从 `last_scanned_path` 断点续扫，跳过已在本次 session 中记录的文件。
- `finished_at` 为空表示未完成，可用于检测异常中断。

### MediaAsset

表示一个逻辑媒体资产。

字段建议：

```text
id
content_hash
size
media_type
taken_at
taken_at_source
duration
width
height
camera_model
gps_lat
gps_lng
perceptual_hash
best_instance_id
archive_status
```

说明：

- 多个 `FileInstance` 可以指向同一个 `MediaAsset`。
- 完全相同的文件应共享同一个 `content_hash`。
- 近似相同的文件可以通过 `perceptual_hash` 或重复组关联。

### DuplicateGroup

表示一组重复或疑似重复文件。

字段建议：

```text
id
duplicate_type
instance_ids
recommended_keep_instance_id
confidence
reason
created_at
review_status
```

`duplicate_type` 可选：

```text
exact
likely
similar
```

说明：

- `exact` 表示完整 hash 一致，内容完全相同。此类型由 `content_hash` 唯一确定，所有实例共享同一个 `MediaAsset`，因此只需 `instance_ids` 即可，不需要额外的 `asset_ids`。
- `likely` 表示元数据和感知指纹高度接近，可能跨不同 `MediaAsset`，需人工确认。
- `similar` 表示文件名、时间、大小接近，仅作提示，不参与自动流程。

`recommended_keep_instance_id` 替代原先的 `recommended_keep_id`，明确语义为推荐保留的物理实例，避免与 `asset_id` 混淆。原始设计中同时存储 `instance_ids` 和 `asset_ids` 会产生冗余，exact 类型的一致性维护成本较高，此处统一以 instance 为主键。

## Hash 策略

### 第一阶段：快速指纹

扫描所有媒体文件时先计算 quick hash。

quick hash 可以由以下信息组成：

```text
文件大小
文件头部若干 MB 的 hash
文件尾部若干 MB 的 hash
```

建议：

```text
前 4MB + 后 4MB
```

**小文件边界处理：**

文件大小 < 8MB 时，前后分段会重叠，退化为对整个文件计算 hash，与 full hash 等价。此时可以直接将 quick hash 和 content_hash 设为同一值，跳过第二阶段单独计算。

阈值建议：

```text
file_size < 8MB → hash 整个文件，quick_hash == content_hash
file_size >= 8MB → hash 前 4MB + 后 4MB
```

用途：

- 快速筛选疑似重复文件。
- 避免对所有大视频直接计算完整 hash。

### 第二阶段：完整 hash

只有当文件满足以下条件时，才计算完整 hash：

```text
size 相同
quick_hash 相同
```

完整 hash 可选：

- SHA256：通用性好。
- BLAKE3：速度快，适合大文件。

如果工具主要用 .NET 实现，可以优先考虑 BLAKE3；如果强调跨语言和长期兼容，SHA256 也足够。

### 第三阶段：感知 hash

用于检测近似重复，不建议第一版实现。

照片：

- pHash
- dHash
- aHash

视频：

- 抽取关键帧。
- 对关键帧计算 pHash。
- 结合 duration、resolution、codec、bitrate 判断。

## 时间识别规则

媒体归档最重要的字段是拍摄时间。

建议优先级：

1. EXIF 或 QuickTime metadata 中的拍摄时间。
2. 文件名中的时间，例如 `IMG20250510190339.jpg`。
3. 文件修改时间（mtime）。
4. 文件创建时间（ctime）。
5. 扫描时间。

**关于 ctime 与 mtime 的顺序说明：**

ctime 在多数场景下比 mtime 更不可靠：将文件从一块硬盘复制到另一块硬盘时，ctime 会被更新为复制时刻，而 `cp -p` / `rsync` 默认会保留 mtime。微信、QQ 等聊天软件保存的文件两者都会破坏，不应依赖。因此 mtime 优先于 ctime。

格式支持说明：

- JPEG / RAW 通常读取 EXIF `DateTimeOriginal`。
- MP4 / MOV 通常读取 QuickTime `creation_time`（注意该字段有时使用 UTC，需与时区结合处理）。
- HEIC / HEIF（iPhone 默认格式）使用 EXIF，但需要专门的解析库支持，常见库如 `MetadataExtractor`（.NET）可覆盖。

数据库中应记录：

```text
taken_at
taken_at_source
```

`taken_at_source` 示例：

```text
exif
quicktime
filename
mtime
ctime
scan_time
unknown
```

这样后续可以知道每个归档时间的可信来源。

## 归档目录结构

推荐按媒体类型和时间归档。

示例：

```text
Archive/
  Photos/
    2025/
      2025-06/
        2025-06-06_10-24-38_IMG20250606102438_a1b2c3d4.jpg
  Videos/
    2025/
      2025-06/
        2025-06-06_10-24-38_VID20250606102438_a1b2c3d4.mp4
  UnknownDate/
    Photos/
    Videos/
```

文件命名建议：

```text
拍摄时间_原文件名片段_短hash.扩展名
```

示例：

```text
2025-06-06_10-24-38_VID20250606102438_a1b2c3d4.mp4
```

**短 hash 定义：**

短 hash 取 `content_hash`（SHA256 或 BLAKE3）的前 8 个十六进制字符。若 content_hash 尚未计算，则退而使用 quick_hash 前 8 字符，并在 dry-run 计划中标注为待确认。

这样可以同时保证：

- 人能读懂。
- 文件名冲突概率低。
- 未来脱离数据库也能基本识别内容。

## 去重策略

### 完全重复

判断条件：

```text
size 相同
content_hash 相同
```

处理建议：

- 自动分组。
- 自动推荐保留文件。
- 可以进入归档计划。
- 不直接删除源文件。

### 高度疑似重复

判断条件示例：

```text
照片 perceptual_hash 接近
视频 duration 接近
视频关键帧 hash 接近
分辨率或码率不同
```

处理建议：

- 生成候选重复组。
- 标记置信度。
- 需要人工确认。

### 弱疑似重复

判断条件示例：

```text
文件名相似
拍摄时间接近
文件大小接近
```

处理建议：

- 只做提示。
- 不参与自动删除或自动移动。

### RAW + JPEG 配对

现代相机和部分手机（如 iPhone ProRAW）会同时生成 RAW 文件（NEF / CR2 / ARW / DNG）和 JPEG，二者 content_hash 不同，但逻辑上来自同一次拍摄。

判断条件：

```text
文件名去除扩展名后相同（如 IMG_1234.ARW 与 IMG_1234.JPG）
拍摄时间一致或相差 < 2 秒
来源目录相同
```

处理建议：

- 识别为配对关系，在 `MediaAsset` 或单独的 `RawJpegPair` 关联表中记录。
- 归档时两者均保留，不将 JPEG 视为 RAW 的重复副本。
- 第一版可以只做识别和标记，不强制配对归档，人工确认后再操作。

## 保留文件推荐规则

重复组中推荐保留文件时，可以按以下优先级排序：

1. 已在正式归档目录中的文件。
2. 完整 hash 已验证的文件。
3. 分辨率更高的照片或视频。
4. 文件大小更大的原始版本。
5. 来源路径更可信，例如 `DCIM/Camera`。
6. 修改时间更早的版本。
7. 文件名更接近相机原始命名的版本。

推荐结果只作为建议，不直接删除其他副本。

## 安全操作原则

所有改变文件系统的操作都应分阶段执行。

### Dry-run

先输出计划，不修改文件。

内容包括：

```text
源文件
目标文件
操作类型
是否重复
是否冲突
推荐原因
```

### Copy and verify

归档时先复制，不移动。

复制后必须重新计算目标文件 hash，并与源文件 hash 对比。

只有校验通过，才标记归档成功。

### Quarantine

重复文件不直接删除。

可以先移动到隔离区：

```text
.media-archive-quarantine/
```

隔离区保留一段时间后，再由用户手动删除。

### archive-apply 幂等性

`archive-apply` 可能被中断后重新执行，必须保证幂等：

- 执行前检查 `MediaAsset.archive_status`，状态为 `archived` 的资产跳过复制。
- 目标路径已存在同名文件时，比对 hash：
  - hash 一致 → 认为已归档成功，更新状态，跳过复制。
  - hash 不一致 → 报告冲突，不覆盖，等待人工确认。
- 复制成功并 hash 校验通过后，才将 `archive_status` 更新为 `archived`，保证状态与文件实际存在一致。

## 命令设计建议

第一版可以做成命令行工具。

示例命令：

```text
scan --device /Volumes/JZAO
scan --device /Volumes/DISK2
duplicates --exact
archive-plan --target /Volumes/Archive
archive-apply --dry-run
archive-apply
```

推荐第一版只实现：

```text
scan
duplicates --exact
archive-plan --dry-run
```

不要第一版就实现自动删除。

## 分阶段实现

### 第一阶段：安全索引和完全重复检测

目标：

- 扫描设备。
- 识别设备 ID。
- 记录文件路径、大小、时间。
- 计算 quick hash。
- 对疑似重复文件计算 full hash。
- 输出完全重复报告。

不做：

- 自动归档。
- 自动删除。
- 近似重复检测。

### 第二阶段：归档计划

目标：

- 读取 EXIF 和视频元数据。
- 推断拍摄时间。
- 生成目标归档路径。
- 处理文件名冲突。
- 输出 dry-run 计划。

### 第三阶段：执行归档

目标：

- 复制文件到归档盘。
- 复制后 hash 校验。
- 标记源文件归档状态。
- 支持重复文件进入隔离区。

### 第四阶段：近似重复检测

目标：

- 照片 pHash。
- 视频关键帧 pHash。
- 生成人工确认列表。
- 支持相似文件组管理。

## 推荐 MVP

推荐最小可用版本：

```text
scan --device <path>
duplicates --exact
archive-plan --target <path> --dry-run
```

MVP 成功标准：

- 能扫描多个移动硬盘。
- 能稳定识别同一块硬盘。
- 能跨硬盘发现完全重复文件。
- 能输出清晰的重复文件报告。
- 能生成不会修改文件的归档计划。

## 风险和注意事项

### 不要依赖文件名判断重复

同名文件可能内容不同，尤其是相机、手机、微信和下载目录中的文件。

### 不要依赖路径识别设备

移动硬盘的挂载路径可能变化。

### 不要直接删除

跨硬盘去重误删成本很高。删除应放在最后，并且只处理完整 hash 一致的文件。

### 注意元数据时间错误

照片和视频的文件时间经常被复制、导入、聊天软件转发过程破坏。应记录时间来源。

### 符号链接处理

扫描时默认不跟随符号链接（symlink）：

- 跟随 symlink 可能导致重复计数（同一文件被多条链接索引多次）。
- 部分系统目录通过 symlink 暴露，跟随可能扫出无关内容。

建议：扫描时检测 symlink，记录到日志但跳过不处理。如有需要，可通过 `--follow-symlinks` 参数显式开启。

### 注意隐藏系统目录

扫描时默认跳过：

```text
.Trashes
.Spotlight-V100
.fseventsd
System Volume Information
$RECYCLE.BIN
```

这些目录通常不是用户主动归档的媒体来源。

## 总结

这个系统的关键不是单纯计算 hash，而是把文件位置、逻辑媒体资产、存储设备、重复关系和归档决策拆开。

第一版应优先保证安全、可追溯和可验证：

- 先扫描建库。
- 再检测完全重复。
- 再生成 dry-run 归档计划。
- 最后才考虑复制、移动和删除。

LiteDB 可以作为第一版数据库，但应通过 Repository 层隔离，避免未来迁移成本过高。
