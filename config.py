"""
云学帮自动刷题脚本 - 配置模块
使用 DeepSeek 网页版获取答案，无需 API Key
"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DeepSeekWebConfig:
    """DeepSeek 网页版配置"""
    # DeepSeek 网页地址
    url: str = "https://chat.deepseek.com/"
    # 是否使用无头模式（必须 False，需要手动登录）
    headless: bool = False
    # 是否开启深度思考模式
    use_deep_thinking: bool = False
    # 是否通过 Enter 键发送消息
    send_with_enter: bool = True
    # 等待回复超时时间（秒）
    response_timeout: int = 120
    # 题目之间的间隔（秒），避免过快请求
    question_interval: float = 2.0
    # 视口宽度
    viewport_width: int = 900
    # 视口高度
    viewport_height: int = 800


@dataclass
class WebAutomationConfig:
    """云学帮 Web 端自动化配置（Playwright）"""
    # 云学帮平台网址
    platform_url: str = "https://www.yunxuebang.com"
    # 是否使用无头模式（建议 False，方便手动登录）
    headless: bool = False
    # 浏览器类型: chromium / firefox / webkit
    browser_type: str = "chromium"
    # 视口宽度
    viewport_width: int = 1280
    # 视口高度
    viewport_height: int = 800
    # 用户数据目录（保持登录状态，可选）
    user_data_dir: Optional[str] = None
    # 每道题之间的延迟（秒），避免过快被检测
    question_delay: float = 2.0
    # 是否在提交前等待用户确认
    confirm_before_submit: bool = True


@dataclass
class AndroidAutomationConfig:
    """云学帮 Android 端自动化配置（uiautomator2）"""
    # 设备序列号（通过 adb devices 查看，留空则自动选择第一个设备）
    device_serial: str = ""
    # 云学帮包名
    app_package: str = "com.huanYu.js.yunxuebang"
    # 启动 Activity
    app_activity: str = ""
    # 每道题之间的延迟（秒）
    question_delay: float = 2.0
    # 是否在提交前等待用户确认
    confirm_before_submit: bool = True
    # UI 操作等待超时（秒）
    ui_timeout: float = 10.0


@dataclass
class AppConfig:
    """全局配置"""
    deepseek: DeepSeekWebConfig = field(default_factory=DeepSeekWebConfig)
    web: WebAutomationConfig = field(default_factory=WebAutomationConfig)
    android: AndroidAutomationConfig = field(default_factory=AndroidAutomationConfig)
    # 运行模式: web / android
    mode: str = "web"
    # 是否开启调试日志
    debug: bool = False
    # 日志文件路径
    log_file: str = "yunxuebang_auto.log"
    # 是否自动提交（False 则只选题不提交）
    auto_submit: bool = False
    # 答题正确率统计输出文件
    stats_file: str = "answer_stats.json"


def get_default_config() -> AppConfig:
    """获取默认配置"""
    return AppConfig()


def load_config_from_env() -> AppConfig:
    """从环境变量加载配置（覆盖默认值）"""
    config = get_default_config()

    # DeepSeek URL
    env_ds_url = os.getenv("DEEPSEEK_URL")
    if env_ds_url:
        config.deepseek.url = env_ds_url

    # 深度思考
    env_thinking = os.getenv("DEEPSEEK_THINKING")
    if env_thinking:
        config.deepseek.use_deep_thinking = env_thinking.lower() == "true"

    # 云学帮平台 URL
    env_url = os.getenv("YUNXUEBANG_URL")
    if env_url:
        config.web.platform_url = env_url

    # 运行模式
    env_mode = os.getenv("AUTO_MODE")
    if env_mode:
        config.mode = env_mode

    # 调试模式
    config.debug = os.getenv("DEBUG", "false").lower() == "true"

    return config
