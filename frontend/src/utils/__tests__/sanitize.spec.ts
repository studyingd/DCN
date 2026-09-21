import { describe, expect, it } from 'vitest'
import { buildWatermarkSvg, renderMarkdown, sanitizeHtml, sanitizeText, xmlEscape } from '../sanitize'

describe('sanitizeHtml', () => {
  it('保留白名单标签与属性', () => {
    const input = '<p class="lead" id="p1">Hello <strong>world</strong> <em>!</em></p>'
    const result = sanitizeHtml(input)
    expect(result).toContain('<p class="lead" id="p1">')
    expect(result).toContain('<strong>world</strong>')
    expect(result).toContain('<em>!</em>')
  })

  it('移除 script 标签及其内容', () => {
    const result = sanitizeHtml('<div>ok</div><script>alert(1)</script>')
    expect(result).toBe('<div>ok</div>')
    expect(result).not.toContain('script')
    expect(result).not.toContain('alert')
  })

  it('移除事件处理器属性', () => {
    expect(sanitizeHtml('<div onclick="alert(1)">x</div>')).toBe('<div>x</div>')
    expect(sanitizeHtml('<a href="/ok" onmouseover="steal()">l</a>')).toBe('<a href="/ok">l</a>')
  })

  it('移除 javascript: 协议的 href', () => {
    const result = sanitizeHtml('<a href="javascript:alert(1)">click</a>')
    expect(result).not.toContain('javascript:')
    expect(result).toContain('>click</a>')
  })

  it('保留安全链接的 href/target/title', () => {
    const result = sanitizeHtml('<a href="https://example.com" target="_blank" title="t">l</a>')
    expect(result).toContain('href="https://example.com"')
    expect(result).toContain('target="_blank"')
    expect(result).toContain('title="t"')
  })

  it('整体移除非白名单标签(iframe/img)', () => {
    expect(sanitizeHtml('<iframe src="https://evil.example"></iframe>')).toBe('')
    expect(sanitizeHtml('<img src="x" onerror="alert(1)">')).toBe('')
  })

  it('移除不在白名单的 style 属性', () => {
    expect(sanitizeHtml('<span style="color:red" id="s1">t</span>')).toBe('<span id="s1">t</span>')
  })
})

describe('sanitizeText', () => {
  it('剥掉全部标签只留纯文本', () => {
    expect(sanitizeText('<p>Hello <em>world</em></p>')).toBe('Hello world')
  })

  it('script 连同内容一起移除', () => {
    expect(sanitizeText('<script>alert(1)</script>safe')).toBe('safe')
    expect(sanitizeText('<img src="x" onerror="alert(1)">text')).toBe('text')
  })

  it('纯文本原样返回', () => {
    expect(sanitizeText('plain text')).toBe('plain text')
  })
})

describe('renderMarkdown', () => {
  it('空字符串返回空串', () => {
    expect(renderMarkdown('')).toBe('')
  })

  it('渲染基础 Markdown 语法', () => {
    expect(renderMarkdown('**bold**')).toContain('<strong>bold</strong>')
    expect(renderMarkdown('# Title')).toContain('<h1>Title</h1>')
    expect(renderMarkdown('`code`')).toContain('<code>code</code>')
    expect(renderMarkdown('~~gone~~')).toContain('<del>gone</del>')
  })

  it('breaks 开启时换行转 <br>', () => {
    expect(renderMarkdown('a\nb')).toContain('a<br>b')
  })

  it('渲染安全链接', () => {
    const result = renderMarkdown('[link](https://example.com)')
    expect(result).toContain('href="https://example.com"')
    expect(result).toContain('>link</a>')
  })

  it('净化 javascript: 链接', () => {
    const result = renderMarkdown('[x](javascript:alert(1))')
    expect(result).not.toContain('javascript:')
    expect(result).not.toContain('alert')
  })

  it('净化内嵌原始 HTML 注入', () => {
    const result = renderMarkdown('Hi <img src="x" onerror="alert(1)">')
    expect(result).not.toContain('<img')
    expect(result).not.toContain('onerror')
    expect(result).toContain('Hi')
  })

  it('净化 script 注入', () => {
    const result = renderMarkdown('<script>alert(1)</script>')
    expect(result).not.toContain('<script')
    expect(result).not.toContain('alert')
  })

  it('渲染 GFM 表格(表格标签在白名单内)', () => {
    const result = renderMarkdown('| a | b |\n| --- | --- |\n| 1 | 2 |')
    expect(result).toContain('<table>')
    expect(result).toContain('<td>1</td>')
  })
})

describe('xmlEscape', () => {
  it('转义 XML 特殊字符', () => {
    expect(xmlEscape('<script>')).toBe('&lt;script&gt;')
    expect(xmlEscape('a & b')).toBe('a &amp; b')
    expect(xmlEscape('"q"')).toBe('&quot;q&quot;')
    expect(xmlEscape("'s'")).toBe('&apos;s&apos;')
  })

  it('组合输入全部转义(& 先处理,不会二次转义)', () => {
    expect(xmlEscape(`<a href="x">&'</a>`)).toBe('&lt;a href=&quot;x&quot;&gt;&amp;&apos;&lt;/a&gt;')
  })

  it('普通文本原样返回', () => {
    expect(xmlEscape('张三 admin')).toBe('张三 admin')
  })
})

describe('buildWatermarkSvg', () => {
  it('返回 encodeURIComponent 编码的 SVG', () => {
    const encoded = buildWatermarkSvg('admin')
    expect(encoded).not.toContain('<svg')
    const svg = decodeURIComponent(encoded)
    expect(svg).toContain('<svg xmlns="http://www.w3.org/2000/svg"')
    expect(svg).toContain('</svg>')
    expect(svg).toContain('>admin</text>')
  })

  it('displayName 中的注入内容被转义', () => {
    const svg = decodeURIComponent(buildWatermarkSvg('<script>alert(1)</script>'))
    expect(svg).toContain('&lt;script&gt;')
    expect(svg).not.toContain('<script')
    expect(svg).not.toContain('alert(1)</script>')
  })

  it('带日期时以两个空格拼接在名字后', () => {
    const svg = decodeURIComponent(buildWatermarkSvg('admin', '2024-01-15'))
    expect(svg).toContain('>admin  2024-01-15</text>')
  })

  it('回归:dateStr 中的注入内容同样被转义', () => {
    const svg = decodeURIComponent(buildWatermarkSvg('admin', '<script>alert(1)</script>'))
    expect(svg).toContain('admin  &lt;script&gt;alert(1)&lt;/script&gt;</text>')
    expect(svg).not.toContain('<script')
  })
})
