"""Deliver complete SVG output as an unchanged file, not rendered chat markup."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
import zipfile


def svg_source(text):
    value = text.strip()
    fenced = re.fullmatch(r'```(?:svg|xml|html)?\s*\n([\s\S]*?)\n```', value, re.I)
    if fenced:
        value = fenced.group(1).strip()
    if not re.match(r'(?:<\?xml[^>]*>\s*)?<svg\b', value, re.I):
        return None
    if len(value.encode()) > 4 * 1024 * 1024 or re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', value, re.I):
        raise ValueError('SVG 文件超限或包含不支持的声明')
    root = ET.fromstring(value)
    if root.tag not in ('svg', '{http://www.w3.org/2000/svg}svg'):
        raise ValueError('SVG 根节点无效')
    title = next((''.join(node.itertext()) for node in root if node.tag.split('}')[-1] == 'title'), 'SVG图形')
    title = re.sub(r'[^\w\u4e00-\u9fff -]', '', title).strip()[:48] or 'SVG图形'
    return value, title


def _save(path, data):
    stage = path.with_suffix('.tmp')
    stage.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
    stage.chmod(0o600)
    stage.replace(path)


def deliver_svg(text, run, workspace, hi, folder):
    """Return chat-safe delivery text; keep original output untouched in result.txt.

    Upload receipt and operation code survive retries. Never invent a link or
    retry an uncertain upload with a fresh operation code.
    """
    if not re.match(r'\s*(?:```(?:svg|xml|html)?\s*\n)?(?:<\?xml[^>]*>\s*)?<svg\b', text, re.I):
        return text
    run, workspace = Path(run), Path(workspace)
    manifest_path = run/'reply-artifact.json'
    record = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    def cli(*args):
        result = subprocess.run([hi, *args], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError('Hi 网盘操作失败')
        data = json.loads(result.stdout, parse_int=str)
        if data.get('success') is False:
            raise RuntimeError('Hi 网盘操作未成功')
        return data

    try:
        source, title = svg_source(text)
        digest = hashlib.sha256(source.encode()).hexdigest()
        if record.get('sha256') and record['sha256'] != digest:
            raise ValueError('原输出已变化，不能复用旧上传记录')
        path = workspace/'Hi绘图'/f'{title}-{run.name[:8]}.svg'
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text() != source:
            raise ValueError('已有文件内容不同，停止覆盖')
        path.write_text(source)
        # Hi pan currently rejects direct SVG uploads with HTTP 415. Preserve
        # the exact SVG in a ZIP instead of converting or modifying its source.
        upload_path = path.with_suffix('.zip')
        if upload_path.exists():
            with zipfile.ZipFile(upload_path) as archive:
                if archive.namelist() != [path.name] or archive.read(path.name) != source.encode():
                    raise ValueError('已有源码压缩包内容不同，停止覆盖')
        else:
            with zipfile.ZipFile(upload_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(path, arcname=path.name)
        record.update(path=str(path), sha256=digest, bytes=len(source.encode()), folder=folder,
                      upload_path=str(upload_path), upload_bytes=upload_path.stat().st_size, upload_format='zip')
        _save(manifest_path, record)
        if not isinstance(folder, str) or not folder.strip():
            raise ValueError('未配置已授权的 SVG 上传目录')
        if not record.get('operate_code'):
            operation = cli('utils:generate-operate-code').get('operateCode')
            if not isinstance(operation, str) or not operation:
                raise ValueError('未取得上传操作码')
            record['operate_code'] = operation
            _save(manifest_path, record)
        if not record.get('receipt'):
            receipt = cli('pan:write', '--file-path', str(upload_path), '--folder-path', folder,
                          '--operate-code', record['operate_code'])
            if not all(receipt.get(key) for key in ('entryId', 'shareUrl', 'fileName')):
                raise ValueError('未取得完整上传回执')
            record['receipt'] = receipt
            _save(manifest_path, record)
        receipt = record['receipt']
        meta = cli('pan:readmeta', '--entry-id', receipt['entryId'])
        if (meta.get('status') != 'ACTIVE' or int(meta.get('fileSize') or -1) != record['upload_bytes']
                or meta.get('fileName') != receipt['fileName'] or meta.get('entryId') != receipt['entryId']):
            raise ValueError('上传元数据核验失败')
        url = receipt['shareUrl']
        if urlsplit(url).scheme != 'https' or any(c.isspace() for c in url):
            raise ValueError('分享地址无效')
        record.update(verified=True, error=None, metadata=meta)
        _save(manifest_path, record)
        url = url.replace('(', '%28').replace(')', '%29')
        return f'**{title}**\n\n[{title}.zip · 下载完整 SVG]({url})\n\n压缩包内是原始 .svg 文件，解压后可用浏览器打开。'
    except Exception as exc:
        record.update(verified=False, error=str(exc))
        _save(manifest_path, record)
        local = record.get('path') or str(run/'result.txt')
        try:
            local = str(Path(local).relative_to(workspace))
        except ValueError:
            pass
        return f'SVG 文件尚未完成分享，暂时没有可用下载链接。\n\n完整内容已保存在本机：{local}。请在本机检查文件交付记录后重试。'
