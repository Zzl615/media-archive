# 路线图

## 实现进度

### 第一阶段：安全索引与精确去重 ✅

- [x] 设备识别（marker file，不依赖挂载路径）
- [x] 媒体文件遍历（跳过 symlink、系统目录）
- [x] quick hash（< 8MB 全量，≥ 8MB 头尾各 4MB）
- [x] full hash（collision 候选）
- [x] 增量扫描（mtime + size 未变则跳过）
- [x] ScanSession 中断恢复（last_scan_session_id）
- [x] 精确重复分组（DuplicateGroup）
- [x] `mard scan` + `mard duplicates --exact`
- [x] Quarantine 隔离区（`mard quarantine`，移到 `.media-archive-quarantine/`）

### 第二阶段：元数据与归档计划 ✅

- [x] 未全量 hash 的大文件补算 content_hash
- [x] 为所有文件创建 MediaAsset
- [x] EXIF 读取（DateTimeOriginal → QuickTime → 文件名 → mtime → ctime）
- [x] 归档路径生成（年/月/时间_原名_hash8.ext）
- [x] 文件名冲突处理
- [x] `mard meta` + `mard archive-plan`

### 第三阶段：执行归档 ✅

- [x] 复制文件到归档目录
- [x] 复制后 hash 校验
- [x] 幂等性（已归档跳过，hash 冲突报告）
- [x] `mard archive-apply --dry-run` / `mard archive-apply`

### 第四阶段：近似重复检测（待实现）

- [ ] 照片感知 hash（pHash/dHash，via imagehash + Pillow）
- [ ] 视频关键帧提取（via ffmpeg-python）
- [ ] 视频关键帧 pHash
- [ ] 生成 DuplicateGroup(likely) 人工确认列表
- [ ] RAW + JPEG 配对识别与标记

### 待完善

- [ ] 归档目录按 Photos/Videos 分子目录
- [ ] 保留文件推荐规则完整实现（当前仅 mtime 最早）
- [ ] scan_time 作为时间识别最终兜底
- [ ] `mard status` 命令：数据库概览统计

---

## MVP 验收标准

- [x] 扫描多块移动硬盘
- [x] 稳定识别同一块硬盘（不依赖挂载路径）
- [x] 跨硬盘发现完全重复文件
- [x] 输出清晰的重复文件报告
- [x] 生成不修改文件的归档计划
- [x] 执行归档并验证完整性

---

## 风险与注意事项

**不要依赖文件名判断重复**
同名文件可能内容不同，尤其是相机、手机、微信和下载目录中的文件。

**不要依赖路径识别设备**
移动硬盘的挂载路径可能变化，macOS 上同一块盘可能挂在不同路径。

**不要直接删除**
跨硬盘去重误删成本极高，删除应放在最后，且只处理 content_hash 完全一致的文件。

**注意元数据时间错误**
照片和视频的文件时间经常被复制、导入、聊天软件转发过程破坏，应记录 `taken_at_source` 以便后续审查。

**inode 在 FAT32/exFAT 上无效**
移动硬盘常用 exFAT 格式，`inode_or_file_id` 为空，不可作为文件唯一性依据。

**符号链接**
扫描时默认跳过 symlink，避免重复计数和扫出系统目录。
