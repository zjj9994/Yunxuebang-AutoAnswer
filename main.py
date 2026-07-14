#!/usr/bin/env python3
"""
云学帮自动刷题脚本 - 主程序
云学帮为微信小程序，通过安卓模拟器运行微信操作
DeepSeek 网页版获取答案，无需 API Key

用法:
  python main.py                     # 启动（自动连接设备 + 打开 DeepSeek）
  python main.py --thinking          # 开启 DeepSeek 深度思考
  python main.py --inspect           # 屏幕检查模式（调试用）
  python main.py --auto-submit       # 自动提交（不等待确认）
  python main.py --device SERIAL     # 指定设备序列号
  python main.py --debug             # 开启调试日志
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import AppConfig, load_config_from_env


def setup_logging(config: AppConfig):
    """配置日志"""
    level = logging.DEBUG if config.debug else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%H:%M:%S"

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(fmt, datefmt))

    file_handler = logging.FileHandler(config.log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(fmt, datefmt))

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers.clear()
    root_logger.addHandler(console)
    root_logger.addHandler(file_handler)


def print_banner():
    """打印启动横幅"""
    banner = """
+----------------------------------------------------------+
|       云学帮自动刷题脚本 (DeepSeek + 微信小程序)           |
|                                                          |
|  AI 引擎: DeepSeek 网页版 (chat.deepseek.com)             |
|  平台: 云学帮 (微信小程序)                                 |
|  引擎: Playwright (DeepSeek) + uiautomator2 (微信)       |
|  OCR 回退: RapidOCR / PaddleOCR / EasyOCR                |
|  特点: 无需 API Key，直接使用网页版对话                    |
+----------------------------------------------------------+
"""
    print(banner)


def print_results_summary(results: list, stats_file: str):
    """打印答题结果摘要并保存统计"""
    total = len(results)
    success = sum(1 for r in results if r.success)
    failed = total - success

    print(f"\n{'='*60}")
    print(f"答题完成！共 {total} 题，成功 {success} 题，失败 {failed} 题")
    print(f"{'='*60}\n")

    for r in results:
        status = "OK" if r.success else "FAIL"
        letters = "".join(r.answer_letters) if r.answer_letters else "N/A"
        print(f"  [{status}] 第{r.question.index + 1}题: {letters} | {r.question.text[:40]}...")

    stats = {
        "timestamp": datetime.now().isoformat(),
        "total": total,
        "success": success,
        "failed": failed,
        "details": [
            {
                "index": r.question.index,
                "type": r.question.question_type,
                "question": r.question.text[:200],
                "answer": "".join(r.answer_letters),
                "reasoning": r.reasoning[:200],
                "success": r.success,
                "error": r.error,
            }
            for r in results
        ],
    }
    try:
        with open(stats_file, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        print(f"\n统计已保存到: {stats_file}")
    except Exception as e:
        print(f"保存统计失败: {e}")


async def run(config: AppConfig, args):
    """主运行流程"""
    from wechat_automator import WeChatMiniProgramAutomator

    automator = WeChatMiniProgramAutomator(config.wechat, config.deepseek)

    try:
        # 1. 连接设备 + 初始化 DeepSeek 浏览器
        await automator.start()

        # 2. 用户登录 DeepSeek
        logger = logging.getLogger(__name__)
        logger.info("正在打开 DeepSeek 网页...")
        await automator.init_deepseek_login()

        # 3. 启动微信
        await automator.open_wechat()

        # 4. 打开云学帮小程序（或等待用户手动打开）
        if config.wechat.auto_open_mini_program:
            await automator.open_mini_program()

        # 5. 等待用户在微信中进入答题页面
        await automator.wait_for_user_ready(
            "请在微信中打开云学帮小程序，进入答题/考试页面"
        )

        # 检查模式
        if args.inspect:
            await automator.inspect_screen()
            await automator.wait_for_user_ready("检查完成")
            return

        # 6. 执行自动答题
        results = await automator.run_auto_answer()

        if results:
            print_results_summary(results, config.stats_file)

    finally:
        await automator.close()


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="云学帮自动刷题脚本 - DeepSeek 网页版 + 微信小程序（无需 API Key）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s                          启动自动答题
  %(prog)s --thinking               开启 DeepSeek 深度思考模式
  %(prog)s --inspect                检查屏幕结构（调试用）
  %(prog)s --auto-submit            自动提交，不等待确认
  %(prog)s --device emulator-5554   指定设备序列号
  %(prog)s --delay 3.0              每题延迟 3 秒
  %(prog)s --auto-open              自动搜索并打开云学帮小程序
  %(prog)s --debug                  开启调试日志

环境变量:
  DEEPSEEK_URL        DeepSeek 网页地址（默认 https://chat.deepseek.com/）
  DEEPSEEK_THINKING   是否开启深度思考 true/false
  MINI_PROGRAM_NAME   小程序名称（默认 云学帮）
  ANDROID_DEVICE      设备序列号
  DEBUG               调试模式 true/false
""",
    )
    parser.add_argument(
        "--thinking", action="store_true",
        help="开启 DeepSeek 深度思考模式（更准确但更慢）",
    )
    parser.add_argument(
        "--inspect", action="store_true",
        help="检查模式：输出屏幕 UI 结构，用于调试",
    )
    parser.add_argument(
        "--auto-submit", action="store_true",
        help="自动提交试卷，不等待用户确认",
    )
    parser.add_argument(
        "--device", help="Android 设备序列号（通过 adb devices 查看）",
    )
    parser.add_argument("--debug", action="store_true", help="开启调试日志")
    parser.add_argument(
        "--delay", type=float, default=None,
        help="每道题之间的延迟秒数（默认 2.0）",
    )
    parser.add_argument(
        "--ds-url", default=None,
        help="DeepSeek 网页地址（默认 https://chat.deepseek.com/）",
    )
    parser.add_argument(
        "--mp-name", default=None,
        help="小程序名称（默认 云学帮）",
    )
    parser.add_argument(
        "--auto-open", action="store_true",
        help="自动在微信中搜索并打开云学帮小程序",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # 加载配置
    config = load_config_from_env()

    # 应用命令行参数
    if args.thinking:
        config.deepseek.use_deep_thinking = True
    if args.auto_submit:
        config.wechat.auto_submit = True
        config.wechat.confirm_before_submit = False
    if args.device:
        config.wechat.device_serial = args.device
    if args.debug:
        config.debug = True
    if args.delay is not None:
        config.wechat.question_delay = args.delay
        config.deepseek.question_interval = args.delay
    if args.ds_url:
        config.deepseek.url = args.ds_url
    if args.mp_name:
        config.wechat.mini_program_name = args.mp_name
    if args.auto_open:
        config.wechat.auto_open_mini_program = True

    # 配置日志
    setup_logging(config)
    logger = logging.getLogger(__name__)

    print_banner()

    logger.info(f"DeepSeek URL: {config.deepseek.url}")
    logger.info(f"深度思考: {config.deepseek.use_deep_thinking}")
    logger.info(f"小程序名称: {config.wechat.mini_program_name}")
    logger.info(f"自动提交: {config.wechat.auto_submit}")
    logger.info(f"自动打开小程序: {config.wechat.auto_open_mini_program}")

    # 运行
    asyncio.run(run(config, args))


if __name__ == "__main__":
    main()
