"""
微信小程序自动化模块（基于 uiautomator2 + Playwright）
通过安卓模拟器运行微信，在微信中打开云学帮小程序
电脑端通过 DeepSeek 网页版获取答案

关键改进 v4:
  - OCR 截图识别作为 WebView 内容提取回退
    （微信小程序渲染在 WebView 中，dump_hierarchy 无法获取内容时自动回退到 OCR）
  - 修复 device.scroll 崩溃问题
  - 修复 resourceClass → className
  - 连续失败时不立即关闭 DeepSeek，提供手动输入回退
  - 过滤状态栏/系统 UI 文本（电池、信号、时间等）
  - 过滤微信和小程序的导航栏文本
  - 多策略选项点击
  - 重试机制
"""

import re
import time
import asyncio
import logging
import os
from datetime import datetime
from typing import List, Optional

from config import WeChatMiniProgramConfig, DeepSeekWebConfig
from models import Question, AnswerResult, build_prompt, parse_response, TYPE_NAMES
from deepseek_web_client import DeepSeekWebClient

logger = logging.getLogger(__name__)

SCREENSHOT_DIR = "screenshots"

# 状态栏 / 系统 UI 黑名单关键词（这些文本不可能是题目或选项）
STATUS_BAR_KEYWORDS = [
    "电池", "电量", "信号", "网络", "蓝牙", "WiFi", "Wi-Fi", "飞行模式",
    "闹钟", "通知", "状态栏", "运营商", "中国移动", "中国联通", "中国电信",
    "GPRS", "EDGE", "LTE", "5G", "4G", "3G", "HD",
    "自动旋转", "勿扰", "护眼", "热点",
    "WLAN", "手机信号", "微信通知", "微信团队",
]

# 微信 / 小程序导航栏黑名单
NAV_BAR_KEYWORDS = [
    "返回", "关闭", "更多", "...", "···", "⋅⋅⋅",
    "上一页", "下一页", "首页", "搜索", "菜单",
    "微信", "通讯录", "发现", "我",
    "服务", "收付款", "扫一扫",
    "小程序", "云学帮",  # 小程序标题栏
    "客服", "反馈", "投诉", "分享", "收藏", "转发",
    "添加到我的小程序", "关于", "设置",
]

# 按钮黑名单（答题界面常见按钮文字）
BUTTON_TEXTS = {
    "提交", "交卷", "确认提交", "提交试卷", "确定", "取消",
    "下一题", "下一页", "继续", "下一道", "下一问",
    "上一题", "上一页", "返回",
    "查看", "查看答案", "查看结果", "查看解析",
    "收藏", "分享", "设置", "首页", "我的",
    "学习", "练习", "考试", "课程", "错题", "记录",
    "继续答题", "知道了", "再练一次", "重新答题",
    "正确答案", "你的答案", "解析",
}


def _is_status_bar_text(text: str) -> bool:
    """判断文本是否来自状态栏或系统 UI"""
    for kw in STATUS_BAR_KEYWORDS:
        if kw in text:
            return True
    # 纯数字（时间 18:30）或纯百分比（95%）
    if re.match(r"^\d{1,2}[:：]\d{1,2}$", text):
        return True
    if re.match(r"^\d+%$", text):
        return True
    return False


def _is_nav_bar_text(text: str) -> bool:
    """判断文本是否来自导航栏"""
    if text in NAV_BAR_KEYWORDS:
        return True
    for kw in NAV_BAR_KEYWORDS:
        if text == kw or (len(text) < 6 and kw in text):
            return True
    return False


def _is_button_text(text: str) -> bool:
    """判断是否是按钮文字"""
    return text in BUTTON_TEXTS


def _is_noise_node(node: dict) -> bool:
    """判断节点是否是噪音（状态栏、导航栏、按钮等）"""
    text = node.get("display", "").strip()
    if not text:
        return True

    # 状态栏文本
    if _is_status_bar_text(text):
        return True

    # 导航栏文本
    if _is_nav_bar_text(text):
        return True

    # 按钮文字
    if _is_button_text(text):
        return True

    # 太短的文本（< 2 个字符，可能是图标 label）
    if len(text) < 2:
        return True

    # 纯符号
    if re.match(r"^[\s\d\W]+$", text) and len(text) < 5:
        return True

    # 状态栏区域（屏幕顶部 y < 150px）
    bounds = node.get("bounds")
    if bounds:
        if isinstance(bounds, (tuple, list)) and len(bounds) >= 2:
            y = bounds[1]
            if isinstance(y, int) and y < 150:
                return True

    return False


class OCREngine:
    """OCR 引擎封装，支持多种后端"""

    _instance = None
    _engine_name = None
    _ocr = None

    @classmethod
    def get_instance(cls):
        """获取 OCR 实例（单例），自动选择可用后端"""
        if cls._instance is not None:
            return cls._instance

        # 尝试 PaddleOCR
        try:
            from paddleocr import PaddleOCR
            cls._ocr = PaddleOCR(use_angle_cls=True, lang='ch', show_log=False)
            cls._engine_name = "PaddleOCR"
            cls._instance = cls()
            logger.info("OCR 引擎: PaddleOCR")
            return cls._instance
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"PaddleOCR 初始化失败: {e}")

        # 尝试 RapidOCR (轻量级)
        try:
            from rapidocr_onnxruntime import RapidOCR
            cls._ocr = RapidOCR()
            cls._engine_name = "RapidOCR"
            cls._instance = cls()
            logger.info("OCR 引擎: RapidOCR")
            return cls._instance
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"RapidOCR 初始化失败: {e}")

        # 尝试 EasyOCR
        try:
            import easyocr
            cls._ocr = easyocr.Reader(['ch_sim', 'en'], verbose=False)
            cls._engine_name = "EasyOCR"
            cls._instance = cls()
            logger.info("OCR 引擎: EasyOCR")
            return cls._instance
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"EasyOCR 初始化失败: {e}")

        logger.warning(
            "未找到可用的 OCR 库！请安装其中之一:\n"
            "  pip install rapidocr-onnxruntime  (推荐，轻量级)\n"
            "  pip install paddlepaddle paddleocr  (最准确，较重)\n"
            "  pip install easyocr  (需要 torch)"
        )
        return None

    def recognize(self, image_path: str) -> list:
        """
        识别图片中的文字，返回节点列表
        每个节点: {text, bounds, confidence}
        """
        try:
            if self._engine_name == "PaddleOCR":
                return self._recognize_paddle(image_path)
            elif self._engine_name == "RapidOCR":
                return self._recognize_rapid(image_path)
            elif self._engine_name == "EasyOCR":
                return self._recognize_easy(image_path)
        except Exception as e:
            logger.error(f"OCR 识别异常: {e}", exc_info=True)
        return []

    def _parse_box(self, box) -> dict:
        """解析 OCR 检测框坐标，返回中心点和边界（兼容 numpy 类型）"""
        try:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            cx = int(sum(xs) / 4)
            cy = int(sum(ys) / 4)
            x1, y1, x2, y2 = int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))
            return {
                "bounds": (cx, cy),
                "bounds_str": f"[{x1},{y1}][{x2},{y2}]",
            }
        except Exception:
            return {"bounds": None, "bounds_str": ""}

    def _make_node(self, text: str, conf, box_info: dict) -> dict:
        """构建 OCR 节点"""
        return {
            "text": text.strip(),
            "desc": "",
            "display": text.strip(),
            "bounds": box_info["bounds"],
            "bounds_str": box_info["bounds_str"],
            "clickable": False,
            "class": "OCR",
            "resource_id": "",
            "confidence": float(conf) if conf is not None else 0.0,
        }

    def _recognize_paddle(self, image_path: str) -> list:
        """PaddleOCR 识别"""
        result = self._ocr.ocr(image_path, cls=True)
        nodes = []
        if not result or not result[0]:
            return nodes

        for line in result[0]:
            box = line[0]       # [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
            text = line[1][0]   # text
            try:
                conf = float(line[1][1])
            except (ValueError, TypeError):
                conf = 0.0

            if conf < 0.5 or not text.strip():
                continue

            box_info = self._parse_box(box)
            if not box_info["bounds"]:
                continue
            nodes.append(self._make_node(text, conf, box_info))
        return nodes

    def _recognize_rapid(self, image_path: str) -> list:
        """RapidOCR 识别"""
        result, elapse = self._ocr(image_path)
        nodes = []
        if not result:
            return nodes

        for item in result:
            box = item[0]       # [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
            text = item[1]
            # RapidOCR 返回 score 为字符串 '0.99'，需转换为 float
            try:
                conf = float(item[2])
            except (ValueError, TypeError, IndexError):
                conf = 0.0

            if conf < 0.5 or not text.strip():
                continue

            box_info = self._parse_box(box)
            if not box_info["bounds"]:
                continue
            nodes.append(self._make_node(text, conf, box_info))
        return nodes

    def _recognize_easy(self, image_path: str) -> list:
        """EasyOCR 识别"""
        result = self._ocr.readtext(image_path)
        nodes = []
        if not result:
            return nodes

        for item in result:
            box = item[0]       # [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
            text = item[1]
            try:
                conf = float(item[2])
            except (ValueError, TypeError, IndexError):
                conf = 0.0

            if conf < 0.5 or not text.strip():
                continue

            box_info = self._parse_box(box)
            if not box_info["bounds"]:
                continue
            nodes.append(self._make_node(text, conf, box_info))
        return nodes


class WeChatMiniProgramAutomator:
    """微信小程序自动化控制器"""

    def __init__(self, config: WeChatMiniProgramConfig, ds_config: DeepSeekWebConfig):
        self.config = config
        self.ds_config = ds_config
        self.device = None
        self.ds_client = None
        self._screenshot_count = 0
        self._ocr_engine = None
        self._ocr_initialized = False

    async def start(self):
        """连接 Android 设备并初始化 DeepSeek 客户端"""
        await asyncio.to_thread(self._connect_device)
        self.ds_client = DeepSeekWebClient(self.ds_config)
        await self.ds_client.init_standalone()
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    def _connect_device(self):
        import uiautomator2 as u2
        if self.config.device_serial:
            self.device = u2.connect(self.config.device_serial)
        else:
            self.device = u2.connect()
        logger.info(f"已连接设备: {self.device.info}")
        self.device.implicitly_wait(self.config.ui_timeout)

    def _init_ocr(self):
        """延迟初始化 OCR 引擎"""
        if self._ocr_initialized:
            return
        self._ocr_initialized = True
        self._ocr_engine = OCREngine.get_instance()

    async def screenshot(self, tag: str = "") -> str:
        """截图并返回文件路径"""
        def _shot():
            try:
                self._screenshot_count += 1
                ts = datetime.now().strftime("%H%M%S")
                filename = f"{SCREENSHOT_DIR}/{self._screenshot_count:03d}_{ts}_{tag}.png"
                self.device.screenshot(filename)
                logger.debug(f"截图: {filename}")
                return filename
            except Exception as e:
                logger.warning(f"截图失败: {e}")
                return ""
        return await asyncio.to_thread(_shot)

    async def open_wechat(self):
        def _open():
            self.device.app_start(self.config.wechat_package)
            logger.info("已启动微信")
            time.sleep(3)
        await asyncio.to_thread(_open)

    async def open_mini_program(self):
        """在微信中搜索并打开云学帮小程序"""
        def _open():
            try:
                self.device.swipe(0.5, 0.2, 0.5, 0.8, duration=0.5)
                time.sleep(1)
                search_box = self.device(resourceId="com.tencent.mm:id/icon_search_bar_text")
                if not search_box.exists:
                    search_box = self.device(text="搜索")
                if search_box.exists:
                    search_box.click()
                    time.sleep(1)
                    search_input = self.device(className="android.widget.EditText")
                    if search_input.exists:
                        search_input.set_text(self.config.mini_program_name)
                        time.sleep(2)
                        mp_item = self.device(text=self.config.mini_program_name)
                        if mp_item.exists:
                            mp_item.click()
                            logger.info(f"已打开小程序: {self.config.mini_program_name}")
                            time.sleep(5)
                            return True
                return False
            except Exception as e:
                logger.warning(f"自动打开失败: {e}")
                return False
        return await asyncio.to_thread(_open)

    async def init_deepseek_login(self):
        await self.ds_client.navigate_and_login()

    def wait_for_user_ready(self, message: str = ""):
        input(f"\n>>> {message}，完成后按回车继续...")

    # ===================== 屏幕内容提取 =====================

    async def get_screen_nodes(self) -> list:
        """通过无障碍树获取屏幕节点，过滤掉状态栏/导航栏噪音"""
        def _get():
            xml_content = self.device.dump_hierarchy()
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(xml_content)
            except ET.ParseError:
                return []

            raw_nodes = []
            for node in root.iter("node"):
                text = node.get("text", "").strip()
                desc = node.get("content-desc", "").strip()
                if not text and not desc:
                    continue
                bounds_str = node.get("bounds", "")
                bounds = self._parse_bounds(bounds_str)
                raw_nodes.append({
                    "text": text,
                    "desc": desc,
                    "display": text or desc,
                    "bounds": bounds,
                    "bounds_str": bounds_str,
                    "clickable": node.get("clickable", "false") == "true",
                    "scrollable": node.get("scrollable", "false") == "true",
                    "class": node.get("class", ""),
                    "resource_id": node.get("resource-id", ""),
                })

            clean_nodes = [n for n in raw_nodes if not _is_noise_node(n)]
            return clean_nodes
        return await asyncio.to_thread(_get)

    async def get_all_nodes_raw(self) -> list:
        """获取所有节点（不过滤），用于调试"""
        def _get():
            xml_content = self.device.dump_hierarchy()
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(xml_content)
            except ET.ParseError:
                return []
            nodes = []
            for node in root.iter("node"):
                text = node.get("text", "").strip()
                desc = node.get("content-desc", "").strip()
                if text or desc:
                    nodes.append({
                        "text": text, "desc": desc, "display": text or desc,
                        "bounds": self._parse_bounds(node.get("bounds", "")),
                        "bounds_str": node.get("bounds", ""),
                        "clickable": node.get("clickable", "false") == "true",
                        "class": node.get("class", ""),
                        "resource_id": node.get("resource-id", ""),
                    })
            return nodes
        return await asyncio.to_thread(_get)

    async def get_ocr_nodes(self) -> list:
        """通过 OCR 截图识别获取屏幕文字节点"""
        def _get():
            if not self._ocr_initialized:
                self._init_ocr()
            if not self._ocr_engine:
                return []

            # 截图
            self._screenshot_count += 1
            ts = datetime.now().strftime("%H%M%S")
            image_path = f"{SCREENSHOT_DIR}/{self._screenshot_count:03d}_{ts}_ocr.png"
            try:
                self.device.screenshot(image_path)
            except Exception as e:
                logger.warning(f"OCR 截图失败: {e}")
                return []

            # OCR 识别
            try:
                raw_nodes = self._ocr_engine.recognize(image_path)
                logger.info(f"OCR 识别到 {len(raw_nodes)} 个文本块")
                for n in raw_nodes:
                    logger.debug(f"  OCR: '{n['display'][:60]}' conf={n.get('confidence', 0):.2f} bounds={n['bounds_str']}")

                # 过滤噪音
                clean_nodes = [n for n in raw_nodes if not _is_noise_node(n)]
                logger.info(f"OCR 过滤后剩余 {len(clean_nodes)} 个有效节点")
                return clean_nodes
            except Exception as e:
                logger.warning(f"OCR 识别失败: {e}")
                return []

        return await asyncio.to_thread(_get)

    async def extract_questions(self) -> List[Question]:
        """从当前屏幕提取题目（先尝试无障碍树，失败则回退到 OCR）"""
        await self.screenshot("extract")

        # 策略1：通过无障碍树提取
        nodes = await self.get_screen_nodes()
        source = "无障碍树"

        # 如果无障碍树没有有效节点，尝试 OCR
        if not nodes:
            logger.info("无障碍树未提取到有效内容（可能是 WebView 渲染），尝试 OCR 识别...")
            nodes = await self.get_ocr_nodes()
            source = "OCR"

        if not nodes:
            logger.warning("OCR 也未能提取到有效内容")
            # 显示原始无障碍树节点帮助调试
            raw_nodes = await self.get_all_nodes_raw()
            logger.info(f"原始无障碍节点（共 {len(raw_nodes)} 个）:")
            for n in raw_nodes:
                logger.info(f"  '{n['display'][:60]}' bounds={n['bounds_str']}")

            # 最后回退：手动输入
            logger.warning("自动提取失败！请手动输入题目")
            question = await self._manual_input_question()
            if question:
                return [question]
            return []

        full_text = "\n".join([n["display"] for n in nodes])
        logger.info(f"[{source}] 过滤后屏幕文本（{len(nodes)} 个节点）:")
        for n in nodes:
            bounds_str = n.get("bounds_str", "")
            logger.info(f"  '{n['display'][:80]}' bounds={bounds_str}")
        logger.debug(f"[{source}] 合并文本:\n{full_text[:500]}")

        # 从节点中解析题目
        question = self._parse_single_question(nodes)
        if question and question.text and len(question.text) > 3:
            logger.info(f"[{source}] 提取到题目: {question.text[:80]}")
            for letter, opt in question.options:
                logger.info(f"  选项 {letter}: {opt[:40]}")
            return [question]

        # 尝试按题号分割多题
        questions = self._parse_multiple_from_text(full_text)
        if questions:
            logger.info(f"[{source}] 提取到 {len(questions)} 道题")
            return questions

        # 回退：手动输入
        logger.warning(f"[{source}] 自动解析题目失败！")
        logger.info(f"屏幕文本:\n{full_text[:800]}")
        question = await self._manual_input_question()
        if question:
            return [question]

        return []

    async def _manual_input_question(self) -> Optional[Question]:
        """手动输入题目（当自动提取失败时）"""
        print("\n" + "=" * 50)
        print("自动提取题目失败，请手动输入")
        print("=" * 50)
        print("请把当前屏幕上的题目和选项输入（或粘贴）到这里")
        print("格式示例：")
        print("  下列哪个是Python的特点")
        print("  A. 解释型语言")
        print("  B. 编译型语言")
        print("  C. 汇编语言")
        print("  D. 机器语言")
        print("输入完成后按回车（多行请用 | 分隔或直接粘贴）")
        print("-" * 50)

        try:
            raw = input("题目内容> ").strip()
            if not raw:
                return None

            # 支持多行粘贴（用 | 或换行分隔）
            lines = re.split(r"[\n|]+", raw)
            lines = [l.strip() for l in lines if l.strip()]
            if not lines:
                return None

            # 第一行是题目
            question_text = lines[0]
            # 后续行是选项
            options = []
            for line in lines[1:]:
                m = re.match(r"^([A-D])\s*[.、）)\]]\s*(.+)", line)
                if m:
                    options.append((m.group(1), m.group(2).strip()))

            qtype = self._detect_type(question_text, options)
            return Question(index=0, text=question_text, options=options, question_type=qtype)
        except Exception:
            return None

    # ===================== 题目解析 =====================

    def _parse_single_question(self, nodes: list) -> Optional[Question]:
        """从过滤后的节点中解析单道题目（支持 OCR 分块合并）"""
        if not nodes:
            return None

        # 按 y 坐标排序（从上到下）
        sorted_nodes = sorted(nodes, key=lambda n: (
            n.get("bounds", (0, 9999))[1] if n.get("bounds") else 9999,
            n.get("bounds", (0, 0))[0] if n.get("bounds") else 0,
        ))

        # 第一步：尝试从合并文本中提取（处理 OCR 分块问题）
        full_text = "\n".join([n["display"] for n in sorted_nodes])
        question_from_text = self._parse_question_from_text(full_text)
        if question_from_text and question_from_text.text and len(question_from_text.text) > 3:
            # 补充坐标信息
            if not question_from_text.options:
                # 没有选项，但也返回题目
                pass
            return question_from_text

        # 第二步：按节点位置分析
        question_candidates = []
        option_candidates = []

        for node in sorted_nodes:
            text = node["display"].strip()
            if not text:
                continue

            # 检查是否是选项（A. xxx / A、xxx / A) xxx / A.xxx）
            opt_match = re.match(r"^([A-D])\s*[.、）)\].]\s*(.+)", text)
            if opt_match:
                option_candidates.append((opt_match.group(1), opt_match.group(2).strip(), node))
                continue

            # 检查是否是选项（只有字母 A/B/C/D）
            if re.match(r"^([A-D])$", text) and len(text) == 1:
                option_candidates.append((text, "", node))
                continue

            # 检查是否是选项（A 开头后跟文字，如 "A正确" "B错误"）
            opt_match2 = re.match(r"^([A-D])(.{2,})", text)
            if opt_match2 and len(text) <= 20:
                option_candidates.append((opt_match2.group(1), opt_match2.group(2).strip(), node))
                continue

            # 排除太短的
            if len(text) < 3:
                continue

            question_candidates.append((text, node))

        # 有选项的情况
        if option_candidates:
            # 去重并排序
            seen_letters = set()
            unique_opts = []
            for oc in option_candidates:
                if oc[0] not in seen_letters:
                    seen_letters.add(oc[0])
                    unique_opts.append(oc)
            unique_opts.sort(key=lambda x: x[0])
            options = [(oc[0], oc[1]) for oc in unique_opts]

            # 找题目：合并选项上方的所有文本（OCR 可能将题目分成多块）
            first_opt_y = unique_opts[0][2]["bounds"][1] if unique_opts[0][2].get("bounds") else 9999

            # 合并选项上方的所有候选文本
            above_texts = []
            for text, node in question_candidates:
                ny = node["bounds"][1] if node.get("bounds") else 9999
                if ny <= first_opt_y:
                    above_texts.append(text)

            if above_texts:
                best_q = "".join(above_texts)
            elif question_candidates:
                # 没有在选项上方的文本，取最长的
                best_q = max(question_candidates, key=lambda x: len(x[0]))[0]
            else:
                best_q = ""

            qtype = self._detect_type(best_q, options)
            return Question(index=0, text=best_q, options=options, question_type=qtype)

        # 无选项（判断题/填空题）- 合并所有候选文本
        if question_candidates:
            # 合并所有文本块作为完整题目
            merged_text = "".join([t for t, _ in question_candidates])
            qtype = self._detect_type(merged_text, [])
            return Question(index=0, text=merged_text, options=[], question_type=qtype)

        return None

    def _parse_question_from_text(self, full_text: str) -> Optional[Question]:
        """从合并的文本中解析题目（处理 OCR 分块合并后的文本）"""
        if not full_text or len(full_text) < 3:
            return None

        # 尝试提取选项
        options = self._extract_options_from_text(full_text)

        # 尝试多种选项格式
        if not options:
            # 尝试 A.xxx B.xxx 格式（无分隔符）
            pattern = r"([A-D])\s*[.、）)\]]?\s*(.{2,}?)(?=\s*[A-D]\s*[.、）)\]]|$)"
            matches = re.findall(pattern, full_text)
            if len(matches) >= 2:
                options = [(m[0], m[1].strip()) for m in matches if m[1].strip()]

        if not options:
            # 尝试 A正确 B错误 格式
            pattern = r"([A-D])\s*(正确|错误|对|错|是|否|True|False)"
            matches = re.findall(pattern, full_text)
            if len(matches) >= 2:
                options = [(m[0], m[1].strip()) for m in matches]

        # 移除选项部分，剩余的是题目
        question_text = full_text
        for letter, opt in options:
            # 移除各种格式的选项
            question_text = re.sub(
                rf"{letter}\s*[.、）)\]]?\s*{re.escape(opt)}", "", question_text
            )
        question_text = question_text.strip()

        # 清理多余换行和空格
        question_text = re.sub(r"\n{2,}", "\n", question_text).strip()

        if not question_text or len(question_text) < 3:
            return None

        qtype = self._detect_type(question_text, options)
        return Question(index=0, text=question_text, options=options, question_type=qtype)

    def _parse_multiple_from_text(self, full_text: str) -> List[Question]:
        """按题号分割多题"""
        splits = re.split(r"(?=\n\s*\d+\s*[.、）)\]])", full_text)
        questions = []
        for idx, q_text in enumerate(splits):
            q_text = q_text.strip()
            if len(q_text) < 5:
                continue
            clean = re.sub(r"^\s*\d+\s*[.、）)\]]\s*", "", q_text).strip()
            if not clean:
                continue
            opts = self._extract_options_from_text(clean)
            qt = clean
            for letter, opt in opts:
                qt = re.sub(rf"{letter}\s*[.、）)\]]\s*{re.escape(opt)}", "", qt).strip()
            if qt and len(qt) > 3:
                questions.append(Question(index=idx, text=qt, options=opts,
                                          question_type=self._detect_type(qt, opts)))
        return questions

    def _detect_type(self, text: str, options: list) -> str:
        if "判断题" in text:
            return "judge"
        if "多选题" in text or "多选" in text:
            return "multiple"
        if "填空题" in text or "____" in text or "（）" in text or "(  )" in text:
            return "fill"
        if not options and "正确" in text and "错误" in text and len(text) < 200:
            return "judge"
        return "single"

    def _extract_options_from_text(self, text: str) -> List[tuple]:
        opts = []
        pattern = r"([A-D])\s*[.、）)\]]\s*([^\n\r]+?)(?=\s*[A-D]\s*[.、）)\]]|$)"
        for letter, opt in re.findall(pattern, text):
            opt = opt.strip()
            if opt and len(opt) < 500:
                opts.append((letter, opt))
        return opts

    # ===================== 答案选择 =====================

    async def select_answer(self, question: Question, answer_letters: List[str]) -> bool:
        """选择答案（先尝试无障碍树，失败则用 OCR 定位）"""
        if not answer_letters:
            return False

        # 先尝试无障碍树获取节点
        nodes = await self.get_screen_nodes()

        # 如果无障碍树没有节点，用 OCR
        if not nodes:
            logger.info("无障碍树无节点，使用 OCR 定位选项...")
            nodes = await self.get_ocr_nodes()

        if not nodes:
            logger.error("无法获取屏幕节点，选择答案失败")
            return False

        success = 0
        for letter in answer_letters:
            clicked = await asyncio.to_thread(self._click_option, nodes, letter, question)
            if clicked:
                success += 1
                logger.info(f"已选择选项 {letter}")
            else:
                logger.warning(f"选项 {letter} 点击失败，尝试坐标点击...")
                # 最后回退：坐标比例点击
                clicked = await asyncio.to_thread(self._click_by_position, letter, question)
                if clicked:
                    success += 1
                    logger.info(f"已通过坐标选择选项 {letter}")
                else:
                    logger.error(f"选项 {letter} 所有策略均失败")
            await asyncio.sleep(0.5)

        await self.screenshot(f"select_{''.join(answer_letters)}")
        return success > 0

    def _click_option(self, nodes: list, letter: str, question: Question) -> bool:
        """多策略点击选项"""
        # 策略1：精确匹配字母
        for node in nodes:
            text = node["display"].strip()
            if text == letter and node.get("bounds"):
                self.device.click(*node["bounds"])
                return True
            if re.match(rf"^{letter}\s*[.、）)\]]", text) and node.get("bounds"):
                self.device.click(*node["bounds"])
                return True

        # 策略2：匹配选项内容
        for opt_l, opt_t in question.options:
            if opt_l != letter:
                continue
            for node in nodes:
                t = node["display"].strip()
                if opt_t and (opt_t in t or t in opt_t) and node.get("bounds"):
                    self.device.click(*node["bounds"])
                    return True

        # 策略3：可点击元素按顺序
        clickables = [n for n in nodes if n.get("clickable") and n.get("bounds")]
        if len(clickables) >= len(question.options):
            idx = ord(letter) - 65
            if idx < len(clickables):
                self.device.click(*clickables[idx]["bounds"])
                return True

        return False

    def _click_by_position(self, letter: str, question: Question) -> bool:
        """按坐标比例点击选项（最后回退策略）"""
        try:
            info = self.device.info
            sw, sh = info["displayWidth"], info["displayHeight"]
            idx = ord(letter) - 65
            total = max(len(question.options), 4)
            y_start = int(sh * 0.35)
            y_end = int(sh * 0.75)
            step = (y_end - y_start) / total
            y = int(y_start + step * (idx + 0.5))
            x = int(sw * 0.5)
            self.device.click(x, y)
            logger.info(f"坐标点击: ({x},{y})")
            return True
        except Exception:
            return False

    def _parse_bounds(self, bounds_str: str) -> Optional[tuple]:
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
        if m:
            x1, y1, x2, y2 = map(int, m.groups())
            return ((x1 + x2) // 2, (y1 + y2) // 2)
        return None

    async def fill_answer(self, question: Question, answer_text: str):
        def _fill():
            edit = self.device(className="android.widget.EditText")
            if edit.exists:
                edit.set_text(answer_text)
                logger.info(f"已填写: {answer_text}")
                return True
            return False
        return await asyncio.to_thread(_fill)

    async def click_submit(self):
        def _submit():
            for t in ["提交", "交卷", "确认提交", "提交试卷", "确定"]:
                btn = self.device(text=t)
                if btn.exists:
                    btn.click()
                    logger.info(f"点击: {t}")
                    time.sleep(1)
                    for ct in ["确认", "确定", "是", "好的"]:
                        c = self.device(text=ct)
                        if c.exists:
                            c.click()
                            return True
                    return True
            return False
        return await asyncio.to_thread(_submit)

    async def click_next_question(self) -> bool:
        def _next():
            for t in ["下一题", "下一页", "继续", "下一道", "下一问", "确定"]:
                btn = self.device(text=t)
                if btn.exists:
                    btn.click()
                    logger.info(f"点击: {t}")
                    time.sleep(1.5)
                    return True
            for d in ["下一题", "下一页"]:
                btn = self.device(description=d)
                if btn.exists:
                    btn.click()
                    time.sleep(1.5)
                    return True
            return False
        return await asyncio.to_thread(_next)

    async def click_view_result(self) -> bool:
        def _click():
            for t in ["查看答案", "查看结果", "查看解析", "继续答题", "知道了"]:
                btn = self.device(text=t)
                if btn.exists:
                    btn.click()
                    logger.info(f"点击: {t}")
                    time.sleep(1)
                    return True
            return False
        return await asyncio.to_thread(_click)

    async def scroll_down(self):
        """向下滑动屏幕"""
        def _scroll():
            try:
                info = self.device.info
                sw, sh = info["displayWidth"], info["displayHeight"]
                # 从屏幕 70% 处滑到 30% 处
                x1 = int(sw * 0.5)
                y1 = int(sh * 0.7)
                x2 = int(sw * 0.5)
                y2 = int(sh * 0.3)
                self.device.swipe(x1, y1, x2, y2, duration=0.5)
                logger.debug("已向下滑动")
            except Exception as e:
                logger.warning(f"滑动失败: {e}")
        await asyncio.to_thread(_scroll)

    # ===================== 主答题循环 =====================

    async def run_auto_answer(self):
        """主答题循环"""
        results: List[AnswerResult] = []
        q_count = 0
        max_q = 200
        fail_streak = 0
        max_fail_streak = 5

        while q_count < max_q:
            q_count += 1
            logger.info(f"\n{'=' * 60}")
            logger.info(f"第 {q_count} 题")

            try:
                questions = await self.extract_questions()
            except Exception as e:
                logger.error(f"提取题目异常: {e}")
                questions = []

            if not questions:
                fail_streak += 1
                logger.warning(f"未检测到题目（连续 {fail_streak} 次）")
                if fail_streak >= max_fail_streak:
                    logger.error(f"连续 {max_fail_streak} 次未检测到题目，停止答题")
                    break
                # 尝试点击"查看结果"等按钮
                if await self.click_view_result():
                    fail_streak = 0
                    continue
                # 尝试滑动后重试
                await self.scroll_down()
                await asyncio.sleep(1)
                continue

            fail_streak = 0
            question = questions[0]
            question.index = q_count - 1
            logger.info(f"题目: {question.text[:80]}")
            logger.info(f"题型: {question.question_type}, 选项数: {len(question.options)}")

            # 如果没有选项也不是判断/填空，提示
            if not question.options and question.question_type == "single":
                logger.warning("未检测到选项！可能需要 OCR 识别或手动输入")

            # 向 DeepSeek 发送题目获取答案
            try:
                result = await self.ds_client.answer_question(question)
            except Exception as e:
                logger.error(f"DeepSeek 答题异常: {e}")
                result = AnswerResult(
                    question=question, answer_letters=[], raw_response="",
                    success=False, error=f"DeepSeek 异常: {e}",
                )

            results.append(result)

            if result.success and result.answer_letters:
                if question.question_type == "fill":
                    await self.fill_answer(question, result.answer_letters[0])
                else:
                    await self.select_answer(question, result.answer_letters)
                if result.reasoning:
                    logger.info(f"解析: {result.reasoning[:150]}")
            else:
                logger.warning(f"第 {q_count} 题未获取答案: {result.error}")
                logger.info("跳过此题，继续下一题")

            await asyncio.sleep(self.config.question_delay)

            # 点击下一题
            if not await self.click_next_question():
                if await self.click_view_result():
                    await asyncio.sleep(1)
                    if not await self.click_next_question():
                        logger.info("无法找到下一题按钮，可能答题已完成")
                        break
                else:
                    logger.info("无法找到下一题按钮，可能答题已完成")
                    break

        # 提交
        if self.config.confirm_before_submit:
            await self.screenshot("before_submit")
            self.wait_for_user_ready("答题完成，请检查")
        if self.config.confirm_before_submit or self.config.auto_submit:
            await self.click_submit()
            await self.screenshot("after_submit")
        return results

    async def inspect_screen(self):
        """调试模式：输出所有节点信息"""
        await self.screenshot("inspect")

        # 无障碍树
        raw_nodes = await self.get_all_nodes_raw()
        filtered = await self.get_screen_nodes()

        logger.info(f"=== 无障碍树（原始 {len(raw_nodes)} 个节点）===")
        for i, n in enumerate(raw_nodes):
            noise = " [噪音]" if _is_noise_node(n) else ""
            logger.info(f"  [{i}] '{n['display'][:60]}'{noise} bounds={n['bounds_str']} click={n['clickable']}")

        logger.info(f"\n=== 无障碍树过滤后 {len(filtered)} 个有效节点 ===")
        for n in filtered:
            logger.info(f"  '{n['display'][:60]}' bounds={n['bounds_str']} click={n['clickable']}")

        # OCR
        logger.info(f"\n=== OCR 识别 ===")
        ocr_nodes = await self.get_ocr_nodes()
        logger.info(f"OCR 识别到 {len(ocr_nodes)} 个有效节点:")
        for n in ocr_nodes:
            conf = n.get("confidence", 0)
            logger.info(f"  '{n['display'][:60]}' conf={conf:.2f} bounds={n['bounds_str']}")

        # 尝试解析题目
        all_nodes = filtered if filtered else ocr_nodes
        question = self._parse_single_question(all_nodes)
        if question:
            logger.info(f"\n解析题目: {question.text}")
            for l, t in question.options:
                logger.info(f"  {l}. {t}")
        else:
            logger.info("\n未能解析到题目")

        # 手动输入回退
        if not question:
            q = await self._manual_input_question()
            if q:
                logger.info(f"手动输入题目: {q.text}")

    async def close(self):
        """释放资源"""
        if self.ds_client:
            await self.ds_client.close()
        logger.info("资源已释放")
