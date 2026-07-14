"""
云学帮自动刷题脚本 - 配置模块
云学帮为微信小程序，通过安卓模拟器运行微信 + uiautomator2 自动化操作
DeepSeek 网页版获取答案，无需 API Key
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
class WeChatMiniProgramConfig:
    """微信小程序自动化配置（uiautomator2）"""
    # 设备序列号（通过 adb devices 查看，留空则自动选择第一个设备）
    device_serial: str = ""
    # 微信包名
    wechat_package: str = "com.tencent.mm"
    # 云学帮小程序名称（用于在微信中搜索）
    mini_program_name: str = "云学帮"
    # 每道题之间的延迟（秒）
    question_delay: float = 2.0
    # 是否在提交前等待用户确认
    confirm_before_submit: bool = True
    # 是否自动提交（False 则只选题不提交）
    auto_submit: bool = False
    # UI 操作等待超时（秒）
    ui_timeout: float = 10.0
    # 是否自动打开小程序（False 则等待用户手动打开）
    auto_open_mini_program: bool = False


@dataclass
class AppConfig:
    """全局配置"""
    deepseek: DeepSeekWebConfig = field(default_factory=DeepSeekWebConfig)
    wechat: WeChatMiniProgramConfig = field(default_factory=WeChatMiniProgramConfig)
    # 是否开启调试日志
    debug: bool = False
    # 日志文件路径
    log_file: str = "yunxuebang_auto.log"
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

    # 小程序名称
    env_mp_name = os.getenv("MINI_PROGRAM_NAME")
    if env_mp_name:
        config.wechat.mini_program_name = env_mp_name

    # 设备序列号
    env_device = os.getenv("ANDROID_DEVICE")
    if env_device:
        config.wechat.device_serial = env_device

    # 调试模式
    config.debug = os.getenv("DEBUG", "false").lower() == "true"

    return config
