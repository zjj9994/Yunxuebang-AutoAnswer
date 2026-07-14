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


# 题型标签（这些是 UI 标签，不是题目内容）
QUESTION_TYPE_LABELS = {"判断题", "单选题", "多选题", "填空题", "单选", "多选", "判断", "填空"}

# 题型标签正则（匹配 "单选题" "多选题" "判断题" "填空题" 等）
QUESTION_TYPE_PATTERN = re.compile(r"^[单多判断填]+[选题]$")


def _is_question_type_label(text: str) -> bool:
    """判断文本是否是题型标签（如 '判断题' '单选题'）"""
    return text.strip() in QUESTION_TYPE_LABELS


def _is_page_indicator(text: str) -> bool:
    """判断文本是否是页码指示器（如 '1/870' '2/100' '第1题'）"""
    text = text.strip()
    # 1/870, 2/100 等
    if re.match(r"^\d+/\d+$", text):
        return True
    # 第1题, 第2题 等
    if re.match(r"^第\s*\d+\s*题$", text):
        return True
    # 1/870 题
    if re.match(r"^\d+/\d+\s*题$", text):
        return True
    # 题目数量提示
    if re.match(r"^共\s*\d+\s*题", text):
        return True
    return False


def _clean_question_text(text: str) -> str:
    """清理题目文本，移除题型标签和页码指示器"""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 跳过题型标签
        if _is_question_type_label(line):
            continue
        # 跳过页码指示器
        if _is_page_indicator(line):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


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


# 有效单字符（选项字母和导航符号，不应被过滤）
VALID_SINGLE_CHARS = {"A", "B", "C", "D", ">", "〉", "›", "→", "❯", "➤", "➜", "》", "﹥", "＞"}


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

    # 题型标签（判断题/单选题等，作为独立行时是 UI 标签）
    if _is_question_type_label(text):
        return True

    # 页码指示器（1/870 等）
    if _is_page_indicator(text):
        return True

    # 有效单字符（选项字母 A/B/C/D 和导航符号 >）不过滤
    if text in VALID_SINGLE_CHARS:
        return False

    # 太短的文本（< 2 个字符，可能是图标 label）
    # 但保留单个字母 A/B/C/D 和 > 符号
    if len(text) < 2 and text not in VALID_SINGLE_CHARS:
        return True

    # 纯符号 - 但保留导航符号
    if re.match(r"^[\s\d\W]+$", text) and len(text) < 5:
        # 检查是否是有效导航符号
        if any(text.strip() == c for c in VALID_SINGLE_CHARS):
            return False
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
    """OCR 引擎封装，支持多种后端 + 图像预处理"""

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
            # 降低检测阈值以识别短文本、数字和符号
            # text_score: 文本识别置信度 (默认 0.5 → 0.3)
            # box_thresh: 检测框阈值 (默认 0.5 → 0.3)
            # thresh: 二值化阈值 (默认 0.3 → 0.2)
            # unclip_ratio: 检测框扩展比例 (默认 1.6 → 2.0)
            kwargs = {
                "text_score": 0.3,
                "box_thresh": 0.3,
                "thresh": 0.2,
                "unclip_ratio": 2.0,
            }
            try:
                cls._ocr = RapidOCR(**kwargs)
            except TypeError:
                # 旧版本不支持部分参数
                try:
                    cls._ocr = RapidOCR(text_score=0.3)
                except TypeError:
                    cls._ocr = RapidOCR()
            cls._engine_name = "RapidOCR"
            cls._instance = cls()
            logger.info(f"OCR 引擎: RapidOCR ({kwargs})")
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

    def _preprocess_image(self, image_path: str) -> str:
        """
        图像预处理：放大2倍 + 灰度化 + 增强对比度
        返回预处理后的临时图片路径
        """
        try:
            from PIL import Image, ImageEnhance, ImageFilter
            import os

            img = Image.open(image_path)
            w, h = img.size

            # 放大 2 倍
            img = img.resize((w * 2, h * 2), Image.LANCZOS)

            # 转灰度
            if img.mode != 'L':
                img = img.convert('L')

            # 增强对比度 1.5 倍
            enhancer = ImageEnhance.Contrast(img)
            img = enhancer.enhance(1.5)

            # 轻微锐化
            img = img.filter(ImageFilter.SHARPEN)

            # 保存预处理图片
            base, ext = os.path.splitext(image_path)
            proc_path = f"{base}_proc{ext}"
            img.save(proc_path)
            return proc_path
        except ImportError:
            logger.debug("PIL 未安装，跳过图像预处理")
            return image_path
        except Exception as e:
            logger.debug(f"图像预处理失败: {e}")
            return image_path

    def _nodes_overlap(self, n1: dict, n2: dict, threshold: float = 0.5) -> bool:
        """判断两个节点的坐标是否重叠（基于 bounds_str 解析）"""
        def parse_bounds_str(s):
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", s)
            if m:
                return tuple(map(int, m.groups()))
            return None

        b1 = parse_bounds_str(n1.get("bounds_str", ""))
        b2 = parse_bounds_str(n2.get("bounds_str", ""))
        if not b1 or not b2:
            # 回退：用中心点距离判断
            c1 = n1.get("bounds")
            c2 = n2.get("bounds")
            if c1 and c2:
                dist = ((c1[0]-c2[0])**2 + (c1[1]-c2[1])**2)**0.5
                return dist < 50
            return False

        x1_1, y1_1, x2_1, y2_1 = b1
        x1_2, y1_2, x2_2, y2_2 = b2

        # 计算重叠区域
        ix1 = max(x1_1, x1_2)
        iy1 = max(y1_1, y1_2)
        ix2 = min(x2_1, x2_2)
        iy2 = min(y2_1, y2_2)

        if ix2 <= ix1 or iy2 <= iy1:
            return False

        overlap_area = (ix2 - ix1) * (iy2 - iy1)
        area1 = (x2_1 - x1_1) * (y2_1 - y1_1)
        area2 = (x2_2 - x1_2) * (y2_2 - y1_2)

        if area1 == 0 or area2 == 0:
            return False

        # 重叠面积占较小节点的比例
        smaller_area = min(area1, area2)
        return overlap_area / smaller_area > threshold

    def recognize(self, image_path: str) -> list:
        """
        识别图片中的文字（双次识别：原图 + 预处理图）
        返回节点列表，每个节点: {text, bounds, confidence}
        """
        all_nodes = []

        # 第一次：原图识别
        try:
            if self._engine_name == "PaddleOCR":
                nodes1 = self._recognize_paddle(image_path)
            elif self._engine_name == "RapidOCR":
                nodes1 = self._recognize_rapid(image_path)
            elif self._engine_name == "EasyOCR":
                nodes1 = self._recognize_easy(image_path)
            else:
                nodes1 = []
            all_nodes.extend(nodes1)
        except Exception as e:
            logger.error(f"原图 OCR 异常: {e}", exc_info=True)
            nodes1 = []

        # 第二次：预处理图识别（放大+增强对比度）
        proc_path = self._preprocess_image(image_path)
        if proc_path != image_path:
            try:
                if self._engine_name == "PaddleOCR":
                    nodes2 = self._recognize_paddle(proc_path, scale=2)
                elif self._engine_name == "RapidOCR":
                    nodes2 = self._recognize_rapid(proc_path, scale=2)
                elif self._engine_name == "EasyOCR":
                    nodes2 = self._recognize_easy(proc_path, scale=2)
                else:
                    nodes2 = []

                # 合并结果：按坐标重叠去重
                # 如果新节点和已有节点坐标重叠，保留置信度更高的
                for node in nodes2:
                    overlap_found = False
                    for i, existing in enumerate(all_nodes):
                        if self._nodes_overlap(node, existing):
                            overlap_found = True
                            # 保留置信度更高的，或者文本更长的
                            if node.get("confidence", 0) > existing.get("confidence", 0):
                                all_nodes[i] = node
                            break
                    if not overlap_found:
                        all_nodes.append(node)
            except Exception as e:
                logger.error(f"预处理图 OCR 异常: {e}", exc_info=True)
            finally:
                # 清理临时文件
                try:
                    import os
                    os.remove(proc_path)
                except Exception:
                    pass

        logger.info(f"OCR 双次识别合并结果: {len(all_nodes)} 个文本块")
        return all_nodes

    def _parse_box(self, box, scale: int = 1) -> dict:
        """解析 OCR 检测框坐标，返回中心点和边界（兼容 numpy 类型，支持缩放）"""
        try:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            cx = int(sum(xs) / 4 / scale)
            cy = int(sum(ys) / 4 / scale)
            x1, y1, x2, y2 = int(min(xs) / scale), int(min(ys) / scale), int(max(xs) / scale), int(max(ys) / scale)
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

    def _recognize_paddle(self, image_path: str, scale: int = 1) -> list:
        """PaddleOCR 识别"""
        result = self._ocr.ocr(image_path, cls=True)
        nodes = []
        if not result or not result[0]:
            return nodes

        for line in result[0]:
            box = line[0]
            text = line[1][0]
            try:
                conf = float(line[1][1])
            except (ValueError, TypeError):
                conf = 0.0

            if conf < 0.3 or not text.strip():
                continue

            box_info = self._parse_box(box, scale)
            if not box_info["bounds"]:
                continue
            nodes.append(self._make_node(text, conf, box_info))
        return nodes

    def _recognize_rapid(self, image_path: str, scale: int = 1) -> list:
        """RapidOCR 识别"""
        result, elapse = self._ocr(image_path)
        nodes = []
        if not result:
            return nodes

        for item in result:
            box = item[0]
            text = item[1]
            try:
                conf = float(item[2])
            except (ValueError, TypeError, IndexError):
                conf = 0.0

            if conf < 0.3 or not text.strip():
                continue

            box_info = self._parse_box(box, scale)
            if not box_info["bounds"]:
                continue
            nodes.append(self._make_node(text, conf, box_info))
        return nodes

    def _recognize_easy(self, image_path: str, scale: int = 1) -> list:
        """EasyOCR 识别"""
        result = self._ocr.readtext(image_path)
        nodes = []
        if not result:
            return nodes

        for item in result:
            box = item[0]
            text = item[1]
            try:
                conf = float(item[2])
            except (ValueError, TypeError, IndexError):
                conf = 0.0

            if conf < 0.3 or not text.strip():
                continue

            box_info = self._parse_box(box, scale)
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
                logger.info(f"OCR 识别到 {len(raw_nodes)} 个文本块（原始）:")
                for n in raw_nodes:
                    conf = n.get('confidence', 0)
                    logger.info(f"  OCR: '{n['display'][:60]}' conf={conf:.2f} bounds={n['bounds_str']}")

                # 过滤噪音
                clean_nodes = [n for n in raw_nodes if not _is_noise_node(n)]
                logger.info(f"OCR 过滤后剩余 {len(clean_nodes)} 个有效节点:")
                for n in clean_nodes:
                    logger.info(f"  [有效] '{n['display'][:60]}' bounds={n['bounds_str']}")
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
        judge_option_nodes = []  # 判断题选项（"正确"/"错误"）

        # 先检测是否有题型标签（判断题/单选题等）
        has_type_label = any(
            _is_question_type_label(n["display"].strip()) for n in sorted_nodes
        )
        detected_type_from_label = None
        for n in sorted_nodes:
            t = n["display"].strip()
            if t == "判断题":
                detected_type_from_label = "judge"
            elif t == "多选题":
                detected_type_from_label = "multiple"
            elif t == "填空题":
                detected_type_from_label = "fill"
            elif t == "单选题":
                detected_type_from_label = "single"

        for node in sorted_nodes:
            text = node["display"].strip()
            if not text:
                continue

            # 跳过题型标签和页码指示器
            if _is_question_type_label(text) or _is_page_indicator(text):
                continue

            # 检查是否是选项（A. xxx / A、xxx / A) xxx / A.xxx）
            opt_match = re.match(r"^([A-D])\s*[.、）)\].]\s*(.+)", text)
            if opt_match:
                option_candidates.append((opt_match.group(1), opt_match.group(2).strip(), node))
                continue

            # 检查是否是选项（只有字母 A/B/C/D）
            if re.match(r"^([A-D])$", text) and len(text) == 1:
                # 字母单独识别，稍后和同 y 坐标的文字配对
                option_candidates.append((text, "", node))
                continue

            # 检查是否是选项（A 开头后跟文字，如 "A正确" "B错误"）
            opt_match2 = re.match(r"^([A-D])(.{2,})", text)
            if opt_match2 and len(text) <= 20:
                option_candidates.append((opt_match2.group(1), opt_match2.group(2).strip(), node))
                continue

            # 判断题选项检测：独立的 "正确" 或 "错误"
            if detected_type_from_label == "judge" or has_type_label:
                if text == "正确" or text == "对":
                    judge_option_nodes.append(("A", "正确", node))
                    continue
                if text == "错误" or text == "错":
                    judge_option_nodes.append(("B", "错误", node))
                    continue

            # 排除太短的
            if len(text) < 3:
                continue

            question_candidates.append((text, node))

        # === 关键步骤：将单独的字母节点和同 y 坐标的文字节点配对 ===
        # OCR 常把 "A" 和 "9:1" 分成两个节点，需要合并
        if option_candidates:
            # 收集所有未配对的节点（不是选项字母也不是题目文本的短文本）
            # 这些可能是选项内容（如 "9:1", "3:1" 等）
            orphan_nodes = []
            for node in sorted_nodes:
                text = node["display"].strip()
                if not text or len(text) < 1:
                    continue
                # 跳过已经是选项字母的
                if text in VALID_SINGLE_CHARS:
                    continue
                # 跳过已经是选项的
                if any(text == oc[1] for oc in option_candidates if oc[1]):
                    continue
                # 跳过题型标签和页码
                if _is_question_type_label(text) or _is_page_indicator(text):
                    continue
                # 跳过太长的（题目文本）
                if len(text) > 100:
                    continue
                # 跳过导航栏和按钮
                if _is_nav_bar_text(text) or _is_button_text(text):
                    continue
                # 这个节点可能是选项内容
                orphan_nodes.append((text, node))

            # 找出内容为空的字母节点
            empty_letters = [oc for oc in option_candidates if not oc[1]]

            for empty_oc in empty_letters:
                letter = empty_oc[0]
                letter_y = empty_oc[2].get("bounds", (0, 9999))[1]
                letter_x = empty_oc[2].get("bounds", (0, 0))[0]

                matched = False

                # 先在 judge_option_nodes 中找同 y 坐标的
                for jo in judge_option_nodes:
                    jo_y = jo[2].get("bounds", (0, 9999))[1]
                    if abs(jo_y - letter_y) < 60:
                        empty_oc_list = list(empty_oc)
                        empty_oc_list[1] = jo[1]
                        option_candidates[option_candidates.index(empty_oc)] = tuple(empty_oc_list)
                        matched = True
                        judge_option_nodes.remove(jo)
                        break

                # 在 orphan_nodes 中找同 y 坐标的选项内容
                if not matched:
                    best_match = None
                    best_dist = 9999
                    for text, node in orphan_nodes:
                        ny = node.get("bounds", (0, 9999))[1]
                        nx = node.get("bounds", (0, 0))[0]
                        # 同一行（y 差距小于 60px）且在字母右侧
                        if abs(ny - letter_y) < 60 and nx > letter_x:
                            dist = abs(ny - letter_y) + abs(nx - letter_x) * 0.1
                            if dist < best_dist:
                                best_dist = dist
                                best_match = (text, node)

                    if best_match:
                        empty_oc_list = list(empty_oc)
                        empty_oc_list[1] = best_match[0]
                        option_candidates[option_candidates.index(empty_oc)] = tuple(empty_oc_list)
                        orphan_nodes.remove(best_match)
                        matched = True

                # 如果还没配对，在 question_candidates 中找
                if not matched:
                    best_match = None
                    best_dist = 9999
                    for text, node in question_candidates:
                        ny = node.get("bounds", (0, 9999))[1]
                        nx = node.get("bounds", (0, 0))[0]
                        if abs(ny - letter_y) < 60 and nx > letter_x:
                            dist = abs(ny - letter_y) + abs(nx - letter_x) * 0.1
                            if dist < best_dist:
                                best_dist = dist
                                best_match = (text, node)

                    if best_match:
                        empty_oc_list = list(empty_oc)
                        empty_oc_list[1] = best_match[0]
                        option_candidates[option_candidates.index(empty_oc)] = tuple(empty_oc_list)
                        question_candidates.remove(best_match)

        # 合并判断题选项（未被字母配对的剩余选项）
        if judge_option_nodes:
            # 检查是否已有同字母的选项
            existing_letters = {oc[0] for oc in option_candidates if oc[1]}
            for jo in judge_option_nodes:
                if jo[0] not in existing_letters:
                    option_candidates.append(jo)

        # 如果有判断题标签且有 正确/错误 但没有字母配对，直接用判断题选项
        if detected_type_from_label == "judge" and judge_option_nodes and not any(oc[1] for oc in option_candidates):
            option_candidates = judge_option_nodes

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
                best_q = "".join([t for t, _ in question_candidates])
            else:
                best_q = ""

            # 清理题目文本
            best_q = _clean_question_text(best_q)
            qtype = detected_type_from_label or self._detect_type(best_q, options)
            return Question(index=0, text=best_q, options=options, question_type=qtype)

        # 无选项（判断题/填空题）- 合并所有候选文本
        if question_candidates:
            merged_text = "".join([t for t, _ in question_candidates])
            merged_text = _clean_question_text(merged_text)
            qtype = detected_type_from_label or self._detect_type(merged_text, [])

            # 判断题：检查文本中是否有 "正确" 和 "错误"
            if qtype == "judge" and not _is_question_type_label(merged_text):
                has_correct = "正确" in merged_text
                has_wrong = "错误" in merged_text
                if has_correct and has_wrong:
                    # 移除 "正确" 和 "错误" 文本
                    merged_text = re.sub(r"正确", "", merged_text)
                    merged_text = re.sub(r"错误", "", merged_text)
                    merged_text = merged_text.strip()
                    return Question(index=0, text=merged_text,
                                    options=[("A", "正确"), ("B", "错误")],
                                    question_type="judge")

            return Question(index=0, text=merged_text, options=[], question_type=qtype)

        return None

    def _parse_question_from_text(self, full_text: str) -> Optional[Question]:
        """从合并的文本中解析题目（处理 OCR 分块合并后的文本）"""
        if not full_text or len(full_text) < 3:
            return None

        # 先检测题型（在清理之前检测，因为 "判断题" 标签会告诉我们题型）
        detected_type = None
        if "判断题" in full_text:
            detected_type = "judge"
        elif "多选题" in full_text or "多选" in full_text:
            detected_type = "multiple"
        elif "填空题" in full_text:
            detected_type = "fill"
        elif "单选题" in full_text:
            detected_type = "single"

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

        # 判断题特殊处理：检测独立的 "正确" 和 "错误" 文本
        if not options and detected_type == "judge":
            has_correct = bool(re.search(r"(?<![不对])正确(?![答案])", full_text))
            has_wrong = "错误" in full_text
            if has_correct and has_wrong:
                options = [("A", "正确"), ("B", "错误")]

        # 即使没有 "判断题" 标签，如果有 正确/错误 选项也判断为判断题
        if not options:
            has_correct = bool(re.search(r"(?<![不对])正确(?![答案])", full_text))
            has_wrong = "错误" in full_text
            if has_correct and has_wrong:
                options = [("A", "正确"), ("B", "错误")]
                if not detected_type or detected_type == "fill":
                    detected_type = "judge"

        # 如果还没检测到题型，根据选项推断
        if not detected_type:
            if options and len(options) >= 2:
                opt_texts = [opt[1] for opt in options]
                if "正确" in opt_texts and "错误" in opt_texts:
                    detected_type = "judge"
                else:
                    detected_type = "single"
            else:
                detected_type = self._detect_type(full_text, options)

        # 移除选项部分
        question_text = full_text
        for letter, opt in options:
            question_text = re.sub(
                rf"{letter}\s*[.、）)\]]?\s*{re.escape(opt)}", "", question_text
            )
        # 移除独立的 "正确" 和 "错误"（判断题选项）
        if detected_type == "judge" and options:
            for _, opt in options:
                question_text = re.sub(rf"\b{re.escape(opt)}\b", "", question_text)
                # 也处理行首行尾的情况
                lines = question_text.split("\n")
                lines = [l for l in lines if l.strip() != opt]
                question_text = "\n".join(lines)

        # 清理题型标签和页码指示器
        question_text = _clean_question_text(question_text)

        # 移除题目中残留的括号（如 （）真空干燥... → 真空干燥...）
        question_text = re.sub(r"^[（）()]+", "", question_text).strip()

        # 清理多余换行和空格
        question_text = re.sub(r"\n{2,}", "\n", question_text).strip()

        if not question_text or len(question_text) < 3:
            return None

        return Question(index=0, text=question_text, options=options, question_type=detected_type)

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
        # 判断题标签优先
        if "判断题" in text:
            return "judge"
        # 多选题
        if "多选题" in text or "多选" in text:
            return "multiple"
        # 判断题：有 "正确"/"错误" 选项时优先于填空题
        # （即使文本中有 （），只要同时有 正确/错误 选项就是判断题）
        if options:
            opt_texts = [opt[1] for opt in options if opt[1]]
            if "正确" in opt_texts and "错误" in opt_texts:
                return "judge"
        # 无选项时检查文本
        if not options and "正确" in text and "错误" in text and len(text) < 200:
            return "judge"
        # 填空题
        if "填空题" in text or "____" in text:
            return "fill"
        # （）不单独作为填空题依据，因为判断题题目中也可能有括号
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
        """选择答案 - 多策略点击（无障碍树 → OCR 精确定位 → 坐标比例）"""
        if not answer_letters:
            return False

        success_count = 0

        # 判断题特殊处理：直接用 OCR 找 "正确"/"错误" 文字点击
        if question.question_type == "judge":
            clicked = await self._click_judge_answer(answer_letters, question)
            if clicked:
                await self.screenshot(f"select_{''.join(answer_letters)}")
                return True
            logger.warning("判断题 OCR 点击失败，尝试通用策略...")

        # 通用策略：先尝试无障碍树
        nodes = await self.get_screen_nodes()
        if nodes:
            for letter in answer_letters:
                clicked = await asyncio.to_thread(self._click_option, nodes, letter, question)
                if clicked:
                    logger.info(f"已选择选项 {letter}（无障碍树）")
                    success_count += 1
                    await asyncio.sleep(0.5)
                    continue
                # OCR 精确定位
                clicked = await self._click_option_by_ocr(letter, question)
                if clicked:
                    logger.info(f"已选择选项 {letter}（OCR）")
                    success_count += 1
                else:
                    logger.warning(f"选项 {letter} OCR 定位失败，尝试坐标点击...")
                    clicked = await asyncio.to_thread(self._click_by_position, letter, question)
                    if clicked:
                        logger.info(f"已选择选项 {letter}（坐标）")
                        success_count += 1
                    else:
                        logger.error(f"选项 {letter} 所有策略均失败")
                await asyncio.sleep(0.5)
        else:
            # WebView 模式：直接用 OCR
            logger.info("WebView 模式，使用 OCR 定位选项...")
            for letter in answer_letters:
                clicked = await self._click_option_by_ocr(letter, question)
                if clicked:
                    logger.info(f"已选择选项 {letter}（OCR）")
                    success_count += 1
                else:
                    logger.warning(f"选项 {letter} OCR 定位失败，尝试坐标点击...")
                    clicked = await asyncio.to_thread(self._click_by_position, letter, question)
                    if clicked:
                        logger.info(f"已选择选项 {letter}（坐标）")
                        success_count += 1
                    else:
                        logger.error(f"选项 {letter} 所有策略均失败")
                await asyncio.sleep(0.5)

        await self.screenshot(f"select_{''.join(answer_letters)}")
        if success_count == 0:
            logger.error("所有选项点击均失败！")
            return False
        return True

    async def _click_judge_answer(self, answer_letters: List[str], question: Question) -> bool:
        """判断题专用点击：用 OCR 找 '正确'/'错误' 文字并点击"""
        # 答案 A=正确, B=错误
        target_texts = []
        for letter in answer_letters:
            for opt_l, opt_t in question.options:
                if opt_l == letter:
                    target_texts.append(opt_t)
                    break
            else:
                # 没有选项信息时，直接映射
                if letter == "A":
                    target_texts.append("正确")
                elif letter == "B":
                    target_texts.append("错误")

        if not target_texts:
            return False

        def _click():
            if not self._ocr_initialized:
                self._init_ocr()
            if not self._ocr_engine:
                return False

            # 截图
            self._screenshot_count += 1
            ts = datetime.now().strftime("%H%M%S")
            image_path = f"{SCREENSHOT_DIR}/{self._screenshot_count:03d}_{ts}_judge.png"
            try:
                self.device.screenshot(image_path)
            except Exception:
                return False

            # OCR 识别
            try:
                raw_nodes = self._ocr_engine.recognize(image_path)
            except Exception:
                return False

            if not raw_nodes:
                logger.warning("判断题 OCR 无识别结果")
                return False

            logger.debug(f"判断题 OCR 识别到 {len(raw_nodes)} 个文本块:")
            for n in raw_nodes:
                logger.debug(f"  '{n['display'][:40]}' bounds={n.get('bounds_str', '')}")

            for target in target_texts:
                # 策略1：精确匹配 "正确"/"错误"
                for node in raw_nodes:
                    text = node["display"].strip()
                    bounds = node.get("bounds")
                    if not bounds:
                        continue
                    if text == target:
                        logger.info(f"OCR 精确匹配 '{target}' at {bounds}")
                        self.device.click(*bounds)
                        return True

                # 策略2：包含匹配（OCR 可能多识别了文字）
                for node in raw_nodes:
                    text = node["display"].strip()
                    bounds = node.get("bounds")
                    if not bounds:
                        continue
                    if target in text and len(text) < 20:
                        logger.info(f"OCR 包含匹配 '{target}' in '{text}' at {bounds}")
                        self.device.click(*bounds)
                        return True

                # 策略3：模糊匹配（"对"/"错"）
                fuzzy = {"正确": ["对", "是对的", "正确"], "错误": ["错", "是错的", "错误"]}
                for fuzzy_word in fuzzy.get(target, []):
                    for node in raw_nodes:
                        text = node["display"].strip()
                        bounds = node.get("bounds")
                        if not bounds:
                            continue
                        if fuzzy_word in text and len(text) < 20:
                            logger.info(f"OCR 模糊匹配 '{fuzzy_word}' in '{text}' at {bounds}")
                            self.device.click(*bounds)
                            return True

            # 策略4：按位置 - 判断题通常两个选项上下排列
            # 找屏幕中下方的短文本节点（可能是选项按钮）
            info = self.device.info
            sh = info["displayHeight"]
            sw = info["displayWidth"]

            candidate_buttons = []
            for node in raw_nodes:
                text = node["display"].strip()
                bounds = node.get("bounds")
                if not bounds:
                    continue
                # 在屏幕中间区域，排除太短和太长的文本
                if 3 <= len(text) <= 10 and bounds[1] > sh * 0.3 and bounds[1] < sh * 0.85:
                    # 排除题目文本（通常较长）
                    candidate_buttons.append(node)

            # 按 y 坐标排序
            candidate_buttons.sort(key=lambda n: n.get("bounds", (0, 0))[1])

            if len(candidate_buttons) >= 2:
                for i, letter in enumerate(answer_letters):
                    idx = 0 if letter == "A" else (1 if letter == "B" else min(i, len(candidate_buttons) - 1))
                    if idx < len(candidate_buttons):
                        bounds = candidate_buttons[idx].get("bounds")
                        text = candidate_buttons[idx]["display"].strip()
                        logger.info(f"判断题按位置点击第{idx+1}个按钮: '{text}' at {bounds}")
                        self.device.click(*bounds)
                        return True

            return False
        return await asyncio.to_thread(_click)

    async def _click_option_by_ocr(self, letter: str, question: Question) -> bool:
        """用 OCR 截图精确定位选项坐标并点击"""
        def _click():
            if not self._ocr_initialized:
                self._init_ocr()
            if not self._ocr_engine:
                return False

            # 截图
            self._screenshot_count += 1
            ts = datetime.now().strftime("%H%M%S")
            image_path = f"{SCREENSHOT_DIR}/{self._screenshot_count:03d}_{ts}_ocr_click.png"
            try:
                self.device.screenshot(image_path)
            except Exception:
                return False

            # OCR 识别
            try:
                raw_nodes = self._ocr_engine.recognize(image_path)
            except Exception:
                return False

            if not raw_nodes:
                return False

            logger.debug(f"选项 OCR 识别到 {len(raw_nodes)} 个文本块:")
            for n in raw_nodes:
                logger.debug(f"  '{n['display'][:50]}' bounds={n.get('bounds_str', '')}")

            # 策略1：找以 "A." "A、" "A)" 等开头的文本块
            for node in raw_nodes:
                text = node["display"].strip()
                bounds = node.get("bounds")
                if not bounds:
                    continue
                if re.match(rf"^{letter}\s*[.、）)\].]", text):
                    logger.info(f"OCR 匹配选项 {letter}: '{text[:40]}' at {bounds}")
                    self.device.click(*bounds)
                    return True

            # 策略2：找只包含字母的文本块
            for node in raw_nodes:
                text = node["display"].strip()
                bounds = node.get("bounds")
                if not bounds:
                    continue
                if text == letter:
                    logger.info(f"OCR 匹配选项字母 {letter} at {bounds}")
                    self.device.click(*bounds)
                    return True

            # 策略3：匹配选项内容
            for opt_l, opt_t in question.options:
                if opt_l != letter or not opt_t:
                    continue
                for node in raw_nodes:
                    text = node["display"].strip()
                    bounds = node.get("bounds")
                    if not bounds:
                        continue
                    # 精确匹配或包含匹配
                    if text == opt_t or opt_t in text or text in opt_t:
                        logger.info(f"OCR 匹配选项内容 {letter}: '{text[:40]}' at {bounds}")
                        self.device.click(*bounds)
                        return True

            # 策略4：按 OCR 节点 y 坐标顺序匹配
            # 收集可能是选项的节点（排除状态栏、导航栏等）
            info = self.device.info
            sh = info["displayHeight"]

            opt_like = []
            for node in raw_nodes:
                text = node["display"].strip()
                bounds = node.get("bounds")
                if not bounds:
                    continue
                # 排除状态栏区域
                if bounds[1] < sh * 0.15:
                    continue
                # 排除题目本身（太长的文本）
                if len(text) > 100:
                    continue
                # 排除页码
                if _is_page_indicator(text):
                    continue
                # 排除题型标签
                if _is_question_type_label(text):
                    continue
                # A/B/C/D 开头 或 短文本（选项通常较短）
                if re.match(r"^[A-D]", text) or len(text) <= 30:
                    opt_like.append(node)

            # 按 y 坐标排序
            opt_like.sort(key=lambda n: n.get("bounds", (0, 9999))[1])

            # 过滤出在选项区域的节点（排除最上方的题目文本）
            # 选项通常在屏幕 35%-85% 区域
            opt_area = [n for n in opt_like if sh * 0.3 < n.get("bounds", (0, 0))[1] < sh * 0.9]

            if len(opt_area) >= len(question.options):
                idx = ord(letter) - 65
                if idx < len(opt_area):
                    bounds = opt_area[idx].get("bounds")
                    text = opt_area[idx]["display"].strip()
                    logger.info(f"OCR 按序号匹配选项 {letter} (第{idx+1}个): '{text[:30]}' at {bounds}")
                    self.device.click(*bounds)
                    return True

            # 策略5：如果没有选项信息，按屏幕区域等分点击
            if not question.options and opt_area:
                idx = ord(letter) - 65
                if idx < len(opt_area):
                    bounds = opt_area[idx].get("bounds")
                    logger.info(f"OCR 按区域匹配选项 {letter} at {bounds}")
                    self.device.click(*bounds)
                    return True

            return False
        return await asyncio.to_thread(_click)

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
            # 使用实际选项数量，不强制最少 4 个
            total = len(question.options) if question.options else 4
            if total < 2:
                total = 2
            # 选项区域：屏幕 35%~80%
            y_start = int(sh * 0.35)
            y_end = int(sh * 0.80)
            step = (y_end - y_start) / total
            y = int(y_start + step * (idx + 0.5))
            x = int(sw * 0.5)
            self.device.click(x, y)
            logger.info(f"坐标点击: ({x},{y}) [共{total}个选项, 第{idx+1}个]")
            return True
        except Exception as e:
            logger.error(f"坐标点击失败: {e}")
            return False

    def _parse_bounds(self, bounds_str: str) -> Optional[tuple]:
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
        if m:
            x1, y1, x2, y2 = map(int, m.groups())
            return ((x1 + x2) // 2, (y1 + y2) // 2)
        return None

    async def fill_answer(self, question: Question, answer_text: str) -> bool:
        """填写填空题答案（支持多空、WebView OCR 定位）"""
        # 支持多空答案（answer_letters 可能是 ["答案1", "答案2"]）
        # 如果传入的是单个字符串，转为列表
        if isinstance(answer_text, str):
            answers = [answer_text]
        elif isinstance(answer_text, list):
            answers = answer_text
        else:
            answers = [str(answer_text)]

        logger.info(f"填空题答案: {answers}")

        # 策略1：通过无障碍树找输入框
        def _fill_accessibility():
            try:
                edits = self.device(className="android.widget.EditText")
                count = edits.count
                if count > 0:
                    for i, ans in enumerate(answers):
                        if i < count:
                            edits[i].click()
                            time.sleep(0.3)
                            edits[i].set_text(ans)
                            logger.info(f"已填写第{i+1}空: {ans}")
                            time.sleep(0.3)
                    return True
            except Exception as e:
                logger.debug(f"无障碍树填写失败: {e}")
            return False

        result = await asyncio.to_thread(_fill_accessibility)
        if result:
            await self.screenshot("fill_done")
            return True

        # 策略2：WebView 模式 - OCR 定位输入框位置
        logger.info("无障碍树未找到输入框，尝试 OCR 定位...")
        result = await self._fill_by_ocr(answers)
        if result:
            await self.screenshot("fill_done")
            return True

        # 策略3：点击屏幕中间偏下区域（输入框常见位置），尝试唤起键盘
        def _fill_by_position():
            try:
                info = self.device.info
                sw, sh = info["displayWidth"], info["displayHeight"]
                # 点击屏幕中间偏下
                self.device.click(int(sw * 0.5), int(sh * 0.55))
                time.sleep(1)
                # 尝试输入
                for i, ans in enumerate(answers):
                    if i > 0:
                        # 多空时按 Tab 键切换
                        self.device.press("tab")
                        time.sleep(0.5)
                    self.device.send_keys(ans)
                    logger.info(f"已填写第{i+1}空: {ans}")
                    time.sleep(0.5)
                return True
            except Exception as e:
                logger.warning(f"坐标填写失败: {e}")
                return False

        result = await asyncio.to_thread(_fill_by_position)
        return result

    async def _fill_by_ocr(self, answers: list) -> bool:
        """用 OCR 定位输入框位置并填写"""
        def _fill():
            if not self._ocr_initialized:
                self._init_ocr()
            if not self._ocr_engine:
                return False

            # 截图
            self._screenshot_count += 1
            ts = datetime.now().strftime("%H%M%S")
            image_path = f"{SCREENSHOT_DIR}/{self._screenshot_count:03d}_{ts}_fill.png"
            try:
                self.device.screenshot(image_path)
            except Exception:
                return False

            # OCR 识别
            try:
                raw_nodes = self._ocr_engine.recognize(image_path)
            except Exception:
                return False

            if not raw_nodes:
                return False

            info = self.device.info
            sw, sh = info["displayWidth"], info["displayHeight"]

            # 策略1：找输入框提示文字（如 "请输入答案" "请填写" 等）
            input_hints = ["请输入", "请填写", "输入答案", "填写答案", "答案"]
            for node in raw_nodes:
                text = node["display"].strip()
                bounds = node.get("bounds")
                if not bounds:
                    continue
                for hint in input_hints:
                    if hint in text:
                        # 点击提示文字位置（输入框通常就在提示文字处或下方）
                        click_x = bounds[0]
                        click_y = bounds[1] + 20  # 稍微偏下
                        logger.info(f"OCR 找到输入提示 '{text[:20]}'，点击 ({click_x},{click_y})")
                        self.device.click(click_x, click_y)
                        time.sleep(1)
                        # 输入答案
                        self.device.send_keys(answers[0])
                        logger.info(f"已填写: {answers[0]}")
                        # 多空处理
                        for i, ans in enumerate(answers[1:], 1):
                            self.device.press("tab")
                            time.sleep(0.5)
                            self.device.send_keys(ans)
                            logger.info(f"已填写第{i+1}空: {ans}")
                        return True

            # 策略2：找空白区域（题目下方的空白行可能是输入框）
            # 找所有文本节点的最下方位置，然后在下方找空白区域
            max_y = 0
            for node in raw_nodes:
                bounds = node.get("bounds")
                if bounds and bounds[1] > max_y:
                    max_y = bounds[1]

            # 在题目文本下方尝试找输入框
            # 填空题的输入框通常在题目下方 50-200px 处
            if max_y > 0:
                for offset in [80, 120, 160, 200]:
                    click_y = max_y + offset
                    if click_y < sh * 0.85:
                        click_x = int(sw * 0.5)
                        logger.info(f"尝试点击题目下方区域 ({click_x},{click_y})")
                        self.device.click(click_x, click_y)
                        time.sleep(1)
                        # 检查是否唤起了输入法
                        if self.device(resourceId="com.android.inputmethod/.InputMethodService"):
                            self.device.send_keys(answers[0])
                            return True
                        # 直接尝试输入
                        try:
                            self.device.send_keys(answers[0])
                            logger.info(f"已填写: {answers[0]}")
                            return True
                        except Exception:
                            continue

            # 策略3：屏幕中间区域逐个尝试
            for y_ratio in [0.5, 0.55, 0.6, 0.65, 0.7]:
                click_x = int(sw * 0.5)
                click_y = int(sh * y_ratio)
                logger.info(f"尝试点击屏幕中间 ({click_x},{click_y})")
                self.device.click(click_x, click_y)
                time.sleep(0.8)
                try:
                    self.device.send_keys(answers[0])
                    logger.info(f"已填写: {answers[0]}")
                    return True
                except Exception:
                    continue

            return False
        return await asyncio.to_thread(_fill)

    async def click_submit(self):
        """点击提交按钮（支持 OCR 检测）"""
        # 策略1：无障碍树
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
        result = await asyncio.to_thread(_submit)
        if result:
            return True

        # 策略2：OCR 检测提交按钮
        logger.info("无障碍树未找到提交按钮，尝试 OCR 检测...")
        result = await self._click_button_by_ocr(["提交", "交卷", "确认提交", "提交试卷", "确定"])
        if result:
            # 尝试点击确认对话框
            await asyncio.sleep(1)
            await asyncio.to_thread(_submit)  # 再试一次确认
            return True
        return False

    async def _click_button_by_ocr(self, target_texts: list) -> bool:
        """用 OCR 检测并点击指定文字的按钮"""
        def _click():
            if not self._ocr_initialized:
                self._init_ocr()
            if not self._ocr_engine:
                return False

            self._screenshot_count += 1
            ts = datetime.now().strftime("%H%M%S")
            image_path = f"{SCREENSHOT_DIR}/{self._screenshot_count:03d}_{ts}_ocr_btn.png"
            try:
                self.device.screenshot(image_path)
            except Exception:
                return False

            try:
                raw_nodes = self._ocr_engine.recognize(image_path)
            except Exception:
                return False

            if not raw_nodes:
                return False

            for node in raw_nodes:
                text = node["display"].strip()
                bounds = node.get("bounds")
                if not bounds:
                    continue
                for target in target_texts:
                    if target in text:
                        logger.info(f"OCR 找到按钮: '{text[:30]}' at {bounds}")
                        self.device.click(*bounds)
                        return True
            return False
        return await asyncio.to_thread(_click)

    async def click_next_question(self) -> bool:
        """点击下一题按钮（支持 ">" 符号和 OCR 检测）"""
        # 策略1：无障碍树按文本查找
        def _next_by_text():
            for t in ["下一题", "下一页", "继续", "下一道", "下一问", "确定",
                       ">", "〉", "›", "→", "❯", "➤", "➜"]:
                btn = self.device(text=t)
                if btn.exists:
                    btn.click()
                    logger.info(f"点击文本按钮: '{t}'")
                    time.sleep(1.5)
                    return True
            for d in ["下一题", "下一页", ">", "下一道"]:
                btn = self.device(description=d)
                if btn.exists:
                    btn.click()
                    logger.info(f"点击描述按钮: '{d}'")
                    time.sleep(1.5)
                    return True
            return False
        result = await asyncio.to_thread(_next_by_text)
        if result:
            return True

        # 策略2：OCR 检测 ">" 和下一题按钮
        logger.info("无障碍树未找到下一题按钮，尝试 OCR 检测...")
        result = await self._click_next_by_ocr()
        if result:
            return True

        # 策略3：点击屏幕右下方区域（">" 通常在右下角）
        def _click_bottom_right():
            try:
                info = self.device.info
                sw, sh = info["displayWidth"], info["displayHeight"]
                # 右下角区域
                x = int(sw * 0.85)
                y = int(sh * 0.85)
                self.device.click(x, y)
                logger.info(f"点击右下角区域: ({x},{y})")
                time.sleep(1.5)
                return True
            except Exception:
                return False
        result = await asyncio.to_thread(_click_bottom_right)
        if result:
            return True

        return False

    async def _click_next_by_ocr(self) -> bool:
        """用 OCR 检测 ">" 和下一题相关按钮"""
        def _click():
            if not self._ocr_initialized:
                self._init_ocr()
            if not self._ocr_engine:
                return False

            # 截图
            self._screenshot_count += 1
            ts = datetime.now().strftime("%H%M%S")
            image_path = f"{SCREENSHOT_DIR}/{self._screenshot_count:03d}_{ts}_ocr_next.png"
            try:
                self.device.screenshot(image_path)
            except Exception:
                return False

            # OCR 识别
            try:
                raw_nodes = self._ocr_engine.recognize(image_path)
            except Exception:
                return False

            if not raw_nodes:
                return False

            logger.debug(f"下一题 OCR 识别到 {len(raw_nodes)} 个文本块:")
            for n in raw_nodes:
                logger.debug(f"  '{n['display'][:40]}' bounds={n.get('bounds_str', '')}")

            # 目标文本：">", "〉", "›", "→", "下一题", "下一页" 等
            next_symbols = [">", "〉", "›", "→", "❯", "➤", "➜", "》", "〉", "›", "﹥", "＞"]
            next_texts = ["下一题", "下一页", "继续", "下一道", "下一问", "确定", "提交", "交卷"]

            # 获取屏幕尺寸
            info = self.device.info
            sw, sh = info["displayWidth"], info["displayHeight"]

            # 策略2a：找 ">" 或箭头符号
            for node in raw_nodes:
                text = node["display"].strip()
                bounds = node.get("bounds")
                if not bounds:
                    continue
                if text in next_symbols:
                    logger.info(f"OCR 找到下一题符号: '{text}' at {bounds}")
                    self.device.click(*bounds)
                    return True

            # 策略2b：找包含 "下一题" 等文字的节点
            for node in raw_nodes:
                text = node["display"].strip()
                bounds = node.get("bounds")
                if not bounds:
                    continue
                for nt in next_texts:
                    if nt in text:
                        logger.info(f"OCR 找到下一题文字: '{text[:30]}' at {bounds}")
                        self.device.click(*bounds)
                        return True

            # 策略2c：找屏幕右下方区域的 ">" 符号
            info = self.device.info
            sw, sh = info["displayWidth"], info["displayHeight"]
            right_area_nodes = []
            for node in raw_nodes:
                text = node["display"].strip()
                bounds = node.get("bounds")
                if not bounds:
                    continue
                # 在屏幕右半部分且下半部分
                if bounds[0] > sw * 0.5 and bounds[1] > sh * 0.5:
                    right_area_nodes.append(node)

            # 在右下角区域找 ">" 类符号
            for node in right_area_nodes:
                text = node["display"].strip()
                if text in next_symbols or len(text) <= 2:
                    bounds = node.get("bounds")
                    logger.info(f"OCR 右下角找到按钮: '{text}' at {bounds}")
                    self.device.click(*bounds)
                    return True

            # 策略2d：右下角最靠右下方的节点
            if right_area_nodes:
                # 按 y 坐标排序，取最靠下的
                right_area_nodes.sort(key=lambda n: n.get("bounds", (0, 0))[1], reverse=True)
                node = right_area_nodes[0]
                bounds = node.get("bounds")
                if bounds:
                    text = node["display"].strip()
                    logger.info(f"OCR 点击右下角最下方节点: '{text[:20]}' at {bounds}")
                    self.device.click(*bounds)
                    return True

            return False
        return await asyncio.to_thread(_click)

    async def click_view_result(self) -> bool:
        """点击查看结果按钮（支持 OCR 检测）"""
        def _click():
            for t in ["查看答案", "查看结果", "查看解析", "继续答题", "知道了"]:
                btn = self.device(text=t)
                if btn.exists:
                    btn.click()
                    logger.info(f"点击: {t}")
                    time.sleep(1)
                    return True
            return False
        result = await asyncio.to_thread(_click)
        if result:
            return True

        # OCR 检测
        result = await self._click_button_by_ocr(
            ["查看答案", "查看结果", "查看解析", "继续答题", "知道了", "确定"]
        )
        return result

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
                    # 填空题：传入所有答案（answer_letters 是答案文本列表）
                    fill_ok = await self.fill_answer(question, result.answer_letters)
                    if not fill_ok:
                        logger.warning("填空题填写失败！等待用户确认...")
                        self.wait_for_user_ready("填空题填写失败，请手动填写后按回车继续")
                else:
                    select_ok = await self.select_answer(question, result.answer_letters)
                    if not select_ok:
                        logger.warning("选项选择失败！等待用户确认...")
                        self.wait_for_user_ready("选项选择失败，请手动选择后按回车继续")
                if result.reasoning:
                    logger.info(f"解析: {result.reasoning[:150]}")
            else:
                logger.warning(f"第 {q_count} 题未获取答案: {result.error}")
                logger.info("跳过此题，继续下一题")

            await asyncio.sleep(self.config.question_delay)

            # 点击下一题 - 多次重试
            next_clicked = False
            for retry in range(3):
                if retry > 0:
                    logger.info(f"重试点击下一题（第 {retry + 1} 次）...")
                    await asyncio.sleep(1)

                if await self.click_next_question():
                    next_clicked = True
                    break

                # 尝试点击"查看结果"等按钮后重试
                if await self.click_view_result():
                    await asyncio.sleep(1)
                    if await self.click_next_question():
                        next_clicked = True
                        break

                # 尝试滑动后重试（按钮可能在屏幕下方）
                if retry < 2:
                    await self.scroll_down()
                    await asyncio.sleep(1)

            if not next_clicked:
                logger.warning("多次重试后仍无法找到下一题按钮")
                # 最后尝试：直接点击底部中央和右下角
                def _last_resort():
                    try:
                        info = self.device.info
                        sw, sh = info["displayWidth"], info["displayHeight"]
                        # 尝试底部中央
                        self.device.click(int(sw * 0.5), int(sh * 0.92))
                        time.sleep(1)
                        # 尝试右下角
                        self.device.click(int(sw * 0.9), int(sh * 0.9))
                        time.sleep(1.5)
                        return True
                    except Exception:
                        return False
                await asyncio.to_thread(_last_resort)
                # 不再 break，继续尝试下一题
                logger.info("已尝试点击底部区域，继续答题...")

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
