"""Conservative GDScript declarations and local call extraction.

This is a lexical fallback, not a type checker. Calls are marked inferred;
comments and string literals are excluded from declaration/call matching.
"""
from __future__ import annotations

import io
import re
import tokenize


def parse_symbols(text: str) -> list[dict]:
    chars = list(text)
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type in (tokenize.STRING, tokenize.COMMENT):
                start = offsets[token.start[0] - 1] + token.start[1]
                end = offsets[token.end[0] - 1] + token.end[1]
                for i in range(start, end):
                    if chars[i] not in '\r\n':
                        chars[i] = ' '
    except (tokenize.TokenError, IndentationError):
        # Incomplete scripts still expose declarations before the error.
        pass
    clean = ''.join(chars).splitlines()
    symbols = []
    classes = []
    for i, line in enumerate(clean):
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        while classes and indent <= classes[-1]['indent']:
            classes.pop()
        declaration = re.match(r'\s*(class_name|class|(?:static\s+)?func)\s+([^\W\d]\w*)', line)
        if not declaration:
            continue
        kind, name = declaration.groups()
        is_class = kind.startswith('class')
        parent = classes[-1]['qualified_name'] if classes else ''
        qualified = '.'.join(filter(None, (parent, name)))
        end = len(clean)
        if kind == 'class_name':
            parent = ''
            qualified = name
        else:
            for j in range(i + 1, len(clean)):
                if clean[j].strip() and len(clean[j]) - len(clean[j].lstrip()) <= indent:
                    end = j
                    break
        body = '\n'.join(clean[i + 1:end])
        symbols.append({'name': name, 'qualified_name': qualified,
                        'label': 'Class' if is_class else 'Method',
                        'line': i + 1, 'end_line': end,
                        'parent': parent, 'body': body,
                        'calls': sorted(set(re.findall(r'\b([^\W\d]\w*)\s*\(', body))) if not is_class else []})
        if kind == 'class':
            classes.append({'qualified_name': qualified, 'indent': indent})
    return symbols
