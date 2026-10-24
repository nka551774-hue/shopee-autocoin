"""uiautomator 階層 XML 的解析與查詢。"""
from __future__ import annotations

import random
import re
from typing import Iterable, Iterator

from lxml import etree

PKG = "com.shopee.tw"
FREE_TEXT_IDS = ("public_screen_view", "danmaku_view", "scr_pin", "tv_live_title", "tv_anchor_name")
_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


class Node:
    """單一 UI 節點的唯讀包裝。"""

    __slots__ = ("el",)

    def __init__(self, el: etree._Element):
        self.el = el

    @property
    def text(self) -> str:
        return self.el.get("text") or ""

    @property
    def desc(self) -> str:
        return self.el.get("content-desc") or ""

    @property
    def rid(self) -> str:
        """去掉套件前綴的 resource-id；RN 的 testID 本來就沒有前綴，原樣保留。"""
        return (self.el.get("resource-id") or "").split(":id/", 1)[-1]

    @property
    def clickable(self) -> bool:
        return self.el.get("clickable") == "true"

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        m = _BOUNDS.match(self.el.get("bounds") or "")
        return tuple(int(v) for v in m.groups()) if m else (0, 0, 0, 0)

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    @property
    def spot(self) -> tuple[int, int]:
        """隨機點必須留在節點內，包含僅一個像素的節點。"""
        if self.width <= 0 or self.height <= 0:
            raise ValueError("無法點擊沒有面積的 UI 節點")
        cx, cy = self.center
        x1, y1, x2, y2 = self.bounds
        return (random.randint(max(x1, cx - 10), min(x2 - 1, cx + 10)),
                random.randint(max(y1, cy - 10), min(y2 - 1, cy + 10)))

    @property
    def width(self) -> int:
        x1, _, x2, _ = self.bounds
        return x2 - x1

    @property
    def height(self) -> int:
        _, y1, _, y2 = self.bounds
        return y2 - y1

    def iter(self) -> Iterator[Node]:
        """自己與所有子孫節點。"""
        for el in self.el.iter("node"):
            if el.get("package") == PKG:
                yield Node(el)

    def find(self, rid: str) -> Node | None:
        return next((n for n in self.iter() if n.rid == rid), None)

    def texts(self) -> list[str]:
        return [n.text for n in self.iter() if n.text]

    def __repr__(self) -> str:
        return f"<Node {self.rid or '-'} {self.text!r} {self.bounds}>"


class Snapshot:
    """一次 dump 的結果，只保留蝦皮自己的節點。"""

    def __init__(self, xml: str, package: str = "", activity: str = ""):
        self.xml = xml
        self.package = package
        self.activity = activity
        if "<!DOCTYPE" in xml.upper():
            raise ValueError("UI XML 不允許 DTD 或外部實體")
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
        root = etree.fromstring(xml.encode("utf-8"), parser=parser)
        self.nodes = [Node(el) for el in root.iter("node") if el.get("package") == PKG]
        self._ids: dict[str, list[Node]] = {}
        self._texts: dict[str, Node] = {}
        self._descs: dict[str, Node] = {}
        for node in self.nodes:
            self._ids.setdefault(node.rid, []).append(node)
        skip = set()
        for rid in FREE_TEXT_IDS:
            for box in self.by_id(rid):
                skip.update(box.el.iter("node"))
        self.trusted_nodes = [node for node in self.nodes if node.el not in skip]
        for node in self.trusted_nodes:
            self._texts.setdefault(node.text, node)
            self._descs.setdefault(node.desc, node)

    def id(self, rid: str) -> Node | None:
        matches = self._ids.get(rid)
        return matches[0] if matches else None

    def by_id(self, rid: str) -> list[Node]:
        return list(self._ids.get(rid, ()))

    def by_prefix(self, prefix: str) -> list[Node]:
        return [n for n in self.nodes if n.rid.startswith(prefix)]

    def text(self, text: str) -> Node | None:
        return self._texts.get(text)

    def desc(self, desc: str) -> Node | None:
        return self._descs.get(desc)

    def texts_excluding(self, container_ids: Iterable[str]) -> list[str]:
        """全部文字，但略過指定容器底下的（例如聊天室，觀眾可以打任何字）。"""
        skip = set()
        for rid in container_ids:
            for box in self.by_id(rid):
                skip.update(box.el.iter("node"))
        return [n.text for n in self.nodes if n.text and n.el not in skip]
