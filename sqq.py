#!/usr/bin/env python3

import argparse
import os
import random
import re
import select
import shlex
import signal
import socket
import subprocess
import sys
import termios
import time
import tty

DIM = "\033[2m"
RED = "\033[2m"
RESET = "\033[0m"
CLEAR_LINE = "\033[2K\r"
USER_HOST_COLOR = "\033[92;22m"
CONT_PROMPT_COLOR = "\033[90;22m"
CONFIRM_COLOR = "\033[93;5;208;5m"

DECOR_ENABLED = False
DECOR_CHARS = "01"
DECOR_LENGTH_PERCENT = 40
DECOR_UPDATE_INTERVAL = 1.0
DECOR_COLOR = "\033[38;5;22m"
DECOR_SPACE_PROB_START = 0.45
DECOR_SPACE_PROB_END = 0.95

CTRL_JUMP_CHARS = 15

BRACKETED_PASTE_START = "\033[?2004h"
BRACKETED_PASTE_END = "\033[?2004l"
PASTE_SEQ_START = "[200~"
PASTE_SEQ_END = "[201~"

_CSI_U_ENTER_RE = re.compile(r"^\[13(;\d+)?u$")
_MODIFY_OTHER_KEYS_ENTER_RE = re.compile(r"^\[27;\d+;13~$")

_raw_mode_active = False
_cooked_settings = None
_current_child = None


def _sigtstp_handler(signum, frame):
    global _raw_mode_active

    if _current_child is not None and _current_child.poll() is None:
        try:
            _current_child.terminate()  # SIGTERM
        except Exception:
            pass
        return

    was_raw = _raw_mode_active
    fd = sys.stdin.fileno()
    if _cooked_settings is not None:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, _cooked_settings)
        except Exception:
            pass
    sys.stdout.flush()
    signal.signal(signal.SIGTSTP, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTSTP)
    signal.signal(signal.SIGTSTP, _sigtstp_handler)
    if was_raw:
        try:
            tty.setraw(fd)
        except Exception:
            pass


SEP_CHARS = ";|&\n"
WORD_BREAK = " " + SEP_CHARS

AC_DIR = "/tmp/shellqq"
AC_FILE = os.path.join(AC_DIR, "ac.txt")
SQQ_SAVE_FILE = os.path.join(os.path.expanduser("~"), ".sqq-ac.txt")

AUTOCOMPLETE_SOURCES = (AC_FILE, SQQ_SAVE_FILE)


def _encode_history_entry(cmd: str) -> str:
    return cmd.replace("\\", "\\\\").replace("\n", "\\n")


def _decode_history_entry(line: str) -> str:
    out = []
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == "\\" and i + 1 < n:
            nxt = line[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


def load_persisted_history() -> list:
    lines = []
    for path in AUTOCOMPLETE_SOURCES:
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines.extend(
                    _decode_history_entry(line.rstrip("\n"))
                    for line in f
                    if line.strip()
                )
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return lines


def persist_history_entry(cmd: str):
    try:
        os.makedirs(AC_DIR, exist_ok=True)
        with open(AC_FILE, "a", encoding="utf-8") as f:
            f.write(_encode_history_entry(cmd) + "\n")
    except OSError:
        pass


def sqq_save():
    try:
        with open(AC_FILE, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        content = ""
    except OSError as e:
        print(f"sqq-save: {e}")
        return

    if not content:
        print("sqq-save: nothing to save for autocomplete yet, write something")
        return

    try:
        with open(SQQ_SAVE_FILE, "a", encoding="utf-8") as f:
            f.write(content)
        print(f"sqq-save: saved in {SQQ_SAVE_FILE}")
    except OSError as e:
        print(f"sqq-save: {e}")


def load_bashrc_aliases() -> dict:
    path = os.path.expanduser("~/.bashrc")
    aliases = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if not stripped.startswith("alias "):
                    continue
                rest = stripped[len("alias ") :].strip()
                if "=" not in rest:
                    continue
                name, _, value = rest.partition("=")
                name = name.strip()
                value = value.strip()
                if not name.isidentifier():
                    continue
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                aliases[name] = value
    except OSError:
        pass
    return aliases


_SEP_RUN_RE = re.compile(r"([;&|\n]+)")
_SEGMENT_SPLIT_RE = re.compile(r"[;&|\n]+")


def expand_aliases(cmd: str, aliases: dict) -> str:
    if not aliases:
        return cmd
    parts = _SEP_RUN_RE.split(cmd)
    out = []
    for part in parts:
        if re.fullmatch(r"[;&|\n]+", part or ""):
            out.append(part)
            continue
        leading_ws = part[: len(part) - len(part.lstrip())]
        stripped = part.lstrip()
        if not stripped:
            out.append(part)
            continue
        tokens = stripped.split(" ", 1)
        first = tokens[0]
        if first in aliases:
            rest = tokens[1] if len(tokens) > 1 else ""
            replaced = aliases[first] + ((" " + rest) if rest else "")
            part = leading_ws + replaced
        out.append(part)
    return "".join(out)


# accidental russian layout fix
_RU_ROW1 = "йцукенгшщзхъ"
_EN_ROW1 = "qwertyuiop[]"
_RU_ROW2 = "фывапролджэ"
_EN_ROW2 = "asdfghjkl;'"
_RU_ROW3 = "ячсмитьбю."
_EN_ROW3 = "zxcvbnm,./"
CYR_TO_LAT = {}
for ru_row, en_row in (
    (_RU_ROW1, _EN_ROW1),
    (_RU_ROW2, _EN_ROW2),
    (_RU_ROW3, _EN_ROW3),
):
    for ru_ch, en_ch in zip(ru_row, en_row):
        CYR_TO_LAT[ru_ch] = en_ch
        CYR_TO_LAT[ru_ch.upper()] = en_ch.upper()
CYR_TO_LAT["ё"] = "`"
CYR_TO_LAT["Ё"] = "~"


def transliterate_layout(s: str) -> str:
    return "".join(CYR_TO_LAT.get(c, c) for c in s)


def has_latin(s: str) -> bool:
    return any("a" <= c.lower() <= "z" for c in s)


def looks_cyrillic(s: str) -> bool:
    return any(("а" <= c.lower() <= "я") or c.lower() in ("ё",) for c in s)


def looks_like_wrong_layout(s: str) -> bool:
    return looks_cyrillic(s) and not has_latin(s)


DANGEROUS_FIRST_WORDS = {
    "rm",
    "dd",
    "mkfs",
    "shutdown",
    "reboot",
    "poweroff",
    "halt",
    "kill",
    "killall",
    "mv",
    "chmod",
    "chown",
    "pkill",
}


CONFIRM_COMMANDS = {
    "rm -rf /",
    "rm -rf /*",
    "rm -rf /~",
    "rm -rf /home",
    "rm -rf /home/",
    ":(){ :|: & };:",
    "dd if=/dev/zero of=/dev/sda",
    "dd if=/dev/zero of=/dev/sda1",
    "mkfs.ext4 /dev/sda",
    "mkfs.ext4 /dev/sda1",
    "dd if=/dev/zero nvme0n1",
    "dd if=/dev/zero nvme0n1p1",
    "yandex",
    "chmod -R 000 /",
}


ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def visible_len(s: str) -> int:
    return len(ANSI_RE.sub("", s))


def gen_decoration(max_len: int) -> str:
    if not DECOR_ENABLED or max_len <= 0:
        return ""
    out = []
    denom = max(1, max_len - 1)
    for i in range(max_len):
        ratio = i / denom
        space_prob = (
            DECOR_SPACE_PROB_START
            + (DECOR_SPACE_PROB_END - DECOR_SPACE_PROB_START) * ratio
        )
        if random.random() < space_prob:
            out.append(" ")
            continue
        ch = random.choice(DECOR_CHARS)
        out.append(f"{DECOR_COLOR}{ch}{RESET}")
    return "".join(out)


def _term_width(fd) -> int:
    try:
        return os.get_terminal_size(fd).columns or 80
    except OSError:
        return 80


_decor_cache = ""
_decor_cache_time = 0.0
_decor_cache_len = -1


def _get_decoration(decor_len: int) -> str:
    global _decor_cache, _decor_cache_time, _decor_cache_len
    now = time.time()
    stale = (now - _decor_cache_time) >= DECOR_UPDATE_INTERVAL
    if stale or decor_len != _decor_cache_len:
        _decor_cache = gen_decoration(decor_len)
        _decor_cache_time = now
        _decor_cache_len = decor_len
    return _decor_cache


def build_prompt_display(fd=None) -> str:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "user"
    host = socket.gethostname()
    cwd = os.getcwd()
    home = os.path.expanduser("~")
    if cwd == home:
        cwd = "~"
    else:
        cwd = os.path.basename(cwd) + "/"

    venv = os.environ.get("VIRTUAL_ENV")
    venv_tag = f"({os.path.basename(venv)}) " if venv else ""

    base = f"{venv_tag}[{user}@{host} {cwd}]"
    tail = "$ "
    return f"{USER_HOST_COLOR}{base}{tail}{RESET}"


def build_prompt_plain() -> str:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "user"
    host = socket.gethostname()
    cwd = os.getcwd()
    home = os.path.expanduser("~")
    if cwd == home:
        cwd = "~"
    else:
        cwd = os.path.basename(cwd) + "/"
    return f"<{user}@{host}> {cwd} $ "



def build_cont_prompt() -> str:
    return f"{CONT_PROMPT_COLOR}...> {RESET}"


def strip_trailing_dangling_quote(cmd: str) -> str:
    if not cmd:
        return cmd
    last = cmd[-1]
    if last in ("'", '"', "`", "\\", "|"):
        attached = len(cmd) == 1 or cmd[-2] != " "
        odd = cmd.count(last) % 2 == 1
        if attached and odd:
            return cmd[:-1]
    return cmd


def quotes_balanced(cmd: str) -> bool:
    state = None  # None | "'" | '"'
    i = 0
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if state is None:
            if c == "'":
                state = "'"
            elif c == '"':
                state = '"'
            elif c == "\\":
                i += 1
        elif state == "'":
            if c == "'":
                state = None
        elif state == '"':
            if c == '"':
                state = None
            elif c == "\\":
                i += 1
        i += 1
    return state is None

_venv_stack = []


def handle_venv(cmd: str) -> bool:
    stripped = cmd.strip()

    if stripped == "deactivate":
        if not _venv_stack:
            return True
        old_path, old_virtual_env = _venv_stack.pop()
        os.environ["PATH"] = old_path
        if old_virtual_env is None:
            os.environ.pop("VIRTUAL_ENV", None)
        else:
            os.environ["VIRTUAL_ENV"] = old_virtual_env
        return True

    m = re.match(r"^(?:source|\.)\s+(.+?/bin/activate)\s*$", stripped)
    if not m:
        return False

    activate_path = os.path.expanduser(m.group(1))
    if not os.path.isfile(activate_path):
        print(f"sqq: venv activate script not found: {activate_path}")
        return True

    venv_dir = os.path.dirname(os.path.dirname(activate_path))  # .../bin/activate -> venv_dir
    bin_dir = os.path.join(venv_dir, "bin")

    _venv_stack.append((os.environ.get("PATH", ""), os.environ.get("VIRTUAL_ENV")))
    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    os.environ["VIRTUAL_ENV"] = venv_dir
    os.environ.pop("PYTHONHOME", None)
    return True

def handle_alias(cmd: str, aliases: dict) -> bool:
    stripped = cmd.strip()
    if stripped != "alias" and not stripped.startswith("alias "):
        return False

    rest = stripped[len("alias"):].strip()
    if not rest:
        for name, value in aliases.items():
            print(f"alias {name}='{value}'")
        return True

    if "=" not in rest:
        return True

    name, _, value = rest.partition("=")
    name = name.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]

    if name and name[0].isdigit():
        print("No aliases starting with numbers are allowed in bash.")
        return True

    if name.isidentifier():
        aliases[name] = value
    return True


def handle_source_bashrc(cmd: str, aliases: dict) -> bool:
    stripped = cmd.strip()
    m = re.match(r"^(?:source|\.)\s+(.+?)\s*$", stripped)
    if not m:
        return False

    target = os.path.expanduser(m.group(1))
    bashrc = os.path.expanduser("~/.bashrc")
    if os.path.abspath(target) != os.path.abspath(bashrc):
        return False

    aliases.update(load_bashrc_aliases())
    return True

def handle_cd(cmd: str) -> bool:
    parts = cmd.split(maxsplit=1)
    if not parts or parts[0] != "cd":
        return False
    raw = parts[1].strip() if len(parts) > 1 else ""
    if not raw:
        target = "~"
        print("No arguments, going to ~/. You were in: " + os.getcwd())
    else:
        try:
            tokens = shlex.split(raw, posix=True)
        except ValueError:
            tokens = [raw]
        target = tokens[0] if tokens else raw
    target = os.path.expanduser(target)
    try:
        os.chdir(target)
    except Exception as e:
        print(f"cd: {e}")
    return True


def segment_start(s: str) -> int:
    i = len(s)
    while i > 0 and s[i - 1] not in SEP_CHARS:
        i -= 1
    return i


def token_start(s: str) -> int:
    i = len(s)
    while i > 0 and s[i - 1] not in WORD_BREAK:
        i -= 1
    return i


HARDCODED_HINTS = {
    "rm": "rm: check the path twice",
    "dd": "dd:check the path twice",
    "mkfs": "mkfs: check the path twice",
    "shutdown": "shutdown: poweroff is better",
    "chmod": "chmod: alternate the file properties",
    "chown": "chown: alternate the file owner",
    "mount": "mount: check the path twice",
    "umount": "umount: check the path twice",
    "fdisk": "fdisk: partition table editor, make sure to edit right thing",
    "parted": "parted: partition table editor, make sure to edit right thing",
    "mkswap": "mkswap: check the path twice",
    "userdel": "userdel: check the user twice",
    "passwd": "passwd: changes a password, don't forget to use right layout",
    "visudo": "visudo: edits sudoers, syntax errors can lock out sudo",
    ">": "'>': don't confuse with '>>', cost file's life",
    "chrome": "chrome: hope you use firefox. Right?",
    "chromium": "chromium: hope you use firefox. Right?",
    "vivaldi": "vivaldi: hope you use firefox. Right?",
    "yandex": "yandex: трояндекс",
    "systemctl": "systemctl: >50 critical vulnerabilities in systemd, isn't 'runit' better",
    "systemd": "systemd: >50 critical vulnerabilities, isn't 'runit' better",
    "sway": "sway: nice choice",
    "sqq": "sqq: sqq inside sqq?",
    "nano": "nano: isn't 'micro' better?",
    "meow": "meow: mrmyau^^",
    "fuck": "fuck: that's just a computer, don't care",
    "docker": "docker: seems to be a smart thing",
    "vs-code": "vs-code: better try Zed",
    "vscode": "vscode: better try Zed",
    "vim": "you know how to exit?",
    "linux": "Linus: Torvalds",
}

HARDCODED_AUTOCOMPLETE = [
    "sqq-save",
    "exit",
    "quit",
    "help",
    "help -ru",
    "cd",
]

HELP_TEXT_EN = """
sqq — a wrapper for bash with autocomplete, history, and suggestions. 'help -ru' чтобы показать на русском.

Built-in commands:
  help        show this help
  help -ru    show this help in russian
  cd    change directory (no argument — goes to $HOME)
  sqq-save    save current autocomplete history to ~/.sqq-ac.txt
  exit, quit  exit the shell

Input:
  Tab             accept full suggestion (path/word) or by word (history)
  Up/Down         command history (or move between lines of multiline input)
  Ctrl+Up/Down    go to the beginning / end of the current line
  Ctrl+Left/Right jump 15 characters back / forward
  Ctrl+J          insert a newline manually without sending the command
  Ctrl+C          cancel current input
  Ctrl+D          exit (on an empty line)
  pasting multiline text inserts it as is, without executing line by line


Misc:
  russian layout during command error — transliteration if the command is safe
  dangling quote at the end of the line — trimmed and command executed without it
  unclosed quote in the middle — "Incorrect quotes.", without waiting for continuation
  """

HELP_TEXT_RU = """
sqq — обёртка над bash с автодополнением, историей и подсказками. 'help' to display in english.

Встроенные команды:
  help -ru    показать эту справку
  cd [путь]   сменить директорию (без аргумента — идёт в $HOME)
  sqq-save    сохранить текущую историю автодополнения в ~/.sqq-ac.txt
  exit, quit  выйти из оболочки

Ввод:
  Tab             принять подсказку целиком (path/word) или по слову (history)
  Up/Down         история команд (или перемещение по строкам многострочного ввода)
  Ctrl+Up/Down    перейти в начало / конец текущей строки
  Ctrl+Left/Right перепрыгнуть на 15 символов назад / вперёд
  Ctrl+J          вставить перевод строки вручную, не отправляя команду
  Ctrl+C          отменить текущий ввод
  Ctrl+D          выйти (на пустой строке)
  вставка (paste) многострочного текста вставляется как есть, без построчного запуска

Прочее:
  неверная раскладка при ошибке команды — транслитерация (ды ьн -> ls my) если комманда безопасна
  висящая кавычка в конце строки — обрезается и команда выполняется без неё
  кавычка не закрыта посередине — "Incorrect quotes.", без ожидания продолжения
  """


_TOKEN_SPLIT_RE = re.compile(r"[\s;&|]+")


def compute_hints(s: str) -> list:
    hints = []
    tokens = [t for t in _TOKEN_SPLIT_RE.split(s) if t]
    for tok in tokens:
        if tok in HARDCODED_HINTS and HARDCODED_HINTS[tok] not in hints:
            hints.append(HARDCODED_HINTS[tok])
    if not quotes_balanced(s):
        hints.append("uncloused quote")
    return hints


def next_word_chunk(remainder: str) -> str:
    if not remainder:
        return ""
    i = 0
    while i < len(remainder) and remainder[i] == " ":
        i += 1
    j = i
    while j < len(remainder) and remainder[j] not in WORD_BREAK:
        j += 1
    if j == i:
        j = i + 1
    chunk = remainder[:j]
    if chunk == "\n":
        return ""
    return chunk


def path_remainder(token: str) -> str:
    expanded = os.path.expanduser(token)
    dirname, base = os.path.split(expanded)
    search_dir = dirname if dirname else "."
    try:
        names = os.listdir(search_dir)
    except OSError:
        return ""
    matches = sorted(n for n in names if n.startswith(base))
    if not matches:
        return ""
    if len(matches) == 1:
        full = matches[0]
        remainder = full[len(base) :]
        if os.path.isdir(os.path.join(search_dir, full)):
            remainder += "/"
        return remainder
    common = os.path.commonprefix(matches)
    if len(common) > len(base):
        return common[len(base) :]
    return ""


def compute_word_suggestion(token: str, history: list) -> str:
    if not token:
        return ""
    for entry in reversed(history):
        for word in _TOKEN_SPLIT_RE.split(entry):
            if word and word != token and word.startswith(token):
                return word[len(token) :]
    return ""


def compute_builtin_suggestion(token: str) -> str:
    if not token:
        return ""
    for word in HARDCODED_AUTOCOMPLETE:
        if word != token and word.startswith(token):
            return word[len(token) :]
    return ""


def compute_suggestion(s: str, history: list):
    seg_begin = segment_start(s)
    segment = s[seg_begin:]
    seg_stripped = segment.lstrip()

    tok_begin_rel = token_start(seg_stripped)
    token = seg_stripped[tok_begin_rel:]

    if token and ("/" in token or token in (".", "..")):
        remainder = path_remainder(token)
        if remainder:
            return ("path", remainder)

    if token and token == seg_stripped:
        builtin_remainder = compute_builtin_suggestion(token)
        if builtin_remainder:
            return ("word", builtin_remainder)

    if seg_stripped:
        for entry in reversed(history):
            for seg in _SEGMENT_SPLIT_RE.split(entry):
                seg_l = seg.lstrip()
                pos = 0
                for word in seg_l.split(" "):
                    candidate = seg_l[pos:]
                    if candidate.startswith(seg_stripped) and candidate != seg_stripped:
                        remainder = candidate[len(seg_stripped) :]
                        if remainder:
                            return ("history", remainder)
                    pos += len(word) + 1

    if token:
        word_remainder = compute_word_suggestion(token, history)
        if word_remainder:
            return ("word", word_remainder)

    return (None, "")


def read_char(fd) -> str:
    b = os.read(fd, 1)
    if not b:
        return ""
    first = b[0]
    if first < 0x80:
        n = 0
    elif first >> 5 == 0b110:
        n = 1
    elif first >> 4 == 0b1110:
        n = 2
    elif first >> 3 == 0b11110:
        n = 3
    else:
        n = 0
    if n:
        b += os.read(fd, n)
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def read_escape_sequence(fd, timeout: float = 0.05) -> str:
    def _read1():
        r, _, _ = select.select([fd], [], [], timeout)
        if not r:
            return ""
        return os.read(fd, 1).decode(errors="ignore")

    first = _read1()
    if first == "":
        return ""
    if first == "[":
        seq = "["
        while True:
            c = _read1()
            if c == "":
                break
            seq += c
            if c.isalpha() or c == "~":
                break
        return seq
    if first == "O":
        c = _read1()
        return "O" + c
    return first


def read_bracketed_paste(fd) -> str:
    out = []
    while True:
        c = read_char(fd)
        if c == "":
            continue
        if c == "\x1b":
            seq = read_escape_sequence(fd)
            if seq == PASTE_SEQ_END:
                break
            continue
        if c == "\r":
            c = "\n"
        out.append(c)
    return "".join(out)


def read_line(prompt_fn, history: list):
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    buf = list("")
    cursor = 0
    hist_index = len(history)
    saved_line = ""
    last_suggestion = ("", "")
    prev_cursor_row = 0

    def term_width() -> int:
        try:
            return os.get_terminal_size(fd).columns or 80
        except OSError:
            return 80

    def split_cursor():
        s = "".join(buf)
        lines = s.split("\n")
        acc = 0
        for idx, line in enumerate(lines):
            if acc + len(line) >= cursor:
                return lines, idx, cursor - acc
            acc += len(line) + 1
        return lines, len(lines) - 1, len(lines[-1])

    def move_cursor_vertical(delta: int) -> bool:
        nonlocal cursor
        lines, idx, col = split_cursor()
        new_idx = idx + delta
        if new_idx < 0 or new_idx >= len(lines):
            return False
        new_col = min(col, len(lines[new_idx]))
        cursor = sum(len(l) + 1 for l in lines[:new_idx]) + new_col
        return True

    def move_cursor_line_start():
        nonlocal cursor
        _, _, col = split_cursor()
        cursor -= col

    def move_cursor_line_end():
        nonlocal cursor
        lines, idx, col = split_cursor()
        cursor += len(lines[idx]) - col

    def move_cursor_jump(delta: int):
        nonlocal cursor
        cursor = max(0, min(len(buf), cursor + delta))

    def redraw():
        nonlocal last_suggestion, prev_cursor_row
        s = "".join(buf)
        at_end = cursor == len(buf)
        if at_end:
            kind, remainder = compute_suggestion(s, history)
        else:
            kind, remainder = (None, "")
        last_suggestion = (kind, remainder)

        hints = compute_hints(s) if at_end else []
        hint_text = f"    # {'; '.join(hints)}" if hints else ""

        width = max(1, term_width())
        prompt = prompt_fn()
        plen = visible_len(prompt)
        cont_prompt = build_cont_prompt()
        cont_plen = visible_len(cont_prompt)

        lines = s.split("\n")

        def line_plen(i):
            return plen if i == 0 else cont_plen

        decor = ""
        if at_end:
            p_len_last = line_plen(len(lines) - 1)
            used = p_len_last + len(lines[-1]) + len(remainder) + len(hint_text)
            avail = max(0, width - used)
            decor_len = int(avail * DECOR_LENGTH_PERCENT / 100)
            decor = _get_decoration(decor_len)
        decor_vlen = visible_len(decor)

        def line_extra(i):
            if i == len(lines) - 1:
                return len(remainder) + decor_vlen + len(hint_text)
            return 0

        def rows_of(i):
            total = line_plen(i) + len(lines[i]) + line_extra(i)
            return max(1, -(-total // width))

        _, cur_line_idx, cur_col = split_cursor()

        rows_before = sum(rows_of(i) for i in range(cur_line_idx))
        p_len_cur = line_plen(cur_line_idx)
        pos_in_line = p_len_cur + cur_col
        row_within = pos_in_line // width
        col_within = pos_in_line % width
        cursor_row_from_top = rows_before + row_within

        total_rows = sum(rows_of(i) for i in range(len(lines)))

        if prev_cursor_row:
            sys.stdout.write(f"\033[{prev_cursor_row}A")
        sys.stdout.write("\r")
        sys.stdout.write("\033[0J")

        for i, line in enumerate(lines):
            p = prompt if i == 0 else cont_prompt
            sys.stdout.write(p + line)
            if i == len(lines) - 1:
                if remainder:
                    sys.stdout.write(DIM + remainder + RESET)
                if decor:
                    sys.stdout.write(decor)
                if hint_text:
                    sys.stdout.write(RED + hint_text + RESET)
            else:
                sys.stdout.write("\r\n")

        end_row = total_rows - 1
        up = end_row - cursor_row_from_top
        if up:
            sys.stdout.write(f"\033[{up}A")
        sys.stdout.write("\r")
        if col_within:
            sys.stdout.write(f"\033[{col_within}C")

        sys.stdout.flush()
        prev_cursor_row = cursor_row_from_top

    def accept_suggestion(full: bool):
        kind, remainder = last_suggestion
        if not remainder:
            return
        if kind in ("path", "word") or full:
            chunk = remainder
        else:
            chunk = next_word_chunk(remainder)
        buf.extend(list(chunk))
        nonlocal cursor
        cursor = len(buf)

    def insert_text(text: str):
        nonlocal cursor
        if not text:
            return
        buf[cursor:cursor] = list(text)
        cursor += len(text)

    global _raw_mode_active
    try:
        tty.setraw(fd)
        _raw_mode_active = True
        redraw()
        while True:
            try:
                r, _, _ = select.select([fd], [], [], DECOR_UPDATE_INTERVAL)
            except (OSError, InterruptedError):
                r = None
            if not r:
                redraw()
                continue

            ch = read_char(fd)
            if ch == "":
                continue

            if ch == "\r":
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                return "".join(buf)

            elif ch == "\n":
                insert_text("\n")
                redraw()

            elif ch == "\x03":
                sys.stdout.write("^C\r\n")
                sys.stdout.flush()
                return ""

            elif ch == "\x04":
                if not buf:
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    return None

            elif ch in ("\x7f", "\x08"):
                if cursor > 0:
                    del buf[cursor - 1]
                    cursor -= 1
                redraw()

            elif ch == "\t":
                accept_suggestion(full=False)
                redraw()

            elif ch == "\x1b":
                seq = read_escape_sequence(fd)
                if seq == "[1;5A":
                    move_cursor_line_start()
                elif seq == "[1;5B":
                    move_cursor_line_end()
                elif seq == "[1;5C":
                    move_cursor_jump(CTRL_JUMP_CHARS)
                elif seq == "[1;5D":
                    move_cursor_jump(-CTRL_JUMP_CHARS)
                elif seq == "[A":  # up
                    if not move_cursor_vertical(-1):
                        if hist_index > 0:
                            if hist_index == len(history):
                                saved_line = "".join(buf)
                            hist_index -= 1
                            buf = list(history[hist_index])
                            cursor = len(buf)
                elif seq == "[B":  # down
                    if not move_cursor_vertical(1):
                        if hist_index < len(history):
                            hist_index += 1
                            buf = (
                                list(saved_line)
                                if hist_index == len(history)
                                else list(history[hist_index])
                            )
                            cursor = len(buf)
                elif seq == "[C":  # right
                    if cursor < len(buf):
                        cursor += 1
                elif seq == "[D":  # left
                    if cursor > 0:
                        cursor -= 1
                elif seq == PASTE_SEQ_START:
                    insert_text(read_bracketed_paste(fd))
                elif (
                    seq in ("\r", "\n")
                    or _CSI_U_ENTER_RE.match(seq)
                    or _MODIFY_OTHER_KEYS_ENTER_RE.match(seq)
                ):
                    insert_text("\n")
                redraw()

            elif ch.isprintable():
                buf.insert(cursor, ch)
                cursor += 1
                redraw()

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        _raw_mode_active = False


def _ignore_sigtstp_in_child():
    signal.signal(signal.SIGTSTP, signal.SIG_IGN)


def _run_and_wait(argv) -> int:
    global _current_child
    proc = subprocess.Popen(argv, preexec_fn=_ignore_sigtstp_in_child)
    _current_child = proc
    try:
        return proc.wait()
    finally:
        _current_child = None


def confirm_dangerous(cmd: str) -> bool:
    stripped = cmd.strip()
    if not stripped:
        return True

    norm = " ".join(stripped.split())

    dangerous_found = [p for p in CONFIRM_COMMANDS if p in norm]
    if not dangerous_found:
        return True

    try:
        answer = input(
            f"{CONFIRM_COLOR}This command is harmful. Sure want to run this? (Unsafe: {', '.join(dangerous_found)}) [Y/yes/n]{RESET} "
        )
    except:
        print("\n\nCommand canceled.")
        return False

    confirmed = answer in ("Y", "yes")
    if not confirmed:
        print("\nCommand canceled.")

    return confirmed


def run_command(cmd: str, aliases: dict):
    expanded = expand_aliases(cmd, aliases)
    returncode = _run_and_wait(["bash", "-c", expanded])
    if returncode != 0 and looks_like_wrong_layout(cmd):
        fixed = transliterate_layout(cmd)
        if fixed == cmd:
            return
        first_word = fixed.split()[0] if fixed.split() else ""
        if first_word in DANGEROUS_FIRST_WORDS:
            print(
                f"{DIM}'{cmd}' seems to be a wrong layout ({fixed}), but not to redirect, command potentially unsafe"
                f"{RESET}"
            )
            return
        print(
            f"{DIM}'{cmd}' seems to be a wrong layout, redirecting to: '{fixed}'{RESET}"
        )
        _run_and_wait(["bash", "-c", expand_aliases(fixed, aliases)])


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-h", "--help", action="store_true")
    args, _ = parser.parse_known_args()
    if args.help:
        print("Run program, then type 'help' or 'help --ru'.")
        return

    global _cooked_settings
    interactive = sys.stdin.isatty()
    if interactive:
        try:
            _cooked_settings = termios.tcgetattr(sys.stdin.fileno())
            signal.signal(signal.SIGTSTP, _sigtstp_handler)
        except Exception:
            pass
        try:
            sys.stdout.write(BRACKETED_PASTE_START)
            sys.stdout.flush()
        except Exception:
            pass

    aliases = load_bashrc_aliases()
    history: list = list(aliases.keys()) + load_persisted_history()

    try:
        while True:
            if interactive:
                try:
                    cmd = read_line(build_prompt_display, history)
                except KeyboardInterrupt:
                    print()
                    continue
                if cmd is None:
                    print()
                    break
            else:
                try:
                    cmd = input(build_prompt_plain())
                except EOFError:
                    break

            if not cmd.strip():
                continue
            if cmd.strip() in ("exit", "quit"):
                break
            if cmd.strip() == "sqq-save":
                sqq_save()
                continue
            if cmd.strip() == "help":
                print(HELP_TEXT_EN)
                continue
            if cmd.strip() == "help -ru":
                print(HELP_TEXT_RU)
                continue

            fixed_cmd = strip_trailing_dangling_quote(cmd)

            if not quotes_balanced(fixed_cmd):
                print("Incorrect quotes.")
                continue

            if not history or history[-1] != fixed_cmd:
                history.append(fixed_cmd)
                persist_history_entry(fixed_cmd)

            if handle_cd(fixed_cmd):
                            continue

            if handle_alias(fixed_cmd, aliases):
                continue

            if handle_source_bashrc(fixed_cmd, aliases):
                continue

            if handle_venv(fixed_cmd):
                continue

            if not confirm_dangerous(fixed_cmd):
                continue

            try:
                run_command(fixed_cmd, aliases)
            except KeyboardInterrupt:
                print()
            except Exception as e:
                print(f"qshell error: {e}")
    finally:
        if interactive:
            try:
                sys.stdout.write(BRACKETED_PASTE_END)
                sys.stdout.flush()
            except Exception:
                pass


if __name__ == "__main__":
    main()
