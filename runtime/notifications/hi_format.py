"""Small, conservative Hi Markdown layouts; no network calls or content inference."""
import re
from urllib.parse import urlsplit

# A local layout budget, not a claim about Hi's official message-size limit.
BODY_BYTES = 2400
LINK = r'\[(?:\\.|[^\]\\\n])+\]\((?:[^()\s]|\([^()\s]*\))*\)'
LINK_RE = re.compile(LINK)
STATUS = {'done': ('✅', '处理完成'), 'working': ('⏳', '正在处理'),
          'error': ('⚠️', '需要处理'), 'info': ('ℹ️', '服务状态')}


def _url_label(url):
    try:
        host = urlsplit(url).hostname or '链接'
    except ValueError:
        return '打开链接'
    if host == 'hi.xiaohongshu.com' and '/clouddrive/' in url:
        return '打开 Hi 网盘文件'
    if host == 'docs.xiaohongshu.com':
        return '打开 REDoc 文档'
    return '打开链接 · ' + host


def _inline(text):
    # Keep valid named links intact, including their query strings and fragments.
    def plain(part):
        part = re.sub(r'`([^`\n]+)`', r'\1', part)
        def shorten(match):
            url = match.group(0)
            tail = ''
            while url and (url[-1] in '。，；！？、）】' or (url[-1] == ')' and url.count(')') > url.count('('))):
                tail = url[-1] + tail; url = url[:-1]
            safe = url.replace('(', '%28').replace(')', '%29')
            return f'[{_url_label(url)}]({safe})' + tail
        return re.sub(r'https?://[^\s<>]+', shorten, part)
    text = re.sub(r'\[([^\]\n]+)\]\(((?:/|file://|sandbox:)[^)\n]+)\)', r'\1（本机：\2）', text)
    out, start = [], 0
    for match in LINK_RE.finditer(text):
        out.append(plain(text[start:match.start()])); out.append(match.group())
        start = match.end()
    out.append(plain(text[start:]))
    return ''.join(out)


def _cells(line):
    line = line.strip()
    if line.startswith('|'): line = line[1:]
    if line.endswith('|') and not line.endswith('\\|'): line = line[:-1]
    result, cell, i, in_code = [], [], 0, False
    while i < len(line):
        link = LINK_RE.match(line, i)
        if link:
            cell.append(link.group()); i = link.end(); continue
        ch = line[i]
        if ch == '\\' and i+1 < len(line) and line[i+1] == '|':
            cell.append('|'); i += 2; continue
        if ch == '`': in_code = not in_code
        if ch == '|' and not in_code:
            result.append(''.join(cell).strip()); cell = []
        else: cell.append(ch)
        i += 1
    result.append(''.join(cell).strip())
    return result


def normalize_body(text):
    lines = text.replace('\r\n', '\n').strip().split('\n')
    out, i, fence = [], 0, None
    while i < len(lines):
        line = lines[i]
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            if fence is None: fence = marker.group(1)[0]
            elif marker.group(1)[0] == fence: fence = None
            out.append(line); i += 1; continue
        if fence:
            out.append(line); i += 1; continue
        # Raw markup is code, not chat Markdown. Never rewrite xmlns URLs or
        # split tags into messages that Hi will render as empty HTML.
        markup = re.match(r'^\s*(?:<\?xml[^>]*>\s*)?<(svg|html)\b', line, re.I)
        if markup:
            code = []
            closing = re.compile(r'</' + markup.group(1) + r'\s*>', re.I)
            while i < len(lines):
                code.append(lines[i]); i += 1
                if closing.search(code[-1]):
                    break
            out.append('```xml\n' + '\n'.join(code) + '\n```')
            continue
        if '|' in line and i+1 < len(lines):
            headers, rule = _cells(line), _cells(lines[i+1])
            if len(headers) > 1 and len(headers) == len(rule) and all(re.fullmatch(r':?-{3,}:?', c) for c in rule):
                rows, j = [], i+2
                while j < len(lines) and '|' in lines[j] and lines[j].strip():
                    cells = _cells(lines[j])
                    if len(cells) != len(headers): break
                    rows.append(cells); j += 1
                if rows:
                    out.append('')
                    for row in rows:
                        # Keep column labels and empty values explicit; do not infer units.
                        label = _inline(headers[0]).replace('**', '')
                        value = _inline(row[0]).replace('**', '') or '（空）'
                        out.append(f'**{label}：{value}**')
                        out.extend(f'• {_inline(h)}：{_inline(v) or "（空）"}' for h,v in zip(headers[1:],row[1:]))
                        out.append('')
                    i = j; continue
        line = re.sub(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*$', r'**\1**', line)
        line = re.sub(r'^\s*[-*+]\s+', '• ', line)
        out.append(_inline(line)); i += 1
    return '\n'.join(out).strip()


def _blocks(text):
    block, fence = [], None
    for line in text.split('\n'):
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            if fence is None: fence = marker.group(1)[0]
            elif marker.group(1)[0] == fence: fence = None
        if not line.strip() and fence is None:
            if block: yield '\n'.join(block); block = []
        else: block.append(line)
    if block: yield '\n'.join(block)


def _split_body(text):
    chunks, current, omitted = [], '', False
    for block in _blocks(text):
        parts = [block]
        if len(block.encode()) > BODY_BYTES:
            if re.match(r'^\s*(`{3,}|~{3,})', block):
                parts = ['代码较长，完整内容见本机任务记录。']; omitted = True
            else:
                # Links, bold spans and inline code are indivisible tokens.
                tokens = re.findall(LINK + r'|\*\*[^\n]*?\*\*|`[^`\n]*`|[\s\S]', block)
                parts, part = [], ''
                for token in tokens:
                    if len(token.encode()) > BODY_BYTES:
                        token = '（该项过长，完整内容见本机任务记录）'; omitted = True
                    if len((part+token).encode()) > BODY_BYTES:
                        parts.append(part); part = ''
                    part += token
                if part: parts.append(part)
        for part in parts:
            if current and len((current+'\n\n'+part).encode()) > BODY_BYTES:
                chunks.append(current); current = ''
            current = (current+'\n\n'+part).strip()
    if current: chunks.append(current)
    return chunks or ['执行完成。'], omitted


def format_reply(text, status='done', title=None, detail_hint=None):
    """Return complete messages. Caller must persist all parts atomically."""
    icon, default_title = STATUS[status]
    body = normalize_body(text)
    explicit_title = re.fullmatch(r'(?:\*\*.+\*\*|#{1,6}\s+.+)', text.strip().split('\n')[0])
    if title is None and explicit_title:
        first, _, rest = body.partition('\n')
        match = re.fullmatch(r'\*\*([^*\n]{1,40})\*\*', first)
        if match:
            title = match.group(1); body = rest.strip()
    title = re.sub(r'[\r\n*`\[\]]', '', title or default_title)[:48]
    chunks, omitted = _split_body(body)
    if omitted and detail_hint:
        chunks.append('完整记录（本机）\n'+detail_hint)
    total = len(chunks)
    return [f'{icon} **{title}**' + (f' · {i}/{total}' if total>1 else '') + '\n\n'+chunk
            for i,chunk in enumerate(chunks,1)]
