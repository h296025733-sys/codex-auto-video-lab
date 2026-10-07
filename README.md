# Codex Auto Video Lab

这是我整理的一套本地视频处理流水线。自然语言方案先变成 JSON 剪辑单，校验过后再交给 FFmpeg 渲染，最后检查实际输出的视频和音频。

这样做是为了把“准备怎么剪”和“最终剪出了什么”分开记录。出问题时可以追到具体素材、时间段、参数和报告，不必重新猜整条流程。

## 实现范围

- 隔离任务目录、素材副本和哈希校验。
- 媒体探测、关键画面准备、可选本地语音识别。
- 视频 / 图片拼接、构图、区间与速度控制、字幕和文字包装。
- 分段配音、背景音乐压低、响度处理及音频检查。
- 在已明确许可的小区域内做水印轮廓修补，记录蒙版和区域外保真度。
- 渲染后完整解码，保留执行结果与质量报告。

```text
素材 → 分析证据 → edit-plan.json → 计划校验 → FFmpeg 渲染 → 成片 QA
```

核心代码在 `src/auto_video_lab/`，JSON 合同在 `config/`，项目技能在 `.codex/skills/`。

## 本地准备

使用 Python 3.12，安装项目依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

将 FFmpeg / ffprobe 放到 `tools/ffmpeg/bin/`，按采用的版本更新 `config/toolchain.json`。仓库不打包 Python 环境、模型、工具可执行文件和个人素材。部分高级语音模块还需要自行准备相应第三方模型，见 `THIRD_PARTY_NOTICES.md`。

```powershell
.\studio.ps1 create --job demo-001 --asset "inputs/demo.mp4" --brief "保留原声和镜头顺序，增加英文字幕"
.\studio.ps1 analyze --job demo-001 --transcribe
.\studio.ps1 draft-plan --job demo-001 --title "产品演示" --burn-captions
.\studio.ps1 validate --job demo-001
.\studio.ps1 render --job demo-001
.\studio.ps1 qa --job demo-001
```

水印修补只能估计被遮挡的纹理，当前实现也不能代表任意运动、遮挡、HDR 或编码格式都适用。方案校验、合成样片和真实素材验收分别记录，不能互相替代。

## 本次公开整理的检查

见 [检查记录](docs/verification.md)，其中区分源码与语法检查、隔离测试和未执行的真实环境路径。
