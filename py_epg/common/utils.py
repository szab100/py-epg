#!/usr/bin/env python3

def clean_text(elem):
    text = ''
    for e in elem.descendants:
        if isinstance(e, str):
            text += e.strip()
        elif e.name == 'br':
            text += '\n'
    return text
