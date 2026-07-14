# 云学帮自动刷题脚本 (DeepSeek 网页版驱动)

通过 **DeepSeek 网页版** 自动回答云学帮平台上的题目，**无需 API Key**，直接使用网页版对话。

## 功能特性

- **无需 API Key**：直接使用 DeepSeek 网页版 (chat.deepseek.com)，不产生任何 API 费用
- **双模式支持**：
  - `web` 模式：同一浏览器中 DeepSeek + 云学帮 双标签页自动化
  - `android` 模式：手机操作云学帮 APP + 电脑浏览器操作 DeepSeek
- **多题型支持**：单选题、多选题、判断题、填空题
- **智能识别**：自动识别页面上的题目和选项，支持多种常见前端框架
- **深度思考**：可选开启 DeepSeek 深度思考模式，提高答题准确率
- **安全机制**：默认不自动提交，需人工确认后才提交
- **详细日志**：记录每道题的题目、答案、解析，并输出统计报告

## 快速开始

### 1. 环境准备

```bash
# Python 3.8+
python --version

# 安装依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器
playwright install chromium
```

### 2. 运行（无需任何 API Key 配置）

```bash
python main.py
```

脚本会自动打开浏览器并创建两个标签页：
- 标签页 1：DeepSeek 网页版（需手动登录）
- 标签页 2：云学帮平台（需手动登录并进入答题页面）

### 3. 使用流程

```
脚本启动
  |
  v
浏览器打开 DeepSeek (chat.deepseek.com)
  |
  v
用户手动登录 DeepSeek（手机号/邮箱/微信扫码）
  |
  v
浏览器打开云学帮平台
  |
  v
用户手动登录云学帮，进入答题/考试页面
  |
  v
终端按回车开始自动答题
  |
  v
+---> 从云学帮页面提取题目
|       |
|       v
|   切换到 DeepSeek 标签页，发送题目
|       |
|       v
|   等待 DeepSeek 生成回复
|       |
|       v
|   提取答案并解析
|       |
|       v
|   切回云学帮标签页，选择答案
|       |
|       v
|   下一题（循环）
+-------+
  |
  v
答题完成 -> 确认提交 -> 输出统计
```

## 常用参数

| 参数 | 说明 |
|---|---|
| `--mode web/android` | 运行模式 |
| `--thinking` | 开启 DeepSeek 深度思考模式（更准确但更慢） |
| `--url URL` | 指定云学帮平台 URL |
| `--ds-url URL` | 指定 DeepSeek 网页地址 |
| `--auto-submit` | 自动提交，不等待确认 |
| `--inspect` | 检查页面/屏幕结构（调试用） |
| `--headless` | 无头模式（不推荐，需手动登录） |
| `--delay SECONDS` | 每题之间的延迟秒数 |
| `--debug` | 开启调试日志 |

## 项目结构

```
yunxuebang_auto/
├── main.py                  # 主程序入口
├── config.py                # 配置模块
├── models.py                # 共享数据结构 (Question/AnswerResult)
├── deepseek_web_client.py   # DeepSeek 网页版客户端 (Playwright)
├── web_automator.py         # 云学帮 Web 端自动化 (Playwright)
├── android_automator.py     # 云学帮 Android 端自动化 (uiautomator2)
├── requirements.txt         # Python 依赖
└── README.md                # 说明文档
```

## 两种模式详解

### Web 模式（默认）

同一浏览器中运行两个标签页：

```
浏览器
├── 标签页 1: chat.deepseek.com  (DeepSeek 网页版)
└── 标签页 2: 云学帮平台          (答题页面)
```

- 脚本自动在两个标签页之间切换
- 从云学帮提取题目 → 切到 DeepSeek 发送 → 等待回复 → 切回云学帮选题

```bash
python main.py --mode web
```

### Android 模式

电脑和手机协同工作：

```
电脑                          手机
├── 浏览器 (DeepSeek)         ├── 云学帮 APP
└── 脚本控制                   └── uiautomator2 控制
```

- 电脑端浏览器操作 DeepSeek 网页版获取答案
- 手机端通过 uiautomator2 操作云学帮 APP

```bash
# 确保 adb 已连接设备
adb devices

python main.py --mode android
```

## DeepSeek 深度思考

开启深度思考模式可获得更准确的答案（但响应更慢）：

```bash
python main.py --thinking
```

或通过环境变量：

```bash
export DEEPSEEK_THINKING=true
```

## 调试

### 页面检查模式

```bash
python main.py --inspect
```

输出页面上匹配到的元素及选择器，帮助定位题目识别问题。

### 开启调试日志

```bash
python main.py --debug
```

日志同时输出到控制台和 `yunxuebang_auto.log` 文件。

### 答题统计

每次运行后，答题统计保存到 `answer_stats.json`，包含每道题的题目、答案、解析和成功/失败状态。

## 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `DEEPSEEK_URL` | DeepSeek 网页地址 | `https://chat.deepseek.com/` |
| `DEEPSEEK_THINKING` | 是否开启深度思考 | `false` |
| `YUNXUEBANG_URL` | 云学帮平台 URL | `https://www.yunxuebang.com` |
| `AUTO_MODE` | 运行模式 | `web` |
| `DEBUG` | 调试模式 | `false` |

## 注意事项

- **无需任何 API Key**，直接使用 DeepSeek 网页版
- 首次运行需要手动在浏览器中登录 DeepSeek 和云学帮
- 建议设置合理的延迟（`--delay`），避免过快被平台检测
- 默认不自动提交试卷，需在终端按回车确认后才提交
- DeepSeek 网页版有使用频率限制，如遇限流请增加延迟
- 仅供参考学习使用，请遵守平台使用规范
