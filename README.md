# 云学帮自动刷题脚本 (DeepSeek + 微信小程序)

通过 **DeepSeek 网页版** 自动回答云学帮小程序上的题目，**无需 API Key**。

## 工作原理

云学帮是微信小程序，没有网页版和独立 APP。脚本通过以下方式实现自动化：

```
电脑                                   安卓模拟器/手机
├── 浏览器 (DeepSeek 网页版)           ├── 微信
│   ├── 发送题目                       │   ├── 云学帮小程序
│   └── 获取答案                       │   └── 答题界面
└── uiautomator2 远程控制 ─────────────┘
```

- **电脑端**：Playwright 驱动浏览器打开 DeepSeek 网页版，发送题目获取答案
- **手机端**：uiautomator2 控制微信中的云学帮小程序，提取题目、选择答案
- **OCR 回退**：微信小程序渲染在 WebView 中，`dump_hierarchy()` 无法获取内容时自动回退到 OCR 截图识别

## 快速开始

### 1. 环境准备

```bash
# Python 3.8+
python --version

# 安装依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器（国内镜像加速）
export PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
playwright install chromium
```

### 2. 安装 OCR 库（重要！）

微信小程序的内容渲染在 WebView 中，uiautomator2 的 `dump_hierarchy()` **无法直接读取** WebView 内容。
脚本会在无障碍树提取失败时自动回退到 **OCR 截图识别**。

三选一安装（推荐 RapidOCR，轻量且无需 PaddlePaddle/Torch）：

```bash
# 推荐：RapidOCR（轻量级，基于 ONNX Runtime）
pip install rapidocr-onnxruntime

# 或：PaddleOCR（最准确，但较重）
pip install paddlepaddle paddleocr

# 或：EasyOCR（需要 PyTorch）
pip install easyocr
```

### 3. 准备安卓模拟器

推荐使用以下模拟器之一：
- **雷电模拟器**（推荐）：默认端口 5555
- **MuMu 模拟器**：默认端口 7555
- **夜神模拟器**：默认端口 62001
- **真机**：开启 USB 调试后用数据线连接

在模拟器中安装微信并登录。

```bash
# 验证设备连接
adb devices
# 输出示例：
# List of devices attached
# emulator-5554    device
```

### 4. 运行

```bash
python main.py
```

### 使用流程

```
脚本启动
  |
  v
电脑浏览器打开 DeepSeek → 用户手动登录
  |
  v
模拟器自动启动微信
  |
  v
用户在微信中打开云学帮小程序，进入答题页面
  |
  v
终端按回车开始自动答题
  |
  v
+---> 从小程序界面提取题目
|       ├── 策略1: 无障碍树 (dump_hierarchy)
|       └── 策略2: OCR 截图识别（WebView 回退）
|       |
|       v
|   DeepSeek 获取答案
|       |
|       v
|   在小程序中选择答案
|       ├── 策略1: 无障碍树定位点击
|       ├── 策略2: OCR 定位点击
|       └── 策略3: 坐标比例点击
|       |
|       v
|   下一题（循环）
+-------+
  |
  v
答题完成 → 确认提交 → 输出统计
```

## 常用参数

| 参数 | 说明 |
|---|---|
| `--thinking` | 开启 DeepSeek 深度思考模式（更准确但更慢） |
| `--auto-submit` | 自动提交，不等待确认 |
| `--device SERIAL` | 指定设备序列号（如 `emulator-5554`） |
| `--auto-open` | 自动在微信中搜索并打开云学帮小程序 |
| `--mp-name NAME` | 指定小程序名称（默认"云学帮"） |
| `--delay SECONDS` | 每题之间的延迟秒数 |
| `--inspect` | 检查屏幕 UI 结构 + OCR 识别结果（调试用） |
| `--debug` | 开启调试日志 |
| `--ds-url URL` | 指定 DeepSeek 网页地址 |

## 项目结构

```
Yunxuebang-AutoAnswer/
├── main.py                  # 主程序入口
├── config.py                # 配置模块
├── models.py                # 共享数据结构 (Question/AnswerResult)
├── deepseek_web_client.py   # DeepSeek 网页版客户端 (Playwright)
├── wechat_automator.py      # 微信小程序自动化 (uiautomator2 + OCR)
├── requirements.txt         # Python 依赖
└── README.md                # 说明文档
```

## 常见模拟器连接方式

| 模拟器 | adb 连接命令 |
|---|---|
| 雷电模拟器 | `adb connect 127.0.0.1:5555` |
| MuMu 模拟器 | `adb connect 127.0.0.1:7555` |
| 夜神模拟器 | `adb connect 127.0.0.1:62001` |
| 逍遥模拟器 | `adb connect 127.0.0.1:21503` |

连接后用 `python main.py --device emulator-5554` 指定设备。

## 调试

### 屏幕检查模式

题目识别不出来时，用检查模式查看小程序的 UI 结构和 OCR 识别结果：

```bash
python main.py --inspect
```

该模式会同时输出：
- 无障碍树原始节点（标注哪些被过滤为噪音）
- 过滤后的有效节点
- OCR 识别结果（文本 + 置信度 + 坐标）
- 自动解析的题目和选项

### 开启调试日志

```bash
python main.py --debug
```

日志同时输出到控制台和 `yunxuebang_auto.log` 文件。

## 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `DEEPSEEK_URL` | DeepSeek 网页地址 | `https://chat.deepseek.com/` |
| `DEEPSEEK_THINKING` | 是否开启深度思考 | `false` |
| `MINI_PROGRAM_NAME` | 小程序名称 | `云学帮` |
| `ANDROID_DEVICE` | 设备序列号 | 自动选择 |
| `DEBUG` | 调试模式 | `false` |

## 注意事项

- **无需任何 API Key**，直接使用 DeepSeek 网页版
- **必须安装 OCR 库**（RapidOCR/PaddleOCR/EasyOCR 三选一），否则无法识别 WebView 中的题目内容
- 需要在模拟器中安装微信并登录
- 首次运行需要手动登录 DeepSeek 和微信
- 默认不自动提交试卷，需按回车确认
- DeepSeek 网页版有频率限制，如遇限流请增加 `--delay`
- 仅供参考学习使用，请遵守平台使用规范
