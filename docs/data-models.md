# 数据模型

## StorageDevice

表示一块移动硬盘或存储设备。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | String (UUID) | 主键 |
| volume_label | String | 卷标（来自挂载路径名） |
| filesystem_uuid | String | 文件系统 UUID（辅助参考） |
| device_marker_id | String | 标记文件中的 UUID，唯一标识此盘 |
| mount_hint | String | 最近一次挂载路径，仅供参考 |
| first_seen_at | DateTime | 首次扫描时间 |
| last_seen_at | DateTime | 最近一次扫描时间 |
| notes | String | 备注 |

## FileInstance

表示某个文件在某块设备上的一次物理存在。同一内容可能对应多个 FileInstance（不同盘、不同路径）。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | String (UUID) | 主键 |
| device_id | FK → StorageDevice | 所在设备 |
| path | String | 相对于设备根目录的路径（正斜杠） |
| file_name | String | 文件名（含扩展名） |
| extension | String | 小写扩展名 |
| size | Integer | 字节数 |
| mtime | DateTime | 文件修改时间 |
| ctime | DateTime | 文件创建/状态变更时间 |
| inode_or_file_id | String | inode 号；FAT32/exFAT 盘为空，不可作为唯一依据 |
| quick_hash | String | 快速指纹（< 8MB 时等于 content_hash）|
| content_hash | String | 完整 BLAKE3/SHA256 哈希 |
| media_asset_id | FK → MediaAsset | 关联的逻辑资产（可为空）|
| scan_at | DateTime | 最近一次被扫描到的时间 |
| last_scan_session_id | FK → ScanSession | 最近一次扫描会话 ID |
| exists | Boolean | False 表示文件已消失 |

唯一约束：`(device_id, path)`

## ScanSession

表示一次扫描任务，支持中断恢复和增量扫描。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | String (UUID) | 主键 |
| device_id | FK → StorageDevice | 扫描的设备 |
| started_at | DateTime | 开始时间 |
| finished_at | DateTime | 完成时间；为空表示未完成 |
| status | Enum | `running` / `completed` / `interrupted` |
| last_scanned_path | String | 最近处理的路径，用于断点续扫 |
| total_files | Integer | 本次发现的媒体文件总数 |
| processed_files | Integer | 已处理数量 |

每次执行 `mard scan` 时新建一个 ScanSession。`FileInstance.last_scan_session_id` 区分"本次扫描到"和"历史记录"，用于标记消失文件。

## MediaAsset

表示一个逻辑媒体资产（内容唯一）。多个 FileInstance 可指向同一个 MediaAsset。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | String (UUID) | 主键 |
| content_hash | String | 完整 hash，唯一索引 |
| size | Integer | 字节数 |
| media_type | String | `photo` / `video` / `other` |
| taken_at | DateTime | 拍摄时间（可为空）|
| taken_at_source | String | 时间来源（见时间识别规则）|
| duration | Float | 视频时长（秒）|
| width | Integer | 宽（像素）|
| height | Integer | 高（像素）|
| camera_model | String | 相机/手机型号 |
| gps_lat | Float | GPS 纬度 |
| gps_lng | Float | GPS 经度 |
| perceptual_hash | String | 感知 hash（第四阶段）|
| best_instance_id | String | 推荐归档来源的 FileInstance ID |
| archive_status | Enum | `pending` / `archived` / `skipped` |

## DuplicateGroup

表示一组重复或疑似重复文件。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | String (UUID) | 主键 |
| duplicate_type | Enum | `exact` / `likely` / `similar` |
| confidence | Float | 置信度（1.0 = 确定重复）|
| reason | String | 判断依据描述 |
| created_at | DateTime | 创建时间 |
| review_status | Enum | `pending` / `reviewed` / `resolved` |
| recommended_keep_instance_id | String | 推荐保留的 FileInstance ID |

- `exact`：content_hash 完全一致，由系统自动分组
- `likely`：感知指纹接近，需人工确认（第四阶段）
- `similar`：文件名/时间/大小接近，仅作提示

成员关系通过 `DuplicateGroupMember(group_id, instance_id)` 关联表存储。
