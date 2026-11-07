"""Micron (NomadNet-Seitenformat) -> sicheres HTML.

Alles Nutzerseitige wird escaped. Unterstützt: Überschriften (>), Trenner (-),
Kommentare (#), Textformatierung (`! `* `_ `` `F `f `B `b), Ausrichtung
(`c `l `r `a), Links (`[Label`ziel`felder]), Eingabefelder, Checkboxen und
Radiobuttons (`<...>), wörtliche Blöcke (`=) und Escape (\\).
"""

import html
import re

_HEX3 = re.compile(r"^[0-9a-fA-F]{3}$")
_HEX6 = re.compile(r"^[0-9a-fA-F]{6}$")
_DIGITS2 = re.compile(r"^[0-9]{2}$")
_FLAGS = re.compile(r"^[!?^]?\d*$")


def _color(spec):
    """Gibt eine CSS-Farbe oder None zurück. spec = Text hinter `F / `B."""
    if _HEX3.match(spec[:3] or ""):
        return "#" + spec[:3], 3
    if spec[:1] == "T" and _HEX6.match(spec[1:7]):
        return "#" + spec[1:7], 7
    if spec[:1] == "g" and _DIGITS2.match(spec[1:3]):
        v = round(int(spec[1:3]) * 255 / 99)
        return "#%02x%02x%02x" % (v, v, v), 3
    return None, 0


class _State:
    def __init__(self):
        self.bold = self.italic = self.underline = False
        self.fg = self.bg = None
        self.align = "left"

    def reset_style(self):
        self.bold = self.italic = self.underline = False
        self.fg = self.bg = None

    def css(self):
        parts = []
        if self.bold:
            parts.append("font-weight:bold")
        if self.italic:
            parts.append("font-style:italic")
        if self.underline:
            parts.append("text-decoration:underline")
        if self.fg:
            parts.append("color:" + self.fg)
        if self.bg:
            parts.append("background:" + self.bg)
        return ";".join(parts)


def _attr(value):
    return html.escape(value, quote=True)


def _render_link(content):
    parts = content.split("`")
    if len(parts) == 1:
        label = dest = parts[0]
        fields = ""
    elif len(parts) == 2:
        label, dest = parts
        fields = ""
    else:
        label, dest, fields = parts[0], parts[1], parts[2]
    label = label or dest
    return (
        '<a href="#" class="mu-link" data-dest="%s" data-fields="%s">%s</a>'
        % (_attr(dest), _attr(fields), html.escape(label))
    )


def _render_field(content):
    spec, _, data = content.partition("`")
    bits = spec.split("|")
    flags = ""
    rest = bits
    if len(bits) > 1 and _FLAGS.match(bits[0]):
        flags, rest = bits[0], bits[1:]
    kind = "text"
    masked = False
    if flags.startswith("?"):
        kind = "checkbox"
    elif flags.startswith("^"):
        kind = "radio"
    elif flags.startswith("!"):
        masked = True
    width = re.sub(r"\D", "", flags) or "24"
    name = rest[0] if rest else ""
    if kind in ("checkbox", "radio"):
        value = rest[1] if len(rest) > 1 else "1"
        checked = len(rest) > 2 and rest[2] == "*"
        return (
            '<label class="mu-opt"><input class="mf" type="%s" data-name="%s" '
            'value="%s"%s> %s</label>'
            % (kind, _attr(name), _attr(value), " checked" if checked else "", html.escape(data))
        )
    return (
        '<input class="mf" type="%s" data-name="%s" value="%s" size="%d">'
        % ("password" if masked else "text", _attr(name), _attr(data), min(int(width), 80))
    )


def _inline(text, state):
    """Rendert eine Zeile. Gibt (html, zeilen_ausrichtung) zurück."""
    out = []
    buf = []
    line_align = state.align
    align_set = False
    span_open = False

    def flush():
        nonlocal span_open
        if not buf:
            return
        txt = html.escape("".join(buf))
        buf.clear()
        css = state.css()
        out.append('<span style="%s">%s</span>' % (css, txt) if css else txt)

    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            buf.append(text[i + 1])
            i += 2
            continue
        if ch == "`" and i + 1 >= n:
            i += 1  # einzelner Backtick am Zeilenende: wie NomadNet verwerfen
            continue
        if ch != "`":
            buf.append(ch)
            i += 1
            continue
        cmd = text[i + 1]
        i += 2
        if cmd == "`":
            flush()
            state.reset_style()
        elif cmd in "!*_":
            flush()
            attr = {"!": "bold", "*": "italic", "_": "underline"}[cmd]
            setattr(state, attr, not getattr(state, attr))
        elif cmd in "FB":
            flush()
            color, used = _color(text[i:])
            if color:
                setattr(state, "fg" if cmd == "F" else "bg", color)
                i += used
        elif cmd in "fb":
            flush()
            setattr(state, "fg" if cmd == "f" else "bg", None)
        elif cmd in "clra":
            new = {"c": "center", "l": "left", "r": "right", "a": "left"}[cmd]
            state.align = new
            if not align_set:
                line_align = new
                align_set = True
        elif cmd == "[":
            end = text.find("]", i)
            if end == -1:
                buf.append("`[")
                continue
            flush()
            out.append(_render_link(text[i:end]))
            i = end + 1
        elif cmd == "<":
            end = text.find(">", i)
            if end == -1:
                buf.append("`<")
                continue
            flush()
            out.append(_render_field(text[i:end]))
            i = end + 1
        elif cmd == "{":
            end = text.find("}", i)
            i = n if end == -1 else end + 1
        # unbekannte Befehle werden wie in NomadNet still verworfen
    flush()
    return "".join(out), line_align


def render(source):
    state = _State()
    depth = 0
    literal = False
    rows = []
    for raw in source.replace("\r\n", "\n").split("\n"):
        if raw.strip() == "`=":
            literal = not literal
            continue
        if literal:
            rows.append('<div class="mu-line">%s</div>' % (html.escape(raw) or "&nbsp;"))
            continue
        line = raw
        if line.startswith("#"):
            continue
        if line.startswith("<"):
            depth = 0
            line = line[1:]
        if line.startswith(">"):
            level = len(line) - len(line.lstrip(">"))
            depth = level
            body, align = _inline(line[level:], state)
            if body:
                rows.append(
                    '<div class="mu-h mu-h%d" style="margin-left:%.1fch;text-align:%s">%s</div>'
                    % (min(level, 3), (level - 1) * 1.5, align, body)
                )
            continue
        if line.startswith("-"):
            rows.append('<hr class="mu-hr">')
            continue
        body, align = _inline(line, state)
        rows.append(
            '<div class="mu-line" style="margin-left:%.1fch;text-align:%s">%s</div>'
            % (depth * 1.5, align, body or "&nbsp;")
        )
    return "\n".join(rows)
