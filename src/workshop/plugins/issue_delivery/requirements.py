"""依据的格式与需求编号；文档由交付对象规则登记，不依赖仓库名字。"""
from pathlib import PurePosixPath
import re


def section(body, heading):
    match = re.search(r"^## " + re.escape(heading) + r"[ \t]*\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    return match[1].strip() if match else ""


def registered_paths(target):
    paths = target.get("requirement_docs")
    if not isinstance(paths, list) or not paths:
        raise ValueError("依据：规则未登记 requirement_docs 需求文档")
    for path in paths:
        if (not isinstance(path, str) or not path.strip() or path.startswith("/")
                or any(part in (".", "..", "") for part in path.rstrip("/").split("/"))):
            raise ValueError("依据：requirement_docs 须为仓库内相对路径，目录以 / 结尾")
    return paths


def registered(path, target):
    if path.startswith("/") or any(part in (".", "..", "") for part in path.split("/")):
        return False
    return any(path.startswith(entry) if entry.endswith("/") else path == entry
               for entry in registered_paths(target))


def changed_requirement(basis):
    # 先切段，避免冒号后的空白匹配跨过空行，把后面的引用也算成变更。
    for paragraph in re.split(r"\r?\n[ \t]*\r?\n", basis):
        match = re.search(r"需求变更\s*[：:]\s*(.*)", paragraph, re.S)
        if match:
            return match[1].strip()
    return None


def change_documents(basis, documents, target):
    """以文件全路径或无歧义的文件名指明要改哪份文档；目录登记允许新增文档。
    只看「需求变更：」所在段落中冒号后的文字，其他段落的引用不算。"""
    change = changed_requirement(basis)
    if change is None:
        change = basis
    paths = set(re.findall(r"[\w./\-]+\.md", change))
    selected = {p for p in paths if registered(p, target)}
    for path in documents:
        stem = PurePosixPath(path).stem
        if re.search(r"(?<![\w])" + re.escape(stem) + r"(?![\w])", change):
            matches = [p for p in documents if PurePosixPath(p).stem == stem]
            if len(matches) > 1 and not selected.intersection(matches):
                raise ValueError(f"依据：需求文档名 {stem} 不唯一，请写完整路径")
            if len(matches) == 1:
                selected.add(path)
    if not selected:
        raise ValueError("依据：需求变更须写明登记的需求文档位置和需求原文")
    return selected


def updated_documents(files, target):
    """只认登记文档的新增、修改；重命名只认有内容修改的新路径。"""
    return {f["filename"] for f in files
            if (f.get("status") in ("added", "modified")
                or (f.get("status") == "renamed" and f.get("changes", 0) > 0))
            and registered(f["filename"], target)}


def missing_documents(basis, documents, target, files, state):
    """当前 PR 和这件活已合入的记录共同满足文档要求。"""
    return (change_documents(basis, documents, target)
            - updated_documents(files, target) - state.get("requirement_changes", {}).keys())


def check_references(basis, documents):
    text = basis.replace("`", "").replace("**", "")
    refs = list(re.finditer(r"(?<![A-Za-z0-9])([RC]\d+)(?![A-Za-z0-9])", text))
    if not refs:
        raise ValueError("依据：须列出需求文档及 R/C 编号，或写明需求变更")
    for ref in refs:
        prefix = re.search(r"([^\s,，;；、：:（）()]+)\s+$", text[:ref.start()])
        name = prefix[1] if prefix else ""
        matches = [p for p in documents if name in (p, PurePosixPath(p).stem, PurePosixPath(p).name)]
        if len(matches) != 1:
            raise ValueError(f"依据：{name} {ref[1]} 的需求文档不存在或不唯一")
        numbers = re.findall(r"^\*\*([RC]\d+)(?=\s|\*\*|[：:])", documents[matches[0]], re.M)
        if ref[1] not in numbers:
            raise ValueError(f"依据：{name} {ref[1]} 在需求文档中找不到")

