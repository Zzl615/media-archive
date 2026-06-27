# 归档指南

## 归档目录结构

按年/月组织，未知日期归入 `unknown/`：

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

### 文件命名规则

```
{taken_at}_{原文件名去扩展名}_{短hash}{扩展名}
```

示例：`2025-06-06_10-24-38_IMG20250606102438_a1b2c3d4.jpg`

**短 hash**：取 `content_hash`（BLAKE3/SHA256）前 8 个十六进制字符。若 content_hash 尚未计算，退而使用 `quick_hash` 前 8 字符（在 plan 中标注）。

命名同时保证：人能读懂 + 冲突概率低 + 脱离数据库也能基本识别内容。

---

## 去重策略

### 精确重复

```
size 相同 AND content_hash 相同
```

- 自动分组为 `DuplicateGroup(exact)`
- 自动推荐保留文件（见下方规则）
- 归档时只复制推荐保留的一份
- 不直接删除其他副本

### 高度疑似重复（第四阶段）

```
perceptual_hash 接近（照片）
关键帧 pHash 接近 + duration 相近（视频）
```

- 生成 `DuplicateGroup(likely)`，标注置信度
- 需人工确认后处理

### 弱疑似重复（第四阶段）

```
文件名相似 + 拍摄时间接近 + 大小接近
```

- 生成 `DuplicateGroup(similar)`，仅作提示
- 不参与自动归档流程

### RAW + JPEG 配对

现代相机和 iPhone ProRAW 会同时生成 RAW（NEF/CR2/ARW/DNG）和 JPEG，内容 hash 不同但逻辑上来自同一次拍摄。

判断条件：
```
去除扩展名后文件名相同（如 IMG_1234.ARW 与 IMG_1234.JPG）
拍摄时间一致或相差 < 2 秒
来源目录相同
```

处理原则：归档时两者均保留，不将 JPEG 视为 RAW 的重复副本。

---

## 保留文件推荐规则

重复组中按以下优先级推荐保留哪一份（当前实现：仅第 6 条）：

| 优先级 | 规则 |
|---|---|
| 1 | 已在正式归档目录中的文件 |
| 2 | content_hash 已验证的文件 |
| 3 | 分辨率更高的版本 |
| 4 | 文件大小更大的原始版本 |
| 5 | 来源路径更可信（如 `DCIM/Camera`）|
| 6 | mtime 更早的版本 ← 当前实现 |
| 7 | 文件名更接近相机原始命名 |

推荐结果只作建议，不自动删除其他副本。

---

## 安全操作原则

### archive-plan.jsonl 字段说明

每行是一个 JSON 对象，包含以下字段：

| 字段 | 说明 |
|---|---|
| `asset_id` | MediaAsset UUID |
| `instance_id` | 被选中归档的 FileInstance UUID |
| `device_id` | 来源设备 UUID |
| `device_label` | 来源设备可读名称（volume_label） |
| `source_path` | 源文件相对路径（相对设备挂载点） |
| `target_path` | 归档目标绝对路径 |
| `content_hash` | 内容 hash（apply 时校验用） |
| `size` | 字节数 |
| `taken_at` | 拍摄时间（ISO 格式，或 null） |
| `date_source` | 时间来源：`exif` / `filename` / `mtime` / `ctime` / null |
| `media_type` | `photo` / `video` / `other` |
| `width` / `height` | 分辨率（若 EXIF 有记录） |
| `duplicate_copies` | 该文件在所有设备上的副本总数 |
| `duplicate_devices` | 持有副本的设备名称列表 |
| `skip` | `true` 则 archive-apply 跳过此条（不更新状态） |
| `name_collision` | `true` 表示目标路径发生冲突，已自动在文件名追加短 ID |

**AI/人工审核流程**：

```bash
mard archive-plan --archive ~/Archive > archive-plan.jsonl
# 用 AI 或脚本标记 "skip": true 后：
mard archive-apply --plan archive-plan.jsonl --device /Volumes/JZAO
```

`date_source` 为 `mtime` 或 null 的条目日期不可靠，目录分类仅供参考。`duplicate_copies > 1` 表示有其他备份，可优先处理。

---

### Dry-run 优先

`mard archive-plan` 只生成 JSONL 计划文件，不修改任何文件。人工检查计划后再执行 `archive-apply`。

`mard archive-apply --dry-run` 模拟复制，输出统计但不写入磁盘。

### Copy and Verify

归档时先复制，不移动：

```
copy(source → target)
actual_hash = compute_hash(target)
assert actual_hash == expected_hash   # 校验失败则删除目标并报错
mark archive_status = archived
```

hash 校验通过后才更新状态，保证状态与文件实际存在一致。

### Quarantine 隔离区

重复文件不直接删除，先移动到设备上的隔离目录：

```
<device-root>/.media-archive-quarantine/<原相对路径>
```

每个重复组保留 `recommended_keep` 那份，其余副本移入隔离区，原目录结构保留。

```bash
mard quarantine --device /Volumes/JZAO --dry-run  # 预览，不移动
mard quarantine --device /Volumes/JZAO            # 执行
```

安全保证：
- 若 keep 文件本身不存在，整组跳过
- 若某盘只有一份文件（`exists` 副本为唯一），跳过
- 移动后 `FileInstance.exists` 标为 `False`，不影响 `archive-plan` 选源

确认隔离区内容无误后手动删除目录即可。执行后建议重新 `mard scan` 更新索引。

### archive-apply 幂等性

中断后重新执行安全：

| 情况 | 处理 |
|---|---|
| 目标不存在 | 正常复制 |
| 目标已存在 + hash 一致 | 跳过，标记 archived |
| 目标已存在 + hash 不一致 | 报告冲突，不覆盖，等待人工确认 |
| asset 已标记 archived | 跳过 |
