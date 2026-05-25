# astrbot_plugin_proactive_action

AstrBot 附属插件：监测**所有出站消息**（含 [astrbot_plugin_proactive_chat](https://github.com/DBJD-CR/astrbot_plugin_proactive_chat) 发出的主动消息），自动识别动作指令并执行对应工具，实现更丰富的多模态对话体验。

## 功能

- 识别 LLM 回复或主动消息中内嵌的图片指令，如 `（发来一张午后阳光洒在琴键上的特写）`，自动调用 [astrbot_plugin_aiimg_enhanced](https://github.com/Justice-ocr/astrbot_plugin_aiimg_enhanced) 生图并将图片附加到消息中
- 通过 `OnDecoratingResultEvent` 钩子拦截所有出站消息，**不修改任何已有插件**
- 可选：扫描用户发来的消息（`scan_incoming=true`），识别并执行动作
- 可选：LLM 意图分类慢通道，对复杂表达做兜底识别
- 可选：动态扫描全部已注册 LLM 工具并注入分类 Prompt（实验性）

## 依赖

- AstrBot
- [astrbot_plugin_aiimg_enhanced](https://github.com/Justice-ocr/astrbot_plugin_aiimg_enhanced)（生图功能需要）

## 安装

将插件目录放到 `data/plugins/astrbot_plugin_proactive_action/`，在 AstrBot 管理界面启用即可。

## 配置项

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `enable` | `true` | 是否启用 |
| `classifier_provider_id` | `""` | 意图分类 LLM Provider，留空用默认 |
| `enable_image_generation` | `true` | 是否启用生图动作 |
| `image_generation_mode` | `"before"` | 图文顺序：`before`=先图后文，`after`=先文后图 |
| `scan_incoming` | `false` | 是否扫描用户来消息 |
| `action_pattern` | `""` | 自定义动作匹配正则（留空用内置规则） |
| `tool_scan_enabled` | `false` | 动态工具扫描（实验性） |

## 扩展

在 `executors/` 下添加新执行器，在 `core/dispatcher.py` 的 `_process()` 里增加分支，在 `core/classifier.py` 的 `ACTION_MAP` 里加映射，即可接入搜索、天气等任意工具。

## 架构

```
OnDecoratingResultEvent (所有出站消息)
        ↓
  IntentClassifier
  ├── 规则快速通道（正则，零延迟）
  └── LLM 慢通道（可选，用于复杂表达）
        ↓
  ActionDispatcher
  └── ImageExecutor → aiimg_enhanced.draw.generate()
        ↓
  修改消息链（注入图片组件，清理括号指令）
```
