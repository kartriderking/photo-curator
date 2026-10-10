# 轻相册 · photo-curator

一个在 Windows 本机运行的照片整理工具：读取拍摄时间和 GPS、查找重复与模糊照片、整理旅行素材，也可以按需启用本地 AI 分类和景点识别。

首页只需要 **选文件夹 → 选整理目的 → 开始整理**。结果以缩略图展示，支持分类筛选、放大、并排对比、导出清单和人工确认移入回收站。

## 安装

当前桌面工作台主要面向 **Windows 10 / 11、Python 3.12**。

下载本仓库后，在项目目录双击 setup.bat，然后双击 start_web.bat。也可以手动安装：

    py -3.12 -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt
    start_web.bat

浏览器打开 [本地工作台](http://127.0.0.1:8618)。

启动脚本会自动打开浏览器；如果工作台已经运行，则打开现有页面，不重复启动任务。缺少环境、端口被其他程序占用或启动失败时，窗口会保留错误信息。首次启动后的服务窗口请保持打开，按 Ctrl+C 停止。

仓库不包含安装包、第三方可执行文件、模型权重或真实照片。详见 **[模型与外部工具安装说明](docs/models.md)**：

- **ExifTool**：读取 GPS / 拍摄时间。Windows 便携版放到 tools/exiftool.exe，保留随附支持目录；也可通过 PATH 或 PHOTO_CURATOR_EXIFTOOL 指定。
- **FFmpeg**：视频抽帧和浏览器播放转换。安装后加入 PATH。
- **OpenCLIP / PyTorch**：仅内容分类与近似查找需要。权重默认下载到 models/open_clip/，也可手动下载并指定路径。
- **Ollama**：仅景点识别需要。用户自行安装服务和下载 qwen3-vl:8b，模型位置可配置为 models/ollama/。

**仅 pHash / 模糊筛选不需要下载 AI 权重，也不需要启动 Ollama。**

## 网页操作

1. 点击「选择文件夹」，也可粘贴路径。
2. 选择「清理重复和模糊」「按时间和地点整理」或「识别照片内容」。
3. 需要单项功能时，展开「自定义功能与高级设置」，例如只选 GPS 或 pHash。
4. 「我的照片」查看缩略图、筛选、搜索、放大和对比建议保留项。
5. 「刷新」只读取已有结果，不触发重新分析；「导出清单」下载 CSV。
6. 「整理记录」可再次整理同一文件夹，或继续中断的任务。
7. 「检查实况照片」独立选择文件夹，用 ExifTool 读取 UUID，检查 Apple Live Photo 是否缺少照片或视频，不需要先运行整理任务。

默认不包含视频。仅视频 GPS 不抽帧；视频内容分析和独立抽帧才调用 FFmpeg。

离线城市查询通常返回拼音或英文；联网中文地名需要在高级设置中选择，会把 GPS 坐标发送到 Nominatim。

pHash 相近、CLIP 内容相似和模糊分数都只是整理建议。时间分组也不保证每一组都是旅行。程序不会自动删除原图；移入回收站前需要人工选择并确认。

实况检查按照片 Apple MakerNotes 与视频 QuickTime Keys 中的 ContentIdentifier 配对，支持改名和不同子文件夹；不按文件名猜测配对。[Apple 关联标识说明](https://developer.apple.com/documentation/avfoundation/avcapturephotosettings/livephotomoviemetadata)、[ExifTool Apple 标签](https://exiftool.org/TagNames/Apple.html)、[ExifTool QuickTime 标签](https://exiftool.org/TagNames/QuickTime.html)。

缺失配对表示当前扫描范围内没有另一半，对方也可能在未扫描的文件夹中；建议包含子文件夹。没有 UUID 的普通照片、标识冲突或元数据读取失败会单独显示，不能直接作为补删依据。移入回收站前会复核扫描范围的文件身份；内容发生变化时必须重新检查。读取过的 UUID 元数据按文件身份缓存到 SQLite。

## 命令行

用真实照片目录替换示例路径。默认只执行重复与模糊筛选：

    .venv\Scripts\python scripts\run_all.py --input-dir "C:\Pictures"

独立运行：

    .venv\Scripts\python scripts\run_all.py --input-dir "C:\Pictures" --features gps --include-videos
    .venv\Scripts\python scripts\run_all.py --input-dir "C:\Pictures" --features phash
    .venv\Scripts\python scripts\run_all.py --input-dir "C:\Pictures" --features places,trips
    .venv\Scripts\python scripts\run_all.py --input-dir "C:\Pictures" --features places --geo-mode online
    .venv\Scripts\python scripts\run_all.py --input-dir "C:\Pictures" --features similar
    .venv\Scripts\python scripts\run_all.py --input-dir "C:\Pictures" --features landmarks --vlm-scope all
    .venv\Scripts\python scripts\run_all.py --input-dir "C:\Pictures" --features frames

完整分析或只导出已有结果：

    .venv\Scripts\python scripts\run_all.py --input-dir "C:\Pictures" --preset deep --include-videos
    .venv\Scripts\python scripts\run_all.py --input-dir "C:\Pictures" --export-only

功能名：gps、places、phash、quality、similar、classify、landmarks、trips、frames。

--steps 兼容步骤号：1 元数据，2 抽帧，3 pHash / 清晰度，4 CLIP 分类 / 相似，5 景点，6 导出。独立执行也把部分结果保存到 SQLite；只有 --export-only 或 --steps 6 才生成 CSV。

--force 重算所选功能，一般无需开启。高级参数包括 --dup-hamming、--similar-threshold、--scenic-threshold、--trip-gap-hours、--vlm-model。

## 数据目录与隐私

- scripts/：程序与前端；tests/：临时合成测试；docs/：安装说明。
- models/：用户下载的权重；tools/：用户安装的外部工具，均不提交 Git。
- cache/curator.sqlite3：统一数据库，保存任务、进度、快照、结果、回收记录和增量分析缓存。缓存按绝对路径、文件大小、纳秒修改时间和算法配置区分记录。
- output/jobs/：网页任务的诊断日志；output/manual/：命令行任务标识目录，可通过 --job-dir 指定。正常分析不生成 JSON / CSV 文件。
- 网页导出：直接从 SQLite 生成 CSV 下载，不在任务目录额外保存报告。命令行 --export-only 把 CSV 保存到对应任务目录。
- cache/frames/：默认视频帧目录，可通过 PHOTO_CURATOR_FRAMES_DIR 改变。

数据目录首次使用时自动创建。数据库使用 WAL 和短事务，让前端读取与后台写入同时进行；快照与整批结果在同一事务提交，中断时保留之前的完整结果。

从旧版升级时，启动工作台会一次性导入 jobs.json、任务进度/快照、已有 CSV 和回收日志，不覆盖已经写入 SQLite 的更新记录，也不删除旧文件。建议升级前备份这些文件及数据库。旧分析缓存仍按文件身份验证后导入；历史景点的模型信息不完整时会注明来源。

仓库排除了照片/视频、GPS 数据、报告、任务索引、日志、数据库、虚拟环境、模型权重和个人配置。不要强制添加这些内容到 Git。

本地 AI 分析不上传照片。首次模型下载和联网地名查询需要网络。工作台仅监听 127.0.0.1，不应直接暴露到互联网。

## 测试

    .venv\Scripts\python -B -m unittest discover -s tests -v

测试图片在临时目录合成，不包含真实图库。GPS 测试需要 ExifTool；视频与 AI 调用使用替身。覆盖缓存复用、跨库隔离、修改失效、部分失败、纯导出和分组逻辑。

## 第三方依赖

第三方工具、模型与权重使用各自许可证；项目不重新分发它们。仓库未额外指定项目开源许可证。
